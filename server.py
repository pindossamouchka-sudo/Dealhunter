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
from datetime import datetime, timedelta, timezone
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
# 1. КОНФИГУРАЦИЯ И ПАРАМЕТРЫ СРЕДЫ
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dealhunter")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip().rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "dh_secret_worker_key_2026").strip()
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

ACCESSORY_STOP_WORDS = (
    "чехол", "стекло", "пленка", "плёнка", "кабель", "провод", "переходник",
    "адаптер", "зарядка", "зарядное", "держатель", "ремешок", "наклейка",
    "брелок", "подставка", "салфетка", "стилус", "коробка от", "муляж",
    "запчасть", "шлейф", "дисплей для", "экран для", "аккумулятор для",
    "батарея для", "корпус для", "крышка для", "линза", "защита камеры",
    "амбушюры", "футляр", "кейс для", "шнурок", "кулер для", "сумка для",
)

app = FastAPI(title="DealHunter PRO v7.0 Full Monolith")

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
# 2. ОГРАНИЧЕНИЕ ЧАСТОТЫ ЗАПРОСОВ И LRU-КЭШ WB
# =====================================================================
USER_RATE_BUCKETS: OrderedDict[int, List[float]] = OrderedDict()
WB_LRU_CACHE: OrderedDict[str, Tuple[float, List[Dict[str, Any]]]] = OrderedDict()
WB_CACHE_MAX_SIZE = 200
WB_CACHE_TTL = 60.0


def check_rate_limit(uid: int, max_requests: int = 35, window_sec: float = 10.0):
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
# 3. КРИПТОГРАФИЧЕСКАЯ АВТОРИЗАЦИЯ TELEGRAM (HMAC-SHA256)
# =====================================================================
def verify_telegram_init_data(init_data_raw: str) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен на сервере")
    if not init_data_raw:
        raise HTTPException(status_code=401, detail="Откройте приложение внутри Telegram через бота")

    try:
        data_dict = dict(parse_qsl(init_data_raw, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Некорректный формат initData")

    received_hash = data_dict.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Криптографическая подпись Telegram отсутствует")

    auth_date_raw = data_dict.get("auth_date", "")
    if not auth_date_raw or not auth_date_raw.isdigit():
        raise HTTPException(status_code=401, detail="Отсутствует auth_date")
    auth_date = int(auth_date_raw)
    now_ts = int(time.time())
    if auth_date <= 0 or (now_ts - auth_date) > 86400 * 3:
        raise HTTPException(status_code=401, detail="Сессия устарела. Перезапустите бота.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Подпись Telegram не прошла проверку подлинности")

    user_raw = data_dict.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Данные пользователя отсутствуют в подписи")

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
        "first_name": str(user_obj.get("first_name") or "Охотник")[:64],
        "username": str(user_obj.get("username") or "")[:64],
        "ref_id": ref_id,
    }


async def get_authenticated_user(request: Request) -> Dict[str, Any]:
    b64_header = request.headers.get("X-Tg-Init-Data-B64", "").strip()
    if not b64_header:
        raise HTTPException(status_code=401, detail="Запустите приложение внутри Telegram (@DealHunterAvito_bot)")
    try:
        decoded_bytes = base64.b64decode(b64_header)
        init_data_raw = unquote(decoded_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Ошибка декодирования заголовка авторизации")

    auth = verify_telegram_init_data(init_data_raw)
    check_rate_limit(auth["uid"])
    return auth


# =====================================================================
# 4. БЕЗОПАСНАЯ ПРОВЕРКА URL (URLPARSE) И ВРЕМЕНИ
# =====================================================================
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
# 5. POSTGRESQL (ASYNCPG, СХЕМА, ТРАНЗАКЦИИ, ПЛАТЕЖИ)
# =====================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_db() -> asyncpg.Pool:
    if not pg_pool:
        raise HTTPException(status_code=503, detail="База данных не подключена (проверьте DATABASE_URL)")
    return pg_pool


async def init_db():
    global pg_pool
    if not DATABASE_URL.startswith("postgres"):
        logger.error("КРИТИЧНО: Задайте корректный DATABASE_URL!")
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
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN DEFAULT FALSE;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month_days INT DEFAULT 0;")
            await conn.execute("ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS found_date DATE DEFAULT CURRENT_DATE;")
        logger.info("PostgreSQL инициализирован.")
    except Exception as e:
        logger.exception(f"Ошибка инициализации PostgreSQL: {e}")


async def check_sponsor_membership(uid: int) -> bool:
    if not SPONSOR_CHANNEL or not bot:
        return True
    try:
        member = await bot.get_chat_member(chat_id=SPONSOR_CHANNEL, user_id=uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Проверка спонсорского канала {SPONSOR_CHANNEL}: {e}")
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
                    username = CASE WHEN EXCLUDED.username != '' THEN EXCLUDED.username ELSE dh_users.username END,
                    referred_by = COALESCE(dh_users.referred_by, EXCLUDED.referred_by)
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
        "name": row["first_name"] or "Охотник",
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
    if SPONSOR_CHANNEL:
        is_member = await check_sponsor_membership(uid)
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail=f"Подпишитесь на канал {SPONSOR_CHANNEL} для активации бесплатного доступа",
            )

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
                f"🎉 <b>Реферал подтверждён: {user_name}!</b>\n\n"
                f"Друг активировал доступ в приложении.\n"
                f"🎁 Вам начислено <b>+{days_awarded_to_ref} дн. подписки DealHunter PRO</b>!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Ошибка уведомления реферера {referrer_to_notify}: {e}")

    return format_user_row(updated_row)


# =====================================================================
# 6. КАТАЛОГ, КЛАСТЕРИЗАЦИЯ И РЕЗЕРВНАЯ ВИТРИНА
# =====================================================================
DEFAULT_QUERIES_BY_CATEGORY = {
    "all": "смартфон Xiaomi",
    "xiaomi": "смартфон Xiaomi Redmi POCO",
    "iphone": "Apple iPhone 15 16",
    "macbook": "ноутбук Apple MacBook",
    "gpu": "видеокарта GeForce RTX",
    "ps5": "консоль PlayStation 5",
}

CURATED_SHOWCASE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "wb_252295571",
        "title": "Смартфон Apple iPhone 16 Pro 256GB Natural Titanium",
        "category": "iphone",
        "source": "Wildberries",
        "city": "Москва",
        "timeAgo": "В наличии · Выборка: 14 шт.",
        "price": 98500,
        "market": 114900,
        "profitRub": 16400,
        "profitPct": 14.3,
        "tags": ["Арт. 252295571", "256 ГБ", "★ 4.9 (640)"],
        "seller": {"name": "Apple Supply", "rating": 4.9, "reviews": 640},
        "desc": "Розничное предложение Wildberries. Сравнение выполнено внутри группы iPhone 16 Pro 256GB (медиана группы: 114 900 ₽).",
        "url": "https://www.wildberries.ru/catalog/252295571/detail.aspx",
    },
    {
        "id": "wb_189351478",
        "title": "Смартфон Apple iPhone 15 Pro 256GB Титановый",
        "category": "iphone",
        "source": "Wildberries",
        "city": "Москва",
        "timeAgo": "В наличии · Выборка: 18 шт.",
        "price": 81900,
        "market": 94500,
        "profitRub": 12600,
        "profitPct": 13.3,
        "tags": ["Арт. 189351478", "eSIM+SIM", "★ 4.9 (1890)"],
        "seller": {"name": "iStore РФ", "rating": 4.9, "reviews": 1890},
        "desc": "Оригинальный iPhone 15 Pro 256 ГБ. Отклонение от медианной цены комплектации составляет −13.3%.",
        "url": "https://www.wildberries.ru/catalog/189351478/detail.aspx",
    },
    {
        "id": "wb_218492014",
        "title": "Смартфон Xiaomi Redmi Note 13 Pro+ 5G 12/512GB Черный",
        "category": "xiaomi",
        "source": "Wildberries",
        "city": "Москва",
        "timeAgo": "В наличии · Выборка: 22 шт.",
        "price": 28400,
        "market": 34900,
        "profitRub": 6500,
        "profitPct": 18.6,
        "tags": ["Арт. 218492014", "12/512 ГБ", "★ 4.9 (920)"],
        "seller": {"name": "Xiaomi Official", "rating": 4.9, "reviews": 920},
        "desc": "Глобальная версия Xiaomi Redmi Note 13 Pro+ 5G (12/512 ГБ). Цена ниже медианы аналогичных предложений на 6 500 ₽.",
        "url": "https://www.wildberries.ru/catalog/218492014/detail.aspx",
    },
    {
        "id": "wb_224910385",
        "title": "Смартфон Xiaomi POCO X6 Pro 5G 12/512GB Серый",
        "category": "xiaomi",
        "source": "Wildberries",
        "city": "Санкт-Петербург",
        "timeAgo": "В наличии · Выборка: 19 шт.",
        "price": 24900,
        "market": 29800,
        "profitRub": 4900,
        "profitPct": 16.4,
        "tags": ["Арт. 224910385", "Dimensity 8300", "★ 4.9 (1340)"],
        "seller": {"name": "POCO Store", "rating": 4.9, "reviews": 1340},
        "desc": "Смартфон POCO X6 Pro 12/512 ГБ (Global Version). Сравнение проведено по 19 предложениям этой модификации.",
        "url": "https://www.wildberries.ru/catalog/224910385/detail.aspx",
    },
    {
        "id": "oz_1429803951",
        "title": "Ноутбук Apple MacBook Air 13 M3 (16 ГБ / 512 ГБ SSD)",
        "category": "macbook",
        "source": "Ozon",
        "city": "Москва",
        "timeAgo": "В наличии · Выборка: 11 шт.",
        "price": 99900,
        "market": 119000,
        "profitRub": 19100,
        "profitPct": 16.1,
        "tags": ["Apple M3", "16/512 ГБ", "★ 4.9 (418)"],
        "seller": {"name": "Ozon Express", "rating": 4.9, "reviews": 418},
        "desc": "Ноутбук Apple MacBook Air 13 M3 (16/512 ГБ). Прямая карточка товара на маркетплейсе Ozon.",
        "url": "https://www.ozon.ru/product/noutbuk-apple-macbook-air-13-m3-16-512-gb-1429803951/",
    },
    {
        "id": "oz_1398451204",
        "title": "Видеокарта Palit GeForce RTX 4070 Super Dual 12 ГБ GDDR6X",
        "category": "gpu",
        "source": "Ozon",
        "city": "Москва",
        "timeAgo": "В наличии · Выборка: 15 шт.",
        "price": 54900,
        "market": 64900,
        "profitRub": 10000,
        "profitPct": 15.4,
        "tags": ["12 ГБ GDDR6X", "Гарантия 3г", "★ 4.9 (530)"],
        "seller": {"name": "Ситилинк", "rating": 4.9, "reviews": 530},
        "desc": "Видеокарта Palit GeForce RTX 4070 SUPER Dual 12GB. Отклонение от медианной цены выборки составляет −15.4%.",
        "url": "https://www.ozon.ru/product/videokarta-palit-geforce-rtx-4070-super-dual-12-gb-1398451204/",
    },
    {
        "id": "wb_195462112",
        "title": "Игровая консоль Sony PlayStation 5 Slim 1TB с дисководом",
        "category": "ps5",
        "source": "Wildberries",
        "city": "Казань",
        "timeAgo": "В наличии · Выборка: 16 шт.",
        "price": 42500,
        "market": 49900,
        "profitRub": 7400,
        "profitPct": 14.8,
        "tags": ["Арт. 195462112", "Slim 1TB", "★ 4.9 (2150)"],
        "seller": {"name": "GameZone", "rating": 4.9, "reviews": 2150},
        "desc": "PlayStation 5 Slim 1 ТБ с дисководом Blu-ray (CFI-2000A01). Сравнение с медианой по 16 аналогичным позициям.",
        "url": "https://www.wildberries.ru/catalog/195462112/detail.aspx",
    },
    {
        "id": "av_4189205412",
        "title": "iPhone 15 Pro Max 256 ГБ Natural Titanium (АКБ 98%)",
        "category": "iphone",
        "source": "Avito",
        "city": "Москва",
        "timeAgo": "Проверенное объявление",
        "price": 79000,
        "market": 94000,
        "profitRub": 15000,
        "profitPct": 16.0,
        "tags": ["АКБ 98%", "Без ремонтов", "Полный комплект"],
        "seller": {"name": "Алексей", "rating": 5.0, "reviews": 64},
        "desc": "Вторичный рынок Avito. Идеальное состояние, полный комплект с чеком магазина re:Store.",
        "url": "https://www.avito.ru/moskva/telefony/iphone_15_pro_max_256gb_natural_titanium_4189205412",
    },
]


def is_accessory_or_junk(title: str, query_text: str) -> bool:
    t_low = title.lower()
    q_low = query_text.lower()
    for stop_w in ACCESSORY_STOP_WORDS:
        if stop_w in t_low and stop_w not in q_low:
            return True
    return False


def extract_model_cluster_key(title: str) -> str:
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
        f"https://search.wb.ru/exactmatch/ru/common/v5/search?ab_testing=false&appType=1&curr=rub&dest={dest}&query={quote(q_clean)}&resultset=catalog&sort=popular&spp=30",
        f"https://search.wb.ru/exactmatch/sng/common/v18/search?ab_testing=false&appType=1&curr=rub&dest={dest}&query={quote(q_clean)}&resultset=catalog&sort=popular&spp=30",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
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
            except Exception as e:
                logger.debug(f"WB шлюз пропущен ({ep[:40]}): {e}")

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

    candidates = []
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        price_rub, qty = get_price_and_qty(p)
        if price_rub < 250 or qty <= 0:
            continue
        name = str(p.get("name") or q_clean).strip()
        brand = str(p.get("brand") or "").strip()
        full_title = f"{brand} {name}".strip() if (brand and brand.lower() not in name.lower()) else name
        if is_accessory_or_junk(full_title, q_clean):
            continue
        candidates.append((price_rub, full_title, p))

    if len(candidates) < 3:
        return []

    all_prices_sorted = sorted(c[0] for c in candidates)
    q75_price = all_prices_sorted[int(len(all_prices_sorted) * 0.75)]
    min_realistic_price = int(q75_price * 0.35) if q75_price > 5000 else 250
    filtered_candidates = [c for c in candidates if c[0] >= min_realistic_price]
    if len(filtered_candidates) < 3:
        filtered_candidates = candidates

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

        if len(cluster_prices) >= 3:
            ref_median = cluster_prices[len(cluster_prices) // 2]
            sample_size = len(cluster_prices)
        else:
            ref_median = global_median
            sample_size = len(filtered_candidates)

        if ref_median <= 0:
            continue

        if price_rub < ref_median:
            diff_rub = ref_median - price_rub
            diff_pct = round((diff_rub / ref_median) * 100, 1)
            if diff_pct > 65.0:
                continue
        else:
            diff_rub = 0
            diff_pct = 0.0

        rating = float(p.get("reviewRating") or p.get("rating") or 4.8)
        feedbacks = int(p.get("feedbacks") or 0)
        supplier = str(p.get("supplier") or p.get("brand") or "Продавец WB")

        items.append({
            "id": f"wb_{pid}",
            "title": full_title[:120],
            "source": "Wildberries",
            "city": city,
            "timeAgo": f"В наличии · Выборка: {sample_size} шт.",
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
                f"Розничное предложение Wildberries (артикул {pid}). "
                f"Сравнение выполнено внутри однородной группы товаров (выборка: {sample_size} поз., медиана группы: {ref_median:,} ₽, регион: {city})."
            ).replace(",", " "),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
        })

    items.sort(key=lambda x: x["profitPct"], reverse=True)
    result = items[:20]
    if result:
        cache_set_wb(cache_key, result)
    return result


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
    source_clean = source if source in ALLOWED_SOURCES else "all"
    q_clean = search_query.strip()
    target_query = q_clean if q_clean else DEFAULT_QUERIES_BY_CATEGORY.get(category, "смартфон Xiaomi")

    has_external_lots, ext_items = await fetch_db_external_lots(
        source=source_clean,
        city=city_clean,
        search_query=q_clean,
    )

    combined = list(ext_items)
    if source_clean in ("all", "Wildberries"):
        wb_items = await fetch_wb_clustered(query_text=target_query, city=city_clean)
        combined.extend(wb_items)

    # Если живой запрос вернул 0 результатов — используем проверенный каталог с фильтрацией
    if not combined:
        fallback = list(CURATED_SHOWCASE_CATALOG)
        if source_clean != "all":
            fallback = [x for x in fallback if x["source"].lower() == source_clean.lower()]
        if category != "all" and not q_clean:
            fallback = [x for x in fallback if x.get("category") == category]
        if q_clean:
            q_words = [w.lower() for w in q_clean.split() if len(w) >= 2]
            matched = [
                x for x in fallback
                if all(w in x["title"].lower() or w in x["desc"].lower() for w in q_words)
            ]
            fallback = matched if matched else fallback
        combined.extend(fallback)

    return has_external_lots, combined


# =====================================================================
# 7. ФОНОВЫЙ СКАНЕР МОНИТОРОВ
# =====================================================================
async def monitor_scanner_loop():
    await asyncio.sleep(12)
    while True:
        try:
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
                                [InlineKeyboardButton(text=f"🔥 Открыть на {lot['source']}", url=lot["url"])],
                                [InlineKeyboardButton(text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))],
                            ]
                        )
                        msg_text = (
                            f"🎯 <b>Совпадение по монитору «{m['title']}»!</b>\n\n"
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
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Ошибка цикла сканера мониторов: {e}")

        await asyncio.sleep(60)


# =====================================================================
# 8. TELEGRAM BOT (/start, ОПЛАТА STARS, АВТО-ВОЗВРАТ)
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
        "<b>DealHunter PRO</b> — радар выгодных предложений <b>Wildberries, Ozon и Avito</b> с кластерным анализом цен.\n\n"
        f"🎁 Новым пользователям доступно <b>{TRIAL_DAYS} дня PRO бесплатно</b> внутри приложения!\n"
        f"👥 Приглашайте друзей и получайте <b>+{REFERRAL_BONUS_DAYS} дня PRO</b> за каждого друга (до {MAX_REF_DAYS_PER_MONTH} дней в месяц).",
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
            error_message="Некорректные параметры счёта. Создайте новый счёт.",
        )
        return
    if not pg_pool:
        await bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Сервис обновляется. Попробуйте через минуту.",
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
                        await upsert_user_atomic(uid, message.from_user.first_name or "Охотник", "")
                        row = await conn.fetchrow("SELECT sub_until FROM dh_users WHERE user_id = $1 FOR UPDATE", uid)
                        cur_end = row["sub_until"] if row else None
                        base_time = cur_end if (cur_end and cur_end > now) else now
                        new_end = base_time + timedelta(days=30)
                        await conn.execute(
                            "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                            uid, new_end,
                        )
            await message.answer(
                "✅ <b>Оплата успешно подтверждена!</b>\n\n"
                "Вам начислено <b>+30 дней подписки DealHunter PRO</b>.\n"
                f"Чек транзакции: <code>{charge_id}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            last_error = e
            logger.error(f"Попытка {attempt + 1}/3 зачисления {charge_id}: {e}")
            await asyncio.sleep(1.5)

    logger.critical(f"СБОЙ ОПЛАТЫ {charge_id} для {uid}: {last_error}. Возврат Stars.")
    try:
        await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
        await message.answer(
            "⚠️ <b>Технический сбой при зачислении подписки.</b>\n\n"
            "Ваши Telegram Stars были <b>автоматически возвращены</b> на ваш баланс.\n"
            f"ID возврата: <code>{charge_id}</code>",
            parse_mode="HTML",
        )
    except Exception as ref_err:
        logger.critical(f"Не удалось выполнить авто-возврат {charge_id}: {ref_err}")


@dp.message(Command("refund"))
async def cmd_admin_refund(message: types.Message, command: Optional[CommandObject] = None):
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
            break
        except Exception as e:
            logger.warning(f"Перезапуск координатора через 5 сек: {e}")
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
    if bot:
        try:
            await bot.session.close()
        except Exception:
            pass
    if background_leader_conn and pg_pool:
        try:
            await pg_pool.release(background_leader_conn)
        except Exception:
            pass
    if pg_pool:
        try:
            await pg_pool.close()
        except Exception:
            pass


# =====================================================================
# 9. REST API ЭНДПОИНТЫ
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
    source: str = "all",
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
    city: str = Field(default="Москва", max_length=50)
    source: str = Field(default="all", max_length=30)
    minMargin: int = Field(default=15, ge=5, le=65)


@app.post("/api/monitors")
async def api_create_monitor(
    data: MonitorCreateReq,
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
    pool = require_db()
    uid = auth["uid"]
    user = await upsert_user_atomic(uid, auth["first_name"], auth["username"])
    # Автоактивация триала при создании первого монитора, чтобы пользователь не сталкивался с блоком
    if not user["subActive"] and not user["trialClaimed"]:
        user = await claim_trial_and_reward_referrer(uid)
    if not user["subActive"]:
        raise HTTPException(status_code=403, detail="Требуется активная подписка PRO")

    mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
    city_val = data.city if data.city in ALLOWED_CITIES else "Москва"
    source_val = data.source if data.source in ALLOWED_SOURCES else "all"

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid)
        if count >= 15:
            raise HTTPException(status_code=400, detail="Лимит: не более 15 мониторов")
        await conn.execute(
            """
            INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, found_date)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0, CURRENT_DATE)
            """,
            mid, uid, data.title.strip(), city_val, source_val, data.minMargin,
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
            description="Полный доступ к поиску карточек, анализу медианных цен и авто-мониторингу.",
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
                logger.warning(f"Отклонён URL ({item.url}): {err}")
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
# 10. ИНТЕРФЕЙС: LIQUID GLASS + ПЕРЕЛИВЫ + ZERO INLINE XSS
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_liquid_glass_ui():
    return """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DealHunter PRO</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/lucide@0.460.0/dist/umd/lucide.min.js"></script>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg-canvas: #E4E6EC; --bg-app: #F2F3F7;
      --glass-card: rgba(255, 255, 255, 0.86);
      --glass-pill: rgba(15, 17, 26, 0.06);
      --glass-dock: rgba(255, 255, 255, 0.88);
      --glass-border: rgba(255, 255, 255, 0.9);
      --glass-stroke: rgba(15, 17, 26, 0.08);
      --text-primary: #111216; --text-secondary: #5E626E; --text-muted: #969AA7;
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.28); --accent-soft: rgba(255, 90, 31, 0.13);
      --green: #18B259; --green-soft: rgba(24, 178, 89, 0.15);
    }
    [data-theme="dark"] {
      --bg-canvas: #040406; --bg-app: #0A0B0E;
      --glass-card: rgba(22, 24, 30, 0.78);
      --glass-pill: rgba(255, 255, 255, 0.07);
      --glass-dock: rgba(18, 20, 26, 0.88);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-stroke: rgba(255, 255, 255, 0.07);
      --text-primary: #F5F6FA; --text-secondary: #9499A6; --text-muted: #5B606E;
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.38); --accent-soft: rgba(255, 90, 31, 0.17);
      --green: #2AD16C; --green-soft: rgba(42, 209, 108, 0.16);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; user-select: none; }
    body {
      background: var(--bg-canvas); color: var(--text-primary);
      font-family: 'Inter', -apple-system, sans-serif;
      display: flex; justify-content: center; min-height: 100vh; overscroll-behavior-y: contain;
    }
    .t-num { font-family: 'Manrope', 'Inter', sans-serif; font-variant-numeric: tabular-nums; }
    .phone-shell {
      width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg-app);
      position: relative; overflow-x: hidden; padding-bottom: 112px;
    }
    .ambient-orb {
      position: fixed; width: 310px; height: 310px; border-radius: 50%;
      background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%);
      top: -95px; right: calc(50% - 210px); pointer-events: none; filter: blur(40px);
      animation: floatOrb 6s ease-in-out infinite alternate;
    }
    @keyframes floatOrb {
      0% { transform: translate(0, 0) scale(1); }
      100% { transform: translate(-18px, 14px) scale(1.08); }
    }
    .screen-content { position: relative; z-index: 1; padding: 14px 14px 26px; animation: fadeIn 0.22s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes spin { 100% { transform: rotate(360deg); } }
    .spin { animation: spin 0.8s linear infinite; }

    .ptr-bar {
      height: 0; overflow: hidden; display: flex; align-items: center; justify-content: center;
      gap: 8px; font-size: 12px; font-weight: 700; color: var(--accent);
      transition: height 0.18s ease; background: var(--glass-pill);
    }
    .glass-card {
      background: var(--glass-card); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--glass-border); border-radius: 24px; padding: 16px; margin-bottom: 12px;
      transition: transform 0.15s ease;
    }
    .glass-card:active { transform: scale(0.99); }
    .top-header {
      position: sticky; top: 0; z-index: 20; background: var(--glass-dock);
      backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
      border-bottom: 1px solid var(--glass-stroke); padding: 12px 16px;
    }

    .search-wrap { position: relative; margin-top: 10px; display: flex; gap: 6px; }
    .search-input {
      width: 100%; height: 40px; padding: 0 34px 0 36px; border-radius: 14px;
      background: var(--glass-pill); border: 1px solid var(--glass-border);
      color: var(--text-primary); font-family: inherit; font-size: 13px; outline: none;
    }
    .search-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
    .search-icon { position: absolute; left: 12px; top: 12px; color: var(--text-muted); pointer-events: none; }
    .search-clear {
      position: absolute; right: 88px; top: 10px; width: 20px; height: 20px;
      border-radius: 50%; border: none; background: var(--glass-stroke);
      color: var(--text-secondary); display: flex; align-items: center; justify-content: center; cursor: pointer;
    }

    .pills-scroll { display: flex; gap: 7px; overflow-x: auto; scrollbar-width: none; margin-top: 9px; }
    .pills-scroll::-webkit-scrollbar { display: none; }
    .cat-pill {
      height: 32px; padding: 0 13px; border-radius: 999px; border: 1px solid var(--glass-stroke);
      background: var(--glass-pill); color: var(--text-secondary); font-size: 12px; font-weight: 600;
      display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; cursor: pointer;
    }
    .cat-pill.active {
      background: linear-gradient(135deg, #FF5A1F, #FF7A45); color: #FFFFFF;
      border-color: transparent; box-shadow: 0 4px 14px -3px var(--accent-glow);
    }
    .spec-tag {
      display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 9px;
      border-radius: 9px; background: var(--glass-pill); border: 1px solid var(--glass-stroke);
      color: var(--text-secondary); font-size: 11px; font-weight: 500;
    }
    .profit-pill {
      display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px;
      border-radius: 12px; background: var(--accent-soft); color: var(--accent);
      font-weight: 800; font-size: 12px;
    }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      border: none; font-family: inherit; font-weight: 600; cursor: pointer; text-decoration: none;
      transition: transform 0.12s ease, opacity 0.12s ease;
    }
    .btn:active { transform: scale(0.97); }
    .btn-icon {
      width: 36px; height: 36px; border-radius: 12px; background: var(--glass-pill);
      border: 1px solid var(--glass-stroke); color: var(--text-primary);
    }
    .btn-pill { height: 36px; padding: 0 14px; border-radius: 12px; font-size: 12px; }
    .btn-large { height: 48px; width: 100%; border-radius: 16px; font-size: 14px; font-weight: 700; }
    .btn-accent {
      background: linear-gradient(135deg, #FF5A1F, #FF7A45); color: #FFFFFF;
      box-shadow: 0 8px 20px -6px var(--accent-glow);
    }
    .btn-stars-bot {
      background: linear-gradient(135deg, rgba(255, 193, 7, 0.16), rgba(255, 90, 31, 0.16));
      border: 1px solid rgba(255, 193, 7, 0.45); color: #FFC107;
    }
    .btn-glass { background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); }

    .floating-dock-wrap {
      position: fixed; bottom: 14px; left: 50%; transform: translateX(-50
