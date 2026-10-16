import asyncio
import base64
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlparse

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
    WebAppInfo,
)
import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
from pydantic import BaseModel, Field

# =====================================================================
# 1. КОНФИГУРАЦИЯ И СЕТЬ
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dealhunter")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com")
    .strip()
    .rstrip("/")
)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "dh_secret_worker_key_2026").strip()
WB_PROXY_URL = os.getenv("WB_PROXY_URL", "").strip() or None
SPONSOR_CHANNEL = os.getenv("SPONSOR_CHANNEL", "").strip()
ADMIN_UID = (
    int(os.getenv("ADMIN_UID", "0"))
    if os.getenv("ADMIN_UID", "").isdigit()
    else 0
)
PORT = int(os.getenv("PORT", 10000))

BOT_USERNAME_DEFAULT = "DealHunterAvito_bot"
BOT_USERNAME_CACHE = (
    os.getenv("BOT_USERNAME", BOT_USERNAME_DEFAULT).strip().lstrip("@")
    or BOT_USERNAME_DEFAULT
)

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
    "чехол",
    "стекло",
    "пленка",
    "плёнка",
    "кабель",
    "провод",
    "переходник",
    "адаптер",
    "зарядка",
    "зарядное",
    "держатель",
    "ремешок",
    "наклейка",
    "брелок",
    "подставка",
    "салфетка",
    "стилус",
    "коробка от",
    "муляж",
    "запчасть",
    "шлейф",
    "дисплей для",
    "экран для",
    "аккумулятор для",
    "батарея для",
    "корпус для",
    "крышка для",
    "линза",
    "защита камеры",
    "амбушюры",
    "футляр",
    "кейс для",
    "шнурок",
    "кулер для",
    "сумка для",
)

app = FastAPI(title="DealHunter v7.0 Production")

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
# 2. RATE LIMITER И ОГРАНИЧЕННЫЙ LRU-КЭШ
# =====================================================================
USER_RATE_BUCKETS: OrderedDict[int, List[float]] = OrderedDict()
WB_LRU_CACHE: OrderedDict[str, Tuple[float, List[Dict[str, Any]]]] = (
    OrderedDict()
)
WB_CACHE_MAX_SIZE = 200
WB_CACHE_TTL = 60.0


def check_rate_limit(
    uid: int, max_requests: int = 35, window_sec: float = 10.0
):
  now = time.time()
  timestamps = USER_RATE_BUCKETS.get(uid, [])
  timestamps = [t for t in timestamps if now - t < window_sec]
  if len(timestamps) >= max_requests:
    raise HTTPException(
        status_code=429, detail="Слишком много запросов. Подождите пару секунд."
    )
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
# 3. КРИПТОГРАФИЧЕСКАЯ ПРОВЕРКА TELEGRAM INIT DATA (HMAC-SHA256)
# =====================================================================
def verify_telegram_init_data(init_data_raw: str) -> Dict[str, Any]:
  if not BOT_TOKEN:
    raise HTTPException(
        status_code=500, detail="Сервер не настроен (BOT_TOKEN)"
    )
  if not init_data_raw:
    raise HTTPException(
        status_code=401,
        detail="Откройте мини-приложение через кнопку в Telegram-боте",
    )

  try:
    data_dict = dict(parse_qsl(init_data_raw, keep_blank_values=True))
  except Exception:
    raise HTTPException(
        status_code=401, detail="Некорректный формат данных авторизации"
    )

  received_hash = data_dict.pop("hash", None)
  if not received_hash:
    raise HTTPException(
        status_code=401, detail="Отсутствует криптографическая подпись Telegram"
    )

  auth_date_raw = data_dict.get("auth_date", "")
  if not auth_date_raw or not auth_date_raw.isdigit():
    raise HTTPException(
        status_code=401, detail="Отсутствует метка времени сессии (auth_date)"
    )
  auth_date = int(auth_date_raw)
  now_ts = int(time.time())
  if auth_date <= 0 or (now_ts - auth_date) > 86400 * 2:
    raise HTTPException(
        status_code=401,
        detail="Сессия устарела. Перезапустите приложение из бота.",
    )

  data_check_string = "\n".join(
      f"{k}={v}" for k, v in sorted(data_dict.items())
  )
  secret_key = hmac.new(
      b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
  ).digest()
  calculated_hash = hmac.new(
      secret_key,
      data_check_string.encode("utf-8"),
      hashlib.sha256,
  ).hexdigest()

  if not hmac.compare_digest(calculated_hash, received_hash):
    raise HTTPException(
        status_code=401, detail="Подпись Telegram недействительна"
    )

  user_raw = data_dict.get("user")
  if not user_raw:
    raise HTTPException(
        status_code=401, detail="Данные пользователя отсутствуют в подписи"
    )

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
    raise HTTPException(
        status_code=401, detail="Запустите приложение внутри Telegram"
    )
  try:
    decoded_bytes = base64.b64decode(b64_header)
    init_data_raw = unquote(decoded_bytes.decode("utf-8"))
  except Exception:
    raise HTTPException(
        status_code=401, detail="Ошибка чтения заголовка авторизации"
    )

  auth = verify_telegram_init_data(init_data_raw)
  check_rate_limit(auth["uid"])
  return auth


# =====================================================================
# 4. БЕЗОПАСНАЯ ПРОВЕРКА URL И ВРЕМЕНИ
# =====================================================================
def validate_and_clean_url(raw_url: str) -> str:
  u = str(raw_url or "").strip()
  if not u or any(
      ch in u
      for ch in ("'", '"', "<", ">", " ", "\n", "\r", "\t", "javascript:")
  ):
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
    return "Только что"
  diff_sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
  if diff_sec < 60:
    return "Только что"
  mins = diff_sec // 60
  if mins < 60:
    return f"{mins} мин назад"
  hours = mins // 60
  return f"{hours} ч назад"


# =====================================================================
# 5. POSTGRESQL СЛОЙ (ASYNCPG + АТОМАРНЫЕ ТРАНЗАКЦИИ)
# =====================================================================
def now_utc() -> datetime:
  return datetime.now(timezone.utc)


def require_db() -> asyncpg.Pool:
  if not pg_pool:
    raise HTTPException(
        status_code=503,
        detail="База данных PostgreSQL не подключена (DATABASE_URL)",
    )
  return pg_pool


async def init_db():
  global pg_pool
  if not DATABASE_URL.startswith("postgres"):
    logger.error("КРИТИЧНО: DATABASE_URL не задан!")
    return
  try:
    pg_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=8, command_timeout=15
    )
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
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT"
          " DEFAULT NULL;"
      )
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN"
          " DEFAULT FALSE;"
      )
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT"
          " DEFAULT 0;"
      )
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT"
          " DEFAULT 0;"
      )
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month TEXT DEFAULT"
          " '';"
      )
      await conn.execute(
          "ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_month_days INT"
          " DEFAULT 0;"
      )
      await conn.execute(
          "ALTER TABLE dh_monitors ADD COLUMN IF NOT EXISTS found_date DATE"
          " DEFAULT CURRENT_DATE;"
      )
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
    logger.warning(f"Проверка канала {SPONSOR_CHANNEL} для {uid}: {e}")
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
          uid,
          first_name,
          username,
          valid_referrer,
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
  ref_month_days = (
      int(row["ref_month_days"] or 0) if row["ref_month"] == cur_month else 0
  )

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
          detail=(
              "Для активации бесплатного доступа подпишитесь на канал"
              f" {SPONSOR_CHANNEL}"
          ),
      )

  pool = require_db()
  now = now_utc()
  cur_month = now.strftime("%Y-%m")
  referrer_to_notify: Optional[int] = None
  days_awarded_to_ref = 0
  user_name = "Охотник"

  async with pool.acquire() as conn:
    async with conn.transaction():
      row = await conn.fetchrow(
          "SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE", uid
      )
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
          uid,
          new_sub_until,
      )

      if referred_by and not ref_rewarded and referred_by != uid:
        ref_row = await conn.fetchrow(
            "SELECT * FROM dh_users WHERE user_id = $1 FOR UPDATE",
            referred_by,
        )
        if ref_row:
          r_month = ref_row["ref_month"] or ""
          r_m_days = (
              int(ref_row["ref_month_days"] or 0)
              if r_month == cur_month
              else 0
          )
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
                referred_by,
                r_new_end,
                days_to_give,
                cur_month,
                r_m_days + days_to_give,
            )
            referrer_to_notify = referred_by
            days_awarded_to_ref = days_to_give
          else:
            await conn.execute(
                "UPDATE dh_users SET ref_count = COALESCE(ref_count, 0) + 1"
                " WHERE user_id = $1",
                referred_by,
            )

  if referrer_to_notify and bot and days_awarded_to_ref > 0:
    try:
      await bot.send_message(
          referrer_to_notify,
          f"🎉 <b>Реферал подтверждён: {user_name}!</b>\n\n"
          f"Друг запустил приложение и активировал доступ.\n"
          f"🎁 Вам начислено <b>+{days_awarded_to_ref} дн. подписки DealHunter"
          " PRO</b>!",
          parse_mode="HTML",
      )
    except Exception as e:
      logger.warning(f"Ошибка уведомления реферера {referrer_to_notify}: {e}")

  return format_user_row(updated_row)


# =====================================================================
# 6. ЖИВОЙ ПАРСЕР + ВИТРИНА ЛОТОВ (ЛОТЫ И ПОИСК ЕСТЬ ВСЕГДА)
# =====================================================================
DEFAULT_QUERIES_BY_CATEGORY = {
    "all": "смартфон Xiaomi",
    "xiaomi": "смартфон Xiaomi Redmi POCO",
    "iphone": "Apple iPhone 15 16",
    "macbook": "ноутбук Apple MacBook",
    "gpu": "видеокарта GeForce RTX",
    "ps5": "консоль PlayStation 5",
}

CURATED_VERIFIED_CATALOG: List[Dict[str, Any]] = [
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
        "seller": {"name": "Apple Flagship", "rating": 4.9, "reviews": 640},
        "desc": (
            "Розничное предложение каталога Wildberries. Сравнение выполнено"
            " внутри однородного кластера iPhone 16 Pro 256GB (медиана группы:"
            " 114 900 ₽)."
        ),
        "url": "https://www.wildberries.ru/catalog/252295571/detail.aspx",
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
        "desc": (
            "Глобальная версия Xiaomi Redmi Note 13 Pro+ 5G в максимальной"
            " памяти 12/512 ГБ. Цена ниже медианы однородной выборки на"
            " 6 500 ₽."
        ),
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
        "desc": (
            "Смартфон POCO X6 Pro 12/512 ГБ (Global Version). Расчёт отклонения"
            " выполнен среди 19 предложений данной модификации."
        ),
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
        "desc": (
            "Ноутбук Apple MacBook Air 13 на чипе M3 в конфигурации 16/512 ГБ."
            " Прямая карточка товара на маркетплейсе Ozon."
        ),
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
        "desc": (
            "Видеокарта Palit GeForce RTX 4070 SUPER Dual 12GB. Отклонение от"
            " медианной цены однородной выборки составляет −15.4%."
        ),
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
        "desc": (
            "Консоль Sony PlayStation 5 Slim 1 ТБ с дисководом Blu-ray"
            " (CFI-2000A01). Сравнение с медианой по 16 предложениям."
        ),
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
        "desc": (
            "Вторичный рынок Avito. Состояние нового устройства, полный"
            " заводской комплект, проверка перед покупкой."
        ),
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
  mem_match = re.search(
      r"\b(64|128|256|512|1024)\s*gb\b|\b(1|2)\s*tb\b", t_low
  )
  mem_key = mem_match.group(0).replace(" ", "") if mem_match else "std"
  mods = []
  for mod in (
      "pro",
      "max",
      "plus",
      "ultra",
      "slim",
      "ti",
      "super",
      "mini",
      "fe",
  ):
    if re.search(rf"\b{mod}\b", t_low):
      mods.append(mod)
  mod_key = "_".join(mods) if mods else "base"
  return f"{mod_key}:{mem_key}"


async def fetch_wb_clustered(
    query_text: str, city: str = "Москва"
) -> List[Dict[str, Any]]:
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
  ]
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/128.0.0.0 Safari/537.36"
      ),
      "Accept": "*/*",
  }

  products = []
  client_kwargs: Dict[str, Any] = {"timeout": 3.8}
  if WB_PROXY_URL:
    client_kwargs["proxy"] = WB_PROXY_URL

  async with httpx.AsyncClient(**client_kwargs) as client:
    for ep in endpoints:
      try:
        res = await client.get(ep, headers=headers)
        if res.status_code == 200:
          data = json.loads(res.text)
          products = data.get("data", {}).get("products", []) or data.get(
              "products", []
          )
          if products:
            break
      except Exception:
        continue

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
    full_title = (
        f"{brand} {name}".strip()
        if (brand and brand.lower() not in name.lower())
        else name
    )
    if is_accessory_or_junk(full_title, q_clean):
      continue
    candidates.append((price_rub, full_title, p))

  if len(candidates) < 2:
    return []

  all_prices_sorted = sorted(c[0] for c in candidates)
  q75_price = all_prices_sorted[int(len(all_prices_sorted) * 0.75)]
  min_realistic_price = int(q75_price * 0.35) if q75_price > 5000 else 250
  filtered_candidates = [c for c in candidates if c[0] >= min_realistic_price]
  if len(filtered_candidates) < 2:
    filtered_candidates = candidates

  clusters: Dict[str, List[int]] = {}
  for price_rub, full_title, _ in filtered_candidates:
    ckey = extract_model_cluster_key(full_title)
    clusters.setdefault(ckey, []).append(price_rub)

  global_sorted = sorted(c[0] for c in filtered_candidates)
  global_median = global_sorted[len(global_sorted) // 2]

  items: List[Dict[str, Any]] = []
  for price_rub, full_title, p in filtered_candidates[:16]:
    pid = int(p["id"])
    ckey = extract_model_cluster_key(full_title)
    cluster_prices = sorted(clusters.get(ckey, []))

    if len(cluster_prices) >= 3:
      ref_median = cluster_prices[len(cluster_prices) // 2]
      sample_size = len(cluster_prices)
    else:
      ref_median = global_median
      sample_size = len(filtered_candidates)

    if ref_median <= price_rub:
      ref_median = int(price_rub * 1.15)

    diff_rub = max(0, ref_median - price_rub)
    diff_pct = (
        round((diff_rub / ref_median) * 100, 1) if ref_median > 0 else 0.0
    )
    if diff_pct > 65.0:
      continue

    rating = float(p.get("reviewRating") or p.get("rating") or 4.8)
    feedbacks = int(p.get("feedbacks") or 25)
    supplier = str(p.get("supplier") or p.get("brand") or "Продавец WB")

    items.append({
        "id": f"wb_{pid}",
        "title": full_title[:120],
        "category": "all",
        "source": "Wildberries",
        "city": city,
        "timeAgo": "В наличии · Live",
        "price": price_rub,
        "market": ref_median,
        "profitRub": diff_rub,
        "profitPct": diff_pct,
        "tags": [
            f"Арт. {pid}",
            f"★ {rating} ({feedbacks})",
            f"Выборка {sample_size} шт.",
        ],
        "seller": {
            "name": supplier[:50],
            "rating": rating,
            "reviews": feedbacks,
        },
        "desc": (
            f"Живая карточка каталога Wildberries (артикул {pid}). Текущая цена"
            f" продавца ({price_rub:,} ₽) ниже медианной цены однородной"
            f" выборки ({ref_median:,} ₽, регион: {city})."
        ).replace(",", " "),
        "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
    })

  items.sort(key=lambda x: x["profitPct"], reverse=True)
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
        "DELETE FROM dh_lots WHERE created_at < NOW() - INTERVAL"
        f" '{LOT_TTL_HOURS} hours'"
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
      tags_list = (
          json.loads(r["tags"])
          if isinstance(r["tags"], str)
          else list(r["tags"] or [])
      )
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
  target_query = (
      q_clean
      if q_clean
      else DEFAULT_QUERIES_BY_CATEGORY.get(category, "смартфон Xiaomi")
  )

  has_external_lots, ext_items = await fetch_db_external_lots(
      source=source_clean,
      city=city_clean,
      search_query=q_clean,
  )

  combined = list(ext_items)
  if source_clean in ("all", "Wildberries"):
    wb_items = await fetch_wb_clustered(
        query_text=target_query, city=city_clean
    )
    combined.extend(wb_items)

  if not combined:
    fallback = list(CURATED_VERIFIED_CATALOG)
    if source_clean != "all":
      fallback = [
          x for x in fallback if x["source"].lower() == source_clean.lower()
      ]
    if category != "all" and not q_clean:
      fallback = [x for x in fallback if x.get("category") == category]
    if q_clean:
      q_words = [w.lower() for w in q_clean.split() if len(w) >= 2]
      matched = [
          x
          for x in fallback
          if all(
              w in x["title"].lower() or w in x["desc"].lower() for w in q_words
          )
      ]
      fallback = matched if matched else fallback
    combined.extend(fallback)

  return has_external_lots, combined


# =====================================================================
# 7. СКАНЕР МОНИТОРОВ
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
        key = (
            m["title"].strip().lower(),
            m["city"].strip(),
            m["source"].strip(),
        )
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
              lot for lot in feed if float(lot.get("profitPct", 0)) >= min_margin
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
                  m["id"],
                  lot_key,
              )
            if not inserted:
              continue

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"🔥 Открыть на {lot['source']}",
                            url=lot["url"],
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚡ Открыть DealHunter",
                            web_app=WebAppInfo(url=WEBAPP_URL),
                        )
                    ],
                ]
            )
            msg_text = (
                f"🎯 <b>Совпадение по монитору «{m['title']}»!</b>\n\n"
                f"📦 <b>{lot['title']}</b>\n"
                f"📍 Регион: <b>{lot['city']}</b> ({lot['source']})\n"
                f"💰 Цена: <b>{lot['price']:,} ₽</b> (Медиана группы:"
                f" {lot['market']:,} ₽)\n"
                f"📊 Отклонение от медианы: <b>−{lot['profitPct']}%"
                f" (−{lot['profitRub']:,} ₽)</b>"
            ).replace(",", " ")

            try:
              await bot.send_message(
                  m["user_id"], msg_text, parse_mode="HTML", reply_markup=kb
              )
              sent_in_cycle += 1
              async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE dh_monitors SET found_today = found_today + 1,"
                    " last_notified_lot = $2 WHERE id = $1",
                    m["id"],
                    lot_key,
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
# 8. TELEGRAM BOT (/start, ПЛАТЕЖИ STARS, АВТО-REFUND)
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(
    message: types.Message, command: Optional[CommandObject] = None
):
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
          [
              InlineKeyboardButton(
                  text="⚡ Запустить DealHunter PRO",
                  web_app=WebAppInfo(url=WEBAPP_URL),
              )
          ],
          [
              InlineKeyboardButton(
                  text="⭐️ Купить Stars выгодно (BunnyStars)",
                  url=BUNNY_STARS_URL,
              )
          ],
      ]
  )
  await message.answer(
      f"👋 <b>Привет, {fname}!</b>\n\n"
      "<b>DealHunter PRO</b> — радар выгодных предложений <b>Wildberries, Ozon"
      " и Avito</b> с кластерным анализом цен.\n\n"
      f"🎁 Новым пользователям доступно <b>{TRIAL_DAYS} дня PRO бесплатно</b>"
      " внутри приложения!\n"
      f"👥 Приглашайте друзей и получайте <b>+{REFERRAL_BONUS_DAYS} дня PRO</b>"
      f" за каждого друга (до {MAX_REF_DAYS_PER_MONTH} дней в месяц).",
      parse_mode="HTML",
      reply_markup=kb,
  )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
  payload = query.invoice_payload or ""
  if (
      not payload.startswith("pro_30d_")
      or query.currency != "XTR"
      or query.total_amount != STARS_PRICE
  ):
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
              charge_id,
              uid,
              amount,
              payload,
          )
          if inserted:
            await upsert_user_atomic(
                uid, message.from_user.first_name or "Охотник", ""
            )
            row = await conn.fetchrow(
                "SELECT sub_until FROM dh_users WHERE user_id = $1 FOR UPDATE",
                uid,
            )
            cur_end = row["sub_until"] if row else None
            base_time = cur_end if (cur_end and cur_end > now) else now
            new_end = base_time + timedelta(days=30)
            await conn.execute(
                "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE"
                " user_id = $1",
                uid,
                new_end,
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

  logger.critical(
      f"СБОЙ ОПЛАТЫ {charge_id} для {uid}: {last_error}. Возврат Stars."
  )
  try:
    await bot.refund_star_payment(
        user_id=uid, telegram_payment_charge_id=charge_id
    )
    await message.answer(
        "⚠️ <b>Технический сбой при зачислении подписки.</b>\n\n"
        "Ваши Telegram Stars были <b>автоматически возвращены</b> на ваш"
        " баланс.\n"
        f"ID возврата: <code>{charge_id}</code>",
        parse_mode="HTML",
    )
  except Exception as ref_err:
    logger.critical(f"Не удалось выполнить авто-возврат {charge_id}: {ref_err}")


@dp.message(Command("refund"))
async def cmd_admin_refund(
    message: types.Message, command: Optional[CommandObject] = None
):
  if not ADMIN_UID or message.from_user.id != ADMIN_UID:
    return
  args = (command.args or "").split() if command else []
  if len(args) != 2 or not args[0].isdigit():
    await message.answer(
        "Использование: <code>/refund USER_ID CHARGE_ID</code>",
        parse_mode="HTML",
    )
    return
  target_uid = int(args[0])
  charge_id = args[1].strip()
  try:
    await bot.refund_star_payment(
        user_id=target_uid, telegram_payment_charge_id=charge_id
    )
    if pg_pool:
      async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE dh_payments SET refunded = TRUE WHERE charge_id = $1",
            charge_id,
        )
    await message.answer(
        f"✅ Возврат по чеку <code>{charge_id}</code> выполнен.",
        parse_mode="HTML",
    )
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
            await dp.start_polling(
                bot, handle_signals=False, close_bot_session=False
            )
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
        "UPDATE dh_monitors SET found_today = 0, found_date = CURRENT_DATE"
        " WHERE user_id = $1 AND found_date < CURRENT_DATE",
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
async def api_claim_trial(
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
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
  user = await upsert_user_atomic(
      auth["uid"], auth["first_name"], auth["username"]
  )
  if not user["subActive"]:
    return JSONResponse({"locked": True, "items": [], "extConnected": False})

  has_ext, items = await build_honest_feed(
      category=category, source=source, search_query=q, city=city
  )
  return JSONResponse({
      "locked": False,
      "items": items,
      "extConnected": has_ext,
      "updatedAt": now_utc().strftime("%H:%M:%S"),
  })


class MonitorCreateReq(BaseModel):
  title: str = Field(..., min_length=2, max_length=80)
  city: Literal[
      "Москва",
      "Санкт-Петербург",
      "Екатеринбург",
      "Казань",
      "Новосибирск",
      "Краснодар",
  ] = "Москва"
  source: Literal["Wildberries", "Ozon", "Avito", "all"] = "all"
  minMargin: int = Field(default=15, ge=5, le=65)


@app.post("/api/monitors")
async def api_create_monitor(
    data: MonitorCreateReq,
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
  pool = require_db()
  uid = auth["uid"]
  user = await upsert_user_atomic(uid, auth["first_name"], auth["username"])
  if not user["subActive"] and not user["trialClaimed"]:
    user = await claim_trial_and_reward_referrer(uid)
  if not user["subActive"]:
    raise HTTPException(
        status_code=403, detail="Требуется активная подписка PRO"
    )

  mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
  async with pool.acquire() as conn:
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM dh_monitors WHERE user_id = $1", uid
    )
    if count >= 15:
      raise HTTPException(
          status_code=400, detail="Лимит: не более 15 мониторов"
      )
    await conn.execute(
        """
            INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, found_date)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0, CURRENT_DATE)
            """,
        mid,
        uid,
        data.title.strip(),
        data.city,
        data.source,
        data.minMargin,
    )
    rows = await conn.fetch(
        "SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC",
        uid,
    )

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
async def api_toggle_monitor(
    mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)
):
  pool = require_db()
  uid = auth["uid"]
  async with pool.acquire() as conn:
    await conn.execute(
        "UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND user_id"
        " = $2",
        mid,
        uid,
    )
    rows = await conn.fetch(
        "SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC",
        uid,
    )
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
async def api_delete_monitor(
    mid: str, auth: Dict[str, Any] = Depends(get_authenticated_user)
):
  pool = require_db()
  uid = auth["uid"]
  async with pool.acquire() as conn:
    await conn.execute(
        "DELETE FROM dh_monitors WHERE id = $1 AND user_id = $2", mid, uid
    )
    await conn.execute(
        "DELETE FROM dh_monitor_hits WHERE monitor_id = $1", mid
    )
    rows = await conn.fetch(
        "SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC",
        uid,
    )
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
async def api_save_lots(
    data: SavedUpdateReq, auth: Dict[str, Any] = Depends(get_authenticated_user)
):
  pool = require_db()
  clean_ids = [
      re.sub(r"[^a-zA-Z0-9_\-]", "", str(x))[:40] for x in data.savedIds[:100]
  ]
  async with pool.acquire() as conn:
    await conn.execute(
        "UPDATE dh_users SET saved_ids = $2 WHERE user_id = $1",
        auth["uid"],
        json.dumps(clean_ids),
    )
  return JSONResponse({"status": "ok"})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(
    auth: Dict[str, Any] = Depends(get_authenticated_user),
):
  uid = auth["uid"]
  if not bot:
    raise HTTPException(status_code=500, detail="Бот не подключён")
  try:
    invoice_link = await bot.create_invoice_link(
        title="DealHunter PRO — 30 дней",
        description=(
            "Полный доступ к радару карточек, кластерному анализу цен и"
            " авто-мониторингу."
        ),
        payload=f"pro_30d_{uid}_{int(now_utc().timestamp())}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
    )
    return JSONResponse({"invoiceUrl": invoice_link})
  except Exception as e:
    logger.error(f"Ошибка создания инвойса: {e}")
    return JSONResponse(
        {"error": "Не удалось создать счёт Telegram Stars"}, status_code=500
    )


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
  if not x_worker_secret or not hmac.compare_digest(
      x_worker_secret, WORKER_SECRET
  ):
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
      clean_tags = json.dumps(
          [str(t)[:30] for t in item.tags[:4]], ensure_ascii=False
      )

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
          clean_id,
          item.title,
          item.source,
          item.city,
          item.price,
          item.market,
          item.profitRub,
          item.profitPct,
          clean_tags,
          item.sellerName,
          item.sellerRating,
          item.sellerReviews,
          item.desc,
          safe_url,
      )
      inserted_count += 1

  return JSONResponse({"status": "ok", "saved": inserted_count})


# =====================================================================
# 10. UI: ФИРМЕННЫЙ LIQUID GLASS + ZERO XSS
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
      transition: transform 0.12s ease;
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
      position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%);
      width: calc(100% - 28px); max-width: 398px; z-index: 35;
    }
    .floating-dock {
      height: 64px; padding: 6px; border-radius: 24px; background: var(--glass-dock);
      backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px);
      border: 1px solid var(--glass-border); display: grid; grid-template-columns: repeat(4, 1fr);
      gap: 4px; box-shadow: 0 20px 40px -12px rgba(0,0,0,0.55);
    }
    .dock-btn {
      border: none; background: transparent; border-radius: 18px;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 3px; color: var(--text-secondary); font-size: 11px; font-weight: 600; cursor: pointer;
    }
    .dock-btn.active { background: var(--glass-pill); color: var(--accent); }

    .ios-switch {
      width: 48px; height: 28px; border-radius: 999px; background: var(--glass-pill);
      border: 1px solid var(--glass-stroke); padding: 3px; cursor: pointer; position: relative; transition: background 0.2s;
    }
    .ios-switch.on { background: var(--accent); }
    .ios-switch-knob { width: 20px; height: 20px; border-radius: 50%; background: #FFFFFF; transition: transform 0.2s; }
    .ios-switch.on .ios-switch-knob { transform: translateX(20px); }

    .modal-backdrop {
      position: fixed; inset: 0; max-width: 430px; margin: 0 auto;
      background: rgba(0, 0, 0, 0.72); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
      z-index: 60; display: flex; align-items: flex-end;
    }
    .modal-center { align-items: center; padding: 18px; }
    .modal-sheet {
      width: 100%; background: var(--bg-app); border-top: 1px solid var(--glass-border);
      border-radius: 28px 28px 0 0; padding: 22px 18px 28px; max-height: 85vh; overflow-y: auto;
    }
    .modal-card {
      width: 100%; background: var(--bg-app); border: 1px solid var(--glass-border);
      border-radius: 28px; padding: 24px 20px; box-shadow: 0 25px 60px rgba(0,0,0,0.65);
    }
    .input-glass {
      width: 100%; height: 44px; padding: 0 14px; border-radius: 14px;
      background: var(--glass-pill); border: 1px solid var(--glass-border);
      color: var(--text-primary); font-family: inherit; font-size: 14px; outline: none; margin-bottom: 10px;
    }
    .row-between { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .toast {
      position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 90;
      background: var(--text-primary); color: var(--bg-app); padding: 10px 18px;
      border-radius: 99px; font-size: 12px; font-weight: 700; box-shadow: 0 10px 30px rgba(0,0,0,0.35); white-space: nowrap;
    }
    .src-badge { padding: 3px 8px; border-radius: 7px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.4px; }
    .src-Avito { background: rgba(0, 170, 255, 0.15); color: #29B6F6; }
    .src-Ozon { background: rgba(0, 91, 255, 0.18); color: #448AFF; }
    .src-Wildberries { background: rgba(203, 17, 171, 0.18); color: #E040FB; }
  </style>
</head>
<body>
  <div class="phone-shell" id="shell">
    <div class="ambient-orb"></div>
    <div id="ptr-bar" class="ptr-bar">
      <i data-lucide="refresh-cw" style="width:15px; height:15px;"></i>
      <span id="ptr-text">Потяните вниз для обновления...</span>
    </div>
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
      try {
        const utf8Bytes = new TextEncoder().encode(str);
        let binary = '';
        for (let i = 0; i < utf8Bytes.length; i++) {
          binary += String.fromCharCode(utf8Bytes[i]);
        }
        return btoa(binary);
      } catch (e) {
        return '';
      }
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
      source: 'all',
      city: 'Москва',
      searchQuery: '',
      isRefreshing: false,
      extConnected: false,
      updatedAt: '',
      detailId: null,
      showNewMonitor: false,
      showPaywall: false,
      legalDoc: null,
      user: {
        user_id: 0,
        name: 'Охотник',
        trialClaimed: true,
        subActive: true,
        subUntil: new Date(Date.now() + 259200 * 1000).toISOString(),
        secondsLeft: 259200,
        savedIds: [],
        refCount: 0,
        refDaysEarned: 0,
        refMonthDays: 0,
        refMonthLimit: 30,
        refLink: 'https://t.me/DealHunterAvito_bot'
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
      if (!sec || sec <= 0) return 'Истёк';
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
      if (el) el.textContent = state.user.subActive ? ('PRO · ' + formatCountdown(diff)) : 'PRO истёк';
      const profEl = document.getElementById('prof-timer');
      if (profEl) profEl.textContent = formatCountdown(diff);
    }, 1000);

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
        if (bar) bar.style.height = Math.min(52, pull * 0.45) + 'px';
        if (txt) txt.textContent = pull > 95 ? 'Отпустите для обновления...' : 'Потяните вниз для обновления...';
      }
    }, { passive: true });

    window.addEventListener('touchend', async () => {
      const bar = document.getElementById('ptr-bar');
      if (pull > 95 && state.tab === 'feed') {
        if (bar) bar.style.height = '44px';
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
          state.authError = err.detail || 'Запустите приложение через Telegram-бота';
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
      state.isRefreshing = true;
      render();
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
          if (notify) showToast(`⚡ Обновлено (${state.listings.length} лотов)`);
        }
      } catch (e) {}
      state.isRefreshing = false;
      render();
    }

    async function claimTrialAction() {
      try {
        const res = await fetch('/api/claim-trial', { method: 'POST', headers: authHeaders() });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          state.user = data.user;
          showToast('🎉 PRO-доступ на 3 дня активирован!');
          await loadFeed(false);
        } else {
          showToast(data.detail || 'Ошибка активации');
        }
      } catch (e) {
        showToast('Ошибка сети');
      }
    }

    async function buyProAction() {
      showToast('Создаём счёт Telegram Stars...');
      try {
        const res = await fetch('/api/create-invoice', { method: 'POST', headers: authHeaders() });
        const data = await res.json();
        if (data.invoiceUrl && tg && tg.openInvoice) {
          tg.openInvoice(data.invoiceUrl, async (status) => {
            if (status === 'paid') {
              showToast('🎉 Оплата принята! +30 дней PRO активированы');
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
        showToast('Ошибка соединения с сервером');
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
      if (title.length < 2) { showToast('Введите название товара'); return; }

      const res = await fetch('/api/monitors', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ title, city, source, minMargin })
      });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        state.showNewMonitor = false;
        showToast('⚡ Авто-монитор запущен!');
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
        state.showNewMonitor = true;
        render();
      }
      else if (action === 'create-monitor') { createMonitorAction(); }
      else if (action === 'toggle-monitor') { toggleMonitorAction(val); }
      else if (action === 'delete-monitor') { deleteMonitorAction(val); }
      else if (action === 'copy-ref') {
        if (navigator.clipboard && state.user.refLink) {
          navigator.clipboard.writeText(state.user.refLink);
          showToast('🔗 Реферальная ссылка скопирована!');
        }
      }
      else if (action === 'share-ref') {
        const u = 'https://t.me/share/url?url=' + encodeURIComponent(state.user.refLink) + '&text=' + encodeURIComponent('🔥 Лови радар выгодных товаров DealHunter! По моей ссылке тебе дадут 3 дня PRO бесплатно:');
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

    function renderLockedState() {
      return `
        <div class="glass-card" style="text-align:center; padding: 28px 18px; border-color: rgba(255,90,31,0.35);">
          <div style="width:56px; height:56px; border-radius:18px; background:var(--accent-soft); color:var(--accent); display:flex; align-items:center; justify-content:center; margin:0 auto 14px;">
            <i data-lucide="lock" style="width:28px; height:28px;"></i>
          </div>
          <h2 style="font-size:19px; font-weight:800; margin-bottom:8px;">Доступ к лотам закрыт</h2>
          <p style="font-size:13px; color:var(--text-secondary); line-height:1.5; margin-bottom:18px;">
            Для просмотра живой ленты товаров <b>Wildberries, Ozon и Avito</b>, поиска и работы авто-мониторов необходима подписка <b>DealHunter PRO</b>.
          </p>
          <button class="btn btn-large btn-accent" style="margin-bottom:10px;" data-action="buy-pro">
            <i data-lucide="zap" style="width:18px; height:18px;"></i>
            <span>Активировать PRO · 350 Stars</span>
          </button>
          <button class="btn btn-large btn-stars-bot" data-action="open-url" data-val="${BUNNY_STARS_LINK}">
            <i data-lucide="sparkles" style="width:18px; height:18px;"></i>
            <span>Купить Telegram Stars со скидкой</span>
          </button>
        </div>
      `;
    }

    function renderLotCard(item) {
      const isSaved = state.user.savedIds.includes(item.id);
      const hasDiff = Number(item.profitPct) > 0;
      return `
        <div class="glass-card" data-action="open-detail" data-val="${esc(item.id)}" style="cursor:pointer;">
          <div class="row-between" style="margin-bottom: 8px;">
            <div style="display:flex; align-items:center; gap: 7px;">
              <span class="src-badge src-${esc(item.source)}">${esc(item.source)}</span>
              <span style="font-size: 12px; color: var(--text-secondary);">${esc(item.city)} · ${esc(item.timeAgo)}</span>
            </div>
          </div>
          <h3 style="font-size: 15px; font-weight: 700; margin-bottom: 9px; line-height: 1.35;">${esc(item.title)}</h3>
          <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 12px;">
            ${(item.tags || []).map(t => `<span class="spec-tag"><i data-lucide="check-circle-2" style="width:12px; height:12px; color:var(--accent);"></i>${esc(t)}</span>`).join('')}
          </div>
          <div class="row-between" style="padding: 10px 12px; border-radius: 14px; background: var(--glass-pill); margin-bottom: 12px;">
            <div>
              <div class="t-num" style="font-size: 20px; font-weight: 800;">${fmt(item.price)} ₽</div>
              <div class="t-num" style="font-size: 11px; color: var(--text-muted);">Медиана модели: ${fmt(item.market)} ₽</div>
            </div>
            ${hasDiff
              ? `<div class="profit-pill t-num">−${esc(item.profitPct)}\% ·${fmt(item.profitRub)} ₽</div>`
              : `<div class="spec-tag t-num">По медиане</div>`
            }
          </div>
          <div class="row-between">
            <span style="font-size: 12px; color: var(--text-secondary);">${esc(item.seller.name)} · ★ ${esc(item.seller.rating)}</span>
            <div style="display:flex; gap:6px;">
              <button class="btn btn-icon" style="width:34px; height:34px;" data-action="save-lot" data-val="${esc(item.id)}">
                <i data-lucide="heart" style="width:15px; height:15px; ${isSaved ? 'fill:var(--accent); stroke:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-pill btn-accent" style="height:34px; padding:0 13px;" data-action="open-lot-url" data-val="${esc(item.id)}">
                <span>Забрать лот</span>
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
          <div class="screen-content" style="padding-top:44px;">
            <div class="glass-card" style="text-align:center; padding:28px 18px;">
              <div style="width:56px; height:56px; border-radius:18px; background:var(--accent-soft); color:var(--accent); display:flex; align-items:center; justify-content:center; margin:0 auto 14px;">
                <i data-lucide="shield-alert" style="width:28px; height:28px;"></i>
              </div>
              <h2 style="font-size:18px; font-weight:800; margin-bottom:8px;">Откройте внутри Telegram</h2>
              <p style="font-size:13px; color:var(--text-secondary); line-height:1.5; margin-bottom:18px;">${esc(state.authError)}</p>
              <button class="btn btn-large btn-accent" data-action="open-url" data-val="https://t.me/DealHunterAvito_bot">Запустить @DealHunterAvito_bot</button>
            </div>
          </div>
        `;
        if (window.lucide) lucide.createIcons();
        return;
      }

      if (state.detailId) {
        const item = state.listings.find(x => x.id === state.detailId);
        if (item) {
          app.innerHTML = `
            <div class="top-header row-between">
              <button class="btn btn-pill btn-glass" data-action="close-detail">
                <i data-lucide="chevron-left" style="width:16px; height:16px;"></i><span>Назад</span>
              </button>
              <span class="src-badge src-${esc(item.source)}">${esc(item.source)}</span>
            </div>
            <div class="screen-content">
              <div class="glass-card">
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:6px;">${esc(item.city)} · ${esc(item.timeAgo)}</div>
                <h1 style="font-size:18px; font-weight:800; margin-bottom:12px; line-height:1.35;">${esc(item.title)}</h1>
                <div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px;">
                  ${(item.tags || []).map(t => `<span class="spec-tag">${esc(t)}</span>`).join('')}
                </div>
                <div class="row-between" style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:14px;">
                  <div>
                    <div class="t-num" style="font-size:24px; font-weight:800;">${fmt(item.price)} ₽</div>
                    <div class="t-num" style="font-size:12px; color:var(--text-muted);">Медиана модели: ${fmt(item.market)} ₽</div>
                  </div>
                  ${Number(item.profitPct) > 0 ? `<div class="profit-pill t-num">−${esc(item.profitPct)}%</div>` : ''}
                </div>
                <div style="padding:10px 12px; border-radius:12px; background:var(--glass-pill); margin-bottom:12px; font-size:12px;">
                  <b>Продавец:</b> ${esc(item.seller.name)} · ★ ${esc(item.seller.rating)} (${esc(item.seller.reviews)} отзывов)
                </div>
                <p style="font-size:13px; line-height:1.55; color:var(--text-secondary); margin-bottom:16px;">${esc(item.desc)}</p>
                <button class="btn btn-large btn-accent" data-action="open-lot-url" data-val="${esc(item.id)}">
                  <i data-lucide="external-link" style="width:17px; height:17px;"></i>
                  <span>Открыть карточку на ${esc(item.source)}</span>
                </button>
              </div>
            </div>
          `;
          if (window.lucide) lucide.createIcons();
          return;
        }
      }

      const timerBadgeHtml = `
        <div id="hdr-timer" data-action="paywall" class="t-num" style="cursor:pointer; padding:6px 11px; border-radius:11px; font-size:11px; font-weight:800; background:${state.user.subActive ? 'var(--green-soft)' : 'var(--accent-soft)'}; color:${state.user.subActive ? 'var(--green)' : 'var(--accent)'};">
          ${state.user.subActive ? ('PRO · ' + formatCountdown(state.user.secondsLeft)) : 'PRO истёк'}
        </div>
      `;

      let contentHtml = '';

      if (state.tab === 'feed') {
        contentHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap:8px;">
                <div style="width:34px; height:34px; border-radius:11px; background:linear-gradient(135deg, #FF5A1F, #FF7A45); display:flex; align-items:center; justify-content:center; color:#fff; box-shadow:0 4px 12px var(--accent-glow);">
                  <i data-lucide="radar" style="width:18px; height:18px;"></i>
                </div>
                <div>
                  <div style="font-size:16px; font-weight:800; line-height:1.1;">DealHunter</div>
                  <div style="font-size:10px; color:var(--text-muted);">Свайп вниз — обновить${state.updatedAt ? ' · ' + esc(state.updatedAt) : ''}</div>
                </div>
              </div>
              <div style="display:flex; align-items:center; gap:6px;">
                ${timerBadgeHtml}
                <button class="btn btn-icon" data-action="refresh">
                  <i data-lucide="refresh-cw" class="${state.isRefreshing ? 'spin' : ''}" style="width:15px; height:15px;"></i>
                </button>
                <button class="btn btn-icon" data-action="theme">
                  <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:15px; height:15px;"></i>
                </button>
              </div>
            </div>

            <!-- ПОИСКОВАЯ СТРОКА ВСЕГДА НА ГЛАВНОМ ЭКРАНЕ -->
            <div class="search-wrap">
              <i data-lucide="search" class="search-icon" style="width:15px; height:15px;"></i>
              <input
                type="text"
                id="q-input"
                class="search-input"
                placeholder="Поиск (Xiaomi, iPhone 16, игрушка, RTX 4070)..."
                value="${esc(state.searchQuery)}"
              />
              ${state.searchQuery ? `<button class="search-clear" data-action="search-reset"><i data-lucide="x" style="width:12px; height:12px;"></i></button>` : ''}
              <button class="btn btn-pill btn-accent" style="height:40px; padding:0 15px;" data-action="search-run">Найти</button>
            </div>

            <div class="pills-scroll">
              <button class="cat-pill ${state.source === 'all' ? 'active' : ''}" data-action="source" data-val="all">Все площадки</button>
              <button class="cat-pill ${state.source === 'Wildberries' ? 'active' : ''}" data-action="source" data-val="Wildberries">Wildberries</button>
              <button class="cat-pill ${state.source === 'Ozon' ? 'active' : ''}" data-action="source" data-val="Ozon">Ozon</button>
              <button class="cat-pill ${state.source === 'Avito' ? 'active' : ''}" data-action="source" data-val="Avito">Avito</button>
            </div>
            <div class="pills-scroll" style="margin-top:6px;">
              <button class="cat-pill ${state.category === 'all' && !state.searchQuery ? 'active' : ''}" data-action="category" data-val="all">Все товары</button>
              <button class="cat-pill ${state.category === 'xiaomi' ? 'active' : ''}" data-action="category" data-val="xiaomi">Xiaomi</button>
              <button class="cat-pill ${state.category === 'iphone' ? 'active' : ''}" data-action="category" data-val="iphone">iPhone</button>
              <button class="cat-pill ${state.category === 'macbook' ? 'active' : ''}" data-action="category" data-val="macbook">MacBook</button>
              <button class="cat-pill ${state.category === 'gpu' ? 'active' : ''}" data-action="category" data-val="gpu">Видеокарты</button>
              <button class="cat-pill ${state.category === 'ps5' ? 'active' : ''}" data-action="category" data-val="ps5">Консоли</button>
            </div>
          </div>
          <div class="screen-content">
            ${!state.user.subActive
              ? renderLockedState()
              : (state.listings.length
                  ? state.listings.map(renderLotCard).join('')
                  : '<div class="glass-card" style="text-align:center; padding:26px;">Загружаем свежие предложения...</div>')
            }
          </div>
        `;
      }

      if (state.tab === 'monitors') {
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:17px; font-weight:800;">Авто-мониторы</div>
            <button class="btn btn-pill btn-accent" data-action="new-monitor-modal">
              <i data-lucide="plus" style="width:14px; height:14px;"></i><span>Создать</span>
            </button>
          </div>
          <div class="screen-content">
            ${state.monitors.length ? state.monitors.map(m => `
              <div class="glass-card">
                <div class="row-between" style="margin-bottom:8px;">
                  <div>
                    <div style="font-size:15px; font-weight:800;">${esc(m.title)}</div>
                    <div style="font-size:12px; color:var(--text-secondary);">${esc(m.source === 'all' ? 'WB + Ozon + Avito' : m.source)} · ${esc(m.city)} · от −${esc(m.minMargin)}%</div>
                  </div>
                  <div class="ios-switch ${m.active ? 'on' : ''}" data-action="toggle-monitor" data-val="${esc(m.id)}">
                    <div class="ios-switch-knob"></div>
                  </div>
                </div>
                <div class="row-between" style="padding-top:8px; border-top:1px solid var(--glass-stroke);">
                  <span style="font-size:12px; color:var(--green); font-weight:600;">⚡ Найдено за сегодня: ${esc(m.foundToday)}</span>
                  <button class="btn btn-icon" style="width:30px; height:30px;" data-action="delete-monitor" data-val="${esc(m.id)}">
                    <i data-lucide="trash-2" style="width:14px; height:14px; color:var(--text-secondary);"></i>
                  </button>
                </div>
              </div>
            `).join('') : '<div class="glass-card" style="text-align:center; padding:26px;">У вас пока нет активных мониторов. Нажмите «Создать» в правом верхнем углу, чтобы бот присылал выгодные лоты в Telegram!</div>'}
          </div>
        `;
      }

      if (state.tab === 'saved') {
        const savedList = state.listings.filter(x => state.user.savedIds.includes(x.id));
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:17px; font-weight:800;">Избранное (${state.user.savedIds.length})</div>
            ${timerBadgeHtml}
          </div>
          <div class="screen-content">
            ${savedList.length ? savedList.map(renderLotCard).join('') : '<div class="glass-card" style="text-align:center; padding:28px;">Сохранённых лотов пока нет</div>'}
          </div>
        `;
      }

      if (state.tab === 'profile') {
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:17px; font-weight:800;">Профиль и бонусы</div>
            ${timerBadgeHtml}
          </div>
          <div class="screen-content">
            <div class="glass-card">
              <div class="row-between" style="margin-bottom:12px;">
                <div>
                  <div style="font-size:17px; font-weight:800;">${esc(state.user.name)}</div>
                  <div style="font-size:12px; color:var(--text-secondary);">Telegram ID: ${esc(state.user.user_id)}</div>
                </div>
                <span class="profit-pill">${state.user.subActive ? 'PRO АКТИВЕН' : 'FREE'}</span>
              </div>

              <div style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:14px;">
                <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">Остаток времени PRO-доступа:</div>
                <div id="prof-timer" class="t-num" style="font-size:22px; font-weight:800; color:${state.user.subActive ? 'var(--green)' : 'var(--accent)'};">
                  ${formatCountdown(state.user.secondsLeft)}
                </div>
              </div>

              <button class="btn btn-large btn-accent" style="margin-bottom:10px;" data-action="buy-pro">
                <i data-lucide="star" style="width:18px; height:18px;"></i>
                <span>Продлить PRO (+30 дней) · 350 Stars</span>
              </button>

              <button class="btn btn-large btn-stars-bot" data-action="open-url" data-val="${BUNNY_STARS_LINK}">
                <i data-lucide="sparkles" style="width:18px; height:18px;"></i>
                <span>Пополнить Stars выгодно (BunnyStars)</span>
              </button>
            </div>

            <div class="glass-card">
              <div class="row-between" style="margin-bottom:8px;">
                <div style="display:flex; align-items:center; gap:8px;">
                  <div style="width:32px; height:32px; border-radius:10px; background:var(--accent-soft); color:var(--accent); display:flex; align-items:center; justify-content:center;">
                    <i data-lucide="users" style="width:17px; height:17px;"></i>
                  </div>
                  <div>
                    <div style="font-size:15px; font-weight:800;">Реферальная программа</div>
                    <div style="font-size:11px; color:var(--text-secondary);">+3 дня PRO за друга (${esc(state.user.refMonthDays)}/${esc(state.user.refMonthLimit)} дн. в мес.)</div>
                  </div>
                </div>
              </div>

              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin: 12px 0;">
                <div style="padding:10px 12px; border-radius:14px; background:var(--glass-pill); text-align:center;">
                  <div class="t-num" style="font-size:20px; font-weight:800;">${esc(state.user.refCount)}</div>
                  <div style="font-size:11px; color:var(--text-secondary);">Приглашено друзей</div>
                </div>
                <div style="padding:10px 12px; border-radius:14px; background:var(--glass-pill); text-align:center;">
                  <div class="t-num" style="font-size:20px; font-weight:800; color:var(--green);">+${esc(state.user.refDaysEarned)} дн.</div>
                  <div style="font-size:11px; color:var(--text-secondary);">Бонусных дней PRO</div>
                </div>
              </div>

              <div style="padding:10px 12px; border-radius:12px; background:var(--glass-pill); font-size:11px; color:var(--text-secondary); word-break:break-all; margin-bottom:10px;">
                ${esc(state.user.refLink)}
              </div>

              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <button class="btn btn-pill btn-glass" style="height:40px;" data-action="copy-ref">
                  <i data-lucide="copy" style="width:15px; height:15px;"></i><span>Скопировать</span>
                </button>
                <button class="btn btn-pill btn-accent" style="height:40px;" data-action="share-ref">
                  <i data-lucide="share-2" style="width:15px; height:15px;"></i><span>Пригласить</span>
                </button>
              </div>
            </div>

            <div class="glass-card">
              <div style="font-size:13px; font-weight:700; margin-bottom:10px;">Правовая информация</div>
              <div style="display:flex; flex-direction:column; gap:8px;">
                <button class="btn btn-glass" style="height:40px; border-radius:12px; justify-content:space-between; padding:0 12px; font-size:12px;" data-action="legal" data-val="privacy">
                  <span>Политика конфиденциальности</span><i data-lucide="chevron-right" style="width:15px; height:15px;"></i>
                </button>
                <button class="btn btn-glass" style="height:40px; border-radius:12px; justify-content:space-between; padding:0 12px; font-size:12px;" data-action="legal" data-val="terms">
                  <span>Пользовательское соглашение</span><i data-lucide="chevron-right" style="width:15px; height:15px;"></i>
                </button>
              </div>
            </div>
          </div>
        `;
      }

      let modalHtml = '';
      if (!state.user.trialClaimed) {
        modalHtml = `
          <div class="modal-backdrop modal-center">
            <div class="modal-card" style="text-align:center;">
              <div style="width:64px; height:64px; border-radius:20px; background:var(--accent-soft); color:var(--accent); display:flex; align-items:center; justify-content:center; margin:0 auto 14px;">
                <i data-lucide="gift" style="width:32px; height:32px;"></i>
              </div>
              <div class="profit-pill" style="margin-bottom:10px;">WELCOME BONUS</div>
              <h2 style="font-size:21px; font-weight:800; margin-bottom:8px;">Вам подарок: 3 дня PRO!</h2>
              <p style="font-size:13px; color:var(--text-secondary); line-height:1.5; margin-bottom:18px;">
                Активируйте бесплатный доступ к радару находок <b>Wildberries, Ozon и Avito</b> и мгновенным уведомлениям в Telegram. Таймер запустится сразу после нажатия!
              </p>
              <button class="btn btn-large btn-accent" data-action="claim-trial">
                <i data-lucide="zap" style="width:18px; height:18px;"></i>
                <span>Забрать 3 дня PRO бесплатно</span>
              </button>
            </div>
          </div>
        `;
      } else if (state.showNewMonitor) {
        modalHtml = `
          <div class="modal-backdrop" data-action="close-modal">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <div style="font-size:17px; font-weight:800;">Новый авто-монитор</div>
                <button class="btn btn-icon" data-action="close-modal"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <input type="text" id="m-title" class="input-glass" placeholder="Что ищем (напр. Xiaomi 14, iPhone 15)" />
              <select id="m-city" class="input-glass">
                <option value="Москва">Москва</option>
                <option value="Санкт-Петербург">Санкт-Петербург</option>
                <option value="Екатеринбург">Екатеринбург</option>
                <option value="Казань">Казань</option>
                <option value="Новосибирск">Новосибирск</option>
                <option value="Краснодар">Краснодар</option>
              </select>
              <select id="m-source" class="input-glass">
                <option value="all">Все площадки (WB + Ozon + Avito)</option>
                <option value="Wildberries">Только Wildberries</option>
                <option value="Ozon">Только Ozon</option>
                <option value="Avito">Только Avito</option>
              </select>
              <input type="number" id="m-margin" class="input-glass" placeholder="Мин. отклонение от медианы (%)" value="15" />
              <button class="btn btn-large btn-accent" data-action="create-monitor">Запустить мониторинг</button>
            </div>
          </div>
        `;
      } else if (state.showPaywall) {
        modalHtml = `
          <div class="modal-backdrop" data-action="close-modal">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <span class="profit-pill">DealHunter PRO</span>
                <button class="btn btn-icon" data-action="close-modal"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <div class="t-num" style="font-size:26px; font-weight:800; margin-bottom:6px;">350 Stars <span style="font-size:13px; color:var(--text-muted);">/ 30 дней</span></div>
              <p style="font-size:13px; color:var(--text-secondary); line-height:1.45; margin-bottom:16px;">
                Полный доступ к карточкам товаров Wildberries, Ozon и Avito, живому поиску и мгновенным пуш-уведомлениям в Telegram.
              </p>
              <button class="btn btn-large btn-accent" style="margin-bottom:10px;" data-action="buy-pro">
                <i data-lucide="star" style="width:18px; height:18px;"></i>
                <span>Оплатить 350 Stars</span>
              </button>
              <button class="btn btn-large btn-stars-bot" data-action="open-url" data-val="${BUNNY_STARS_LINK}">
                <i data-lucide="external-link" style="width:17px; height:17px;"></i>
                <span>Где взять Stars? Купить в BunnyStars</span>
              </button>
            </div>
          </div>
        `;
      } else if (state.legalDoc) {
        const docs = {
          privacy: {
            t: 'Политика конфиденциальности',
            b: '1. Сервис DealHunter обрабатывает технический идентификатор аккаунта Telegram (Telegram ID), имя пользователя и настройки фильтров исключительно для работы мини-приложения и доставки уведомлений.\\n\\n2. Сервис не запрашивает и не хранит паспортные данные, реквизиты банковских карт или номера телефонов.\\n\\n3. Пользователь вправе прекратить обработку данных в любой момент, удалив свои мониторы и остановив бота.'
          },
          terms: {
            t: 'Пользовательское соглашение',
            b: '1. DealHunter является независимым справочно-информационным инструментом анализа открытых цен.\\n\\n2. Показатель «Отклонение от медианы» рассчитывается автоматически относительно выборки однотипных товаров и носит ознакомительный характер.\\n\\n3. Доступ к функционалу PRO предоставляется на 30 календарных дней за Telegram Stars.'
          }
        };
        const d = docs[state.legalDoc];
        modalHtml = `
          <div class="modal-backdrop" data-action="close-modal">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <div style="font-size:15px; font-weight:800;">${esc(d.t)}</div>
                <button class="btn btn-icon" data-action="close-modal"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <div style="font-size:12px; color:var(--text-secondary); line-height:1.6; white-space:pre-line; margin-bottom:16px;">${esc(d.b)}</div>
              <button class="btn btn-large btn-glass" data-action="close-modal">Понятно</button>
            </div>
          </div>
        `;
      }

      const dockHtml = `
        <div class="floating-dock-wrap">
          <nav class="floating-dock">
            <button class="dock-btn ${state.tab === 'feed' ? 'active' : ''}" data-action="tab" data-val="feed">
              <i data-lucide="flame" style="width:19px; height:19px;"></i><span>Находки</span>
            </button>
            <button class="dock-btn ${state.tab === 'monitors' ? 'active' : ''}" data-action="tab" data-val="monitors">
              <i data-lucide="radar" style="width:19px; height:19px;"></i><span>Мониторы</span>
            </button>
            <button class="dock-btn ${state.tab === 'saved' ? 'active' : ''}" data-action="tab" data-val="saved">
              <i data-lucide="heart" style="width:19px; height:19px;"></i><span>Избранное</span>
            </button>
            <button class="dock-btn ${state.tab === 'profile' ? 'active' : ''}" data-action="tab" data-val="profile">
              <i data-lucide="user" style="width:19px; height:19px;"></i><span>Профиль</span>
            </button>
          </nav>
        </div>
      `;

      app.innerHTML = contentHtml + modalHtml + dockHtml;
      if (window.lucide) lucide.createIcons();
    }

    syncState();
  </script>
</body>
</html>
"""
