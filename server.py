import os
import re
import hmac
import secrets
from pathlib import Path
import html as html_lib
import json
import time
import base64
import hashlib
import logging
import asyncio
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, parse_qsl, urlparse

import httpx
import asyncpg
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi import Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
)

# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dealhunter")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip().rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()
WB_PROXY_URL = os.getenv("WB_PROXY_URL", "").strip() or None
SPONSOR_CHANNEL = os.getenv("SPONSOR_CHANNEL", "").strip()
ADMIN_UID = int(os.getenv("ADMIN_UID", "0")) if os.getenv("ADMIN_UID", "").isdigit() else 0
PORT = int(os.getenv("PORT", 10000))

BOT_USERNAME_DEFAULT = "DealHunterAvito_bot"
BOT_USERNAME_CACHE = os.getenv("BOT_USERNAME", BOT_USERNAME_DEFAULT).strip().lstrip("@") or BOT_USERNAME_DEFAULT

STARS_PRICE = 350
TRIAL_DAYS = 3
REFERRAL_BONUS_DAYS = 3
MAX_REF_DAYS_PER_MONTH = 30
MAX_TRIALS_PER_IP = 3
LOT_TTL_HOURS = 6
ADVISORY_LOCK_ID = 94827165
HEARTBEAT_SEC = 20

QUIET_START = int(os.getenv("QUIET_START", "23")) % 24
QUIET_END = int(os.getenv("QUIET_END", "9")) % 24

BUNNY_STARS_URL = "https://t.me/BunnyStarsBot?startapp=ref_5123484212"

CITY_DEST_MAP: Dict[str, int] = {
    "Москва": -1257786,
    "Санкт-Петербург": -1198055,
    "Екатеринбург": -5818883,
    "Казань": -2133462,
    "Новосибирск": -364763,
    "Краснодар": -1075831,
}
from cities import CITIES
ALLOWED_CITIES = set(CITIES)
ALLOWED_SOURCES = {"all", "Wildberries", "Ozon", "Avito", "TG", "GIFTS", "PLAYEROK"}
ALLOWED_HOSTS = {
    "www.wildberries.ru", "wildberries.ru",
    "www.ozon.ru", "ozon.ru",
    "www.avito.ru", "avito.ru",
    "t.me", "www.t.me",
    "market.tonnel.network", "tonnel.network", "www.tonnel.network",
    "playerok.com", "www.playerok.com",
}

ACCESSORY_STOP_WORDS = (
    "насадка", "насадки", "чехол", "стекло", "пленка", "плёнка", "кабель",
    "провод", "переходник", "адаптер", "фильтр для", "щетка для", "кейс для",
    "подставка", "коробка от", "муляж", "запчасть", "мешок для", "шланг для",
    "аккумулятор для", "кронштейн", "ремешок",
)

app = FastAPI(title="DealHunter Stable API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_URL, "https://web.telegram.org", "https://t.me"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Tg-Init-Data-B64", "X-Worker-Secret"],
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
pg_pool: Optional[asyncpg.Pool] = None
background_leader_conn: Optional[asyncpg.Connection] = None
leader_task: Optional[asyncio.Task] = None

# =====================================================================
# 2. КЭШИРОВАНИЕ И ЛИМИТЫ
# =====================================================================
USER_RATE_BUCKETS: OrderedDict[int, List[float]] = OrderedDict()
WB_LRU_CACHE: OrderedDict[str, Tuple[float, List[Dict[str, Any]]]] = OrderedDict()
WB_CACHE_MAX_SIZE = 300
WB_CACHE_TTL = 30.0


def check_rate_limit(uid: int, max_requests: int = 50, window_sec: float = 10.0):
    now = time.time()
    timestamps = USER_RATE_BUCKETS.get(uid, [])
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= max_requests:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подождите пару секунд.")
    timestamps.append(now)
    USER_RATE_BUCKETS[uid] = timestamps
    USER_RATE_BUCKETS.move_to_end(uid)
    while len(USER_RATE_BUCKETS) > 2000:
        USER_RATE_BUCKETS.popitem(last=False)


def cache_get_wb(key: str) -> Optional[List[Dict[str, Any]]]:
    if key not in WB_LRU_CACHE:
        return None
    ts, data = WB_LRU_CACHE[key]
    if time.time() - ts > WB_CACHE_TTL:
        WB_LRU_CACHE.pop(key, None)
        return None
    WB_LRU_CACHE.move_to_end(key)
    return data


def cache_set_wb(key: str, data: List[Dict[str, Any]]):
    WB_LRU_CACHE[key] = (time.time(), data)
    WB_LRU_CACHE.move_to_end(key)
    while len(WB_LRU_CACHE) > WB_CACHE_MAX_SIZE:
        WB_LRU_CACHE.popitem(last=False)


# =====================================================================
# 3. АВТОРИЗАЦИЯ TELEGRAM (HMAC-SHA256)
# =====================================================================
def verify_telegram_init_data(init_data_raw: str) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен на сервере")
    if not init_data_raw:
        raise HTTPException(status_code=401, detail="Откройте приложение внутри Telegram через бота")

    try:
        data_dict = dict(parse_qsl(init_data_raw, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный формат данных авторизации")

    received_hash = data_dict.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Отсутствует криптографическая подпись Telegram")

    auth_date_raw = data_dict.get("auth_date", "")
    if not auth_date_raw or not auth_date_raw.isdigit():
        raise HTTPException(status_code=401, detail="Отсутствует метка времени сессии (auth_date)")
    auth_date = int(auth_date_raw)
    now_ts = int(time.time())
    if auth_date <= 0 or (now_ts - auth_date) > 86400 * 3:
        raise HTTPException(status_code=401, detail="Сессия устарела. Перезапустите приложение из бота.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Подпись Telegram недействительна")

    user_raw = data_dict.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Данные пользователя отсутствуют в подписи")

    try:
        user_obj = json.loads(user_raw)
        uid = int(user_obj["id"])
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректные данные пользователя")
    start_param = data_dict.get("start_param", "")
    ref_id = None
    if start_param.startswith("ref_"):
        r_clean = start_param.replace("ref_", "").strip()
        if r_clean.isdigit() and int(r_clean) != uid:
            ref_id = int(r_clean)

    return {
        "uid": uid,
        "first_name": str(user_obj.get("first_name") or "Охотник")[:64],
        "username": str(user_obj.get("username") or "")[:64],
        "ref_id": ref_id,
    }


# =====================================================================
# 3.4 АНТИФРОД: ХЭШ IP-АДРЕСА КЛИЕНТА
# =====================================================================
def get_client_ip_hash(request: Request) -> str:
    """Хэш IP пользователя (за Render скрывается реальный адрес, он приходит в X-Forwarded-For).

    Пустая строка = IP неизвестен (локальная разработка), проверки по нему не делаем.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    real_ip = request.headers.get("x-real-ip", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else real_ip).strip()
    if not ip:
        return ""
    raw = f"{ip}::{WORKER_SECRET or BOT_TOKEN}::anti-fraud-v1"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def get_authenticated_user(request: Request) -> Dict[str, Any]:
    session_token = request.headers.get("X-Session-Token", "").strip()
    b64_header = request.headers.get("X-Tg-Init-Data-B64", "").strip()

    if session_token and pg_pool and fallback_sessions:
        auth = fallback_sessions.get_session_auth(session_token)
        if auth:
            check_rate_limit(auth["uid"])
            auth["ip_hash"] = get_client_ip_hash(request)
            return auth

    if not b64_header:
        raise HTTPException(status_code=401, detail="Запустите приложение внутри Telegram")
    try:
        decoded_bytes = base64.b64decode(b64_header)
        init_data_raw = decoded_bytes.decode("utf-8")
    except Exception:
        raise HTTPException(status_code=401, detail="Ошибка чтения заголовка авторизации")

    auth = verify_telegram_init_data(init_data_raw)
    check_rate_limit(auth["uid"])
    auth["ip_hash"] = get_client_ip_hash(request)
    return auth


# =====================================================================
# 3.2 FALLBACK-АВТОРИЗАЦИЯ (Telegram Desktop открывает мини-апп в браузере, initData там нет)
# =====================================================================
class FallbackSessions:
    """Одноразовые вход-ссылки из бота (#token=...) и长效 сессии в памяти процесса."""

    def __init__(self):
        self.login_tokens: Dict[str, Tuple[int, float, str, str]] = {}   # token -> (uid, expires_ts, first_name, username)
        self.sessions: Dict[str, Tuple[int, float, str, str]] = {}  # token -> (uid, expires, name, username)
        self._made = {}

    def issue_login_token(self, uid: int, first_name: str = "", username: str = "") -> str:
        tok = secrets.token_urlsafe(24)
        self.login_tokens[tok] = (uid, time.time() + 300, first_name or "", username or "")
        expired = [k for k, (_, exp, _, _) in self.login_tokens.items() if exp < time.time()]
        for k in expired:
            self.login_tokens.pop(k, None)
        self._made[uid] = self._made.get(uid, 0) + 1
        return tok

    def redeem_login_token(self, tok: str) -> Optional[str]:
        data = self.login_tokens.pop(tok, None)
        if not data:
            return None
        uid, exp, first_name, username = data
        if exp < time.time():
            return None
        sess = secrets.token_urlsafe(32)
        self.sessions[sess] = (uid, time.time() + 30 * 86400, first_name, username)
        expired = [k for k, (_, exp2, _, _) in self.sessions.items() if exp2 < time.time()]
        for k in expired:
            self.sessions.pop(k, None)
        return sess

    def get_session_auth(self, tok: str) -> Optional[Dict[str, Any]]:
        data = self.sessions.get(tok)
        if not data:
            return None
        uid, exp, first_name, username = data
        if exp < time.time():
            self.sessions.pop(tok, None)
            return None
        return {"uid": uid, "first_name": first_name or "Охотник", "username": username or "", "ref_id": None}


    def drop_sessions_for_uid(self, uid: int) -> int:
        to_kill = [k for k, (u, *_rest) in self.sessions.items() if u == uid]
        for k in to_kill:
            self.sessions.pop(k, None)
        return len(to_kill)


fallback_sessions = FallbackSessions()


def validate_and_clean_url(raw_url: str) -> str:
    u = str(raw_url or "").strip()
    if not u or any(ch in u for ch in ("'", '"', "<", ">", " ", "\n", "\r", "\t", "javascript:")):
        raise ValueError("URL содержит недопустимые символы")
    parsed = urlparse(u)
    if parsed.scheme != "https":
        raise ValueError("Разрешён только протокол https://")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Домен {host} не разрешён")
    return u


def humanize_time_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return "В наличии"
    diff_sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if diff_sec < 60:
        return "Только что"
    mins = diff_sec // 60
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    return f"{hours} ч назад"


# =====================================================================
# 4. POSTGRESQL СЛОЙ
# =====================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_db() -> asyncpg.Pool:
    if not pg_pool:
        raise HTTPException(status_code=503, detail="База данных PostgreSQL не подключена (DATABASE_URL)")
    return pg_pool


async def init_db():
    global pg_pool
    if not DATABASE_URL.startswith("postgres"):
        logger.error("КРИТИЧНО: DATABASE_URL не задан или не начинается с postgres!")
        return
    try:
        pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=15)
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dh_users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT DEFAULT 'Охотник',
                    username TEXT DEFAULT '',
                    trial_claimed BOOLEAN DEFAULT FALSE,
                    sub_until TIMESTAMPTZ DEFAULT NULL,
                    saved_ids TEXT DEFAULT '[]',
                    claimed_ids TEXT DEFAULT '[]',
                    referred_by BIGINT DEFAULT NULL,
                    ref_rewarded BOOLEAN DEFAULT FALSE,
                    ref_count INT DEFAULT 0,
                    ref_days_earned INT DEFAULT 0,
                    ref_month TEXT DEFAULT '',
                    ref_month_days INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS dh_monitors (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    city TEXT DEFAULT 'Москва',
                    source TEXT DEFAULT 'all',
                    min_margin INT DEFAULT 15,
                    active BOOLEAN DEFAULT TRUE,
                    found_today INT DEFAULT 0,
                    found_date DATE DEFAULT CURRENT_DATE,
                    last_notified_lot TEXT DEFAULT '',
                    extras TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS dh_monitor_hits (
                    monitor_id TEXT NOT NULL,
                    lot_key TEXT NOT NULL,
                    notified_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (monitor_id, lot_key)
                );

                CREATE TABLE IF NOT EXISTS dh_lots (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    city TEXT NOT NULL,
                    price INT NOT NULL,
                    market INT NOT NULL,
                    profit_rub INT NOT NULL,
                    profit_pct FLOAT NOT NULL,
                    tags JSONB DEFAULT '[]'::jsonb,
                    seller_name TEXT DEFAULT 'Продавец',
                    seller_rating FLOAT DEFAULT 5.0,
                    seller_reviews INT DEFAULT 0,
                    description TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS dh_payments (
                    charge_id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount_stars INT NOT NULL,
                    days_granted INT NOT NULL,
                    payload TEXT DEFAULT '',
                    refunded BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS claimed_ids TEXT DEFAULT '[]';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN DEFAULT FALSE;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month_days INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS found_date DATE DEFAULT CURRENT_DATE;")
            await conn.execute("ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS extras TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS signup_ip_hash TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS trial_claimed_at TIMESTAMPTZ DEFAULT NULL;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS quiet_start INT DEFAULT 23;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS quiet_end INT DEFAULT 9;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMPTZ DEFAULT NULL;")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dh_users_ip_hash ON dh_users (signup_ip_hash);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dh_users_referred_by ON dh_users (referred_by);")
        logger.info("PostgreSQL инициализирован.")
    except Exception as e:
        logger.exception(f"Ошибка инициализации PostgreSQL: {e}")


async def upsert_user_atomic(
    uid: int,
    first_name: str,
    username: str,
    candidate_ref_id: Optional[int] = None,
) -> Dict[str, Any]:
    pool = require_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            valid_referrer = None
            if candidate_ref_id and candidate_ref_id != uid:
                ref_exists = await conn.fetchval(
                    "SELECT user_id FROM dh_users WHERE user_id = $1",
                    candidate_ref_id,
                )
                if ref_exists:
                    valid_referrer = candidate_ref_id

            row = await conn.fetchrow(
                """
                INSERT INTO dh_users (
                    user_id, first_name, username, trial_claimed, sub_until,
                    saved_ids, claimed_ids, referred_by, ref_rewarded, ref_count, ref_days_earned
                )
                VALUES ($1, $2, $3, FALSE, NULL, '[]', '[]', $4, FALSE, 0, 0)
                ON CONFLICT (user_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    username = CASE WHEN EXCLUDED.username != '' THEN EXCLUDED.username ELSE dh_users.username END
                RETURNING *
                """,
                uid, first_name, username, valid_referrer,
            )

    return format_user_row(row)


def format_user_row(row: asyncpg.Record) -> Dict[str, Any]:
    now = now_utc()
    sub_until: Optional[datetime] = row["sub_until"]
    is_active = bool(sub_until and sub_until > now)
    seconds_left = int((sub_until - now).total_seconds()) if is_active else 0
    try:
        saved_ids = json.loads(row["saved_ids"] or "[]")
    except Exception:
        saved_ids = []
    try:
        claimed_ids = json.loads(row["claimed_ids"] or "[]")
    except Exception:
        claimed_ids = []

    cur_month = now.strftime("%Y-%m")
    ref_month_days = int(row["ref_month_days"] or 0) if row["ref_month"] == cur_month else 0

    try:
        terms_at = row["terms_accepted_at"]
    except (KeyError, Exception):
        terms_at = None

    return {
        "user_id": row["user_id"],
        "name": row["first_name"] or "Охотник",
        "trialClaimed": bool(row["trial_claimed"]),
        "subActive": is_active,
        "subUntil": sub_until.isoformat() if sub_until else None,
        "secondsLeft": seconds_left,
        "savedIds": saved_ids,
        "claimedIds": claimed_ids,
        "refCount": row["ref_count"] or 0,
        "refDaysEarned": row["ref_days_earned"] or 0,
        "refMonthDays": ref_month_days,
        "refMonthLimit": MAX_REF_DAYS_PER_MONTH,
        "refBonus": REFERRAL_BONUS_DAYS,
        "termsAccepted": bool(terms_at),
        "quietStart": int(row["quiet_start"] if row["quiet_start"] is not None else QUIET_START),
        "quietEnd": int(row["quiet_end"] if row["quiet_end"] is not None else QUIET_END),
        "botUsername": BOT_USERNAME_CACHE,
        "sponsorChannel": SPONSOR_CHANNEL,
        "refLink": f"https://t.me/{BOT_USERNAME_CACHE}?start=ref_{row['user_id']}",
    }


async def claim_trial_and_reward_referrer(uid: int, ip_hash: str = "") -> Dict[str, Any]:
    """
    Выдача триала + вознаграждение реферера.

    Антифрод (активен только когда известен IP молодого пользователя):
    1. Ферма триалов: не более MAX_TRIALS_PER_IP активаций с одного IP за 30 дней.
    2. Мультиаккаунт-рефералы: вознаграждение не выдаётся, если у реферера уже
       получено вознаграждение за пользователя с этим же IP-хэшем.
    """
    pool = require_db()
    now = now_utc()
    cur_month = now.strftime("%Y-%m")
    referrer_to_notify: Optional[int] = None
    days_awarded_to_ref = 0
    user_name = "Охотник"

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
            if not row:
                raise HTTPException(status_code=404, detail="Профиль не найден")
            if row["trial_claimed"]:
                return format_user_row(row)

            user_name = row["first_name"] or "Друг"

            # --- Антифрод 1: лимит триалов с одного IP ---
            if ip_hash:
                same_ip_claims = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM dh_users
                    WHERE signup_ip_hash = $1
                      AND user_id <> $2
                      AND trial_claimed_at > NOW() - INTERVAL '30 days'
                    """,
                    ip_hash, uid,
                )
                if int(same_ip_claims or 0) >= MAX_TRIALS_PER_IP:
                    raise HTTPException(
                        status_code=403,
                        detail="С одного устройства уже активировали слишком много аккаунтов. Используйте основной аккаунт.",
                    )

            cur_end = row["sub_until"]
            base_time = cur_end if (cur_end and cur_end > now) else now
            new_sub_until = base_time + timedelta(days=TRIAL_DAYS)

            referred_by = row["referred_by"]
            ref_rewarded = bool(row["ref_rewarded"])

            # --- Антифрод 2: реферал с того же IP, за которого уже платили ---
            ref_reward_allowed = False
            if referred_by and not ref_rewarded and referred_by != uid:
                ref_reward_allowed = True
                if ip_hash:
                    dup = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM dh_users
                        WHERE referred_by = $1
                          AND ref_rewarded = TRUE
                          AND user_id <> $2
                          AND signup_ip_hash = $3
                        """,
                        referred_by, uid, ip_hash,
                    )
                    if int(dup or 0) > 0:
                        ref_reward_allowed = False
                        logger.info(f"Антифрод: реферальное вознаграждение {referred_by} не выдано — тот же IP, что у предыдущего реферала")

            updated_row = await conn.fetchrow(
                """
                UPDATE dh_users
                SET trial_claimed = TRUE,
                    trial_claimed_at = $2,
                    signup_ip_hash = $3,
                    sub_until = $4,
                    ref_rewarded = TRUE
                WHERE user_id = $1
                RETURNING *
                """,
                uid, now, ip_hash, new_sub_until,
            )

            if referred_by and ref_reward_allowed:
                ref_row = await conn.fetchrow(
                    "SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE",
                    referred_by,
                )
                if ref_row:
                    r_month = ref_row["ref_month"] or ""
                    r_m_days = int(ref_row["ref_month_days"] or 0) if r_month == cur_month else 0
                    available_quota = max(0, MAX_REF_DAYS_PER_MONTH - r_m_days)
                    days_to_give = min(REFERRAL_BONUS_DAYS, available_quota)

                    if days_to_give > 0:
                        r_end = ref_row["sub_until"]
                        r_base = r_end if (r_end and r_end > now) else now
                        r_new_end = r_base + timedelta(days=days_to_give)
                        await conn.execute(
                            """
                            UPDATE dh_users
                            SET sub_until = $2,
                                ref_count = COALESCE(ref_count, 0) + 1,
                                ref_days_earned = COALESCE(ref_days_earned, 0) + $3,
                                ref_month = $4,
                                ref_month_days = $5
                            WHERE user_id = $1
                            """,
                            referred_by, r_new_end, days_to_give, cur_month, r_m_days + days_to_give,
                        )
                        referrer_to_notify = referred_by
                        days_awarded_to_ref = days_to_give
                    elif ref_reward_allowed:
                        # квота месяца исчерпана — счётчик растёт, дни не начисляются
                        await conn.execute(
                            "UPDATE dh_users SET ref_count = COALESCE(ref_count, 0) + 1 WHERE user_id = $1",
                            referred_by,
                        )

    if referrer_to_notify and bot and days_awarded_to_ref > 0:
        try:
            await bot.send_message(
                referrer_to_notify,
                f"🎉 <b>Реферал подтверждён: {user_name}!</b>\n\n"
                f"Друг запустил приложение и активировал доступ.\n"
                f"🎁 Вам начислено <b>+{days_awarded_to_ref} дн. подписки DealHunter PRO</b>!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Ошибка уведомления реферера {referrer_to_notify}: {e}")

    return format_user_row(updated_row)


# =====================================================================
# 5. ПОИСКОВЫЙ ДВИЖОК: ЖИВОЙ WB + РАСШИРЕННАЯ ПРОВЕРЕННАЯ БАЗА
# =====================================================================
DEFAULT_BRAND_QUERIES = {
    "all": "Dyson фен стайлер",
    "electronics": "Apple iPhone 15 Pro",
    "xiaomi": "Xiaomi 14 POCO",
    "sony": "Sony PlayStation 5",
    "laptops": "Apple MacBook Air M3",
    "lego": "LEGO Technic",
}

async def fetch_wb_realtime_api(query_text: str, city: str = "Москва") -> List[Dict[str, Any]]:
    q_clean = query_text.strip() or "Dyson"
    dest = CITY_DEST_MAP.get(city, -1257786)
    cache_key = f"{q_clean.lower()}::{dest}"

    cached = cache_get_wb(cache_key)
    if cached is not None:
        return cached

    endpoints = [
        f"https://search.wb.ru/exactmatch/ru/common/v18/search?ab_testing=false&appType=1&curr=rub&dest={dest}&query={quote(q_clean)}&resultset=catalog&sort=popular&spp=30",
        f"https://search.wb.ru/exactmatch/ru/common/v7/search?ab_testing=false&appType=1&curr=rub&dest={dest}&query={quote(q_clean)}&resultset=catalog&sort=popular&spp=30",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    products = []
    client_kwargs: Dict[str, Any] = {"timeout": 4.0}
    if WB_PROXY_URL:
        client_kwargs["proxy"] = WB_PROXY_URL

    async with httpx.AsyncClient(**client_kwargs) as client:
        for ep in endpoints:
            try:
                res = await client.get(ep, headers=headers)
                if res.status_code == 200:
                    data = json.loads(res.text)
                    products = data.get("data", {}).get("products", []) or data.get("products", [])
                    if products:
                        break
            except Exception:
                continue

    if not products:
        return []

    def get_price_and_stock(p: dict) -> Tuple[int, int]:
        sizes = p.get("sizes") or []
        qty = int(p.get("totalQuantity", 1))
        if sizes and isinstance(sizes, list):
            for sz in sizes:
                pr = sz.get("price", {})
                val = pr.get("product") or pr.get("total") or pr.get("basic") or 0
                if val > 0:
                    return int(val / 100), qty
        sale_u = p.get("salePriceU") or p.get("priceU") or 0
        return int(sale_u / 100), qty

    candidates = []
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        price_rub, qty = get_price_and_stock(p)
        if price_rub < 300 or qty <= 0:
            continue

        raw_name = str(p.get("name") or "").strip()
        brand = str(p.get("brand") or "").strip()
        full_title = f"{brand} {raw_name}".strip() if (brand and brand.lower() not in raw_name.lower()) else raw_name

        t_low = full_title.lower()
        if any(w in t_low for w in ACCESSORY_STOP_WORDS) and not any(w in q_clean.lower() for w in ACCESSORY_STOP_WORDS):
            continue

        candidates.append((price_rub, full_title, brand, p))

    if len(candidates) < 8:
        return []

    all_prices = sorted(c[0] for c in candidates)
    idx_market = len(all_prices) // 2
    market_benchmark = all_prices[min(idx_market, len(all_prices) - 1)]
    sample_size = len(candidates)

    items: List[Dict[str, Any]] = []
    for price_rub, full_title, brand, p in candidates[:30]:
        pid = int(p["id"])
        rating = float(p.get("reviewRating") or p.get("rating") or 4.8)
        feedbacks = int(p.get("feedbacks") or 15)
        supplier = str(p.get("supplier") or brand or "Wildberries")

        if price_rub < market_benchmark:
            diff_rub = market_benchmark - price_rub
            diff_pct = round((diff_rub / market_benchmark) * 100, 1)
        else:
            diff_rub = 0
            diff_pct = 0.0

        items.append({
            "id": f"wb_{pid}",
            "title": full_title[:120],
            "category": "all",
            "source": "Wildberries",
            "city": city,
            "timeAgo": "В наличии · Live",
            "price": price_rub,
            "market": market_benchmark,
            "profitRub": diff_rub,
            "profitPct": diff_pct,
            "tags": [
                f"Арт. {pid}",
                f"★ {rating} ({feedbacks})",
                f"Выборка: {sample_size} шт.",
            ],
            "seller": {
                "name": supplier[:45],
                "rating": rating,
                "reviews": feedbacks,
            },
            "desc": (
                f"Товар каталога Wildberries (Артикул: {pid}). "
                f"Цена со скидкой: {price_rub:,} ₽. Среднерыночная стоимость по группе: {market_benchmark:,} ₽."
            ).replace(",", " "),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
            "createdAtTs": int(time.time()),
        })

    cache_set_wb(cache_key, items)
    return items


async def fetch_db_external_lots(
    source: str = "all",
    city: str = "Москва",
    search_query: str = "",
) -> Tuple[bool, List[Dict[str, Any]]]:
    if not pg_pool:
        return False, []
    async with pg_pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM dh_lots WHERE created_at < NOW() - INTERVAL '{LOT_TTL_HOURS} hours'"
        )
        # гигиена TG: спрос-посты («купл/ищу/предложите») не лоты
        await conn.execute(
            "DELETE FROM dh_lots WHERE source = 'TG' AND title ~* '^([\\W_]+)?(купл|ищу|предложите|нужен|спрос)'"
        )
        total_ext = await conn.fetchval("SELECT COUNT(*) FROM dh_lots")
        rows = await conn.fetch("SELECT * FROM dh_lots ORDER BY created_at DESC LIMIT 100")

    results = []
    try:
        from tg_scan import PRICE_FLOORS as TG_FLOORS, DEFAULT_FLOOR as TG_FLOOR
    except Exception:
        TG_FLOORS, TG_FLOOR = {}, 900
    q_words = [w.lower() for w in search_query.split() if len(w) >= 2]
    for r in rows:
        if source != "all" and r["source"].lower() != source.lower():
            continue
        if r["source"] == "TG":
            tl = re.sub(r"^[\W_]+", "", r["title"]).lower()
            floor = TG_FLOOR
            for brand, fl in TG_FLOORS.items():
                if brand in tl:
                    floor = fl
                    break
            if r["price"] < floor or "запчаст" in tl:
                continue
        if city and city not in r["city"] and r["city"] != "Вся РФ":
            continue
        if q_words:
            text_blob = (r["title"] + " " + r["description"]).lower()
            if not all(w in text_blob for w in q_words):
                continue

        try:
            tags_list = json.loads(r["tags"]) if isinstance(r["tags"], str) else list(r["tags"] or [])
        except Exception:
            tags_list = []

        results.append({
            "id": r["id"],
            "title": r["title"],
            "category": "all",
            "source": r["source"],
            "city": r["city"],
            "timeAgo": humanize_time_ago(r["created_at"]),
            "price": r["price"],
            "market": r["market"],
            "profitRub": r["profit_rub"],
            "profitPct": r["profit_pct"],
            "tags": tags_list,
            "seller": {
                "name": r["seller_name"],
                "rating": r["seller_rating"],
                "reviews": r["seller_reviews"],
            },
            "desc": r["description"],
            "url": r["url"],
            "createdAtTs": int(r["created_at"].timestamp()),
        })

    return bool(total_ext and total_ext > 0), results


async def build_honest_feed(
    category: str = "all",
    source: str = "all",
    search_query: str = "",
    city: str = "Москва",
    sort_by: str = "profit",
    excluded_ids: Optional[List[str]] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    city_clean = city if city in ALLOWED_CITIES else "Москва"
    source_clean = source if source in ALLOWED_SOURCES else "all"
    q_clean = search_query.strip()
    target_query = q_clean if q_clean else DEFAULT_BRAND_QUERIES.get(category, "Dyson")

    has_external_lots, ext_items = await fetch_db_external_lots(
        source=source_clean,
        city=city_clean,
        search_query=q_clean,
    )

    combined = list(ext_items)
    if source_clean in ("all", "Wildberries"):
        wb_items = await fetch_wb_realtime_api(query_text=target_query, city=city_clean)
        combined.extend(wb_items)

    if excluded_ids:
        ex_set = set(excluded_ids)
        combined = [x for x in combined if x["id"] not in ex_set]

    if sort_by == "price_asc":
        combined.sort(key=lambda x: x["price"])
    elif sort_by == "date":
        combined.sort(key=lambda x: x.get("createdAtTs", 0), reverse=True)
    else:
        combined.sort(key=lambda x: (x["profitRub"], x["profitPct"]), reverse=True)

    return has_external_lots, combined


# =====================================================================
# 6. СКАНЕР МОНИТОРОВ
# =====================================================================
def rub(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def is_quiet_for(start: int, end: int) -> bool:
    """Тихие часы для диапазона start..end (по МСК). Диапазон может переходить через полночь."""
    hour = (now_utc() + timedelta(hours=3)).hour
    start, end = int(start) % 24, int(end) % 24
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def in_quiet_hours() -> bool:
    """Глобальные тихие часы (по МСК): ночью не будим пользователей пушами. Сканирование просто пропускается."""
    return is_quiet_for(QUIET_START, QUIET_END)


async def monitor_scanner_loop():
    await asyncio.sleep(12)
    last_quiet_log = ""
    while True:
        try:
            if in_quiet_hours():
                m = now_utc().strftime("%d-%m %H") [:9]
                if m != last_quiet_log:
                    logger.info(f"Тихие часы {QUIET_START}:00–{QUIET_END}:00 МСК — пуши приостановлены")
                    last_quiet_log = m
                await asyncio.sleep(300)
                continue
            if not bot or not pg_pool:
                await asyncio.sleep(30)
                continue

            async with pg_pool.acquire() as conn:
                await conn.execute("""
                    UPDATE dh_monitors
                    SET found_today = 0, found_date = CURRENT_DATE
                    WHERE found_date < CURRENT_DATE
                """)
                await conn.execute("""
                    DELETE FROM dh_monitor_hits
                    WHERE notified_at < NOW() - INTERVAL '7 days'
                """)

                rows = await conn.fetch("""
                    SELECT m.*, u.quiet_start AS u_quiet_start, u.quiet_end AS u_quiet_end
                    FROM dh_monitors m
                    JOIN dh_users u ON u.user_id = m.user_id
                    WHERE m.active = TRUE
                      AND u.sub_until IS NOT NULL
                      AND u.sub_until > NOW()
                """)

            if not rows:
                await asyncio.sleep(60)
                continue

            grouped: Dict[Tuple[str, str, str], List[asyncpg.Record]] = {}
            for m in rows:
                key = (m["title"].strip().lower(), m["city"].strip(), m["source"].strip())
                grouped.setdefault(key, []).append(m)

            for (q_low, city, source), mon_list in grouped.items():
                _, feed = await build_honest_feed(
                    category="all",
                    source=source,
                    search_query=q_low,
                    city=city,
                )
                if not feed:
                    continue

                for m in mon_list:
                    # Персональные тихие часы пользователя: не отправляем пуши в его окно.
                    # Хит не записывается — лот либо попадёт в следующий цикл, либо устареет.
                    u_qs = m["u_quiet_start"] if m["u_quiet_start"] is not None else QUIET_START
                    u_qe = m["u_quiet_end"] if m["u_quiet_end"] is not None else QUIET_END
                    if is_quiet_for(int(u_qs), int(u_qe)):
                        continue

                    min_margin = int(m["min_margin"])
                    extra_words = [w.strip().lower() for w in (m.get("extras") or "").replace(",", " ").split() if w.strip()]
                    eligible_lots = [
                        lot for lot in feed
                        if float(lot.get("profitPct", 0)) >= min_margin and lot.get("profitRub", 0) >= 1000
                    ]
                    if extra_words:
                        eligible_lots = [
                            lot for lot in eligible_lots
                            if all(w in (lot.get("title", "") + " " + str(lot.get("desc", ""))).lower() for w in extra_words)
                        ]
                    if not eligible_lots:
                        continue

                    sent_in_cycle = 0
                    for lot in eligible_lots:
                        if sent_in_cycle >= 3:
                            break
                        lot_key = f"{lot['id']}_{lot['price']}"

                        async with pg_pool.acquire() as conn:
                            inserted = await conn.fetchval(
                                """
                                INSERT INTO dh_monitor_hits (monitor_id, lot_key)
                                VALUES ($1, $2)
                                ON CONFLICT (monitor_id, lot_key) DO NOTHING
                                RETURNING lot_key
                                """,
                                m["id"], lot_key,
                            )
                        if not inserted:
                            continue

                        kb = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text=f"🔥 Открыть на {lot['source']}", url=lot["url"])],
                                [InlineKeyboardButton(text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
                            ]
                        )
                        msg_text = (
                            f"🎯 <b>Найдена выгодная цена по монитору «{html_lib.escape(m['title'])}»!</b>\n\n"
                            f"📦 <b>{html_lib.escape(lot['title'])}</b>\n"
                            f"📍 Регион: <b>{html_lib.escape(lot['city'])}</b> ({html_lib.escape(lot['source'])})\n"
                            f"💰 Цена: <b>{rub(lot['price'])} ₽</b>\n"
                            f"📊 Рыночная планка: <b>{rub(lot['market'])} ₽</b>\n"
                            f"🔥 Выгода: <b>−{lot['profitPct']}% (экономия {rub(lot['profitRub'])} ₽)</b>"
                        )

                        try:
                            await bot.send_message(m["user_id"], msg_text, parse_mode="HTML", reply_markup=kb)
                            sent_in_cycle += 1
                            async with pg_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE dh_monitors SET found_today = found_today + 1, last_notified_lot = $2 WHERE id = $1",
                                    m["id"], lot_key,
                                )
                        except Exception as e:
                            logger.warning(f"Ошибка пуша монитора {m['id']}: {e}")

                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Ошибка сканера: {e}")

        await asyncio.sleep(60)


# =====================================================================
# 7. TELEGRAM BOT
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: Optional[CommandObject] = None):
    uid = message.from_user.id
    fname = (message.from_user.first_name or "Охотник")[:64]
    uname = (message.from_user.username or "")[:64]

    candidate_ref = None
    args = command.args if command else None
    if not args and message.text and " " in message.text:
        args = message.text.split(" ", 1)[1].strip()
    if args and args.startswith("ref_"):
        r_str = args.replace("ref_", "").strip()
        if r_str.isdigit() and int(r_str) != uid:
            candidate_ref = int(r_str)

    if pg_pool:
        await upsert_user_atomic(uid, fname, uname, candidate_ref_id=candidate_ref)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Запустить DealHunter PRO", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton(text="⭐️ Купить Stars выгодно (BunnyStars)", url=BUNNY_STARS_URL)],
        ]
    )
    await message.answer(
        f"👋 <b>Привет, {fname}!</b>\n\n"
        "<b>DealHunter PRO</b> — радар скидок и честного сравнения цен на <b>Wildberries, Ozon и Avito</b>.\n\n"
        f"🎁 Новым пользователям доступно <b>{TRIAL_DAYS} дня PRO бесплатно</b> при активации внутри приложения!\n"
        f"👥 Приглашайте друзей и получайте <b>+{REFERRAL_BONUS_DAYS} дня PRO</b> за каждого друга.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.message(Command("login"))
async def cmd_login(message: types.Message):
    uid = message.from_user.id
    if message.chat.type != "private":
        await message.answer("Ссылка входа отправляется только в личных сообщениях боту")
        return
    tok = fallback_sessions.issue_login_token(uid, (message.from_user.first_name or "")[:64],
                                              (message.from_user.username or "")[:64])
    await message.answer(
        f"🔗 <b>Ссылка входа для компьютера</b> (действует 5 минут):\n\n{WEBAPP_URL}/#/t={tok}\n\n"
        "Дождитесь страницы приложения — она сама запомнит тебя. "
        "Новая ссылка — снова команда /login.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload or ""
    if not payload.startswith("pro_30d_") or query.currency != "XTR" or query.total_amount != STARS_PRICE:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Некорректный счёт")
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    uid = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    try:
        await upsert_user_atomic(uid, message.from_user.first_name or "Охотник", message.from_user.username or "")
        pool = require_db()
        now = now_utc()
        async with pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    "INSERT INTO dh_payments (charge_id, user_id, amount_stars, days_granted, payload) "
                    "VALUES ($1, $2, $3, 30, $4) ON CONFLICT (charge_id) DO NOTHING RETURNING charge_id",
                    charge_id, uid, sp.total_amount, sp.invoice_payload,
                )
                if inserted:
                    row = await conn.fetchrow("SELECT sub_until FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
                    cur_end = row["sub_until"] if row else None
                    base = cur_end if (cur_end and cur_end > now) else now
                    await conn.execute(
                        "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                        uid, base + timedelta(days=30),
                    )
        await message.answer(f"✅ <b>Оплата подтверждена!</b>\n\n+30 дней PRO начислено.\nЧек: <code>{html_lib.escape(charge_id)}</code>", parse_mode="HTML")
    except Exception:
        logger.exception(f"ОПЛАТА НЕ ЗАЧИСЛЕНА: uid={uid} charge={charge_id}")
        if ADMIN_UID and bot:
            try:
                await bot.send_message(ADMIN_UID, f"⚠️ Оплата не зачислена: uid={uid}, charge={charge_id}")
            except Exception:
                pass
        try:
            await message.answer("Оплата получена, но начисление задержалось. Мы разберёмся и начислим вручную.")
        except Exception:
            pass


async def _drop_leader_conn():
    """Закрыть и вернуть в пул соединение, на котором держится advisory lock."""
    global background_leader_conn
    conn = background_leader_conn
    background_leader_conn = None
    if conn is None:
        return
    try:
        conn.terminate()
    except Exception:
        pass
    if pg_pool:
        try:
            await pg_pool.release(conn)
        except Exception:
            pass


async def run_leader_session():
    """Одна лидерская сессия: попытка взять лок, polling бота и сканер.

    Возвращает False, если лок занят другим инстансом. Возвращается также при
    потере связи с БД — тогда соединение сброшено и вызывающий попробует снова.
    """
    global BOT_USERNAME_CACHE, background_leader_conn

    conn = await pg_pool.acquire()
    background_leader_conn = conn

    locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID)
    if not locked:
        await _drop_leader_conn()
        return False

    logger.info("Лидер получен: запуск бота и сканера.")
    scanner_task = asyncio.create_task(monitor_scanner_loop())
    polling: Optional[asyncio.Task] = None
    hb_task: Optional[asyncio.Task] = None
    try:
        if bot:
            me = await bot.get_me()
            if me and me.username:
                BOT_USERNAME_CACHE = me.username
            await bot.delete_webhook(drop_pending_updates=False)
            polling = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, close_bot_session=False)
            )

        async def heartbeat_loop():
            while True:
                await asyncio.sleep(HEARTBEAT_SEC)
                try:
                    await asyncio.wait_for(conn.fetchval("SELECT 1"), 10)
                except Exception as hb_err:
                    logger.error(f"Соединение лидера с БД потеряно: {hb_err}")
                    return

        # Связь с БД теряется независимо от того, подключён ли бот, поэтому heartbeat
        # всегда в наборе. Ждём первого завершившегося: heartbeat (потеря связи),
        # polling (бот остановился) или сканер (упал).
        hb_task = asyncio.create_task(heartbeat_loop())
        tasks = [hb_task, scanner_task]
        if polling is not None:
            tasks.append(polling)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        if hb_task.done() and not hb_task.cancelled() and hb_task.exception() is None:
            # heartbeat вышел штатно → связь с БД потеряна
            if polling is not None:
                polling.cancel()
            await _drop_leader_conn()
            return
    finally:
        for t in (hb_task, polling):
            if t is not None and not t.done():
                t.cancel()
        scanner_task.cancel()


async def single_leader_background_manager():
    global background_leader_conn
    await asyncio.sleep(2)
    while True:
        try:
            if not pg_pool:
                await asyncio.sleep(5)
                continue
            if background_leader_conn is not None and background_leader_conn.is_closed():
                background_leader_conn = None

            became_leader_session = await run_leader_session()
            if not became_leader_session and background_leader_conn is None:
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Рестарт фонового воркера: {e}")
            await _drop_leader_conn()
            await asyncio.sleep(5)


@app.on_event("startup")
async def on_startup():
    global leader_task
    await init_db()
    leader_task = asyncio.create_task(single_leader_background_manager())


@app.on_event("shutdown")
async def on_shutdown():
    global leader_task, background_leader_conn, pg_pool
    if leader_task:
        leader_task.cancel()
    if background_leader_conn and pg_pool:
        await _drop_leader_conn()
    if bot:
        try:
            await bot.session.close()
        except Exception:
            pass
    if pg_pool:
        try:
            await pg_pool.close()
        except Exception:
            pass


# =====================================================================
# 8. REST API ЭНДПОИНТЫ
# =====================================================================
@app.get("/health")
@app.head("/")
async def health():
    return {"status": "ok", "bot": BOT_USERNAME_CACHE, "db_connected": bool(pg_pool)}


@app.get("/api/state")
async def api_state(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    user = await upsert_user_atomic(
        uid=auth["uid"],
        first_name=auth["first_name"],
        username=auth["username"],
        candidate_ref_id=auth["ref_id"],
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE dh_monitors SET found_today = 0, found_date = CURRENT_DATE WHERE user_id = $1 AND found_date < CURRENT_DATE", auth["uid"])
        rows = await conn.fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", auth["uid"])
    monitors = [
        {
            "id": r["id"],
            "title": r["title"],
            "city": r["city"],
            "source": r["source"],
            "minMargin": r["min_margin"],
            "active": bool(r["active"]),
            "foundToday": r["found_today"],
            "extras": r.get("extras", "") or "",
        }
        for r in rows
    ]
    return JSONResponse({"user": user, "monitors": monitors})


async def ensure_sponsor_subscription(uid: int):
    if not SPONSOR_CHANNEL or not bot:
        return
    ok = False
    try:
        m = await bot.get_chat_member(SPONSOR_CHANNEL, uid)
        ok = m.status in ("creator", "administrator", "member") or (m.status == "restricted" and bool(getattr(m, "is_member", False)))
    except Exception as e:
        logger.warning(f"Проверка подписки не удалась (бот должен быть админом канала): {e}")
    if not ok:
        raise HTTPException(status_code=403, detail=f"Подпишитесь на {SPONSOR_CHANNEL} и нажмите ещё раз")


@app.post("/api/auth-token")
async def api_auth_token(data: dict = Body(...)):
    tok = str(data.get("token", "")).strip()
    if not tok or len(tok) < 16:
        raise HTTPException(status_code=400, detail="Некорректный токен")
    sess = fallback_sessions.redeem_login_token(tok)
    if not sess:
        raise HTTPException(status_code=401, detail="Ссылка устарела или уже использована. Запросите новую в боте")
    return JSONResponse({"token": sess})


class DeleteMeReq(BaseModel):
    confirm: bool = Field(...)


@app.post("/api/accept-terms")
async def api_accept_terms(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    await pool.execute("UPDATE dh_users SET terms_accepted_at = NOW() WHERE user_id = $1", auth["uid"])
    return JSONResponse({"status": "ok"})


@app.post("/api/delete-me")
async def api_delete_me(data: DeleteMeReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    """Удаление всех данных пользователя: мониторы, настройки, подписка, сессия.
    Финансовые записи (dh_payments) сохраняются для учёта сделок — они не содержат контактов."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Нужно подтверждение")
    uid = auth["uid"]
    pool = require_db()
    n_mon = 0
    async with pool.acquire() as conn:
        n_mon = await conn.fetchval("SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid) or 0
        await conn.execute("DELETE FROM dh_monitors WHERE user_id = $1", uid)
        await conn.execute("DELETE FROM dh_users WHERE user_id = $1", uid)
    fallback_sessions.drop_sessions_for_uid(uid)
    return JSONResponse({"status": "ok", "monitorsDeleted": n_mon, "message": "Данные удалены"})


@app.post("/api/claim-trial")
async def api_claim_trial(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    await ensure_sponsor_subscription(auth["uid"])
    await upsert_user_atomic(
        uid=auth["uid"],
        first_name=auth["first_name"],
        username=auth["username"],
        candidate_ref_id=auth["ref_id"],
    )
    user = await claim_trial_and_reward_referrer(auth["uid"], auth.get("ip_hash", ""))
    return JSONResponse({"status": "ok", "user": user})


@app.get("/api/live-feed")
async def api_live_feed(
    category: str = "all",
    source: str = "all",
    q: str = "",
    city: str = "Москва",
    sort: str = "profit",
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    user = await upsert_user_atomic(auth["uid"], auth["first_name"], auth["username"])
    if not user["subActive"]:
        return JSONResponse({"locked": True, "items": [], "extConnected": False})

    has_ext, items = await build_honest_feed(
        category=category,
        source=source,
        search_query=q,
        city=city,
        sort_by=sort,
    )
    return JSONResponse({
        "locked": False,
        "items": items,
        "extConnected": has_ext,
        "updatedAt": now_utc().strftime("%H:%M:%S"),
    })


class ClaimLotReq(BaseModel):
    lotId: str = Field(..., max_length=80)


@app.post("/api/claim-lot")
async def api_claim_lot(data: ClaimLotReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    uid = auth["uid"]
    lot_id = data.lotId.strip()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT claimed_ids FROM dh_users WHERE user_id = $1", uid)
        cur_list = []
        if row and row["claimed_ids"]:
            try:
                cur_list = json.loads(row["claimed_ids"])
            except Exception:
                cur_list = []
        if lot_id not in cur_list:
            cur_list.append(lot_id)
            cur_list = cur_list[-200:]
            await conn.execute("UPDATE dh_users SET claimed_ids = $2 WHERE user_id = $1", uid, json.dumps(cur_list))
    return JSONResponse({"status": "ok", "claimedId": lot_id})


class MonitorCreateReq(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    city: str = Field(default="Москва", max_length=50)
    source: str = Field(default="all", max_length=30)
    minMargin: int = Field(default=15, ge=1, le=70)
    extras: str = Field(default="", max_length=200)


@app.post("/api/monitors")
async def api_create_monitor(
    data: MonitorCreateReq,
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    pool = require_db()
    uid = auth["uid"]
    user = await upsert_user_atomic(uid, auth["first_name"], auth["username"])
    if not user["subActive"]:
        raise HTTPException(status_code=403, detail="Мониторы доступны с активной подпиской PRO")

    mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
    city_val = data.city if data.city in ALLOWED_CITIES else "Москва"
    source_val = data.source if data.source in ALLOWED_SOURCES else "all"

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid)
        if count >= 20:
            raise HTTPException(status_code=400, detail="Лимит: не более 20 мониторов")
        await conn.execute(
            """
            INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, found_date, extras)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0, CURRENT_DATE, $7)
            """,
            mid, uid, data.title.strip(), city_val, source_val, data.minMargin, data.extras.strip(),
        )
        rows = await conn.fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", uid)

    return JSONResponse({
        "status": "ok",
        "monitors": [
            {
                "id": r["id"],
                "title": r["title"],
                "city": r["city"],
                "source": r["source"],
                "minMargin": r["min_margin"],
                "active": bool(r["active"]),
                "foundToday": r["found_today"],
                "extras": r.get("extras", "") or "",
            }
            for r in rows
        ],
    })


@app.post("/api/monitors/{mid}/toggle")
async def api_toggle_monitor(mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    uid = auth["uid"]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND user_id = $2", mid, uid)
        rows = await conn.fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", uid)
    return JSONResponse({
        "status": "ok",
        "monitors": [
            {
                "id": r["id"],
                "title": r["title"],
                "city": r["city"],
                "source": r["source"],
                "minMargin": r["min_margin"],
                "active": bool(r["active"]),
                "foundToday": r["found_today"],
            }
            for r in rows
        ],
    })


@app.delete("/api/monitors/{mid}")
async def api_delete_monitor(mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    uid = auth["uid"]
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dh_monitors WHERE id = $1 AND user_id = $2", mid, uid)
        await conn.execute("DELETE FROM dh_monitor_hits WHERE monitor_id = $1", mid)
        rows = await conn.fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", uid)
    return JSONResponse({
        "status": "ok",
        "monitors": [
            {
                "id": r["id"],
                "title": r["title"],
                "city": r["city"],
                "source": r["source"],
                "minMargin": r["min_margin"],
                "active": bool(r["active"]),
                "foundToday": r["found_today"],
            }
            for r in rows
        ],
    })


class QuietUpdateReq(BaseModel):
    quietStart: int = Field(..., ge=0, le=23)
    quietEnd: int = Field(..., ge=0, le=23)


@app.post("/api/quiet")
async def api_set_quiet(
    data: QuietUpdateReq,
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    pool = require_db()
    uid = auth["uid"]
    if data.quietStart == data.quietEnd:
        raise HTTPException(status_code=400, detail="Начало и конец тихих часов не должны совпадать")
    await upsert_user_atomic(uid, auth["first_name"], auth["username"])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE dh_users SET quiet_start = $2, quiet_end = $3 WHERE user_id = $1",
            uid, data.quietStart, data.quietEnd,
        )
    return JSONResponse({
        "status": "ok",
        "quietStart": data.quietStart,
        "quietEnd": data.quietEnd,
        "note": "Пуши не будут приходить в это окно по Москве",
    })


class SavedUpdateReq(BaseModel):
    savedIds: List[str]


@app.post("/api/saved")
async def api_save_lots(data: SavedUpdateReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    clean_ids = [re.sub(r"[^a-zA-Z0-9_\-]", "", str(x))[:40] for x in data.savedIds[:100]]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE dh_users SET saved_ids = $2 WHERE user_id = $1", auth["uid"], json.dumps(clean_ids))
    return JSONResponse({"status": "ok"})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    uid = auth["uid"]
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не подключён")
    try:
        invoice_link = await bot.create_invoice_link(
            title="DealHunter PRO — 30 дней",
            description="Полный доступ к поиску, честному расчету выгоды от розничной планки и авто-мониторингу.",
            payload=f"pro_30d_{uid}_{int(now_utc().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
        )
        return JSONResponse({"invoiceUrl": invoice_link})
    except Exception as e:
        logger.error(f"Ошибка создания инвойса: {e}")
        return JSONResponse({"error": "Не удалось создать счёт Telegram Stars"}, status_code=500)


class IngestLot(BaseModel):
    id: str = Field(..., max_length=80)
    title: str = Field(..., max_length=200)
    source: str
    city: str = Field(default="Москва", max_length=50)
    price: int = Field(..., gt=0, lt=100_000_000)
    market: int = Field(..., gt=0, lt=100_000_000)
    tags: List[str] = Field(default_factory=list, max_length=8)
    sellerName: str = Field(default="Продавец", max_length=60)
    sellerRating: float = Field(default=0, ge=0, le=5)
    sellerReviews: int = Field(default=0, ge=0)
    description: str = Field(default="", max_length=1000)
    url: str


@app.post("/api/worker/ingest")
async def api_worker_ingest(lots: List[IngestLot], x_worker_secret: str = Header(default="")):
    if not WORKER_SECRET or not hmac.compare_digest(x_worker_secret.encode(), WORKER_SECRET.encode()):
        raise HTTPException(status_code=403, detail="forbidden")
    if len(lots) > 200:
        raise HTTPException(status_code=400, detail="Максимум 200 лотов за запрос")
    pool = require_db()
    saved = skipped = 0
    async with pool.acquire() as conn:
        for lot in lots:
            try:
                url = validate_and_clean_url(lot.url)
            except ValueError:
                skipped += 1
                continue
            profit = lot.market - lot.price
            if lot.source not in ("Avito", "Ozon", "Wildberries", "TG", "GIFTS", "PLAYEROK"):
                skipped += 1
                continue
            if lot.source not in ("TG", "GIFTS", "PLAYEROK") and profit <= 0:
                skipped += 1
                continue
            if lot.source == "TG":
                # у б/у-лот из публичной барахолки нет «рыночной» цены: показываем как есть
                lot.market = lot.price
                profit = 0
            if lot.source in ("GIFTS", "PLAYEROK"):
                # цифровые товары: city-фильтр в ленте не должен их резать
                lot.city = "Вся РФ"
            await conn.execute(
                """
                INSERT INTO dh_lots (id, title, source, city, price, market, profit_rub, profit_pct, tags,
                                     seller_name, seller_rating, seller_reviews, description, url)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14)
                ON CONFLICT (id) DO UPDATE SET price=EXCLUDED.price, market=EXCLUDED.market,
                    profit_rub=EXCLUDED.profit_rub, profit_pct=EXCLUDED.profit_pct, tags=EXCLUDED.tags,
                    description=EXCLUDED.description, url=EXCLUDED.url
                """,
                lot.id, lot.title, lot.source, lot.city, lot.price, lot.market, profit,
                round(profit / lot.market * 100, 1), json.dumps(lot.tags, ensure_ascii=False),
                lot.sellerName, lot.sellerRating, lot.sellerReviews, lot.description, url,
            )
            saved += 1
    return {"status": "ok", "saved": saved, "skipped": skipped}


# =====================================================================
# 9. UI LIQUID GLASS
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    page = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return page.replace("__CITIES__", json.dumps(sorted(ALLOWED_CITIES), ensure_ascii=False))
