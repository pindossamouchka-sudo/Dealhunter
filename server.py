import os
import re
import hmac
import json
import time
import base64
import hashlib
import html as htmllib
import logging
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Literal
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, parse_qsl, urlparse

import httpx
import asyncpg
from fastapi import FastAPI, Request, Header, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dealhunter")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip().rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()
WB_PROXY_URL = os.getenv("WB_PROXY_URL", "").strip() or None
SPONSOR_CHANNEL = os.getenv("SPONSOR_CHANNEL", "").strip()  # например "@my_channel"
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "").strip()  # например "@my_support"
_admin_raw = os.getenv("ADMIN_UID", "").strip()
ADMIN_UID = int(_admin_raw) if _admin_raw.isdigit() else 0

BOT_USERNAME_DEFAULT = "DealHunterAvito_bot"
BOT_USERNAME_CACHE = os.getenv("BOT_USERNAME", BOT_USERNAME_DEFAULT).strip().lstrip("@") or BOT_USERNAME_DEFAULT
BUNNY_STARS_URL = os.getenv("BUNNY_STARS_URL", "https://t.me/BunnyStarsBot?startapp=ref_5123484212").strip()

STARS_PRICE = 350
TRIAL_DAYS = 3
REFERRAL_BONUS_DAYS = 3
MAX_REF_DAYS_PER_MONTH = 30
LOT_TTL_HOURS = 6
MAX_SAVED = 100
MAX_MONITORS = 15
REFRESH_SEC = 60
ADVISORY_LOCK_ID = 94827165

CITY_DEST_MAP: Dict[str, int] = {
    "Москва": -1257786,
    "Санкт-Петербург": -1198055,
    "Екатеринбург": -5818883,
    "Казань": -2133462,
    "Новосибирск": -364763,
    "Краснодар": -1075831,
}
ALLOWED_CITIES = set(CITY_DEST_MAP.keys())
ALLOWED_SOURCES = {"all", "Wildberries", "Ozon", "Avito"}
SOURCE_HOSTS: Dict[str, set] = {
    "Wildberries": {"www.wildberries.ru", "wildberries.ru"},
    "Ozon": {"www.ozon.ru", "ozon.ru"},
    "Avito": {"www.avito.ru", "avito.ru"},
}

# Аксессуары, восстановленные, витринные и копии: исключаем, если пользователь сам их не ищет
ACCESSORY_STOP_WORDS = (
    "чехол", "стекло", "пленка", "плёнка", "кабель", "провод", "переходник",
    "адаптер", "зарядка", "зарядное", "держатель", "ремешок", "наклейка",
    "брелок", "подставка", "салфетка", "стилус", "коробка от", "муляж",
    "запчасть", "шлейф", "дисплей для", "экран для", "аккумулятор для",
    "батарея для", "корпус для", "крышка для", "линза", "защита камеры",
    "амбушюры", "футляр", "кейс для", "шнурок", "кулер для", "сумка для",
    "восстановлен", "refurbished", "витрин", "копия", "реплика",
    "для iphone", "для xiaomi", "для macbook", "для samsung", "чехол-книжка",
)

DEFAULT_QUERIES_BY_CATEGORY = {
    "all": "смартфон Xiaomi Redmi Note",
    "xiaomi": "смартфон Xiaomi POCO",
    "iphone": "смартфон Apple iPhone 15",
    "macbook": "ноутбук Apple MacBook Air",
    "gpu": "видеокарта GeForce RTX 4060",
    "ps5": "игровая консоль PlayStation 5 Slim",
}

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
pg_pool: Optional[asyncpg.Pool] = None
background_leader_conn: Optional[asyncpg.Connection] = None
background_task: Optional[asyncio.Task] = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# 2. RATE LIMIT И КЭШ
# =====================================================================
USER_RATE_BUCKETS: "OrderedDict[int, List[float]]" = OrderedDict()
WB_LRU_CACHE: "OrderedDict[str, Tuple[float, List[Dict[str, Any]], bool]]" = OrderedDict()
WB_CACHE_MAX_SIZE = 200
WB_CACHE_TTL_OK = 60.0
WB_CACHE_TTL_EMPTY = 10.0


def check_rate_limit(uid: int, max_requests: int = 25, window_sec: float = 10.0):
    now = time.time()
    stamps = [t for t in USER_RATE_BUCKETS.get(uid, []) if now - t < window_sec]
    if len(stamps) >= max_requests:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подождите несколько секунд.")
    stamps.append(now)
    USER_RATE_BUCKETS[uid] = stamps
    USER_RATE_BUCKETS.move_to_end(uid)
    while len(USER_RATE_BUCKETS) > 2000:
        USER_RATE_BUCKETS.popitem(last=False)


def cache_get_wb(key: str) -> Optional[Tuple[List[Dict[str, Any]], bool]]:
    entry = WB_LRU_CACHE.get(key)
    if not entry:
        return None
    expires_at, data, available = entry
    if time.time() > expires_at:
        WB_LRU_CACHE.pop(key, None)
        return None
    WB_LRU_CACHE.move_to_end(key)
    return data, available


def cache_set_wb(key: str, data: List[Dict[str, Any]], available: bool, ttl: float):
    WB_LRU_CACHE[key] = (time.time() + ttl, data, available)
    WB_LRU_CACHE.move_to_end(key)
    while len(WB_LRU_CACHE) > WB_CACHE_MAX_SIZE:
        WB_LRU_CACHE.popitem(last=False)


# =====================================================================
# 3. ПРОВЕРКА TELEGRAM INIT DATA
# =====================================================================
def verify_telegram_init_data(init_data_raw: str) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Сервер не настроен (нет BOT_TOKEN)")
    if not init_data_raw:
        raise HTTPException(status_code=401, detail="Откройте приложение внутри Telegram")
    try:
        data_dict = dict(parse_qsl(init_data_raw, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный формат данных авторизации")

    received_hash = data_dict.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Отсутствует подпись")

    auth_date_raw = data_dict.get("auth_date", "")
    if not auth_date_raw.isdigit():
        raise HTTPException(status_code=401, detail="Отсутствует метка времени сессии")
    auth_date = int(auth_date_raw)
    now_ts = int(time.time())
    if auth_date <= 0 or (now_ts - auth_date) > 86400 or (auth_date - now_ts) > 300:
        raise HTTPException(status_code=401, detail="Сессия устарела. Перезапустите приложение из бота.")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(status_code=401, detail="Подпись Telegram недействительна")

    try:
        user_obj = json.loads(data_dict.get("user", ""))
        uid = int(user_obj["id"])
    except Exception:
        raise HTTPException(status_code=401, detail="Профиль пользователя отсутствует в подписи")

    ref_id = None
    start_param = data_dict.get("start_param", "")
    if start_param.startswith("ref_"):
        r_clean = start_param[4:].strip()
        if r_clean.isdigit() and int(r_clean) != uid:
            ref_id = int(r_clean)

    return {
        "uid": uid,
        "first_name": str(user_obj.get("first_name") or "Пользователь")[:64],
        "username": str(user_obj.get("username") or "")[:64],
        "ref_id": ref_id,
    }


async def get_authenticated_user(request: Request) -> Dict[str, Any]:
    b64_header = request.headers.get("X-Tg-Init-Data-B64", "").strip()
    if not b64_header:
        raise HTTPException(status_code=401, detail="Требуется запуск внутри Telegram")
    try:
        init_data_raw = unquote(base64.b64decode(b64_header).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный заголовок авторизации")
    auth = verify_telegram_init_data(init_data_raw)
    check_rate_limit(auth["uid"])
    return auth


# =====================================================================
# 4. URL И ВРЕМЯ
# =====================================================================
def validate_and_clean_url(raw_url: str, source: Optional[str] = None) -> str:
    """Строгий разбор URL: https, без кавычек и пробелов, хост из белого списка и совпадает с источником."""
    u = str(raw_url or "").strip()
    if not u or any(ch in u for ch in ("'", '"', "<", ">", " ", "\n", "\r", "\t", "\\")) or "javascript:" in u.lower():
        raise ValueError("URL содержит недопустимые символы")
    parsed = urlparse(u)
    if parsed.scheme != "https":
        raise ValueError("Разрешён только https")
    host = (parsed.hostname or "").lower()
    allowed = SOURCE_HOSTS.get(source) if source else set().union(*SOURCE_HOSTS.values())
    if not allowed or host not in allowed:
        raise ValueError(f"Домен {host} не соответствует источнику {source}")
    return u


def humanize_time_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return "В наличии"
    diff = max(0, int((now_utc() - dt).total_seconds()))
    if diff < 60:
        return "Только что"
    mins = diff // 60
    if mins < 60:
        return f"{mins} мин назад"
    return f"{mins // 60} ч назад"


# =====================================================================
# 5. БАЗА ДАННЫХ
# =====================================================================
def require_db() -> asyncpg.Pool:
    if not pg_pool:
        raise HTTPException(status_code=503, detail="База данных не подключена (проверьте DATABASE_URL)")
    return pg_pool


async def init_db():
    global pg_pool
    if not DATABASE_URL.startswith("postgres"):
        logger.error("КРИТИЧНО: задайте DATABASE_URL (PostgreSQL)!")
        return
    try:
        pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=15)
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dh_users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT DEFAULT 'Пользователь',
                    username TEXT DEFAULT '',
                    trial_claimed BOOLEAN DEFAULT FALSE,
                    sub_until TIMESTAMPTZ DEFAULT NULL,
                    saved_ids TEXT DEFAULT '[]',
                    referred_by BIGINT DEFAULT NULL,
                    ref_rewarded BOOLEAN DEFAULT FALSE,
                    ref_count INT DEFAULT 0,
                    ref_days_earned INT DEFAULT 0,
                    ref_month TEXT DEFAULT '',
                    ref_month_days INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS dh_trial_used (user_id BIGINT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS dh_monitors (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    city TEXT DEFAULT 'Москва',
                    source TEXT DEFAULT 'Wildberries',
                    min_margin INT DEFAULT 15,
                    active BOOLEAN DEFAULT TRUE,
                    found_today INT DEFAULT 0,
                    found_date DATE DEFAULT CURRENT_DATE,
                    last_notified_lot TEXT DEFAULT '',
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
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_seen TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS dh_saved (
                    user_id BIGINT NOT NULL,
                    lot_id TEXT NOT NULL,
                    snapshot JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, lot_id)
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
            # Миграции для старой схемы
            for stmt in (
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL",
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN DEFAULT FALSE",
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT DEFAULT 0",
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT DEFAULT 0",
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month TEXT DEFAULT ''",
                "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month_days INT DEFAULT 0",
                "ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS found_date DATE DEFAULT CURRENT_DATE",
                "ALTER TABLE dh_lots ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ DEFAULT NOW()",
                "ALTER TABLE dh_lots ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ DEFAULT NOW()",
            ):
                await conn.execute(stmt)
        logger.info("PostgreSQL инициализирован.")
    except Exception as e:
        logger.exception(f"Ошибка инициализации PostgreSQL: {e}")
        pg_pool = None


def sponsor_url() -> str:
    ch = SPONSOR_CHANNEL.strip()
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    if ch.startswith("https://t.me/"):
        return ch
    return ""


async def check_sponsor_membership(uid: int) -> bool:
    """Fail-closed: при любой ошибке проверки подписка НЕ считается выполненной."""
    if not SPONSOR_CHANNEL:
        return True
    if not bot:
        return False
    try:
        member = await bot.get_chat_member(chat_id=SPONSOR_CHANNEL, user_id=uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Не удалось проверить подписку {uid} на {SPONSOR_CHANNEL} (бот должен быть админом канала): {e}")
        return False


def format_user_row(row: asyncpg.Record) -> Dict[str, Any]:
    now = now_utc()
    sub_until: Optional[datetime] = row["sub_until"]
    active = bool(sub_until and sub_until > now)
    cur_month = now.strftime("%Y-%m")
    month_days = int(row["ref_month_days"] or 0) if row["ref_month"] == cur_month else 0
    return {
        "user_id": row["user_id"],
        "name": row["first_name"] or "Пользователь",
        "trialClaimed": bool(row["trial_claimed"]),
        "subActive": active,
        "subUntil": sub_until.isoformat() if sub_until else None,
        "secondsLeft": int((sub_until - now).total_seconds()) if active else 0,
        "refCount": row["ref_count"] or 0,
        "refDaysEarned": row["ref_days_earned"] or 0,
        "refMonthDays": month_days,
        "refMonthLimit": MAX_REF_DAYS_PER_MONTH,
        "sponsorChannel": SPONSOR_CHANNEL,
        "sponsorUrl": sponsor_url(),
        "refLink": f"https://t.me/{BOT_USERNAME_CACHE}?start=ref_{row['user_id']}",
    }


async def upsert_user_atomic(uid: int, first_name: str, username: str, candidate_ref_id: Optional[int] = None) -> Dict[str, Any]:
    pool = require_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            valid_referrer = None
            if candidate_ref_id and candidate_ref_id != uid:
                if await conn.fetchval("SELECT user_id FROM dh_users WHERE user_id = $1", candidate_ref_id):
                    valid_referrer = candidate_ref_id
            row = await conn.fetchrow(
                """
                INSERT INTO dh_users (user_id, first_name, username, referred_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    username = CASE WHEN EXCLUDED.username != '' THEN EXCLUDED.username ELSE dh_users.username END
                RETURNING *
                """,
                uid, first_name, username, valid_referrer,
            )
    return format_user_row(row)


async def get_existing_user(uid: int) -> Dict[str, Any]:
    pool = require_db()
    row = await pool.fetchrow("SELECT * FROM dh_users WHERE user_id = $1", uid)
    if not row:
        raise HTTPException(status_code=404, detail="Профиль не найден. Перезапустите приложение из бота.")
    return format_user_row(row)


async def claim_trial_and_reward_referrer(uid: int) -> Dict[str, Any]:
    pool = require_db()
    claimed = await pool.fetchval("SELECT trial_claimed FROM dh_users WHERE user_id = $1", uid)
    if claimed is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    if not claimed:
        if await pool.fetchval("SELECT 1 FROM dh_trial_used WHERE user_id = $1", uid):
            raise HTTPException(status_code=403, detail="Пробный период для этого аккаунта уже был использован")
        if not await check_sponsor_membership(uid):
            raise HTTPException(
                status_code=403,
                detail=f"Подпишитесь на канал {SPONSOR_CHANNEL} и нажмите кнопку ещё раз. Если вы уже подписаны, подождите несколько секунд.",
            )

    now = now_utc()
    cur_month = now.strftime("%Y-%m")
    referrer_to_notify: Optional[int] = None
    days_to_ref = 0
    user_name = "Пользователь"

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
            if not row:
                raise HTTPException(status_code=404, detail="Профиль не найден")
            if row["trial_claimed"]:
                return format_user_row(row)

            user_name = row["first_name"] or "Друг"
            cur_end = row["sub_until"]
            base = cur_end if (cur_end and cur_end > now) else now
            updated = await conn.fetchrow(
                "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2, ref_rewarded = TRUE WHERE user_id = $1 RETURNING *",
                uid, base + timedelta(days=TRIAL_DAYS),
            )

            referred_by = row["referred_by"]
            if referred_by and not row["ref_rewarded"] and referred_by != uid:
                ref_row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE", referred_by)
                if ref_row:
                    used = int(ref_row["ref_month_days"] or 0) if ref_row["ref_month"] == cur_month else 0
                    days = min(REFERRAL_BONUS_DAYS, max(0, MAX_REF_DAYS_PER_MONTH - used))
                    if days > 0:
                        r_end = ref_row["sub_until"]
                        r_base = r_end if (r_end and r_end > now) else now
                        await conn.execute(
                            """
                            UPDATE dh_users SET sub_until = $2,
                                ref_count = COALESCE(ref_count, 0) + 1,
                                ref_days_earned = COALESCE(ref_days_earned, 0) + $3,
                                ref_month = $4, ref_month_days = $5
                            WHERE user_id = $1
                            """,
                            referred_by, r_base + timedelta(days=days), days, cur_month, used + days,
                        )
                        referrer_to_notify, days_to_ref = referred_by, days
                    else:
                        await conn.execute(
                            "UPDATE dh_users SET ref_count = COALESCE(ref_count, 0) + 1 WHERE user_id = $1", referred_by
                        )

    if referrer_to_notify and bot and days_to_ref > 0:
        try:
            await bot.send_message(
                referrer_to_notify,
                f"🎉 <b>Реферал подтверждён: {htmllib.escape(user_name)}</b>\n\n"
                f"Друг активировал доступ. Вам начислено <b>+{days_to_ref} дн. PRO</b>.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить реферера {referrer_to_notify}: {e}")
    return format_user_row(updated)


async def grant_payment(uid: int, first_name: str, charge_id: str, amount: int, payload: str, days: int = 30) -> bool:
    """Идемпотентно записывает платёж и продлевает подписку. True если платёж новый."""
    pool = require_db()
    now = now_utc()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO dh_payments (charge_id, user_id, amount_stars, days_granted, payload)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (charge_id) DO NOTHING RETURNING charge_id
                """,
                charge_id, uid, amount, days, payload,
            )
            if not inserted:
                return False
            await conn.execute(
                "INSERT INTO dh_users (user_id, first_name) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
                uid, first_name[:64],
            )
            row = await conn.fetchrow("SELECT sub_until FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
            cur_end = row["sub_until"]
            base = cur_end if (cur_end and cur_end > now) else now
            await conn.execute(
                "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                uid, base + timedelta(days=days),
            )
    return True


# =====================================================================
# 6. ЧЕСТНАЯ МЕДИАНА: ФИЛЬТРЫ И КЛАСТЕРЫ
# =====================================================================
def is_accessory_or_junk(title: str, query_text: str) -> bool:
    t_low, q_low = title.lower(), query_text.lower()
    return any(w in t_low and w not in q_low for w in ACCESSORY_STOP_WORDS)


def extract_model_id(t: str) -> str:
    """Поколение/модель: iPhone 13 и 15, RTX 4060 и 4070, MacBook M1 и M3 нельзя сравнивать между собой."""
    m = re.search(r"iphone\s*(se|xr|xs|x|\d{1,2})\b", t)
    if m:
        return "iphone" + m.group(1)
    m = re.search(r"\b(rtx|gtx|rx)\s*-?\s*(\d{3,4})\b", t)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"macbook\s*(air|pro)?.{0,40}?\bm([1-4])\b", t)
    if m:
        return f"mac{m.group(1) or ''}m{m.group(2)}"
    if re.search(r"playstation\s*5|\bps\s*5\b", t):
        return "ps5"
    m = re.search(r"(redmi note|redmi|poco|xiaomi|galaxy)\s*([a-z]?\d{1,3}[a-z]?)", t)
    if m:
        return (m.group(1) + m.group(2)).replace(" ", "")
    return "gen"


def extract_model_cluster_key(title: str) -> str:
    t = title.lower().replace("гб", "gb").replace("тб", "tb")
    mem = re.search(r"\b(64|128|256|512|1024)\s*gb\b|\b(1|2)\s*tb\b", t)
    mem_key = mem.group(0).replace(" ", "") if mem else "std"
    mods = [m for m in ("pro", "max", "plus", "ultra", "slim", "ti", "super", "mini", "fe") if re.search(rf"\b{m}\b", t)]
    return f"{extract_model_id(t)}:{'_'.join(mods) or 'base'}:{mem_key}"


def get_price_and_qty(p: dict) -> Tuple[int, int]:
    qty = int(p.get("totalQuantity", 1) or 0)
    for sz in (p.get("sizes") or []):
        pr = sz.get("price", {}) or {}
        val = pr.get("product") or pr.get("total") or pr.get("basic") or 0
        if val > 0:
            return int(val / 100), qty
    return int((p.get("salePriceU") or p.get("priceU") or 0) / 100), qty


async def fetch_wb_clustered(query_text: str, city: str = "Москва") -> Tuple[List[Dict[str, Any]], bool]:
    """Возвращает (лоты, источник_доступен)."""
    q_clean = query_text.strip() or "смартфон"
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
        "Accept": "application/json",
    }
    client_kwargs: Dict[str, Any] = {"timeout": 4.5}
    if WB_PROXY_URL:
        client_kwargs["proxy"] = WB_PROXY_URL

    products: list = []
    got_ok_response = False
    async with httpx.AsyncClient(**client_kwargs) as client:
        for ep in endpoints:
            try:
                res = await client.get(ep, headers=headers)
                if res.status_code == 200:
                    got_ok_response = True
                    data = res.json()
                    products = data.get("data", {}).get("products", []) or data.get("products", [])
                    if products:
                        break
                elif res.status_code == 429:
                    logger.warning("WB API: 429 Too Many Requests")
                else:
                    logger.warning(f"WB API: статус {res.status_code}")
            except Exception as e:
                logger.warning(f"Ошибка обращения к WB API: {e}")

    if not got_ok_response:
        cache_set_wb(cache_key, [], False, WB_CACHE_TTL_EMPTY)
        return [], False

    candidates = []
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        price_rub, qty = get_price_and_qty(p)
        if price_rub < 300 or qty <= 0:
            continue
        name = str(p.get("name") or q_clean).strip()
        brand = str(p.get("brand") or "").strip()
        title = f"{brand} {name}".strip() if (brand and brand.lower() not in name.lower()) else name
        if is_accessory_or_junk(title, q_clean):
            continue
        candidates.append((price_rub, title, p))

    if len(candidates) < 4:
        cache_set_wb(cache_key, [], True, WB_CACHE_TTL_EMPTY)
        return [], True

    prices_sorted = sorted(c[0] for c in candidates)
    q75 = prices_sorted[int(len(prices_sorted) * 0.75)]
    floor_price = int(q75 * 0.35) if q75 > 5000 else 300
    filtered = [c for c in candidates if c[0] >= floor_price]
    if len(filtered) < 4:
        filtered = candidates

    clusters: Dict[str, List[int]] = {}
    for price_rub, title, _ in filtered:
        clusters.setdefault(extract_model_cluster_key(title), []).append(price_rub)

    items: List[Dict[str, Any]] = []
    for price_rub, title, p in filtered:
        cluster_prices = sorted(clusters.get(extract_model_cluster_key(title), []))
        if len(cluster_prices) < 3:
            continue  # без достаточной выборки однотипных позиций медиану не считаем
        ref_median = cluster_prices[len(cluster_prices) // 2]
        if ref_median <= 0:
            continue
        if price_rub < ref_median:
            diff_rub = ref_median - price_rub
            diff_pct = round(diff_rub / ref_median * 100, 1)
            if diff_pct > 65.0:
                continue  # почти всегда другой товар или комплектация
        else:
            diff_rub, diff_pct = 0, 0.0

        pid = int(p["id"])
        rating = float(p.get("reviewRating") or p.get("rating") or 0.0)
        feedbacks = int(p.get("feedbacks") or 0)
        supplier = str(p.get("supplier") or p.get("brand") or "Продавец WB")
        items.append({
            "id": f"wb_{pid}",
            "title": title[:120],
            "source": "Wildberries",
            "city": city,
            "timeAgo": "В наличии · Розница",
            "price": price_rub,
            "market": ref_median,
            "profitRub": diff_rub,
            "profitPct": diff_pct,
            "tags": [f"Арт. {pid}", f"★ {rating} ({feedbacks})", f"Выборка: {len(cluster_prices)} шт."],
            "seller": {"name": supplier[:50], "rating": rating, "reviews": feedbacks},
            "desc": (
                f"Розничное предложение Wildberries (артикул {pid}). Отклонение посчитано от розничной медианы "
                f"однородной группы товаров: {len(cluster_prices)} поз., медиана {ref_median:,} ₽, регион {city}. "
                "Это не гарантия выгоды для перепродажи."
            ).replace(",", " "),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
        })

    items.sort(key=lambda x: x["profitPct"], reverse=True)
    result = items[:20]
    cache_set_wb(cache_key, result, True, WB_CACHE_TTL_OK if result else WB_CACHE_TTL_EMPTY)
    return result, True


async def fetch_db_external_lots(source: str, city: str, search_query: str) -> Tuple[bool, List[Dict[str, Any]]]:
    if not pg_pool:
        return False, []
    rows = await pg_pool.fetch(
        """
        SELECT * FROM dh_lots
        WHERE last_seen > NOW() - ($1::int * INTERVAL '1 hour')
          AND ($2 = 'all' OR source = $2)
        ORDER BY last_seen DESC LIMIT 200
        """,
        LOT_TTL_HOURS, source,
    )
    q_words = [w.lower() for w in search_query.split() if len(w) >= 2]
    results = []
    for r in rows:
        if city and city not in r["city"] and r["city"] != "Вся РФ":
            continue
        if q_words and not all(w in (r["title"] + " " + (r["description"] or "")).lower() for w in q_words):
            continue
        tags = r["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        results.append({
            "id": r["id"], "title": r["title"], "source": r["source"], "city": r["city"],
            "timeAgo": humanize_time_ago(r["first_seen"]),
            "price": r["price"], "market": r["market"],
            "profitRub": r["profit_rub"], "profitPct": r["profit_pct"],
            "tags": list(tags or []),
            "seller": {"name": r["seller_name"], "rating": r["seller_rating"], "reviews": r["seller_reviews"]},
            "desc": r["description"] or "", "url": r["url"],
        })
    return bool(rows), results


async def build_honest_feed(category: str, source: str, search_query: str, city: str) -> Tuple[bool, List[Dict[str, Any]], bool]:
    """Возвращает (есть_внешние_лоты, лоты, wb_недоступен)."""
    city = city if city in ALLOWED_CITIES else "Москва"
    source = source if source in ALLOWED_SOURCES else "Wildberries"
    q = search_query.strip()
    target = q if q else DEFAULT_QUERIES_BY_CATEGORY.get(category, DEFAULT_QUERIES_BY_CATEGORY["all"])

    has_ext, combined = await fetch_db_external_lots(source, city, q)
    unavailable = False
    if source in ("all", "Wildberries"):
        wb_items, available = await fetch_wb_clustered(target, city)
        unavailable = not available
        combined = combined + wb_items
    return has_ext, combined, unavailable


# =====================================================================
# 7. СКАНЕР МОНИТОРОВ
# =====================================================================
async def deactivate_user_monitors(uid: int):
    if pg_pool:
        await pg_pool.execute("UPDATE dh_monitors SET active = FALSE WHERE user_id = $1", uid)
    logger.info(f"Мониторы пользователя {uid} отключены (бот заблокирован или чат недоступен)")


async def monitor_scanner_loop():
    await asyncio.sleep(12)
    while True:
        try:
            if not bot or not pg_pool:
                await asyncio.sleep(30)
                continue
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE dh_monitors SET found_today = 0, found_date = CURRENT_DATE WHERE found_date < CURRENT_DATE")
                await conn.execute("DELETE FROM dh_monitor_hits WHERE notified_at < NOW() - INTERVAL '7 days'")
                await conn.execute("DELETE FROM dh_lots WHERE last_seen < NOW() - ($1::int * INTERVAL '1 hour')", LOT_TTL_HOURS)
                rows = await conn.fetch(
                    """
                    SELECT m.* FROM dh_monitors m JOIN dh_users u ON u.user_id = m.user_id
                    WHERE m.active = TRUE AND u.sub_until IS NOT NULL AND u.sub_until > NOW()
                    """
                )
            if not rows:
                await asyncio.sleep(REFRESH_SEC)
                continue

            grouped: Dict[Tuple[str, str, str], List[asyncpg.Record]] = {}
            for m in rows:
                grouped.setdefault((m["title"].strip().lower(), m["city"].strip(), m["source"].strip()), []).append(m)

            blocked: set = set()
            for (q_low, city, source), mon_list in grouped.items():
                _, feed, unavailable = await build_honest_feed("all", source, q_low, city)
                if unavailable or not feed:
                    await asyncio.sleep(1.5)
                    continue
                for m in mon_list:
                    if m["user_id"] in blocked:
                        continue
                    eligible = [lot for lot in feed if float(lot.get("profitPct", 0)) >= int(m["min_margin"])]
                    sent = 0
                    for lot in eligible:
                        if sent >= 3:
                            break
                        lot_key = f"{lot['id']}_{lot['price']}"
                        inserted = await pg_pool.fetchval(
                            "INSERT INTO dh_monitor_hits (monitor_id, lot_key) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING lot_key",
                            m["id"], lot_key,
                        )
                        if not inserted:
                            continue
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text=f"Открыть карточку на {lot['source']}", url=lot["url"])],
                            [InlineKeyboardButton(text="Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
                        ])
                        text = (
                            f"🎯 <b>Совпадение по монитору «{htmllib.escape(m['title'])}»</b>\n\n"
                            f"📦 <b>{htmllib.escape(lot['title'])}</b>\n"
                            f"📍 {htmllib.escape(lot['city'])} ({lot['source']})\n"
                            f"💰 Цена: <b>{lot['price']:,} ₽</b> (медиана группы: {lot['market']:,} ₽)\n"
                            f"📊 Отклонение от медианы: <b>−{lot['profitPct']}% (−{lot['profitRub']:,} ₽)</b>"
                        ).replace(",", " ")
                        try:
                            await bot.send_message(m["user_id"], text, parse_mode="HTML", reply_markup=kb)
                            sent += 1
                            await pg_pool.execute(
                                "UPDATE dh_monitors SET found_today = found_today + 1, last_notified_lot = $2 WHERE id = $1",
                                m["id"], lot_key,
                            )
                        except TelegramForbiddenError:
                            await deactivate_user_monitors(m["user_id"])
                            blocked.add(m["user_id"])
                            break
                        except TelegramRetryAfter as e:
                            await pg_pool.execute("DELETE FROM dh_monitor_hits WHERE monitor_id = $1 AND lot_key = $2", m["id"], lot_key)
                            await asyncio.sleep(min(float(e.retry_after), 30.0))
                            break
                        except TelegramBadRequest as e:
                            if "chat not found" in str(e).lower() or "user not found" in str(e).lower():
                                await deactivate_user_monitors(m["user_id"])
                                blocked.add(m["user_id"])
                                break
                            logger.warning(f"BadRequest при отправке монитору {m['id']}: {e}")
                        except Exception as e:
                            await pg_pool.execute("DELETE FROM dh_monitor_hits WHERE monitor_id = $1 AND lot_key = $2", m["id"], lot_key)
                            logger.warning(f"Ошибка отправки монитору {m['id']}: {e}")
                        await asyncio.sleep(0.05)
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.exception(f"Ошибка цикла сканера: {e}")
        await asyncio.sleep(REFRESH_SEC)


# =====================================================================
# 8. TELEGRAM BOT
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: Optional[CommandObject] = None):
    uid = message.from_user.id
    fname = (message.from_user.first_name or "Пользователь")[:64]
    uname = (message.from_user.username or "")[:64]
    ref = None
    args = (command.args if command else None) or ""
    if args.startswith("ref_") and args[4:].strip().isdigit() and int(args[4:]) != uid:
        ref = int(args[4:])
    if pg_pool:
        try:
            await upsert_user_atomic(uid, fname, uname, candidate_ref_id=ref)
        except Exception as e:
            logger.warning(f"/start: не удалось создать профиль {uid}: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="Пополнить Stars (BunnyStars)", url=BUNNY_STARS_URL)],
    ])
    sponsor_line = f"\n• Для активации пробного периода нужна подписка на {SPONSOR_CHANNEL}." if SPONSOR_CHANNEL else ""
    await message.answer(
        f"Здравствуйте, <b>{htmllib.escape(fname)}</b>.\n\n"
        "<b>DealHunter</b> — сравнение розничных цен и фоновый мониторинг карточек товаров.\n\n"
        f"• Новым пользователям: <b>{TRIAL_DAYS} дня PRO бесплатно</b> при активации в приложении.{sponsor_line}\n"
        f"• За друга, активировавшего доступ: <b>+{REFERRAL_BONUS_DAYS} дня PRO</b> (до {MAX_REF_DAYS_PER_MONTH} дней в месяц).\n"
        "• Уведомления в Telegram приходят только при активной подписке.\n\n"
        "/paysupport — вопросы по оплате, /terms — условия, /deletedata — удаление данных.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: types.Message):
    contact = ADMIN_CONTACT or "администратору сервиса"
    await message.answer(
        "<b>Поддержка по платежам</b>\n\n"
        f"По вопросам оплаты и возврата Stars напишите {contact} и укажите ID транзакции "
        "(он приходит в сообщении после оплаты). Возврат оформляется вручную, подписка при этом снимается.",
        parse_mode="HTML",
    )


@dp.message(Command("terms"))
async def cmd_terms(message: types.Message):
    await message.answer(
        "<b>Условия использования</b>\n\n"
        "DealHunter — справочный инструмент сравнения открытых розничных цен. «Отклонение от медианы» рассчитывается "
        "по выборке однотипных товаров и не является гарантией выгоды или рекомендацией к покупке.\n"
        f"PRO: {STARS_PRICE} Stars за 30 дней. Вопросы по платежам: /paysupport.",
        parse_mode="HTML",
    )


@dp.message(Command("deletedata"))
async def cmd_deletedata(message: types.Message, command: Optional[CommandObject] = None):
    uid = message.from_user.id
    confirm_arg = ((command.args if command else "") or "").strip().lower()
    if confirm_arg != "confirm":
        await message.answer(
            "Будут удалены ваш профиль, мониторы и закладки. <b>Оставшиеся дни подписки сгорят.</b> "
            "Платежи сохраняются без привязки к вам, а факт использования пробного периода — "
            "в виде числового идентификатора (защита от повторных триалов).\n\n"
            "Для подтверждения отправьте: <code>/deletedata confirm</code>",
            parse_mode="HTML",
        )
        return
    if not pg_pool:
        await message.answer("Сервис временно недоступен, попробуйте позже.")
        return
    try:
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                trial = await conn.fetchval("SELECT trial_claimed FROM dh_users WHERE user_id = $1", uid)
                if trial:
                    await conn.execute("INSERT INTO dh_trial_used (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
                await conn.execute("DELETE FROM dh_monitor_hits WHERE monitor_id IN (SELECT id FROM dh_monitors WHERE user_id = $1)", uid)
                await conn.execute("DELETE FROM dh_monitors WHERE user_id = $1", uid)
                await conn.execute("DELETE FROM dh_saved WHERE user_id = $1", uid)
                await conn.execute("UPDATE dh_payments SET user_id = 0 WHERE user_id = $1", uid)
                await conn.execute("UPDATE dh_users SET referred_by = NULL WHERE referred_by = $1", uid)
                await conn.execute("DELETE FROM dh_users WHERE user_id = $1", uid)
        await message.answer("Ваши данные удалены.")
    except Exception as e:
        logger.exception(f"Ошибка удаления данных {uid}: {e}")
        await message.answer("Не удалось удалить данные. Попробуйте позже или напишите в поддержку.")


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    parts = (query.invoice_payload or "").split("_")
    valid = (
        len(parts) == 4 and parts[0] == "pro" and parts[1] == "30d"
        and parts[2] == str(query.from_user.id)
        and query.currency == "XTR" and query.total_amount == STARS_PRICE
    )
    if not valid:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Некорректный счёт. Создайте новый в приложении.")
        return
    if not pg_pool:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Сервис обновляется. Попробуйте через минуту.")
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    uid = message.from_user.id
    charge_id = sp.telegram_payment_charge_id

    # 1) Запись в БД (до 3 попыток). Уведомление отправляется ОТДЕЛЬНО и на возврат не влияет.
    granted, is_new = False, False
    for attempt in range(3):
        try:
            is_new = await grant_payment(uid, message.from_user.first_name or "Пользователь", charge_id,
                                         sp.total_amount, sp.invoice_payload, 30)
            granted = True
            break
        except Exception as e:
            logger.error(f"Попытка {attempt + 1}/3 зачисления платежа {charge_id} не удалась: {e}")
            await asyncio.sleep(1.5)

    if not granted:
        logger.critical(f"СБОЙ ЗАЧИСЛЕНИЯ {charge_id} для {uid}. Выполняю возврат Stars.")
        try:
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
            text = ("⚠️ Технический сбой при зачислении подписки. Ваши Stars <b>автоматически возвращены</b>.\n"
                    f"ID транзакции: <code>{charge_id}</code>")
        except Exception as e:
            logger.critical(f"Не удалось выполнить автовозврат {charge_id}: {e}")
            text = ("⚠️ Платёж получен, но активация задержалась. Сохраните ID транзакции "
                    f"<code>{charge_id}</code> и напишите в поддержку (/paysupport).")
        try:
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение о сбое {uid}: {e}")
        return

    try:
        if is_new:
            await message.answer(
                "✅ <b>Оплата подтверждена.</b> Подписка PRO продлена на 30 дней.\n"
                f"ID транзакции: <code>{charge_id}</code>",
                parse_mode="HTML",
            )
        else:
            await message.answer("ℹ️ Этот платёж уже был зачислен ранее.")
    except Exception as e:
        logger.warning(f"Подписка начислена, но уведомление не отправлено ({uid}): {e}")


@dp.message(Command("refund"))
async def cmd_admin_refund(message: types.Message, command: Optional[CommandObject] = None):
    """Только для администратора: /refund USER_ID CHARGE_ID. Возвращает Stars и снимает начисленные дни."""
    if not ADMIN_UID or message.from_user.id != ADMIN_UID or not pg_pool:
        return
    args = (command.args or "").split() if command else []
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Использование: <code>/refund USER_ID CHARGE_ID</code>", parse_mode="HTML")
        return
    target_uid, charge_id = int(args[0]), args[1].strip()
    pay = await pg_pool.fetchrow("SELECT * FROM dh_payments WHERE charge_id = $1", charge_id)
    if not pay:
        await message.answer("Платёж не найден в базе.")
        return
    if pay["refunded"]:
        await message.answer("Этот платёж уже возвращён.")
        return
    if pay["user_id"] not in (target_uid, 0):
        await message.answer("USER_ID не совпадает с владельцем платежа.")
        return
    try:
        await bot.refund_star_payment(user_id=target_uid, telegram_payment_charge_id=charge_id)
    except Exception as e:
        await message.answer(f"Ошибка возврата: {e}")
        return
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE dh_payments SET refunded = TRUE WHERE charge_id = $1", charge_id)
            await conn.execute(
                "UPDATE dh_users SET sub_until = GREATEST(NOW(), sub_until - ($2::int * INTERVAL '1 day')) WHERE user_id = $1 AND sub_until IS NOT NULL",
                target_uid, pay["days_granted"],
            )
    await message.answer(f"✅ Возврат по <code>{charge_id}</code> выполнен, {pay['days_granted']} дн. подписки сняты.", parse_mode="HTML")


async def single_leader_background_manager():
    """Advisory lock: бота и сканер запускает только один процесс."""
    global BOT_USERNAME_CACHE, background_leader_conn
    await asyncio.sleep(2)
    while True:
        try:
            if not pg_pool:
                await asyncio.sleep(5)
                continue
            if background_leader_conn is None or background_leader_conn.is_closed():
                background_leader_conn = await pg_pool.acquire()
            if await background_leader_conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID):
                logger.info("Лидер получен: запуск бота и сканера.")
                scanner_task = asyncio.create_task(monitor_scanner_loop())
                try:
                    if bot:
                        me = await bot.get_me()
                        if me and me.username:
                            BOT_USERNAME_CACHE = me.username
                        await bot.delete_webhook(drop_pending_updates=False)
                        await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
                    else:
                        await scanner_task
                finally:
                    scanner_task.cancel()
            else:
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Перезапуск координатора через 5 с: {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global background_task
    await init_db()
    background_task = asyncio.create_task(single_leader_background_manager())
    yield
    if background_task:
        background_task.cancel()
    if bot:
        try:
            await bot.session.close()
        except Exception:
            pass
    if pg_pool:
        await pg_pool.close()


app = FastAPI(title="DealHunter v7", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_URL, "https://web.telegram.org", "https://t.me"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Tg-Init-Data-B64", "X-Worker-Secret"],
)


# =====================================================================
# 9. REST API
# =====================================================================
def monitor_to_json(r: asyncpg.Record) -> Dict[str, Any]:
    return {
        "id": r["id"], "title": r["title"], "city": r["city"], "source": r["source"],
        "minMargin": r["min_margin"], "active": bool(r["active"]), "foundToday": r["found_today"],
    }


async def list_monitors(uid: int) -> List[Dict[str, Any]]:
    rows = await require_db().fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", uid)
    return [monitor_to_json(r) for r in rows]


async def list_saved(uid: int) -> List[Dict[str, Any]]:
    rows = await require_db().fetch("SELECT snapshot FROM dh_saved WHERE user_id = $1 ORDER BY created_at DESC", uid)
    out = []
    for r in rows:
        snap = r["snapshot"]
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception:
                continue
        out.append(snap)
    return out


@app.get("/health")
async def health():
    return {"status": "ok", "bot": BOT_USERNAME_CACHE, "db_connected": bool(pg_pool)}


@app.head("/")
async def head_root():
    return HTMLResponse("")


@app.get("/api/state")
async def api_state(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    user = await upsert_user_atomic(auth["uid"], auth["first_name"], auth["username"], auth["ref_id"])
    await require_db().execute(
        "UPDATE dh_monitors SET found_today = 0, found_date = CURRENT_DATE WHERE user_id = $1 AND found_date < CURRENT_DATE",
        auth["uid"],
    )
    return JSONResponse({
        "user": user,
        "monitors": await list_monitors(auth["uid"]),
        "saved": await list_saved(auth["uid"]),
        "bunnyUrl": BUNNY_STARS_URL,
        "refreshSec": REFRESH_SEC,
    })


@app.post("/api/claim-trial")
async def api_claim_trial(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    await upsert_user_atomic(auth["uid"], auth["first_name"], auth["username"], auth["ref_id"])
    return JSONResponse({"status": "ok", "user": await claim_trial_and_reward_referrer(auth["uid"])})


@app.get("/api/live-feed")
async def api_live_feed(
    category: str = Query("all", max_length=20),
    source: str = Query("Wildberries", max_length=20),
    q: str = Query("", max_length=80),
    city: str = Query("Москва", max_length=40),
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    user = await get_existing_user(auth["uid"])
    if not user["subActive"]:
        return JSONResponse({"locked": True, "items": [], "extConnected": False, "sourceUnavailable": False})
    category = category if category in DEFAULT_QUERIES_BY_CATEGORY else "all"
    source = source if source in ALLOWED_SOURCES else "Wildberries"
    city = city if city in ALLOWED_CITIES else "Москва"
    has_ext, items, unavailable = await build_honest_feed(category, source, q, city)
    return JSONResponse({
        "locked": False, "items": items, "extConnected": has_ext,
        "sourceUnavailable": unavailable, "updatedAt": now_utc().strftime("%H:%M:%S"),
    })


class MonitorCreateReq(BaseModel):
    title: str = Field(..., min_length=2, max_length=80)
    city: Literal["Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск", "Краснодар"] = "Москва"
    source: Literal["Wildberries", "Ozon", "Avito", "all"] = "Wildberries"
    minMargin: int = Field(default=15, ge=5, le=65)


@app.post("/api/monitors")
async def api_create_monitor(data: MonitorCreateReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    uid = auth["uid"]
    user = await get_existing_user(uid)
    if not user["subActive"]:
        raise HTTPException(status_code=403, detail="Требуется активная подписка PRO")
    pool = require_db()
    if await pool.fetchval("SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid) >= MAX_MONITORS:
        raise HTTPException(status_code=400, detail=f"Лимит: не более {MAX_MONITORS} мониторов")
    mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
    await pool.execute(
        "INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, found_date) "
        "VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0, CURRENT_DATE)",
        mid, uid, data.title.strip(), data.city, data.source, data.minMargin,
    )
    return JSONResponse({"status": "ok", "monitors": await list_monitors(uid)})


@app.post("/api/monitors/{mid}/toggle")
async def api_toggle_monitor(mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    await require_db().execute("UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND user_id = $2", mid, auth["uid"])
    return JSONResponse({"status": "ok", "monitors": await list_monitors(auth["uid"])})


@app.delete("/api/monitors/{mid}")
async def api_delete_monitor(mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchval("DELETE FROM dh_monitors WHERE id = $1 AND user_id = $2 RETURNING id", mid, auth["uid"])
            if deleted:
                await conn.execute("DELETE FROM dh_monitor_hits WHERE monitor_id = $1", mid)
    return JSONResponse({"status": "ok", "monitors": await list_monitors(auth["uid"])})


class SavedLot(BaseModel):
    id: str = Field(..., max_length=60)
    title: str = Field(..., max_length=140)
    source: Literal["Avito", "Ozon", "Wildberries"]
    city: str = Field(default="", max_length=50)
    price: int = Field(..., gt=0)
    market: int = Field(..., gt=0)
    profitRub: int = 0
    profitPct: float = 0.0
    sellerName: str = Field(default="Продавец", max_length=60)
    sellerRating: float = 0.0
    sellerReviews: int = 0
    url: str = Field(..., max_length=400)


class SaveReq(BaseModel):
    lot: SavedLot


@app.post("/api/saved")
async def api_save_lot(data: SaveReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    uid, lot = auth["uid"], data.lot
    pool = require_db()
    try:
        safe_url = validate_and_clean_url(lot.url, lot.source)
    except ValueError:
        raise HTTPException(status_code=400, detail="Недопустимая ссылка на карточку")
    lot_id = re.sub(r"[^a-zA-Z0-9_\-]", "", lot.id)[:60]
    if not lot_id:
        raise HTTPException(status_code=400, detail="Некорректный идентификатор лота")
    if await pool.fetchval("SELECT COUNT(*) FROM dh_saved WHERE user_id = $1", uid) >= MAX_SAVED:
        raise HTTPException(status_code=400, detail=f"Лимит закладок: {MAX_SAVED}")
    snapshot = {
        "id": lot_id, "title": lot.title, "source": lot.source, "city": lot.city,
        "timeAgo": "Сохранено " + now_utc().strftime("%d.%m %H:%M"),
        "price": lot.price, "market": lot.market,
        "profitRub": max(0, lot.profitRub), "profitPct": max(0.0, lot.profitPct),
        "tags": ["Цена на момент сохранения"],
        "seller": {"name": lot.sellerName, "rating": lot.sellerRating, "reviews": lot.sellerReviews},
        "desc": "Снимок карточки на момент сохранения. Актуальную цену и наличие смотрите на странице товара.",
        "url": safe_url,
    }
    await pool.execute(
        "INSERT INTO dh_saved (user_id, lot_id, snapshot) VALUES ($1, $2, $3::jsonb) "
        "ON CONFLICT (user_id, lot_id) DO NOTHING",
        uid, lot_id, json.dumps(snapshot, ensure_ascii=False),
    )
    return JSONResponse({"status": "ok", "saved": await list_saved(uid)})


@app.delete("/api/saved/{lot_id}")
async def api_unsave_lot(lot_id: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    await require_db().execute("DELETE FROM dh_saved WHERE user_id = $1 AND lot_id = $2", auth["uid"], lot_id[:60])
    return JSONResponse({"status": "ok", "saved": await list_saved(auth["uid"])})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не подключён")
    try:
        link = await bot.create_invoice_link(
            title="DealHunter PRO — 30 дней",
            description="Поиск карточек, сравнение с медианой модели и фоновые уведомления.",
            payload=f"pro_30d_{auth['uid']}_{int(now_utc().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
        )
        return JSONResponse({"invoiceUrl": link})
    except Exception as e:
        logger.error(f"Ошибка создания инвойса: {e}")
        return JSONResponse({"error": "Не удалось создать счёт Telegram Stars"}, status_code=500)


class ExternalLotItem(BaseModel):
    id: str = Field(..., max_length=60)
    title: str = Field(..., max_length=140)
    source: Literal["Avito", "Ozon", "Wildberries"]
    city: str = Field(default="Москва", max_length=50)
    price: int = Field(..., gt=0)
    market: int = Field(..., gt=0)
    tags: List[str] = Field(default_factory=list)
    sellerName: str = Field(default="Продавец", max_length=60)
    sellerRating: float = Field(default=5.0)
    sellerReviews: int = Field(default=0)
    desc: str = Field(default="", max_length=600)
    url: str = Field(..., max_length=400)


@app.post("/api/push-lots")
async def api_push_external_lots(lots: List[ExternalLotItem], x_worker_secret: Optional[str] = Header(None)):
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET не настроен")
    if not x_worker_secret or not hmac.compare_digest(x_worker_secret, WORKER_SECRET):
        raise HTTPException(status_code=401, detail="Неверный ключ авторизации")

    pool = require_db()
    saved = 0
    async with pool.acquire() as conn:
        for item in lots[:50]:
            try:
                safe_url = validate_and_clean_url(item.url, item.source)
            except ValueError as err:
                logger.warning(f"Отклонён лот {item.id}: {err}")
                continue
            lot_id = re.sub(r"[^a-zA-Z0-9_\-]", "", item.id)
            if not lot_id:
                continue
            # Выгоду всегда считаем на сервере, значениям от воркера не доверяем
            profit_rub = max(0, item.market - item.price)
            profit_pct = round(profit_rub / item.market * 100, 1)
            tags = json.dumps([str(t)[:30] for t in item.tags[:4]], ensure_ascii=False)
            await conn.execute(
                """
                INSERT INTO dh_lots (id, title, source, city, price, market, profit_rub, profit_pct, tags,
                                     seller_name, seller_rating, seller_reviews, description, url, first_seen, last_seen)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    price = EXCLUDED.price, market = EXCLUDED.market,
                    profit_rub = EXCLUDED.profit_rub, profit_pct = EXCLUDED.profit_pct,
                    last_seen = NOW()
                """,
                lot_id, item.title, item.source, item.city, item.price, item.market, profit_rub, profit_pct, tags,
                item.sellerName, item.sellerRating, item.sellerReviews, item.desc, safe_url,
            )
            saved += 1
    return JSONResponse({"status": "ok", "saved": saved})


# =====================================================================
# 10. ИНТЕРФЕЙС (index.html рядом с main.py)
# =====================================================================
_INDEX_PATH = Path(__file__).resolve().parent / "index.html"


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    try:
        html = _INDEX_PATH.read_text(encoding="utf-8")
    except Exception:
        return HTMLResponse("index.html не найден рядом с main.py", status_code=500)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
