import os
import re
import hmac
import json
import time
import base64
import hashlib
import logging
import asyncio
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple, Literal
from datetime import datetime, timedelta, timezone, date
from urllib.parse import quote, unquote, parse_qsl, urlparse

import httpx
import asyncpg
from fastapi import FastAPI, Request, Header, HTTPException, Depends
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
# 1. КОНФИГУРАЦИЯ И СТРОГИЕ СПИСКИ РАЗРЕШЕННЫХ ЗНАЧЕНИЙ
# =====================================================================
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
SPONSOR_CHANNEL = os.getenv("SPONSOR_CHANNEL", "").strip()  # Напр. "@my_channel"
ADMIN_UID = int(os.getenv("ADMIN_UID", "0")) if os.getenv("ADMIN_UID", "").isdigit() else 0
PORT = int(os.getenv("PORT", 10000))

BOT_USERNAME_DEFAULT = "DealHunterAvito_bot"
BOT_USERNAME_CACHE = os.getenv("BOT_USERNAME", BOT_USERNAME_DEFAULT).strip().lstrip("@") or BOT_USERNAME_DEFAULT

STARS_PRICE = 350
TRIAL_DAYS = 3
REFERRAL_BONUS_DAYS = 3
MAX_REF_DAYS_PER_MONTH = 30
LOT_TTL_HOURS = 6
ADVISORY_LOCK_ID = 94827165

BUNNY_STARS_URL = "https://t.me/BunnyStarsBot?startapp=ref_5123484212"

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
ALLOWED_HOSTS = {
    "www.wildberries.ru",
    "wildberries.ru",
    "www.ozon.ru",
    "ozon.ru",
    "www.avito.ru",
    "avito.ru",
}

# Стоп-слова для жесткого отсечения аксессуаров, чехлов и запчастей
ACCESSORY_STOP_WORDS = (
    "чехол", "стекло", "пленка", "плёнка", "кабель", "провод", "переходник",
    "адаптер", "зарядка", "зарядное", "держатель", "ремешок", "наклейка",
    "брелок", "подставка", "салфетка", "стилус", "коробка от", "муляж",
    "запчасть", "шлейф", "дисплей для", "экран для", "аккумулятор для",
    "батарея для", "корпус для", "крышка для", "линза", "защита камеры",
    "амбушюры", "футляр", "кейс для", "шнурок", "кулер для", "сумка для",
)

app = FastAPI(title="DealHunter v6.0 Production")

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

# =====================================================================
# 2. RATE LIMITER И ОГРАНИЧЕННЫЙ LRU-КЭШ (ЗАЩИТА ПАМЯТИ)
# =====================================================================
USER_RATE_BUCKETS: OrderedDict[int, List[float]] = OrderedDict()
WB_LRU_CACHE: OrderedDict[str, Tuple[float, List[Dict[str, Any]]]] = OrderedDict()
WB_CACHE_MAX_SIZE = 200
WB_CACHE_TTL = 60.0


def check_rate_limit(uid: int, max_requests: int = 25, window_sec: float = 10.0):
    now = time.time()
    timestamps = USER_RATE_BUCKETS.get(uid, [])
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= max_requests:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подождите несколько секунд.")
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
# 3. СТРОГАЯ ПРОВЕРКА INIT DATA (ОБЯЗАТЕЛЬНЫЙ AUTH_DATE + HMAC)
# =====================================================================
def verify_telegram_init_data(init_data_raw: str) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Сервер не настроен (отсутствует BOT_TOKEN)")
    if not init_data_raw:
        raise HTTPException(status_code=401, detail="Откройте приложение внутри Telegram")

    try:
        data_dict = dict(parse_qsl(init_data_raw, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный формат данных авторизации")

    received_hash = data_dict.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Отсутствует криптографическая подпись")

    # Строгая обязательная проверка auth_date
    auth_date_raw = data_dict.get("auth_date", "")
    if not auth_date_raw or not auth_date_raw.isdigit():
        raise HTTPException(status_code=401, detail="Отсутствует метка времени сессии (auth_date)")
    auth_date = int(auth_date_raw)
    now_ts = int(time.time())
    if auth_date <= 0 or (now_ts - auth_date) > 86400 or (auth_date - now_ts) > 300:
        raise HTTPException(status_code=401, detail="Сессия устарела. Перезапустите мини-приложение из бота.")

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
        raise HTTPException(status_code=401, detail="Профиль пользователя отсутствует в подписи")

    user_obj = json.loads(user_raw)
    uid = int(user_obj["id"])
    start_param = data_dict.get("start_param", "")
    ref_id = None
    if start_param.startswith("ref_"):
        r_clean = start_param.replace("ref_", "").strip()
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
        decoded_bytes = base64.b64decode(b64_header)
        init_data_raw = unquote(decoded_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный заголовок авторизации")

    auth = verify_telegram_init_data(init_data_raw)
    check_rate_limit(auth["uid"])
    return auth


# =====================================================================
# 4. БЕЗОПАСНАЯ РАБОТА С URL И ФОРМАТИРОВАНИЕМ ВРЕМЕНИ
# =====================================================================
def validate_and_clean_url(raw_url: str) -> str:
    """Строгий разбор URL через urlparse: никаких кавычек, пробелов или чужих доменов."""
    u = str(raw_url or "").strip()
    if not u or any(ch in u for ch in ("'", '"', "<", ">", " ", "\n", "\r", "\t", "javascript:")):
        raise ValueError("URL содержит недопустимые символы")
    parsed = urlparse(u)
    if parsed.scheme != "https":
        raise ValueError("Разрешён только протокол https://")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Домен {host} не входит в список разрешённых площадок")
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
# 5. БАЗА ДАННЫХ POSTGRESQL (СХЕМА, ЛОТЫ С TTL, ДЕДУП МОНИТОРОВ)
# =====================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_db() -> asyncpg.Pool:
    if not pg_pool:
        raise HTTPException(status_code=503, detail="СУБД PostgreSQL не подключена (проверьте DATABASE_URL)")
    return pg_pool


async def init_db():
    global pg_pool
    if not DATABASE_URL.startswith("postgres"):
        logger.error("КРИТИЧНО: Задайте корректный DATABASE_URL (PostgreSQL)!")
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
            # Безопасные миграции для старой схемы
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN DEFAULT FALSE;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month_days INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS found_date DATE DEFAULT CURRENT_DATE;")
        logger.info("PostgreSQL схема v6.0 успешно инициализирована.")
    except Exception as e:
        logger.exception(f"Ошибка инициализации PostgreSQL: {e}")


async def check_sponsor_membership(uid: int) -> bool:
    """Если задан SPONSOR_CHANNEL, проверяет подписку пользователя на канал."""
    if not SPONSOR_CHANNEL or not bot:
        return True
    try:
        member = await bot.get_chat_member(chat_id=SPONSOR_CHANNEL, user_id=uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Не удалось проверить подписку на {SPONSOR_CHANNEL} для {uid}: {e}")
        return True


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
                    saved_ids, referred_by, ref_rewarded, ref_count, ref_days_earned
                )
                VALUES ($1, $2, $3, FALSE, NULL, '[]', $4, FALSE, 0, 0)
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

    cur_month = now.strftime("%Y-%m")
    ref_month_days = int(row["ref_month_days"] or 0) if row["ref_month"] == cur_month else 0

    return {
        "user_id": row["user_id"],
        "name": row["first_name"] or "Пользователь",
        "trialClaimed": bool(row["trial_claimed"]),
        "subActive": is_active,
        "subUntil": sub_until.isoformat() if sub_until else None,
        "secondsLeft": seconds_left,
        "savedIds": saved_ids,
        "refCount": row["ref_count"] or 0,
        "refDaysEarned": row["ref_days_earned"] or 0,
        "refMonthDays": ref_month_days,
        "refMonthLimit": MAX_REF_DAYS_PER_MONTH,
        "botUsername": BOT_USERNAME_CACHE,
        "refLink": f"https://t.me/{BOT_USERNAME_CACHE}?start=ref_{row['user_id']}",
    }


async def claim_trial_and_reward_referrer(uid: int) -> Dict[str, Any]:
    """
    Активирует 3 бесплатных дня PRO.
    Проверяет подписку на спонсора (если задан SPONSOR_CHANNEL) и месячный лимит реферера (макс. 30 дней/мес).
    """
    if SPONSOR_CHANNEL:
        is_member = await check_sponsor_membership(uid)
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail=f"Для активации бесплатного доступа подпишитесь на канал {SPONSOR_CHANNEL}",
            )

    pool = require_db()
    now = now_utc()
    cur_month = now.strftime("%Y-%m")
    referrer_to_notify: Optional[int] = None
    days_awarded_to_ref = 0
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
            base_time = cur_end if (cur_end and cur_end > now) else now
            new_sub_until = base_time + timedelta(days=TRIAL_DAYS)

            referred_by = row["referred_by"]
            ref_rewarded = bool(row["ref_rewarded"])

            updated_row = await conn.fetchrow(
                """
                UPDATE dh_users
                SET trial_claimed = TRUE,
                    sub_until = $2,
                    ref_rewarded = TRUE
                WHERE user_id = $1
                RETURNING *
                """,
                uid, new_sub_until,
            )

            if referred_by and not ref_rewarded and referred_by != uid:
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
                    else:
                        await conn.execute(
                            "UPDATE dh_users SET ref_count = COALESCE(ref_count, 0) + 1 WHERE user_id = $1",
                            referred_by,
                        )

    if referrer_to_notify and bot and days_awarded_to_ref > 0:
        try:
            await bot.send_message(
                referrer_to_notify,
                f"🎉 <b>Реферал подтверждён: {user_name}</b>\n\n"
                f"Друг активировал доступ в приложении.\n"
                f"🎁 Вам начислено <b>+{days_awarded_to_ref} дн. подписки DealHunter PRO</b>!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Ошибка уведомления реферера {referrer_to_notify}: {e}")

    return format_user_row(updated_row)


# =====================================================================
# 6. КЛАСТЕРИЗАЦИЯ МОДЕЛЕЙ И ФИЛЬТРАЦИЯ МУСОРА (ЧЕСТНАЯ МЕДИАНА)
# =====================================================================
DEFAULT_QUERIES_BY_CATEGORY = {
    "all": "смартфон Xiaomi Redmi Note",
    "xiaomi": "смартфон Xiaomi POCO",
    "iphone": "смартфон Apple iPhone 15",
    "macbook": "ноутбук Apple MacBook Air",
    "gpu": "видеокарта GeForce RTX 4060",
    "ps5": "игровая консоль PlayStation 5 Slim",
}


def is_accessory_or_junk(title: str, query_text: str) -> bool:
    """Отсекает чехлы, защитные стекла и аксессуары, если пользователь сам их не ищет."""
    t_low = title.lower()
    q_low = query_text.lower()
    for stop_w in ACCESSORY_STOP_WORDS:
        if stop_w in t_low and stop_w not in q_low:
            return True
    return False


def extract_model_cluster_key(title: str) -> str:
    """
    Выделяет сигнатуру объема памяти (напр. 128gb, 256gb, 512gb, 1tb) и модификации (pro, max, plus, ultra),
    чтобы сравнивать цены только однотипных комплектаций между собой.
    """
    t_low = title.lower().replace("гб", "gb").replace("тб", "tb")
    mem_match = re.search(r"\b(64|128|256|512|1024)\s*gb\b|\b(1|2)\s*tb\b", t_low)
    mem_key = mem_match.group(0).replace(" ", "") if mem_match else "std"

    mods = []
    for mod in ("pro", "max", "plus", "ultra", "slim", "ti", "super", "mini", "fe"):
        if re.search(rf"\b{mod}\b", t_low):
            mods.append(mod)
    mod_key = "_".join(mods) if mods else "base"
    return f"{mod_key}:{mem_key}"


async def fetch_wb_clustered(query_text: str, city: str = "Москва") -> List[Dict[str, Any]]:
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

    products = []
    client_kwargs: Dict[str, Any] = {"timeout": 4.5}
    if WB_PROXY_URL:
        client_kwargs["proxy"] = WB_PROXY_URL

    async with httpx.AsyncClient(**client_kwargs) as client:
        for ep in endpoints:
            try:
                res = await client.get(ep, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    products = data.get("data", {}).get("products", []) or data.get("products", [])
                    if products:
                        break
                elif res.status_code == 429:
                    logger.warning("WB API вернул 429 Too Many Requests")
            except Exception as e:
                logger.warning(f"Ошибка обращения к WB API: {e}")

    if not products:
        return []

    def get_price_and_qty(p: dict) -> Tuple[int, int]:
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

    # 1. Первичная очистка от отсутствующих товаров и явных аксессуаров/чехлов
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
        full_title = f"{brand} {name}".strip() if (brand and brand.lower() not in name.lower()) else name
        if is_accessory_or_junk(full_title, q_clean):
            continue
        candidates.append((price_rub, full_title, p))

    if len(candidates) < 4:
        return []

    # 2. Отсечение ценовых выбросов снизу (например, чехол без слова «чехол» за 500 ₽ среди телефонов за 50 000 ₽)
    all_prices_sorted = sorted(c[0] for c in candidates)
    q75_price = all_prices_sorted[int(len(all_prices_sorted) * 0.75)]
    min_ realistic_price = int(q75_price * 0.35) if q75_price > 5000 else 300
    filtered_candidates = [c for c in candidates if c[0] >= min_realistic_price]

    if len(filtered_candidates) < 4:
        filtered_candidates = candidates

    # 3. Кластеризация по модификации и объему памяти (сравниваем только одинаковые модели!)
    clusters: Dict[str, List[int]] = {}
    for price_rub, full_title, _ in filtered_candidates:
        ckey = extract_model_cluster_key(full_title)
        clusters.setdefault(ckey, []).append(price_rub)

    global_sorted = sorted(c[0] for c in filtered_candidates)
    global_median = global_sorted[len(global_sorted) // 2]

    items: List[Dict[str, Any]] = []
    for price_rub, full_title, p in filtered_candidates:
        pid = int(p["id"])
        ckey = extract_model_cluster_key(full_title)
        cluster_prices = sorted(clusters.get(ckey, []))

        # Требуем минимум 3 однотипных позиции в кластере для точной медианы кластера, иначе общая медиана очищенной выборки
        if len(cluster_prices) >= 3:
            ref_median = cluster_prices[len(cluster_prices) // 2]
            sample_size = len(cluster_prices)
        else:
            ref_median = global_median
            sample_size = len(filtered_candidates)

        if ref_median <= 0:
            continue

        # Отсекаем аномалии (>65% отклонения почти всегда означает другой товар/комплектацию)
        if price_rub < ref_median:
            diff_rub = ref_median - price_rub
            diff_pct = round((diff_rub / ref_median) * 100, 1)
            if diff_pct > 65.0:
                continue
        else:
            diff_rub = 0
            diff_pct = 0.0

        rating = float(p.get("reviewRating") or p.get("rating") or 0.0)
        feedbacks = int(p.get("feedbacks") or 0)
        supplier = str(p.get("supplier") or p.get("brand") or "Продавец WB")

        items.append({
            "id": f"wb_{pid}",
            "title": full_title[:120],
            "source": "Wildberries",
            "city": city,
            "timeAgo": "В наличии · Розница",
            "price": price_rub,
            "market": ref_median,
            "profitRub": diff_rub,
            "profitPct": diff_pct,
            "tags": [
                f"Арт. {pid}",
                f"★ {rating} ({feedbacks})",
                f"Выборка: {sample_size} шт.",
            ],
            "seller": {
                "name": supplier[:50],
                "rating": rating,
                "reviews": feedbacks,
            },
            "desc": (
                f"Розничное предложение каталога Wildberries (артикул {pid}). "
                f"Сравнение выполнено внутри однородной группы товаров (выборка: {sample_size} поз., медиана группы: {ref_median:,} ₽, регион: {city})."
            ).replace(",", " "),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
        })

    items.sort(key=lambda x: x["profitPct"], reverse=True)
    result = items[:20]
    cache_set_wb(cache_key, result)
    return result


async def fetch_db_external_lots(
    source: str = "all",
    city: str = "Москва",
    search_query: str = "",
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Читает свежие лоты из таблицы dh_lots в PostgreSQL (не старше 6 часов) и считает живой timeAgo."""
    if not pg_pool:
        return False, []
    async with pg_pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM dh_lots WHERE created_at < NOW() - INTERVAL '{LOT_TTL_HOURS} hours'"
        )
        total_ext = await conn.fetchval("SELECT COUNT(*) FROM dh_lots")
        rows = await conn.fetch(
            "SELECT * FROM dh_lots ORDER BY created_at DESC LIMIT 100"
        )

    results = []
    q_words = [w.lower() for w in search_query.split() if len(w) >= 2]
    for r in rows:
        if source != "all" and r["source"].lower() != source.lower():
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
        })

    return bool(total_ext and total_ext > 0), results


async def build_honest_feed(
    category: str = "all",
    source: str = "all",
    search_query: str = "",
    city: str = "Москва",
) -> Tuple[bool, List[Dict[str, Any]]]:
    city_clean = city if city in ALLOWED_CITIES else "Москва"
    source_clean = source if source in ALLOWED_SOURCES else "Wildberries"
    q_clean = search_query.strip()
    target_query = q_clean if q_clean else DEFAULT_QUERIES_BY_CATEGORY.get(category, "смартфон Xiaomi Redmi Note")

    has_external_lots, ext_items = await fetch_db_external_lots(
        source=source_clean,
        city=city_clean,
        search_query=q_clean,
    )

    combined = list(ext_items)
    if source_clean in ("all", "Wildberries"):
        wb_items = await fetch_wb_clustered(query_text=target_query, city=city_clean)
        combined.extend(wb_items)

    return has_external_lots, combined


# =====================================================================
# 7. СКАНЕР МОНИТОРОВ (ДЕДУП ПО ИСТОРИИ + СБРОС FOUND_TODAY + ДО 3 ЛОТОВ)
# =====================================================================
async def monitor_scanner_loop():
    await asyncio.sleep(12)
    while True:
        try:
            if not bot or not pg_pool:
                await asyncio.sleep(30)
                continue

            async with pg_pool.acquire() as conn:
                # Ежедневное обнуление счетчика found_today при смене календарных суток
                await conn.execute("""
                    UPDATE dh_monitors
                    SET found_today = 0, found_date = CURRENT_DATE
                    WHERE found_date < CURRENT_DATE
                """)
                # Очистка старой истории дедупликации мониторов (старше 7 дней)
                await conn.execute("""
                    DELETE FROM dh_monitor_hits
                    WHERE notified_at < NOW() - INTERVAL '7 days'
                """)

                rows = await conn.fetch("""
                    SELECT m.*
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
                    min_margin = int(m["min_margin"])
                    eligible_lots = [
                        lot for lot in feed
                        if float(lot.get("profitPct", 0)) >= min_margin
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
                                [InlineKeyboardButton(text=f"Открыть карточку на {lot['source']}", url=lot["url"])],
                                [InlineKeyboardButton(text="Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
                            ]
                        )
                        msg_text = (
                            f"🎯 <b>Совпадение по монитору «{m['title']}»</b>\n\n"
                            f"📦 <b>{lot['title']}</b>\n"
                            f"📍 Регион: <b>{lot['city']}</b> ({lot['source']})\n"
                            f"💰 Цена: <b>{lot['price']:,} ₽</b> (Медиана группы: {lot['market']:,} ₽)\n"
                            f"📊 Отклонение от медианы: <b>−{lot['profitPct']}% (−{lot['profitRub']:,} ₽)</b>"
                        ).replace(",", " ")

                        try:
                            await bot.send_message(m["user_id"], msg_text, parse_mode="HTML", reply_markup=kb)
                            sent_in_cycle += 1
                            async with pg_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE dh_monitors SET found_today = found_today + 1, last_notified_lot = $2 WHERE id = $1",
                                    m["id"], lot_key,
                                )
                        except Exception as e:
                            logger.warning(f"Ошибка отправки пуша монитора {m['id']}: {e}")

                await asyncio.sleep(1.5)
        except Exception as e:
            logger.exception(f"Ошибка цикла сканера мониторов: {e}")

        await asyncio.sleep(60)


# =====================================================================
# 8. TELEGRAM BOT (ПРОВЕРКА PAYLOAD, РЕТРАИ ОПЛАТЫ И АВТО-REFUND STARS)
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: Optional[CommandObject] = None):
    uid = message.from_user.id
    fname = (message.from_user.first_name or "Пользователь")[:64]
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
            [InlineKeyboardButton(text="Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton(text="Пополнить Stars (BunnyStars)", url=BUNNY_STARS_URL)],
        ]
    )
    await message.answer(
        f"Здравствуйте, <b>{fname}</b>.\n\n"
        "<b>DealHunter</b> — аналитический радар сравнения цен и фонового мониторинга карточек товаров.\n\n"
        f"• Новым пользователям доступно <b>{TRIAL_DAYS} дня PRO бесплатно</b> при активации внутри приложения.\n"
        f"• Реферальная программа: <b>+{REFERRAL_BONUS_DAYS} дня PRO</b> за каждого друга (до {MAX_REF_DAYS_PER_MONTH} дней в месяц).",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload or ""
    if not payload.startswith("pro_30d_") or query.currency != "XTR" or query.total_amount != STARS_PRICE:
        await bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Некорректные параметры счёта. Создайте новый счёт в приложении.",
        )
        return
    if not pg_pool:
        await bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Сервис временно обновляется. Попробуйте через минуту.",
        )
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    uid = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    amount = sp.total_amount
    payload = sp.invoice_payload

    # 3 попытки зачисления в БД с паузой
    last_error = None
    for attempt in range(3):
        try:
            pool = require_db()
            now = now_utc()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    inserted = await conn.fetchval(
                        """
                        INSERT INTO dh_payments (charge_id, user_id, amount_stars, days_granted, payload)
                        VALUES ($1, $2, $3, 30, $4)
                        ON CONFLICT (charge_id) DO NOTHING
                        RETURNING charge_id
                        """,
                        charge_id, uid, amount, payload,
                    )
                    if inserted:
                        await upsert_user_atomic(uid, message.from_user.first_name or "Пользователь", "")
                        row = await conn.fetchrow("SELECT sub_until FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
                        cur_end = row["sub_until"] if row else None
                        base_time = cur_end if (cur_end and cur_end > now) else now
                        new_end = base_time + timedelta(days=30)
                        await conn.execute(
                            "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                            uid, new_end,
                        )
            await message.answer(
                "✅ <b>Оплата подтверждена.</b>\n\n"
                "Подписка <b>DealHunter PRO</b> продлена на <b>30 дней</b>.\n"
                f"Чек транзакции: <code>{charge_id}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            last_error = e
            logger.error(f"Попытка {attempt + 1}/3 зачисления платежа {charge_id} не удалась: {e}")
            await asyncio.sleep(1.5)

    # Если все 3 попытки записи в БД упали — делаем автоматический возврат Stars пользователю!
    logger.critical(f"КРИТИЧЕСКИЙ СБОЙ ОПЛАТЫ {charge_id} для {uid}: {last_error}. Выполняю возврат Stars.")
    try:
        await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
        await message.answer(
            "⚠️ <b>Технический сбой базы данных при зачислении подписки.</b>\n\n"
            "Ваши Telegram Stars были <b>автоматически возвращены</b> на ваш баланс в полном объёме.\n"
            f"ID возврата: <code>{charge_id}</code>",
            parse_mode="HTML",
        )
    except Exception as ref_err:
        logger.critical(f"Не удалось выполнить авто-возврат {charge_id}: {ref_err}")
        await message.answer(
            "⚠️ <b>Платеж получен, но возникла задержка активации.</b>\n"
            f"Сохраните ваш код транзакции: <code>{charge_id}</code> и перешлите администратору.",
            parse_mode="HTML",
        )


@dp.message(Command("refund"))
async def cmd_admin_refund(message: types.Message, command: Optional[CommandObject] = None):
    """Команда возврата звезд для администратора: /refund <user_id> <charge_id>"""
    if not ADMIN_UID or message.from_user.id != ADMIN_UID:
        return
    args = (command.args or "").split() if command else []
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Использование: <code>/refund USER_ID CHARGE_ID</code>", parse_mode="HTML")
        return
    target_uid = int(args[0])
    charge_id = args[1].strip()
    try:
        await bot.refund_star_payment(user_id=target_uid, telegram_payment_charge_id=charge_id)
        if pg_pool:
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE dh_payments SET refunded = TRUE WHERE charge_id = $1", charge_id)
        await message.answer(f"✅ Возврат по чеку <code>{charge_id}</code> выполнен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка возврата: {e}")


async def single_leader_background_manager():
    """
    Использует PostgreSQL Advisory Lock, чтобы при нескольких воркерах uvicorn
    только ОДИН процесс запускал Telegram Polling и сканер мониторов!
    """
    global BOT_USERNAME_CACHE, background_leader_conn
    await asyncio.sleep(2)
    while True:
        try:
            if not pg_pool:
                await asyncio.sleep(5)
                continue
            if background_leader_conn is None or background_leader_conn.is_closed():
                background_leader_conn = await pg_pool.acquire()

            locked = await background_leader_conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID
            )
            if locked:
                logger.info("Процесс получил статус лидера (Advisory Lock). Запуск бота и сканера.")
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
        except Exception as e:
            logger.warning(f"Перезапуск координатора фоновых задач через 5 сек: {e}")
            await asyncio.sleep(5)


@app.on_event("startup")
async def on_startup():
    await init_db()
    asyncio.create_task(single_leader_background_manager())


# =====================================================================
# 9. ЗАЩИЩЕННЫЕ REST API ЭНДПОИНТЫ
# =====================================================================
@app.get("/health")
@app.head("/")
async def health():
    return {
        "status": "ok",
        "bot": BOT_USERNAME_CACHE,
        "db_connected": bool(pg_pool),
    }


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
        await conn.execute(
            "UPDATE dh_monitors SET found_today = 0, found_date = CURRENT_DATE WHERE user_id = $1 AND found_date < CURRENT_DATE",
            auth["uid"],
        )
        rows = await conn.fetch(
            "SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC",
            auth["uid"],
        )
    monitors = [
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
    ]
    return JSONResponse({"user": user, "monitors": monitors})


@app.post("/api/claim-trial")
async def api_claim_trial(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    await upsert_user_atomic(
        uid=auth["uid"],
        first_name=auth["first_name"],
        username=auth["username"],
        candidate_ref_id=auth["ref_id"],
    )
    user = await claim_trial_and_reward_referrer(auth["uid"])
    return JSONResponse({"status": "ok", "user": user})


@app.get("/api/live-feed")
async def api_live_feed(
    category: str = "all",
    source: str = "Wildberries",
    q: str = "",
    city: str = "Москва",
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    user = await upsert_user_atomic(auth["uid"], auth["first_name"], auth["username"])
    if not user["subActive"]:
        return JSONResponse({"locked": True, "items": [], "extConnected": False})

    has_ext, items = await build_honest_feed(category=category, source=source, search_query=q, city=city)
    return JSONResponse({
        "locked": False,
        "items": items,
        "extConnected": has_ext,
        "updatedAt": now_utc().strftime("%H:%M:%S"),
    })


class MonitorCreateReq(BaseModel):
    title: str = Field(..., min_length=2, max_length=80)
    city: Literal["Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск", "Краснодар"] = "Москва"
    source: Literal["Wildberries", "Ozon", "Avito", "all"] = "Wildberries"
    minMargin: int = Field(default=15, ge=5, le=65)


@app.post("/api/monitors")
async def api_create_monitor(
    data: MonitorCreateReq,
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    pool = require_db()
    uid = auth["uid"]
    user = await upsert_user_atomic(uid, auth["first_name"], auth["username"])
    if not user["subActive"]:
        raise HTTPException(status_code=403, detail="Требуется активная подписка PRO")

    mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid)
        if count >= 15:
            raise HTTPException(status_code=400, detail="Лимит: не более 15 мониторов")
        await conn.execute(
            """
            INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, found_date)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0, CURRENT_DATE)
            """,
            mid, uid, data.title.strip(), data.city, data.source, data.minMargin,
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
            }
            for r in rows
        ],
    })


@app.post("/api/monitors/{mid}/toggle")
async def api_toggle_monitor(mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    uid = auth["uid"]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND user_id = $2",
            mid, uid,
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


class SavedUpdateReq(BaseModel):
    savedIds: List[str]


@app.post("/api/saved")
async def api_save_lots(data: SavedUpdateReq, auth: Dict[str, Any] = Depends(get_authenticated_user)):
    pool = require_db()
    clean_ids = [re.sub(r"[^a-zA-Z0-9_\-]", "", str(x))[:40] for x in data.savedIds[:100]]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE dh_users SET saved_ids = $2 WHERE user_id = $1",
            auth["uid"], json.dumps(clean_ids),
        )
    return JSONResponse({"status": "ok"})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(auth: Dict[str, Any] = Depends(get_authenticated_user)):
    uid = auth["uid"]
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не подключён")
    try:
        invoice_link = await bot.create_invoice_link(
            title="DealHunter PRO — 30 дней",
            description="Доступ к поиску карточек, анализу медианных цен и фоновому мониторингу.",
            payload=f"pro_30d_{uid}_{int(now_utc().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
        )
        return JSONResponse({"invoiceUrl": invoice_link})
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
    profitRub: int = Field(default=0)
    profitPct: float = Field(default=0.0)
    tags: List[str] = Field(default_factory=list)
    sellerName: str = Field(default="Продавец", max_length=60)
    sellerRating: float = Field(default=5.0)
    sellerReviews: int = Field(default=0)
    desc: str = Field(default="", max_length=600)
    url: str = Field(..., max_length=400)


@app.post("/api/push-lots")
async def api_push_external_lots(
    lots: List[ExternalLotItem],
    x_worker_secret: Optional[str] = Header(None),
):
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET не настроен")
    if not x_worker_secret or not hmac.compare_digest(x_worker_secret, WORKER_SECRET):
        raise HTTPException(status_code=401, detail="Неверный ключ авторизации")

    pool = require_db()
    inserted_count = 0
    async with pool.acquire() as conn:
        for item in lots[:50]:
            try:
                safe_url = validate_and_clean_url(item.url)
            except ValueError as err:
                logger.warning(f"Отклонён лот с некорректным URL ({item.url}): {err}")
                continue

            clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", item.id)
            if not clean_id:
                continue
            clean_tags = json.dumps([str(t)[:30] for t in item.tags[:4]], ensure_ascii=False)

            await conn.execute(
                """
                INSERT INTO dh_lots (
                    id, title, source, city, price, market, profit_rub, profit_pct,
                    tags, seller_name, seller_rating, seller_reviews, description, url, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    price = EXCLUDED.price,
                    market = EXCLUDED.market,
                    profit_rub = EXCLUDED.profit_rub,
                    profit_pct = EXCLUDED.profit_pct,
                    created_at = NOW()
                """,
                clean_id, item.title, item.source, item.city, item.price, item.market,
                item.profitRub, item.profitPct, clean_tags, item.sellerName,
                item.sellerRating, item.sellerReviews, item.desc, safe_url,
            )
            inserted_count += 1

    return JSONResponse({"status": "ok", "saved": inserted_count})


# =====================================================================
# 10. UI: МИНИМАЛИЗМ (ЧЕРНЫЙ / БЕЛЫЙ / ОРАНЖЕВЫЙ + ZERO INLINE ONCLICK XSS)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_minimalist_ui():
    return """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DealHunter</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/lucide@0.460.0/dist/umd/lucide.min.js"></script>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root[data-theme="dark"] {
      --bg: #0B0C0E;
      --surface: #121418;
      --surface-2: #191C22;
      --border: #242830;
      --text: #FFFFFF;
      --text-sec: #949BA6;
      --text-muted: #5E6470;
      --accent: #FF5500;
      --accent-soft: rgba(255, 85, 0, 0.14);
    }
    :root[data-theme="light"] {
      --bg: #F7F8FA;
      --surface: #FFFFFF;
      --surface-2: #F0F2F5;
      --border: #E2E5EC;
      --text: #0B0C0E;
      --text-sec: #525866;
      --text-muted: #8A909E;
      --accent: #FF5500;
      --accent-soft: rgba(255, 85, 0, 0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', -apple-system, sans-serif;
      display: flex;
      justify-content: center;
      min-height: 100vh;
      overscroll-behavior-y: contain;
    }
    .mono { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }
    .shell { width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg); padding-bottom: 76px; position: relative; }
    
    .ptr {
      height: 0; overflow: hidden; display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 600; color: var(--accent);
      background: var(--surface); border-bottom: 1px solid var(--border); transition: height 0.15s ease;
    }
    .header {
      position: sticky; top: 0; z-index: 20; background: var(--bg);
      border-bottom: 1px solid var(--border); padding: 12px 14px;
    }
    .content { padding: 12px 14px; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 10px;
    }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    
    .search-box { display: flex; gap: 6px; margin-top: 10px; }
    .input {
      width: 100%; height: 38px; padding: 0 12px; border-radius: 8px;
      background: var(--surface); border: 1px solid var(--border);
      color: var(--text); font-family: inherit; font-size: 13px; outline: none;
    }
    .input:focus { border-color: var(--accent); }
    
    .chips { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; margin-top: 8px; }
    .chips::-webkit-scrollbar { display: none; }
    .chip {
      height: 28px; padding: 0 11px; border-radius: 6px;
      background: var(--surface); border: 1px solid var(--border);
      color: var(--text-sec); font-size: 12px; font-weight: 500;
      white-space: nowrap; cursor: pointer; display: inline-flex; align-items: center;
    }
    .chip.active { background: var(--accent); color: #FFFFFF; border-color: var(--accent); font-weight: 600; }

    .tag {
      display: inline-flex; align-items: center; height: 22px; padding: 0 7px;
      border-radius: 4px; background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text-sec); font-size: 11px;
    }
    .badge-src {
      padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.4px; border: 1px solid var(--border);
      color: var(--text);
    }
    .badge-accent {
      padding: 4px 8px; border-radius: 6px; background: var(--accent-soft);
      border: 1px solid var(--accent); color: var(--accent);
      font-size: 12px; font-weight: 700;
    }
    .badge-neutral {
      padding: 4px 8px; border-radius: 6px; background: var(--surface-2);
      border: 1px solid var(--border); color: var(--text-sec); font-size: 11px; font-weight: 600;
    }

    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 6px;
      border: 1px solid transparent; border-radius: 8px; font-family: inherit;
      font-size: 13px; font-weight: 600; cursor: pointer; text-decoration: none;
    }
    .btn-sm { height: 32px; padding: 0 10px; font-size: 12px; }
    .btn-md { height: 38px; padding: 0 14px; }
    .btn-lg { height: 44px; width: 100%; font-size: 13px; }
    .btn-primary { background: var(--accent); color: #FFFFFF; }
    .btn-secondary { background: var(--surface-2); border-color: var(--border); color: var(--text); }
    .btn-outline { background: transparent; border-color: var(--border); color: var(--text-sec); }

    .nav-bar {
      position: fixed; bottom: 0; left: 50%; transform: translateX(-50%);
      width: 100%; max-width: 430px; height: 58px; background: var(--surface);
      border-top: 1px solid var(--border); display: grid; grid-template-columns: repeat(4, 1fr); z-index: 30;
    }
    .nav-item {
      border: none; background: transparent; color: var(--text-muted);
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 4px; font-size: 10px; font-weight: 600; cursor: pointer;
    }
    .nav-item.active { color: var(--accent); }

    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.75);
      z-index: 50; display: flex; align-items: center; justify-content: center; padding: 16px;
    }
    .modal-box {
      width: 100%; max-width: 390px; background: var(--surface);
      border: 1px solid var(--border); border-radius: 12px; padding: 20px;
      max-height: 85vh; overflow-y: auto;
    }
    .toast {
      position: fixed; top: 14px; left: 50%; transform: translateX(-50%);
      z-index: 90; background: var(--accent); color: #FFFFFF;
      padding: 8px 14px; border-radius: 6px; font-size: 12px; font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="shell">
    <div id="ptr-bar" class="ptr"><span id="ptr-text">Потяните вниз для обновления</span></div>
    <div id="toast-box"></div>
    <div id="app"></div>
  </div>

  <script>
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) { tg.ready(); tg.expand(); }

    const RAW_INIT_DATA = (tg && tg.initData) ? tg.initData : '';
    const BUNNY_STARS_LINK = "https://t.me/BunnyStarsBot?startapp=ref_5123484212";

    function esc(val) {
      if (val === null || val === undefined) return '';
      return String(val)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function encodeInitDataB64(str) {
      if (!str) return '';
      try { return btoa(encodeURIComponent(str)); } catch (e) { return ''; }
    }

    function authHeaders() {
      return {
        'Content-Type': 'application/json',
        'X-Tg-Init-Data-B64': encodeInitDataB64(RAW_INIT_DATA)
      };
    }

    const savedTheme = localStorage.getItem('dh_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', savedTheme);

    const state = {
      theme: savedTheme,
      authError: null,
      tab: 'feed',
      category: 'all',
      source: 'Wildberries',
      city: 'Москва',
      searchQuery: '',
      extConnected: false,
      updatedAt: '',
      detailId: null,
      showNewMonitor: false,
      showPaywall: false,
      legalDoc: null,
      user: {
        user_id: 0,
        name: 'Пользователь',
        trialClaimed: true,
        subActive: false,
        subUntil: null,
        secondsLeft: 0,
        savedIds: [],
        refCount: 0,
        refDaysEarned: 0,
        refMonthDays: 0,
        refMonthLimit: 30,
        refLink: ''
      },
      listings: [],
      monitors: []
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(Number(n) || 0);

    function showToast(msg) {
      const box = document.getElementById('toast-box');
      const div = document.createElement('div');
      div.className = 'toast';
      div.textContent = String(msg);
      box.innerHTML = '';
      box.appendChild(div);
      clearTimeout(window._tTimer);
      window._tTimer = setTimeout(() => { box.innerHTML = ''; }, 2400);
    }

    function formatCountdown(sec) {
      if (!sec || sec <= 0) return 'Не активен';
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const pad = (v) => String(v).padStart(2, '0');
      return d > 0 ? `${d}д ${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(h)}:${pad(m)}:${pad(s)}`;
    }

    setInterval(() => {
      if (!state.user || !state.user.subUntil) return;
      const diff = Math.floor((new Date(state.user.subUntil).getTime() - Date.now()) / 1000);
      state.user.secondsLeft = Math.max(0, diff);
      state.user.subActive = diff > 0;
      const el = document.getElementById('hdr-timer');
      if (el) el.textContent = state.user.subActive ? formatCountdown(diff) : 'PRO истёк';
      const profEl = document.getElementById('prof-timer');
      if (profEl) profEl.textContent = formatCountdown(diff);
    }, 1000);

    // Pull-to-refresh
    let startY = 0;
    let pull = 0;
    window.addEventListener('touchstart', (e) => {
      startY = (window.scrollY <= 5 && state.tab === 'feed' && !state.detailId) ? e.touches[0].clientY : 0;
    }, { passive: true });

    window.addEventListener('touchmove', (e) => {
      if (!startY) return;
      pull = Math.max(0, e.touches[0].clientY - startY);
      if (pull > 12 && window.scrollY <= 5) {
        const bar = document.getElementById('ptr-bar');
        const txt = document.getElementById('ptr-text');
        if (bar) bar.style.height = Math.min(46, pull * 0.4) + 'px';
        if (txt) txt.textContent = pull > 95 ? 'Отпустите для обновления' : 'Потяните вниз для обновления';
      }
    }, { passive: true });

    window.addEventListener('touchend', async () => {
      const bar = document.getElementById('ptr-bar');
      if (pull > 95 && state.tab === 'feed') {
        if (bar) bar.style.height = '38px';
        await loadFeed(true);
      }
      if (bar) bar.style.height = '0px';
      startY = 0; pull = 0;
    });

    function toggleTheme() {
      state.theme = state.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', state.theme);
      localStorage.setItem('dh_theme', state.theme);
      render();
    }

    function openSafeUrl(rawUrl) {
      try {
        const parsed = new URL(String(rawUrl));
        if (parsed.protocol !== 'https:') return;
        const u = parsed.href;
        if (tg && tg.openTelegramLink && u.startsWith('https://t.me/')) {
          tg.openTelegramLink(u);
        } else if (tg && tg.openLink) {
          tg.openLink(u);
        } else {
          window.open(u, '_blank', 'noopener,noreferrer');
        }
      } catch (e) {}
    }

    async function syncState() {
      try {
        const res = await fetch('/api/state', { headers: authHeaders() });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          state.authError = err.detail || 'Требуется вход через Telegram';
          render();
          return;
        }
        const data = await res.json();
        state.authError = null;
        state.user = data.user;
        state.monitors = data.monitors || [];
        await loadFeed(false);
      } catch (e) {
        state.authError = 'Нет соединения с сервером';
        render();
      }
    }

    async function loadFeed(notify = false) {
      if (!state.user.subActive) {
        state.listings = [];
        render();
        return;
      }
      try {
        const params = new URLSearchParams({
          category: state.category,
          source: state.source,
          city: state.city,
          q: state.searchQuery
        });
        const res = await fetch(`/api/live-feed?${params.toString()}`, { headers: authHeaders() });
        if (res.ok) {
          const data = await res.json();
          state.listings = data.items || [];
          state.extConnected = Boolean(data.extConnected);
          state.updatedAt = data.updatedAt || '';
          if (notify) showToast(`Обновлено: ${state.listings.length} поз.`);
        }
      } catch (e) {}
      render();
    }

    async function claimTrialAction() {
      try {
        const res = await fetch('/api/claim-trial', { method: 'POST', headers: authHeaders() });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          state.user = data.user;
          showToast('PRO-доступ на 3 дня активирован');
          await loadFeed(false);
        } else {
          showToast(data.detail || 'Ошибка активации');
        }
      } catch (e) {
        showToast('Ошибка сети');
      }
    }

    async function buyProAction() {
      showToast('Формирование счёта...');
      try {
        const res = await fetch('/api/create-invoice', { method: 'POST', headers: authHeaders() });
        const data = await res.json();
        if (data.invoiceUrl && tg && tg.openInvoice) {
          tg.openInvoice(data.invoiceUrl, async (status) => {
            if (status === 'paid') {
              showToast('Оплата подтверждена (+30 дней PRO)');
              state.showPaywall = false;
              await syncState();
            }
          });
        } else if (data.invoiceUrl) {
          openSafeUrl(data.invoiceUrl);
        } else {
          showToast(data.error || 'Ошибка создания счёта');
        }
      } catch (e) {
        showToast('Ошибка сети');
      }
    }

    async function toggleSaveLot(lotId) {
      const idStr = String(lotId);
      const idx = state.user.savedIds.indexOf(idStr);
      if (idx >= 0) state.user.savedIds.splice(idx, 1);
      else state.user.savedIds.push(idStr);
      render();
      await fetch('/api/saved', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ savedIds: state.user.savedIds })
      });
    }

    async function createMonitorAction() {
      const title = document.getElementById('m-title').value.trim();
      const city = document.getElementById('m-city').value;
      const source = document.getElementById('m-source').value;
      const minMargin = parseInt(document.getElementById('m-margin').value) || 15;
      if (title.length < 2) { showToast('Введите поисковый запрос'); return; }

      const res = await fetch('/api/monitors', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ title, city, source, minMargin })
      });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        state.showNewMonitor = false;
        showToast('Монитор сохранён');
        render();
      } else {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || 'Ошибка создания монитора');
      }
    }

    async function toggleMonitorAction(mid) {
      const res = await fetch(`/api/monitors/${encodeURIComponent(mid)}/toggle`, { method: 'POST', headers: authHeaders() });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        render();
      }
    }

    async function deleteMonitorAction(mid) {
      const res = await fetch(`/api/monitors/${encodeURIComponent(mid)}`, { method: 'DELETE', headers: authHeaders() });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        showToast('Монитор удалён');
        render();
      }
    }

    // ЕДИНЫЙ БЕЗОПАСНЫЙ ДЕЛЕГАТ СОБЫТИЙ (БЕЗ INLINE ONCLICK И XSS)
    document.addEventListener('click', (e) => {
      const el = e.target.closest('[data-action]');
      if (!el) return;
      const action = el.getAttribute('data-action');
      const val = el.getAttribute('data-val') || '';

      if (action === 'tab') { state.tab = val; state.detailId = null; render(); }
      else if (action === 'theme') { toggleTheme(); }
      else if (action === 'refresh') { loadFeed(true); }
      else if (action === 'paywall') { state.showPaywall = true; render(); }
      else if (action === 'close-modal') {
        state.showPaywall = false; state.showNewMonitor = false; state.legalDoc = null; render();
      }
      else if (action === 'source') { state.source = val; loadFeed(false); }
      else if (action === 'category') { state.category = val; state.searchQuery = ''; loadFeed(false); }
      else if (action === 'search-run') {
        const inp = document.getElementById('q-input');
        if (inp) {
          state.searchQuery = inp.value.trim();
          if (state.searchQuery) state.category = 'all';
          loadFeed(true);
        }
      }
      else if (action === 'search-reset') { state.searchQuery = ''; loadFeed(false); }
      else if (action === 'open-detail') { state.detailId = val; window.scrollTo(0, 0); render(); }
      else if (action === 'close-detail') { state.detailId = null; render(); }
      else if (action === 'save-lot') { e.stopPropagation(); toggleSaveLot(val); }
      else if (action === 'open-lot-url') {
        e.stopPropagation();
        const item = state.listings.find(x => x.id === val);
        if (item && item.url) openSafeUrl(item.url);
      }
      else if (action === 'open-url') { openSafeUrl(val); }
      else if (action === 'claim-trial') { claimTrialAction(); }
      else if (action === 'buy-pro') { buyProAction(); }
      else if (action === 'new-monitor-modal') {
        if (!state.user.subActive) state.showPaywall = true;
        else state.showNewMonitor = true;
        render();
      }
      else if (action === 'create-monitor') { createMonitorAction(); }
      else if (action === 'toggle-monitor') { toggleMonitorAction(val); }
      else if (action === 'delete-monitor') { deleteMonitorAction(val); }
      else if (action === 'copy-ref') {
        if (navigator.clipboard && state.user.refLink) {
          navigator.clipboard.writeText(state.user.refLink);
          showToast('Ссылка скопирована');
        }
      }
      else if (action === 'share-ref') {
        const u = 'https://t.me/share/url?url=' + encodeURIComponent(state.user.refLink) + '&text=' + encodeURIComponent('Аналитический радар цен DealHunter (3 дня PRO бесплатно):');
        openSafeUrl(u);
      }
      else if (action === 'legal') { state.legalDoc = val; render(); }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target && e.target.id === 'q-input') {
        state.searchQuery = e.target.value.trim();
        if (state.searchQuery) state.category = 'all';
        loadFeed(true);
      }
    });

    function renderLotCard(item) {
      const isSaved = state.user.savedIds.includes(item.id);
      const hasDiff = Number(item.profitPct) > 0;
      return `
        <div class="card" data-action="open-detail" data-val="${esc(item.id)}" style="cursor:pointer;">
          <div class="row" style="margin-bottom:6px;">
            <div style="display:flex; align-items:center; gap:6px;">
              <span class="badge-src">${esc(item.source)}</span>
              <span style="font-size:11px; color:var(--text-sec);">${esc(item.city)} · ${esc(item.timeAgo)}</span>
            </div>
          </div>
          <div style="font-size:14px; font-weight:600; line-height:1.35; margin-bottom:8px;">${esc(item.title)}</div>
          <div style="display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px;">
            ${(item.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}
          </div>
          <div class="row" style="padding:10px; border-radius:8px; background:var(--bg); border:1px solid var(--border); margin-bottom:10px;">
            <div>
              <div class="mono" style="font-size:18px; font-weight:700;">${fmt(item.price)} ₽</div>
              <div class="mono" style="font-size:11px; color:var(--text-muted);">Медиана модели: ${fmt(item.market)} ₽</div>
            </div>
            ${hasDiff
              ? `<div class="badge-accent mono">−${esc(item.profitPct)}\% ·${fmt(item.profitRub)} ₽</div>`
              : `<div class="badge-neutral mono">По медиане</div>`
            }
          </div>
          <div class="row">
            <span style="font-size:11px; color:var(--text-sec);">${esc(item.seller.name)} · ★ ${esc(item.seller.rating)}</span>
            <div style="display:flex; gap:6px;">
              <button class="btn btn-sm btn-secondary" data-action="save-lot" data-val="${esc(item.id)}">
                <i data-lucide="bookmark" style="width:14px; height:14px; ${isSaved ? 'fill:var(--accent); color:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-sm btn-primary" data-action="open-lot-url" data-val="${esc(item.id)}">
                <span>Открыть</span>
              </button>
            </div>
          </div>
        </div>
      `;
    }

    function render() {
      const app = document.getElementById('app');

      if (state.authError) {
        app.innerHTML = `
          <div class="content" style="padding-top:40px;">
            <div class="card" style="text-align:center; padding:24px 16px;">
              <div style="font-size:15px; font-weight:700; margin-bottom:8px;">Доступ только внутри Telegram</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.5; margin-bottom:16px;">${esc(state.authError)}</p>
              <button class="btn btn-lg btn-primary" data-action="open-url" data-val="https://t.me/DealHunterAvito_bot">Запустить @DealHunterAvito_bot</button>
            </div>
          </div>
        `;
        return;
      }

      if (state.detailId) {
        const item = state.listings.find(x => x.id === state.detailId);
        if (item) {
          app.innerHTML = `
            <div class="header row">
              <button class="btn btn-sm btn-secondary" data-action="close-detail">← Назад</button>
              <span class="badge-src">${esc(item.source)}</span>
            </div>
            <div class="content">
              <div class="card">
                <div style="font-size:11px; color:var(--text-sec); margin-bottom:6px;">${esc(item.city)} · ${esc(item.timeAgo)}</div>
                <div style="font-size:16px; font-weight:700; margin-bottom:12px; line-height:1.35;">${esc(item.title)}</div>
                <div class="row" style="padding:12px; border-radius:8px; background:var(--bg); border:1px solid var(--border); margin-bottom:12px;">
                  <div>
                    <div class="mono" style="font-size:20px; font-weight:700;">${fmt(item.price)} ₽</div>
                    <div class="mono" style="font-size:11px; color:var(--text-muted);">Медиана модели: ${fmt(item.market)} ₽</div>
                  </div>
                  ${Number(item.profitPct) > 0 ? `<div class="badge-accent mono">Отклонение −${esc(item.profitPct)}%</div>` : ''}
                </div>
                <div style="font-size:12px; color:var(--text-sec); line-height:1.55; margin-bottom:16px;">${esc(item.desc)}</div>
                <button class="btn btn-lg btn-primary" data-action="open-lot-url" data-val="${esc(item.id)}">Перейти к карточке товара</button>
              </div>
            </div>
          `;
          if (window.lucide) lucide.createIcons();
          return;
        }
      }

      const timerHtml = `
        <button class="btn btn-sm btn-secondary mono" data-action="paywall" id="hdr-timer">
          ${state.user.subActive ? formatCountdown(state.user.secondsLeft) : 'PRO не активен'}
        </button>
      `;

      let bodyHtml = '';

      if (state.tab === 'feed') {
        let feedListHtml = '';
        if (!state.user.subActive) {
          feedListHtml = `
            <div class="card" style="text-align:center; padding:24px 16px;">
              <div style="font-size:16px; font-weight:700; margin-bottom:8px;">Требуется подписка PRO</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.5; margin-bottom:16px;">
                Доступ к сравнению цен однородных моделей, фильтрации аксессуаров и фоновым мониторам открывается по подписке.
              </p>
              <button class="btn btn-lg btn-primary" style="margin-bottom:8px;" data-action="buy-pro">Оформить PRO (30 дней) · 350 Stars</button>
              <button class="btn btn-lg btn-secondary" data-action="open-url" data-val="${BUNNY_STARS_LINK}">Купить Stars в BunnyStars</button>
            </div>
          `;
        } else if ((state.source === 'Avito' || state.source === 'Ozon') && !state.extConnected && state.listings.length === 0) {
          feedListHtml = `
            <div class="card" style="padding:20px 16px;">
              <div style="font-size:14px; font-weight:700; margin-bottom:6px;">Каталог ${esc(state.source)}: ожидание свежих объявлений</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.5; margin-bottom:12px;">
                В данный момент в очереди нет активных объявлений ${esc(state.source)} за последние 6 часов. Вы можете переключиться на живой поиск по каталогу Wildberries.
              </p>
              <button class="btn btn-md btn-primary" data-action="source" data-val="Wildberries">Переключить на Wildberries</button>
            </div>
          `;
        } else if (state.listings.length === 0) {
          feedListHtml = `<div class="card" style="text-align:center; color:var(--text-sec);">По запросу «${esc(state.searchQuery || state.category)}» подходящих карточек без учёта аксессуаров не найдено.</div>`;
        } else {
          feedListHtml = state.listings.map(renderLotCard).join('');
        }

        bodyHtml = `
          <div class="header">
            <div class="row">
              <div>
                <div style="font-size:15px; font-weight:700;">DealHunter</div>
                <div style="font-size:10px; color:var(--text-muted);">Свайп вниз — обновить${state.updatedAt ? ' · ' + esc(state.updatedAt) : ''}</div>
              </div>
              <div style="display:flex; gap:6px;">
                ${timerHtml}
                <button class="btn btn-sm btn-secondary" data-action="refresh"><i data-lucide="refresh-cw" style="width:13px; height:13px;"></i></button>
                <button class="btn btn-sm btn-secondary" data-action="theme"><i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:13px; height:13px;"></i></button>
              </div>
            </div>

            <div class="search-box">
              <input
                type="text"
                id="q-input"
                class="input"
                placeholder="Поиск модели (Xiaomi 14, игрушка, RTX 4070)..."
                value="${esc(state.searchQuery)}"
              />
              ${state.searchQuery ? `<button class="btn btn-md btn-secondary" data-action="search-reset">✕</button>` : ''}
              <button class="btn btn-md btn-primary" data-action="search-run">Найти</button>
            </div>

            <div class="chips">
              <button class="chip ${state.source === 'Wildberries' ? 'active' : ''}" data-action="source" data-val="Wildberries">Wildberries</button>
              <button class="chip ${state.source === 'all' ? 'active' : ''}" data-action="source" data-val="all">Все площадки</button>
              <button class="chip ${state.source === 'Avito' ? 'active' : ''}" data-action="source" data-val="Avito">Avito</button>
              <button class="chip ${state.source === 'Ozon' ? 'active' : ''}" data-action="source" data-val="Ozon">Ozon</button>
            </div>

            <div class="chips">
              <button class="chip ${state.category === 'all' && !state.searchQuery ? 'active' : ''}" data-action="category" data-val="all">Популярное</button>
              <button class="chip ${state.category === 'xiaomi' ? 'active' : ''}" data-action="category" data-val="xiaomi">Xiaomi</button>
              <button class="chip ${state.category === 'iphone' ? 'active' : ''}" data-action="category" data-val="iphone">iPhone</button>
              <button class="chip ${state.category === 'macbook' ? 'active' : ''}" data-action="category" data-val="macbook">Ноутбуки</button>
              <button class="chip ${state.category === 'gpu' ? 'active' : ''}" data-action="category" data-val="gpu">Видеокарты</button>
              <button class="chip ${state.category === 'ps5' ? 'active' : ''}" data-action="category" data-val="ps5">Консоли</button>
            </div>
          </div>
          <div class="content">${feedListHtml}</div>
        `;
      }

      if (state.tab === 'monitors') {
        bodyHtml = `
          <div class="header row">
            <div style="font-size:15px; font-weight:700;">Авто-мониторы</div>
            <button class="btn btn-sm btn-primary" data-action="new-monitor-modal">+ Добавить</button>
          </div>
          <div class="content">
            ${state.monitors.length ? state.monitors.map(m => `
              <div class="card">
                <div class="row" style="margin-bottom:8px;">
                  <div>
                    <div style="font-size:14px; font-weight:600;">${esc(m.title)}</div>
                    <div style="font-size:11px; color:var(--text-sec);">${esc(m.source)} · ${esc(m.city)} · отклонение ≥ ${esc(m.minMargin)}%</div>
                  </div>
                  <button class="btn btn-sm ${m.active ? 'btn-primary' : 'btn-secondary'}" data-action="toggle-monitor" data-val="${esc(m.id)}">
                    ${m.active ? 'Активен' : 'Пауза'}
                  </button>
                </div>
                <div class="row" style="border-top:1px solid var(--border); padding-top:8px;">
                  <span style="font-size:11px; color:var(--text-muted);">Найдено за сегодня: ${esc(m.foundToday)}</span>
                  <button class="btn btn-sm btn-outline" data-action="delete-monitor" data-val="${esc(m.id)}">Удалить</button>
                </div>
              </div>
            `).join('') : '<div class="card" style="text-align:center; color:var(--text-sec);">Активных мониторов нет. Бот присылает уведомление, когда цена модели опускается ниже медианы однородной выборки на заданный процент.</div>'}
          </div>
        `;
      }

      if (state.tab === 'saved') {
        const saved = state.listings.filter(x => state.user.savedIds.includes(x.id));
        bodyHtml = `
          <div class="header row">
            <div style="font-size:15px; font-weight:700;">Закладки (${state.user.savedIds.length})</div>
            ${timerHtml}
          </div>
          <div class="content">
            ${saved.length ? saved.map(renderLotCard).join('') : '<div class="card" style="text-align:center; color:var(--text-sec);">Сохранённых карточек нет</div>'}
          </div>
        `;
      }

      if (state.tab === 'profile') {
        bodyHtml = `
          <div class="header row">
            <div style="font-size:15px; font-weight:700;">Профиль</div>
            <div style="display:flex; gap:6px;">
              ${timerHtml}
              <button class="btn btn-sm btn-secondary" data-action="theme"><i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:13px; height:13px;"></i></button>
            </div>
          </div>
          <div class="content">
            <div class="card">
              <div class="row" style="margin-bottom:10px;">
                <div>
                  <div style="font-size:15px; font-weight:700;">${esc(state.user.name)}</div>
                  <div class="mono" style="font-size:11px; color:var(--text-sec);">ID: ${esc(state.user.user_id)}</div>
                </div>
                <span class="${state.user.subActive ? 'badge-accent' : 'badge-neutral'}">${state.user.subActive ? 'PRO АКТИВЕН' : 'FREE'}</span>
              </div>
              <div style="padding:10px; border-radius:8px; background:var(--bg); border:1px solid var(--border); margin-bottom:10px;">
                <div style="font-size:11px; color:var(--text-sec);">Остаток подписки:</div>
                <div id="prof-timer" class="mono" style="font-size:17px; font-weight:700; margin-top:2px;">${formatCountdown(state.user.secondsLeft)}</div>
              </div>
              <button class="btn btn-lg btn-primary" style="margin-bottom:8px;" data-action="buy-pro">Продлить PRO (+30 дней) · 350 Stars</button>
              <button class="btn btn-lg btn-secondary" data-action="open-url" data-val="${BUNNY_STARS_LINK}">Пополнить Stars (BunnyStars)</button>
            </div>

            <div class="card">
              <div class="row" style="margin-bottom:4px;">
                <div style="font-size:14px; font-weight:700;">Реферальная программа (+3 дня PRO)</div>
                <span class="mono" style="font-size:11px; color:var(--text-sec);">${esc(state.user.refMonthDays)}/${esc(state.user.refMonthLimit)} дн. в мес.</span>
              </div>
              <div style="font-size:11px; color:var(--text-sec); margin-bottom:10px;">
                За каждого нового пользователя, запустившего приложение и активировавшего доступ, начисляется +3 дня PRO (лимит: до 30 дней в месяц).
              </div>
              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:10px;">
                <div style="padding:10px; border-radius:8px; background:var(--bg); border:1px solid var(--border); text-align:center;">
                  <div class="mono" style="font-size:18px; font-weight:700;">${esc(state.user.refCount)}</div>
                  <div style="font-size:10px; color:var(--text-sec);">Приглашено друзей</div>
                </div>
                <div style="padding:10px; border-radius:8px; background:var(--bg); border:1px solid var(--border); text-align:center;">
                  <div class="mono" style="font-size:18px; font-weight:700; color:var(--accent);">+${esc(state.user.refDaysEarned)} дн.</div>
                  <div style="font-size:10px; color:var(--text-sec);">Всего получено PRO</div>
                </div>
              </div>
              <div class="mono" style="padding:8px 10px; border-radius:6px; background:var(--bg); border:1px solid var(--border); font-size:11px; color:var(--text-sec); word-break:break-all; margin-bottom:8px;">
                ${esc(state.user.refLink)}
              </div>
              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <button class="btn btn-md btn-secondary" data-action="copy-ref">Скопировать</button>
                <button class="btn btn-md btn-primary" data-action="share-ref">Поделиться</button>
              </div>
            </div>

            <div class="card">
              <div style="font-size:12px; font-weight:700; margin-bottom:8px; color:var(--text-sec);">Правовая информация</div>
              <div style="display:flex; flex-direction:column; gap:6px;">
                <button class="btn btn-md btn-secondary" style="justify-content:space-between;" data-action="legal" data-val="privacy">
                  <span>Политика конфиденциальности</span><span>→</span>
                </button>
                <button class="btn btn-md btn-secondary" style="justify-content:space-between;" data-action="legal" data-val="terms">
                  <span>Пользовательское соглашение</span><span>→</span>
                </button>
              </div>
            </div>
          </div>
        `;
      }

      let modalHtml = '';
      if (!state.user.trialClaimed) {
        modalHtml = `
          <div class="modal-overlay">
            <div class="modal-box" style="text-align:center;">
              <div class="badge-accent" style="display:inline-block; margin-bottom:10px;">WELCOME PRO</div>
              <div style="font-size:18px; font-weight:700; margin-bottom:8px;">Пробный доступ на 3 дня</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.5; margin-bottom:16px;">
                Активируйте 3 дня подписки DealHunter PRO для доступа к поиску карточек, кластерному анализу цен и фоновым мониторам.
              </p>
              <button class="btn btn-lg btn-primary" data-action="claim-trial">Активировать 3 дня PRO</button>
            </div>
          </div>
        `;
      } else if (state.showNewMonitor) {
        modalHtml = `
          <div class="modal-overlay" data-action="close-modal">
            <div class="modal-box" onclick="event.stopPropagation();">
              <div class="row" style="margin-bottom:12px;">
                <div style="font-size:15px; font-weight:700;">Новый монитор</div>
                <button class="btn btn-sm btn-secondary" data-action="close-modal">✕</button>
              </div>
              <input type="text" id="m-title" class="input" style="margin-bottom:8px;" placeholder="Модель товара (напр. iPhone 15 128)" />
              <select id="m-city" class="input" style="margin-bottom:8px;">
                <option value="Москва">Москва</option>
                <option value="Санкт-Петербург">Санкт-Петербург</option>
                <option value="Екатеринбург">Екатеринбург</option>
                <option value="Казань">Казань</option>
                <option value="Новосибирск">Новосибирск</option>
                <option value="Краснодар">Краснодар</option>
              </select>
              <select id="m-source" class="input" style="margin-bottom:8px;">
                <option value="Wildberries">Wildberries</option>
                <option value="all">Все площадки</option>
                <option value="Avito">Avito</option>
                <option value="Ozon">Ozon</option>
              </select>
              <input type="number" id="m-margin" class="input" style="margin-bottom:12px;" placeholder="Мин. отклонение от медианы (%)" value="15" />
              <button class="btn btn-lg btn-primary" data-action="create-monitor">Сохранить монитор</button>
            </div>
          </div>
        `;
      } else if (state.showPaywall) {
        modalHtml = `
          <div class="modal-overlay" data-action="close-modal">
            <div class="modal-box" onclick="event.stopPropagation();">
              <div class="row" style="margin-bottom:10px;">
                <div style="font-size:15px; font-weight:700;">DealHunter PRO</div>
                <button class="btn btn-sm btn-secondary" data-action="close-modal">✕</button>
              </div>
              <div class="mono" style="font-size:22px; font-weight:700; margin-bottom:6px; color:var(--accent);">350 Stars / 30 дней</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.5; margin-bottom:14px;">
                Полный доступ к поиску моделей без информационного шума, сравнению с медианной ценой комплектации и фоновым уведомлениям в Telegram.
              </p>
              <button class="btn btn-lg btn-primary" style="margin-bottom:8px;" data-action="buy-pro">Оплатить 350 Stars</button>
              <button class="btn btn-lg btn-secondary" data-action="open-url" data-val="${BUNNY_STARS_LINK}">Купить Stars в BunnyStars</button>
            </div>
          </div>
        `;
      } else if (state.legalDoc) {
        const docs = {
          privacy: {
            t: 'Политика конфиденциальности',
            b: '1. Сервис DealHunter обрабатывает технический идентификатор аккаунта Telegram (Telegram ID), имя пользователя и пользовательские настройки фильтров исключительно для функционирования мини-приложения и доставки уведомлений.\\n\\n2. Сервис не запрашивает и не хранит паспортные данные, платёжные реквизиты банковских карт или номера телефонов.\\n\\n3. Пользователь может прекратить обработку данных в любой момент, удалив свои мониторы и остановив бота.'
          },
          terms: {
            t: 'Пользовательское соглашение',
            b: '1. DealHunter является независимым справочно-информационным инструментом анализа открытых розничных цен.\\n\\n2. Показатель «Отклонение от медианы» рассчитывается математически относительно выборки однотипных товаров в поисковой выдаче и носит исключительно ознакомительный характер.\\n\\n3. Доступ к расширенному функционалу PRO предоставляется на 30 календарных дней за Telegram Stars.'
          }
        };
        const d = docs[state.legalDoc];
        modalHtml = `
          <div class="modal-overlay" data-action="close-modal">
            <div class="modal-box" onclick="event.stopPropagation();">
              <div style="font-size:14px; font-weight:700; margin-bottom:8px;">${esc(d.t)}</div>
              <p style="font-size:12px; color:var(--text-sec); line-height:1.55; white-space:pre-line; margin-bottom:14px;">${esc(d.b)}</p>
              <button class="btn btn-lg btn-secondary" data-action="close-modal">Закрыть</button>
            </div>
          </div>
        `;
      }

      const navHtml = `
        <nav class="nav-bar">
          <button class="nav-item ${state.tab === 'feed' ? 'active' : ''}" data-action="tab" data-val="feed">
            <i data-lucide="search" style="width:18px; height:18px;"></i><span>Поиск</span>
          </button>
          <button class="nav-item ${state.tab === 'monitors' ? 'active' : ''}" data-action="tab" data-val="monitors">
            <i data-lucide="bell" style="width:18px; height:18px;"></i><span>Мониторы</span>
          </button>
          <button class="nav-item ${state.tab === 'saved' ? 'active' : ''}" data-action="tab" data-val="saved">
            <i data-lucide="bookmark" style="width:18px; height:18px;"></i><span>Закладки</span>
          </button>
          <button class="nav-item ${state.tab === 'profile' ? 'active' : ''}" data-action="tab" data-val="profile">
            <i data-lucide="user" style="width:18px; height:18px;"></i><span>Профиль</span>
          </button>
        </nav>
      `;

      app.innerHTML = bodyHtml + modalHtml + navHtml;
      if (window.lucide) lucide.createIcons();
    }

    syncState();
  </script>
</body>
</html>
"""
