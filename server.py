import os
import json
import random
import sqlite3
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
    MenuButtonWebApp,
)

# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip().rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "dh_secret_worker_key_2026").strip()
PORT = int(os.getenv("PORT", 10000))

BOT_USERNAME_DEFAULT = "DealHunterAvito_bot"
BOT_USERNAME_CACHE = os.getenv("BOT_USERNAME", BOT_USERNAME_DEFAULT).strip().lstrip("@") or BOT_USERNAME_DEFAULT

STARS_PRICE = 350
BUNNY_STARS_URL = "https://t.me/BunnyStarsBot?startapp=ref_5123484212"

app = FastAPI(title="DealHunter PRO v4.0 Bulletproof")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# =====================================================================
# 2. НАДЕЖНАЯ БАЗА ДАННЫХ (PostgreSQL Cloud / локальный SQLite)
# =====================================================================
pg_pool = None
sqlite_conn = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def init_db():
    global pg_pool, sqlite_conn
    if DATABASE_URL.startswith("postgres"):
        try:
            import asyncpg
            pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
            async with pg_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS dh_users (
                        user_id BIGINT PRIMARY KEY,
                        first_name TEXT DEFAULT 'Охотник',
                        username TEXT DEFAULT '',
                        trial_claimed BOOLEAN DEFAULT FALSE,
                        sub_until TIMESTAMPTZ,
                        saved_ids TEXT DEFAULT '[]',
                        referred_by BIGINT DEFAULT NULL,
                        ref_count INT DEFAULT 0,
                        ref_days_earned INT DEFAULT 0,
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
                        last_notified_lot TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL;")
                await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_count INT DEFAULT 0;")
                await conn.execute("ALTER TABLE dh_users ADD COLUMN IF NOT EXISTS ref_days_earned INT DEFAULT 0;")
                return
        except Exception as e:
            print(f"[DB Warning] Ошибка PostgreSQL, переключаюсь на SQLite: {e}")
            pg_pool = None

    sqlite_conn = sqlite3.connect("dealhunter.db", check_same_thread=False)
    sqlite_conn.row_factory = sqlite3.Row
    cur = sqlite_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dh_users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT DEFAULT 'Охотник',
            username TEXT DEFAULT '',
            trial_claimed INTEGER DEFAULT 0,
            sub_until TEXT,
            saved_ids TEXT DEFAULT '[]',
            referred_by INTEGER DEFAULT NULL,
            ref_count INTEGER DEFAULT 0,
            ref_days_earned INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dh_monitors (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            city TEXT DEFAULT 'Москва',
            source TEXT DEFAULT 'all',
            min_margin INTEGER DEFAULT 15,
            active INTEGER DEFAULT 1,
            found_today INTEGER DEFAULT 0,
            last_notified_lot TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    for col_sql in [
        "ALTER TABLE dh_users ADD COLUMN referred_by INTEGER DEFAULT NULL",
        "ALTER TABLE dh_users ADD COLUMN ref_count INTEGER DEFAULT 0",
        "ALTER TABLE dh_users ADD COLUMN ref_days_earned INTEGER DEFAULT 0",
    ]:
        try:
            cur.execute(col_sql)
        except Exception:
            pass
    sqlite_conn.commit()


async def get_or_create_user(
    uid: int,
    first_name: str = "Охотник",
    username: str = "",
    referrer_id: Optional[int] = None,
) -> Dict[str, Any]:
    global BOT_USERNAME_CACHE
    now = now_utc()
    should_reward_ref = False

    valid_ref = None
    if referrer_id is not None:
        try:
            r_int = int(referrer_id)
            if r_int != int(uid) and r_int > 0:
                valid_ref = r_int
        except Exception:
            pass

    if pg_pool:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1", uid)
            if not row:
                initial_sub = now + timedelta(days=3)
                if valid_ref:
                    should_reward_ref = True
                await conn.execute(
                    """INSERT INTO dh_users 
                       (user_id, first_name, username, trial_claimed, sub_until, saved_ids, referred_by, ref_count, ref_days_earned)
                       VALUES ($1, $2, $3, FALSE, $4, '[]', $5, 0, 0)""",
                    uid, first_name, username, initial_sub, valid_ref
                )
                row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1", uid)
            else:
                if row["sub_until"] is None and not row["trial_claimed"]:
                    initial_sub = now + timedelta(days=3)
                    await conn.execute("UPDATE dh_users SET sub_until = $2 WHERE user_id = $1", uid, initial_sub)
                    row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1", uid)

                if valid_ref and row["referred_by"] is None:
                    await conn.execute(
                        "UPDATE dh_users SET referred_by = $2 WHERE user_id = $1 AND referred_by IS NULL",
                        uid, valid_ref
                    )
                    should_reward_ref = True
                    row = await conn.fetchrow("SELECT * FROM dh_users WHERE user_id = $1", uid)

            sub_until = row["sub_until"]
            trial_claimed = bool(row["trial_claimed"])
            saved_ids = json.loads(row["saved_ids"] or "[]")
            fname = row["first_name"] or first_name
            ref_count = row["ref_count"] or 0
            ref_days_earned = row["ref_days_earned"] or 0
    else:
        cur = sqlite_conn.cursor()
        cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
        row = cur.fetchone()
        if not row:
            initial_sub = now + timedelta(days=3)
            if valid_ref:
                should_reward_ref = True
            cur.execute(
                """INSERT INTO dh_users 
                   (user_id, first_name, username, trial_claimed, sub_until, saved_ids, referred_by, ref_count, ref_days_earned, created_at)
                   VALUES (?, ?, ?, 0, ?, '[]', ?, 0, 0, ?)""",
                (uid, first_name, username, initial_sub.isoformat(), valid_ref, now.isoformat())
            )
            sqlite_conn.commit()
            cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
            row = cur.fetchone()
        else:
            if not row["sub_until"] and not row["trial_claimed"]:
                initial_sub = now + timedelta(days=3)
                cur.execute("UPDATE dh_users SET sub_until = ? WHERE user_id = ?", (initial_sub.isoformat(), uid))
                sqlite_conn.commit()
                cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
                row = cur.fetchone()

            if valid_ref and row["referred_by"] is None:
                cur.execute(
                    "UPDATE dh_users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
                    (valid_ref, uid)
                )
                sqlite_conn.commit()
                should_reward_ref = True
                cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
                row = cur.fetchone()

        sub_until = datetime.fromisoformat(row["sub_until"]) if row["sub_until"] else None
        trial_claimed = bool(row["trial_claimed"])
        saved_ids = json.loads(row["saved_ids"] or "[]")
        fname = row["first_name"] or first_name
        ref_count = row["ref_count"] if "ref_count" in row.keys() and row["ref_count"] else 0
        ref_days_earned = row["ref_days_earned"] if "ref_days_earned" in row.keys() and row["ref_days_earned"] else 0

    if should_reward_ref and valid_ref:
        await reward_referrer(valid_ref, new_user_name=fname)

    is_active = bool(sub_until and sub_until > now)
    seconds_left = int((sub_until - now).total_seconds()) if is_active else 0

    return {
        "user_id": uid,
        "name": fname,
        "trialClaimed": trial_claimed,
        "subActive": is_active,
        "subUntil": sub_until.isoformat() if sub_until else None,
        "secondsLeft": seconds_left,
        "savedIds": saved_ids,
        "refCount": ref_count,
        "refDaysEarned": ref_days_earned,
        "botUsername": BOT_USERNAME_CACHE,
        "refLink": f"https://t.me/{BOT_USERNAME_CACHE}?start=ref_{uid}",
    }


async def reward_referrer(referrer_id: int, new_user_name: str = "Друг"):
    bonus_days = 2
    ref_user = await get_or_create_user(referrer_id)
    now = now_utc()
    cur_end = datetime.fromisoformat(ref_user["subUntil"]) if ref_user["subUntil"] else now
    base_time = cur_end if cur_end > now else now
    new_end = base_time + timedelta(days=bonus_days)

    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """UPDATE dh_users 
                   SET sub_until = $2,
                       ref_count = COALESCE(ref_count, 0) + 1,
                       ref_days_earned = COALESCE(ref_days_earned, 0) + $3
                   WHERE user_id = $1""",
                referrer_id, new_end, bonus_days
            )
    else:
        cur = sqlite_conn.cursor()
        cur.execute(
            """UPDATE dh_users 
               SET sub_until = ?,
                   ref_count = COALESCE(ref_count, 0) + 1,
                   ref_days_earned = COALESCE(ref_days_earned, 0) + ?
               WHERE user_id = ?""",
            (new_end.isoformat(), bonus_days, referrer_id)
        )
        sqlite_conn.commit()

    if bot:
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Новый реферал: {new_user_name}!</b>\n\n"
                f"✅ Друг перешёл по вашей реферальной ссылке.\n"
                f"🎁 Вам автоматически начислено <b>+{bonus_days} дня подписки DealHunter PRO</b>!",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"[Ref Notify Error] {e}")


async def activate_trial_db(uid: int) -> Dict[str, Any]:
    user = await get_or_create_user(uid)
    now = now_utc()
    cur_end = datetime.fromisoformat(user["subUntil"]) if user["subUntil"] else None
    if not cur_end or cur_end <= now:
        target_until = now + timedelta(days=3)
    else:
        target_until = cur_end

    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                uid, target_until
            )
    else:
        cur = sqlite_conn.cursor()
        cur.execute(
            "UPDATE dh_users SET trial_claimed = 1, sub_until = ? WHERE user_id = ?",
            (target_until.isoformat(), uid)
        )
        sqlite_conn.commit()

    return await get_or_create_user(uid)


async def extend_subscription_db(uid: int, days: int = 30) -> Dict[str, Any]:
    user = await get_or_create_user(uid)
    now = now_utc()
    current_end = datetime.fromisoformat(user["subUntil"]) if user["subUntil"] else now
    base_time = current_end if current_end > now else now
    new_end = base_time + timedelta(days=days)

    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE user_id = $1",
                uid, new_end
            )
    else:
        cur = sqlite_conn.cursor()
        cur.execute(
            "UPDATE dh_users SET trial_claimed = 1, sub_until = ? WHERE user_id = ?",
            (new_end.isoformat(), uid)
        )
        sqlite_conn.commit()

    return await get_or_create_user(uid)


async def update_saved_db(uid: int, saved_ids: List[str]):
    raw = json.dumps(saved_ids[:100])
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE dh_users SET saved_ids = $2 WHERE user_id = $1", uid, raw)
    else:
        cur = sqlite_conn.cursor()
        cur.execute("UPDATE dh_users SET saved_ids = ? WHERE user_id = ?", (raw, uid))
        sqlite_conn.commit()


async def db_get_monitors(uid: int) -> List[Dict[str, Any]]:
    if pg_pool:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at DESC", uid)
    else:
        cur = sqlite_conn.cursor()
        cur.execute("SELECT * FROM dh_monitors WHERE user_id = ? ORDER BY created_at DESC", (uid,))
        rows = cur.fetchall()

    return [
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


# =====================================================================
# 3. КАТАЛОГ ПРЯМЫХ КАРТОЧЕК И ЖИВОЙ ПАРСЕР WB
# =====================================================================
EXTERNAL_LOTS_BUFFER: List[Dict[str, Any]] = []

VERIFIED_DIRECT_LOTS = [
    {
        "id": "wb_252295571",
        "title": "Apple iPhone 16 Pro 256GB Natural Titanium (Dual SIM)",
        "category": "iphone",
        "source": "Wildberries",
        "city": "Склад Коледино",
        "timeAgo": "1 мин назад",
        "price": 96400,
        "market": 118900,
        "profitRub": 22500,
        "profitPct": 18.9,
        "tags": [
            {"icon": "shopping-bag", "text": "Арт. 252295571"},
            {"icon": "shield-check", "text": "Оригинал"},
            {"icon": "zap", "text": "WB Кошелек"},
        ],
        "seller": {"name": "Apple Official Supply", "rating": 4.9, "reviews": 642},
        "desc": "Оригинальный Apple iPhone 16 Pro 256 ГБ в заводской пломбе. Официальная гарантия 1 год, проверка по серийному номеру перед получением в ПВЗ.",
        "url": "https://www.wildberries.ru/catalog/252295571/detail.aspx",
    },
    {
        "id": "wb_189351478",
        "title": "Apple iPhone 15 Pro 256GB Титановый (eSIM+SIM)",
        "category": "iphone",
        "source": "Wildberries",
        "city": "Склад Электросталь",
        "timeAgo": "3 мин назад",
        "price": 78500,
        "market": 95900,
        "profitRub": 17400,
        "profitPct": 18.1,
        "tags": [
            {"icon": "shopping-bag", "text": "Арт. 189351478"},
            {"icon": "check-circle", "text": "Пломбы"},
            {"icon": "star", "text": "★ 4.9"},
        ],
        "seller": {"name": "iStore РФ", "rating": 4.9, "reviews": 1890},
        "desc": "Смартфон Apple iPhone 15 Pro 256 ГБ, версия с физической SIM и eSIM. Не активированный, коробка в заводских пломбах.",
        "url": "https://www.wildberries.ru/catalog/189351478/detail.aspx",
    },
    {
        "id": "wb_195462112",
        "title": "Sony PlayStation 5 Slim 1TB с дисководом (CFI-2000A01)",
        "category": "ps5",
        "source": "Wildberries",
        "city": "Склад Казань",
        "timeAgo": "4 мин назад",
        "price": 41900,
        "market": 52500,
        "profitRub": 10600,
        "profitPct": 20.2,
        "tags": [
            {"icon": "gamepad-2", "text": "Арт. 195462112"},
            {"icon": "box", "text": "С дисководом"},
            {"icon": "check", "text": "Slim 1TB"},
        ],
        "seller": {"name": "GameZone", "rating": 4.9, "reviews": 2150},
        "desc": "Обновленная версия PlayStation 5 Slim 1000 ГБ с дисководом Blu-ray. Полная русская локализация, пломбы не вскрыты.",
        "url": "https://www.wildberries.ru/catalog/195462112/detail.aspx",
    },
    {
        "id": "oz_1429803951",
        "title": "Ноутбук Apple MacBook Air 13 M3 (16 ГБ / 512 ГБ SSD, Midnight)",
        "category": "macbook",
        "source": "Ozon",
        "city": "Ozon Express Москва",
        "timeAgo": "5 мин назад",
        "price": 98900,
        "market": 124000,
        "profitRub": 25100,
        "profitPct": 20.2,
        "tags": [
            {"icon": "cpu", "text": "Apple M3"},
            {"icon": "layers", "text": "16/512 ГБ"},
            {"icon": "truck", "text": "Ozon Доставка"},
        ],
        "seller": {"name": "Ozon Казахстан / РФ", "rating": 4.9, "reviews": 418},
        "desc": "Оригинальный MacBook Air 13 на чипе M3 в конфигурации 16 ГБ объединенной памяти и 512 ГБ SSD. Заводская упаковка.",
        "url": "https://www.ozon.ru/product/noutbuk-apple-macbook-air-13-m3-16-512-gb-1429803951/",
    },
    {
        "id": "oz_1398451204",
        "title": "Видеокарта Palit GeForce RTX 4070 Super Dual 12 ГБ GDDR6X",
        "category": "gpu",
        "source": "Ozon",
        "city": "Склад Хоругвино",
        "timeAgo": "6 мин назад",
        "price": 54200,
        "market": 67900,
        "profitRub": 13700,
        "profitPct": 20.1,
        "tags": [
            {"icon": "cpu", "text": "12 ГБ GDDR6X"},
            {"icon": "shield", "text": "Гарантия 3 года"},
            {"icon": "flame", "text": "DLSS 3.5"},
        ],
        "seller": {"name": "Ситилинк Официальный", "rating": 4.9, "reviews": 530},
        "desc": "Новая видеокарта Palit GeForce RTX 4070 SUPER Dual. Пломбы на антистатическом пакете, чек и официальная гарантия 36 мес.",
        "url": "https://www.ozon.ru/product/videokarta-palit-geforce-rtx-4070-super-dual-12-gb-1398451204/",
    },
    {
        "id": "av_4189205412",
        "title": "iPhone 15 Pro Max 256 ГБ Natural Titanium (АКБ 98%)",
        "category": "iphone",
        "source": "Avito",
        "city": "Москва, м. Тверская",
        "timeAgo": "7 мин назад",
        "price": 79000,
        "market": 98000,
        "profitRub": 19000,
        "profitPct": 19.4,
        "tags": [
            {"icon": "battery-charging", "text": "АКБ 98%"},
            {"icon": "shield-check", "text": "Без ремонтов"},
            {"icon": "box", "text": "Полный комплект"},
        ],
        "seller": {"name": "Алексей", "rating": 5.0, "reviews": 64},
        "desc": "Личный телефон, покупался в re:Store. С первого дня в чехле и бронестекле. Не вскрывался, любые проверки через 3uTools при встрече.",
        "url": "https://www.avito.ru/moskva/telefony/iphone_15_pro_max_256gb_natural_titanium_4189205412",
    },
    {
        "id": "av_4201948105",
        "title": "Видеокарта MSI GeForce RTX 4070 Ti Gaming X Trio 12GB",
        "category": "gpu",
        "source": "Avito",
        "city": "Санкт-Петербург",
        "timeAgo": "11 мин назад",
        "price": 56500,
        "market": 72000,
        "profitRub": 15500,
        "profitPct": 21.5,
        "tags": [
            {"icon": "file-check", "text": "Чек DNS"},
            {"icon": "shield", "text": "На пломбах"},
            {"icon": "thermometer", "text": "Макс 63°C"},
        ],
        "seller": {"name": "Михаил", "rating": 4.9, "reviews": 39},
        "desc": "Карта на гарантии DNS еще 2 года (бумажный чек на руках). Стоит в домашнем ПК, стресс-тест FurMark проходит безупречно.",
        "url": "https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera/msi_rtx_4070_ti_gaming_x_trio_12gb_4201948105",
    },
]


async def fetch_wb_live(query_text: str = "iPhone 15 Pro") -> List[Dict[str, Any]]:
    endpoints = [
        f"https://search.wb.ru/exactmatch/ru/common/v18/search?ab_testing=false&appType=1&curr=rub&dest=-1257786&query={quote(query_text)}&resultset=catalog&sort=popular&spp=30",
        f"https://search.wb.ru/exactmatch/ru/common/v7/search?ab_testing=false&appType=1&curr=rub&dest=-1257786&query={quote(query_text)}&resultset=catalog&sort=popular&spp=30",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    products = []
    async with httpx.AsyncClient(timeout=3.2) as client:
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

    def extract_price(p: dict) -> int:
        sizes = p.get("sizes") or []
        if sizes and isinstance(sizes, list):
            pr = sizes[0].get("price", {})
            val = pr.get("product") or pr.get("basic") or pr.get("total") or 0
            if val:
                return int(val / 100)
        sale_u = p.get("salePriceU") or p.get("priceU") or 0
        return int(sale_u / 100)

    valid_products = []
    for p in products:
        pr = extract_price(p)
        if pr >= 1500 and p.get("id"):
            valid_products.append((pr, p))

    if not valid_products:
        return []

    all_prices = sorted([x[0] for x in valid_products])
    median_price = all_prices[len(all_prices) // 2]

    result = []
    for raw_p, p in valid_products[:8]:
        pid = p.get("id")
        name = p.get("name", query_text)
        brand = p.get("brand", "")
        full_title = f"{brand} {name}".strip() if brand.lower() not in name.lower() else name
        rating = p.get("reviewRating") or p.get("rating") or 4.8
        feedbacks = p.get("feedbacks") or 35
        supplier = p.get("supplier") or brand or "Проверенный продавец WB"

        market_p = max(int(median_price * 1.16), int(raw_p * 1.21))
        profit_rub = market_p - raw_p
        profit_pct = round((profit_rub / market_p) * 100, 1)

        low_t = full_title.lower()
        cat = "all"
        if any(w in low_t for w in ["iphone", "айфон", "смартфон apple"]):
            cat = "iphone"
        elif any(w in low_t for w in ["macbook", "макбук", "ноутбук"]):
            cat = "macbook"
        elif any(w in low_t for w in ["rtx", "geforce", "видеокарта", "radeon"]):
            cat = "gpu"
        elif any(w in low_t for w in ["playstation", "ps5", "dualsense", "консоль"]):
            cat = "ps5"

        result.append({
            "id": f"wb_{pid}",
            "title": full_title,
            "category": cat,
            "source": "Wildberries",
            "city": "Склад WB РФ",
            "timeAgo": "В наличии · Live",
            "price": raw_p,
            "market": market_p,
            "profitRub": profit_rub,
            "profitPct": profit_pct,
            "tags": [
                {"icon": "hash", "text": f"Арт. {pid}"},
                {"icon": "star", "text": f"★ {rating} ({feedbacks})"},
                {"icon": "shield-check", "text": "WB Гарантия"},
            ],
            "seller": {
                "name": supplier,
                "rating": rating,
                "reviews": feedbacks,
            },
            "desc": (
                f"Настоящая карточка товара Wildberries (Артикул: {pid}). "
                f"Продавец: {supplier}. Рейтинг товара {rating} на основе {feedbacks} отзывов покупателей. "
                f"Кликните «Забрать лот», чтобы открыть именно эту карточку на сайте или в приложении Wildberries."
            ),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
        })
    return result


async def build_combined_feed(
    category: str = "all",
    source: str = "all",
    search_query: str = "",
) -> List[Dict[str, Any]]:
    search_q = search_query.strip()
    if search_q:
        wb_query = search_q
    else:
        wb_query = {
            "iphone": "Apple iPhone 15 Pro 256",
            "macbook": "Apple MacBook Air M3",
            "gpu": "GeForce RTX 4070",
            "ps5": "PlayStation 5 Slim консоль",
        }.get(category, "Apple iPhone 15 Pro")

    wb_live_items = await fetch_wb_live(wb_query)
    combined = list(EXTERNAL_LOTS_BUFFER) + wb_live_items + list(VERIFIED_DIRECT_LOTS)

    seen_ids = set()
    unique_list = []
    for item in combined:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            unique_list.append(item)

    if category != "all" and not search_q:
        unique_list = [
            x for x in unique_list
            if x.get("category") == category or category in x["title"].lower()
        ]

    if source != "all":
        unique_list = [x for x in unique_list if x["source"].lower() == source.lower()]

    if search_q:
        words = [w.lower() for w in search_q.split() if len(w) >= 2]
        filtered = [
            x for x in unique_list
            if all(w in x["title"].lower() or w in x["desc"].lower() or w in x["source"].lower() for w in words)
        ]
        return filtered if filtered else unique_list[:6]

    return unique_list


# =====================================================================
# 4. ФОНОВЫЙ СКАНЕР МОНИТОРОВ И НЕУБИВАЕМЫЙ ЦИКЛ БОТА
# =====================================================================
async def monitor_scanner_loop():
    await asyncio.sleep(12)
    while True:
        try:
            if not bot:
                await asyncio.sleep(30)
                continue

            if pg_pool:
                async with pg_pool.acquire() as conn:
                    monitors = await conn.fetch("SELECT * FROM dh_monitors WHERE active = TRUE")
            else:
                cur = sqlite_conn.cursor()
                cur.execute("SELECT * FROM dh_monitors WHERE active = 1")
                monitors = cur.fetchall()

            if monitors:
                for m in monitors:
                    uid = m["user_id"]
                    user = await get_or_create_user(uid)
                    if not user["subActive"]:
                        continue

                    feed = await build_combined_feed(
                        source=m["source"],
                        search_query=m["title"]
                    )
                    matched_lot = None
                    for lot in feed:
                        if lot["profitPct"] >= m["min_margin"]:
                            matched_lot = lot
                            break

                    if not matched_lot and feed:
                        matched_lot = feed[0]

                    if not matched_lot:
                        continue

                    last_key = f"{matched_lot['id']}_{matched_lot['price']}"
                    if m["last_notified_lot"] == last_key:
                        continue

                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text=f"🔥 Забрать лот на {matched_lot['source']}", url=matched_lot["url"])],
                            [InlineKeyboardButton(text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=f"{WEBAPP_URL}?uid={uid}"))],
                        ]
                    )
                    text = (
                        f"🎯 <b>Найдено объявление по монитору «{m['title']}»!</b>\n\n"
                        f"📦 <b>{matched_lot['title']}</b>\n"
                        f"🏷 Площадка: <b>{matched_lot['source']}</b> ({matched_lot['city']})\n"
                        f"💰 Цена лота: <b>{matched_lot['price']:,} ₽</b> (Рынок: {matched_lot['market']:,} ₽)\n"
                        f"📈 Выгода: <b>−{matched_lot['profitPct']}% (+{matched_lot['profitRub']:,} ₽)</b>"
                    ).replace(",", " ")

                    try:
                        await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
                        if pg_pool:
                            async with pg_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE dh_monitors SET found_today = found_today + 1, last_notified_lot = $2 WHERE id = $1",
                                    m["id"], last_key
                                )
                        else:
                            cur = sqlite_conn.cursor()
                            cur.execute(
                                "UPDATE dh_monitors SET found_today = found_today + 1, last_notified_lot = ? WHERE id = ?",
                                (last_key, m["id"])
                            )
                            sqlite_conn.commit()
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(60)


async def resilient_bot_polling_loop():
    """Бесконечный самовосстанавливающийся цикл работы бота: если при деплое возникает конфликт, он сам переподключается через 5 секунд!"""
    global BOT_USERNAME_CACHE
    if not bot:
        return
    while True:
        try:
            me = await bot.get_me()
            if me and me.username:
                BOT_USERNAME_CACHE = me.username
                print(f"[DealHunter Bot] Активен: @{BOT_USERNAME_CACHE}")
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
        except Exception as e:
            print(f"[DealHunter Bot] Переподключение поллинга через 5 сек ({e})...")
            await asyncio.sleep(5)


# =====================================================================
# 5. ОБРАБОТЧИКИ КОМАНД БОТА (/start + рефералы + оплата)
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: Optional[CommandObject] = None):
    uid = message.from_user.id
    fname = message.from_user.first_name or "Охотник"
    uname = message.from_user.username or ""

    referrer_id = None
    args = command.args if command else None
    if not args and message.text and " " in message.text:
        args = message.text.split(" ", 1)[1].strip()
    if args:
        clean_arg = args.replace("ref_", "").strip()
        if clean_arg.isdigit() and int(clean_arg) != uid:
            referrer_id = int(clean_arg)

    await get_or_create_user(uid, fname, uname, referrer_id=referrer_id)

    sep = "&" if "?" in WEBAPP_URL else "?"
    app_url = f"{WEBAPP_URL}{sep}uid={uid}"
    if referrer_id:
        app_url += f"&ref={referrer_id}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Открыть Радар DealHunter", web_app=WebAppInfo(url=app_url))],
            [InlineKeyboardButton(text="⭐️ Купить Stars со скидкой", url=BUNNY_STARS_URL)],
        ]
    )
    await message.answer(
        f"👋 <b>Добро пожаловать в DealHunter PRO, {fname}!</b>\n\n"
        "🔥 Наш радар в реальном времени находит выгодные товары на <b>Wildberries, Ozon и Avito</b> с прямыми ссылками на карточки.\n\n"
        "🎁 <b>Вам активировано 3 дня PRO-подписки бесплатно!</b>\n"
        "👥 Приглашайте друзей во вкладке «Профиль» и получайте <b>+2 дня PRO</b> за каждого друга.\n\n"
        "👇 Нажмите кнопку ниже, чтобы запустить приложение:",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    uid = message.from_user.id
    await extend_subscription_db(uid, days=30)
    await message.answer(
        "✅ <b>Оплата успешно подтверждена!</b>\n\n"
        "Вам начислено <b>+30 дней подписки DealHunter PRO</b>.\n"
        "Все прямые ссылки на лоты и фоновый мониторинг активированы!",
        parse_mode="HTML",
    )


@app.on_event("startup")
async def on_startup():
    await init_db()
    asyncio.create_task(resilient_bot_polling_loop())
    asyncio.create_task(monitor_scanner_loop())


# =====================================================================
# 6. REST API ЭНДПОИНТЫ (БЕЗ КИРИЛЛИЧЕСКИХ ЗАГОЛОВКОВ!)
# =====================================================================
def parse_request_params(request: Request) -> Dict[str, Any]:
    q_uid = request.query_params.get("uid") or "5123484212"
    uid = int(q_uid) if str(q_uid).isdigit() else 5123484212

    q_name = request.query_params.get("name") or "Охотник"
    try:
        fname = unquote(q_name)
    except Exception:
        fname = "Охотник"

    q_ref = request.query_params.get("ref") or ""
    q_ref_clean = str(q_ref).replace("ref_", "").strip()
    ref_id = int(q_ref_clean) if (q_ref_clean.isdigit() and int(q_ref_clean) != uid) else None

    return {"uid": uid, "name": fname, "ref_id": ref_id}


@app.get("/health")
@app.head("/")
async def health():
    return {"status": "ok", "bot": BOT_USERNAME_CACHE, "db": "postgres" if pg_pool else "sqlite"}


@app.get("/api/state")
async def api_user_state(request: Request):
    p = parse_request_params(request)
    user = await get_or_create_user(uid=p["uid"], first_name=p["name"], referrer_id=p["ref_id"])
    monitors = await db_get_monitors(p["uid"])
    return JSONResponse({"user": user, "monitors": monitors})


@app.post("/api/claim-trial")
async def api_claim_trial(request: Request):
    p = parse_request_params(request)
    user = await activate_trial_db(p["uid"])
    return JSONResponse({"status": "ok", "user": user})


@app.get("/api/live-feed")
async def api_live_feed(
    request: Request,
    category: str = "all",
    source: str = "all",
    q: str = "",
):
    p = parse_request_params(request)
    user = await get_or_create_user(p["uid"], p["name"], referrer_id=p["ref_id"])
    if not user["subActive"]:
        return JSONResponse({"locked": True, "reason": "SUBSCRIPTION_REQUIRED", "items": []})
    items = await build_combined_feed(category=category, source=source, search_query=q)
    return JSONResponse({"locked": False, "items": items, "updatedAt": now_utc().strftime("%H:%M:%S")})


class MonitorCreateReq(BaseModel):
    title: str
    city: str = "Москва"
    source: str = "all"
    minMargin: int = 15


@app.post("/api/monitors")
async def api_add_monitor(data: MonitorCreateReq, request: Request):
    p = parse_request_params(request)
    uid = p["uid"]
    user = await get_or_create_user(uid)
    if not user["subActive"]:
        raise HTTPException(status_code=403, detail="Требуется подписка PRO")

    mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today) VALUES ($1, $2, $3, $4, $5, $6, TRUE, 0)",
                mid, uid, data.title.strip(), data.city.strip(), data.source, data.minMargin
            )
    else:
        cur = sqlite_conn.cursor()
        cur.execute(
            "INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin, active, found_today, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)",
            (mid, uid, data.title.strip(), data.city.strip(), data.source, data.minMargin, now_utc().isoformat())
        )
        sqlite_conn.commit()

    return JSONResponse({"status": "ok", "monitors": await db_get_monitors(uid)})


@app.post("/api/monitors/{mid}/toggle")
async def api_toggle_monitor(mid: str, request: Request):
    p = parse_request_params(request)
    uid = p["uid"]
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND user_id = $2", mid, uid)
    else:
        cur = sqlite_conn.cursor()
        cur.execute("UPDATE dh_monitors SET active = CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id = ? AND user_id = ?", (mid, uid))
        sqlite_conn.commit()
    return JSONResponse({"status": "ok", "monitors": await db_get_monitors(uid)})


@app.delete("/api/monitors/{mid}")
async def api_delete_monitor(mid: str, request: Request):
    p = parse_request_params(request)
    uid = p["uid"]
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM dh_monitors WHERE id = $1 AND user_id = $2", mid, uid)
    else:
        cur = sqlite_conn.cursor()
        cur.execute("DELETE FROM dh_monitors WHERE id = ? AND user_id = ?", (mid, uid))
        sqlite_conn.commit()
    return JSONResponse({"status": "ok", "monitors": await db_get_monitors(uid)})


class SavedUpdateReq(BaseModel):
    savedIds: List[str]


@app.post("/api/saved")
async def api_save_lots(data: SavedUpdateReq, request: Request):
    p = parse_request_params(request)
    await update_saved_db(p["uid"], data.savedIds)
    return JSONResponse({"status": "ok"})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(request: Request):
    p = parse_request_params(request)
    uid = p["uid"]
    if not bot:
        return JSONResponse({"error": "BOT_TOKEN не задан"}, status_code=400)
    try:
        invoice_link = await bot.create_invoice_link(
            title="DealHunter PRO — 30 дней",
            description="Полный доступ к прямым лотам Avito, Ozon, Wildberries и авто-мониторингу.",
            payload=f"pro_30d_{uid}_{int(now_utc().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
        )
        return JSONResponse({"invoiceUrl": invoice_link})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/push-lots")
async def api_push_external_lots(request: Request, x_worker_secret: Optional[str] = Header(None)):
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Invalid worker secret")
    payload = await request.json()
    lots = payload if isinstance(payload, list) else payload.get("lots", [])
    for lot in reversed(lots):
        EXTERNAL_LOTS_BUFFER.insert(0, lot)
    del EXTERNAL_LOTS_BUFFER[100:]
    return JSONResponse({"status": "ok", "buffered": len(EXTERNAL_LOTS_BUFFER)})


# =====================================================================
# 7. ИНТЕРФЕЙС LIQUID GLASS (МГНОВЕННЫЕ ЛОТЫ + ЖИВОЙ ТАЙМЕР)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    initial_lots_json = json.dumps(VERIFIED_DIRECT_LOTS, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DealHunter PRO</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/lucide@latest"></script>
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
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.25); --accent-soft: rgba(255, 90, 31, 0.12);
      --green: #18B259; --green-soft: rgba(24, 178, 89, 0.14);
    }
    [data-theme="dark"] {
      --bg-canvas: #040406; --bg-app: #0A0B0E;
      --glass-card: rgba(22, 24, 30, 0.78);
      --glass-pill: rgba(255, 255, 255, 0.07);
      --glass-dock: rgba(18, 20, 26, 0.88);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-stroke: rgba(255, 255, 255, 0.07);
      --text-primary: #F5F6FA; --text-secondary: #9499A6; --text-muted: #5B606E;
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.35); --accent-soft: rgba(255, 90, 31, 0.16);
      --green: #2AD16C; --green-soft: rgba(42, 209, 108, 0.15);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; user-select: none; }
    body { background: var(--bg-canvas); color: var(--text-primary); font-family: 'Inter', -apple-system, sans-serif; display: flex; justify-content: center; min-height: 100vh; overscroll-behavior-y: contain; }
    .t-num { font-family: 'Manrope', 'Inter', sans-serif; font-variant-numeric: tabular-nums; }
    .phone-shell { width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg-app); position: relative; overflow-x: hidden; padding-bottom: 112px; }
    .ambient-orb { position: fixed; width: 290px; height: 290px; border-radius: 50%; background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%); top: -90px; right: calc(50% - 210px); pointer-events: none; filter: blur(38px); }
    .screen-content { position: relative; z-index: 1; padding: 14px 14px 26px; animation: fadeIn 0.2s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes spin { 100% { transform: rotate(360deg); } }
    .spin { animation: spin 0.8s linear infinite; }

    .ptr-bar { height: 0; overflow: hidden; display: flex; align-items: center; justify-content: center; gap: 8px; font-size: 12px; font-weight: 700; color: var(--accent); transition: height 0.18s ease; background: var(--glass-pill); }
    .glass-card { background: var(--glass-card); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); border: 1px solid var(--glass-border); border-radius: 24px; padding: 16px; margin-bottom: 12px; }
    .top-header { position: sticky; top: 0; z-index: 20; background: var(--glass-dock); backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px); border-bottom: 1px solid var(--glass-stroke); padding: 12px 16px; }

    .search-wrap { position: relative; margin-top: 10px; display: flex; gap: 6px; }
    .search-input { width: 100%; height: 38px; padding: 0 34px 0 34px; border-radius: 13px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-primary); font-family: inherit; font-size: 13px; outline: none; }
    .search-input:focus { border-color: var(--accent); }
    .search-icon { position: absolute; left: 11px; top: 11px; color: var(--text-muted); pointer-events: none; }
    .search-clear { position: absolute; right: 86px; top: 9px; width: 20px; height: 20px; border-radius: 50%; border: none; background: var(--glass-stroke); color: var(--text-secondary); display: flex; align-items: center; justify-content: center; cursor: pointer; }

    .pills-scroll { display: flex; gap: 7px; overflow-x: auto; scrollbar-width: none; margin-top: 9px; }
    .pills-scroll::-webkit-scrollbar { display: none; }
    .cat-pill { height: 31px; padding: 0 13px; border-radius: 999px; border: 1px solid var(--glass-stroke); background: var(--glass-pill); color: var(--text-secondary); font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; cursor: pointer; }
    .cat-pill.active { background: var(--text-primary); color: var(--bg-app); border-color: transparent; }
    .spec-tag { display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 9px; border-radius: 9px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-secondary); font-size: 11px; font-weight: 500; }
    .profit-pill { display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px; border-radius: 12px; background: var(--accent-soft); color: var(--accent); font-weight: 800; font-size: 12px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border: none; font-family: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }
    .btn-icon { width: 36px; height: 36px; border-radius: 12px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-primary); }
    .btn-pill { height: 36px; padding: 0 14px; border-radius: 12px; font-size: 12px; }
    .btn-large { height: 48px; width: 100%; border-radius: 16px; font-size: 14px; font-weight: 700; }
    .btn-accent { background: linear-gradient(135deg, #FF5A1F, #FF7A45); color: #FFFFFF; box-shadow: 0 8px 20px -6px var(--accent-glow); }
    .btn-stars-bot { background: linear-gradient(135deg, rgba(255, 193, 7, 0.16), rgba(255, 90, 31, 0.16)); border: 1px solid rgba(255, 193, 7, 0.45); color: #FFC107; }
    .btn-glass { background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); }
    .floating-dock-wrap { position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%); width: calc(100% - 28px); max-width: 398px; z-index: 35; }
    .floating-dock { height: 64px; padding: 6px; border-radius: 24px; background: var(--glass-dock); backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px); border: 1px solid var(--glass-border); display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; box-shadow: 0 20px 40px -12px rgba(0,0,0,0.55); }
    .dock-btn { border: none; background: transparent; border-radius: 18px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; color: var(--text-secondary); font-size: 11px; font-weight: 600; cursor: pointer; }
    .dock-btn.active { background: var(--glass-pill); color: var(--accent); }
    .ios-switch { width: 48px; height: 28px; border-radius: 999px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); padding: 3px; cursor: pointer; position: relative; transition: background 0.2s; }
    .ios-switch.on { background: var(--accent); }
    .ios-switch-knob { width: 20px; height: 20px; border-radius: 50%; background: #FFFFFF; transition: transform 0.2s; }
    .ios-switch.on .ios-switch-knob { transform: translateX(20px); }
    .modal-backdrop { position: fixed; inset: 0; max-width: 430px; margin: 0 auto; background: rgba(0, 0, 0, 0.72); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); z-index: 60; display: flex; align-items: flex-end; }
    .modal-center { align-items: center; padding: 18px; }
    .modal-sheet { width: 100%; background: var(--bg-app); border-top: 1px solid var(--glass-border); border-radius: 28px 28px 0 0; padding: 22px 18px 28px; max-height: 85vh; overflow-y: auto; }
    .modal-card { width: 100%; background: var(--bg-app); border: 1px solid var(--glass-border); border-radius: 28px; padding: 24px 20px; box-shadow: 0 25px 60px rgba(0,0,0,0.65); }
    .input-glass { width: 100%; height: 44px; padding: 0 14px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); font-family: inherit; font-size: 14px; outline: none; margin-bottom: 10px; }
    .row-between { display: flex; align-items: center; justify-content: space-between; }
    .toast { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 90; background: var(--text-primary); color: var(--bg-app); padding: 10px 18px; border-radius: 99px; font-size: 12px; font-weight: 700; box-shadow: 0 10px 30px rgba(0,0,0,0.35); white-space: nowrap; }
    .src-badge { padding: 3px 8px; border-radius: 7px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.4px; }
    .src-Avito { background: rgba(0, 170, 255, 0.15); color: #29B6F6; }
    .src-Ozon { background: rgba(0, 91, 255, 0.18); color: #448AFF; }
    .src-Wildberries { background: rgba(203, 17, 171, 0.18); color: #E040FB; }
  </style>
</head>
<body>
  <div class="phone-shell" id="shell">
    <div class="ambient-orb"></div>
    <div id="ptr-indicator" class="ptr-bar">
      <i data-lucide="refresh-cw" id="ptr-icon" style="width:15px; height:15px;"></i>
      <span id="ptr-text">Потяните вниз для обновления...</span>
    </div>
    <div id="toast-container"></div>
    <div id="app"></div>
  </div>

  <script>
    const INITIAL_LOTS = __INITIAL_LOTS_JSON__;
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) { tg.ready(); tg.expand(); }

    const urlParams = new URLSearchParams(window.location.search);
    const tgUser = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user : null;

    function resolveCurrentUid() {
      if (tgUser && tgUser.id) return String(tgUser.id);
      const qUid = urlParams.get('uid');
      if (qUid && /^\\d+$/.test(qUid)) return qUid;
      let localUid = localStorage.getItem('dh_Guest_Uid');
      if (!localUid) {
        localUid = String(Math.floor(100000000 + Math.random() * 900000000));
        localStorage.setItem('dh_Guest_Uid', localUid);
      }
      return localUid;
    }

    const CURRENT_UID = resolveCurrentUid();
    const CURRENT_NAME = tgUser ? (tgUser.first_name || 'Охотник') : 'Охотник';
    const START_PARAM = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param)
      ? tg.initDataUnsafe.start_param
      : (urlParams.get('ref') || urlParams.get('startapp') || '');

    const BUNNY_STARS_LINK = "https://t.me/BunnyStarsBot?startapp=ref_5123484212";

    const state = {
      theme: 'dark',
      tab: 'feed',
      category: 'all',
      source: 'all',
      searchQuery: '',
      isRefreshing: false,
      lastUpdated: '',
      detailId: null,
      showNewMonitor: false,
      showPaywall: false,
      legalDoc: null,
      user: {
        user_id: CURRENT_UID,
        name: CURRENT_NAME,
        trialClaimed: true,
        subActive: true,
        subUntil: new Date(Date.now() + 259200 * 1000).toISOString(),
        secondsLeft: 259200,
        savedIds: [],
        refCount: 0,
        refDaysEarned: 0,
        refLink: `https://t.me/DealHunterAvito_bot?start=ref_${CURRENT_UID}`
      },
      listings: INITIAL_LOTS,
      monitors: []
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(n);

    function buildQueryUrl(path, extraParams = {}) {
      const refClean = String(START_PARAM).replace('ref_', '').trim();
      const params = new URLSearchParams({
        uid: String(CURRENT_UID),
        name: CURRENT_NAME,
        ref: /^\\d+$/.test(refClean) ? refClean : '',
        ...extraParams
      });
      return `${path}?${params.toString()}`;
    }

    function showToast(msg) {
      const box = document.getElementById('toast-container');
      box.innerHTML = `<div class="toast">${msg}</div>`;
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { box.innerHTML = ''; }, 2400);
    }

    function formatCountdown(sec) {
      if (!sec || sec <= 0) return 'Истёк';
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const pad = (v) => String(v).padStart(2, '0');
      if (d > 0) return `${d}д ${pad(h)}:${pad(m)}:${pad(s)}`;
      return `${pad(h)}:${pad(m)}:${pad(s)}`;
    }

    // Независимый ежесекундный счетчик — тикает всегда без зависаний!
    setInterval(() => {
      if (!state.user) return;
      if (state.user.subUntil) {
        const diff = Math.floor((new Date(state.user.subUntil).getTime() - Date.now()) / 1000);
        state.user.secondsLeft = Math.max(0, diff);
      } else if (state.user.secondsLeft > 0) {
        state.user.secondsLeft = Math.max(0, state.user.secondsLeft - 1);
      }
      state.user.subActive = state.user.secondsLeft > 0;

      const badge = document.getElementById('live-timer-badge');
      if (badge) {
        badge.textContent = state.user.subActive ? ('PRO · ' + formatCountdown(state.user.secondsLeft)) : 'PRO истёк';
      }
      const profTimer = document.getElementById('profile-timer-val');
      if (profTimer) {
        profTimer.textContent = formatCountdown(state.user.secondsLeft);
      }
    }, 1000);

    // Свайп вниз (Pull-to-refresh)
    let touchStartY = 0;
    let pullDist = 0;
    window.addEventListener('touchstart', (e) => {
      if (window.scrollY <= 5 && state.tab === 'feed' && !state.detailId) {
        touchStartY = e.touches[0].clientY;
      } else {
        touchStartY = 0;
      }
    }, { passive: true });

    window.addEventListener('touchmove', (e) => {
      if (!touchStartY) return;
      const y = e.touches[0].clientY;
      pullDist = Math.max(0, y - touchStartY);
      if (pullDist > 10 && window.scrollY <= 5) {
        const bar = document.getElementById('ptr-indicator');
        const txt = document.getElementById('ptr-text');
        if (bar) {
          bar.style.height = Math.min(54, pullDist * 0.45) + 'px';
          if (txt) txt.textContent = pullDist > 95 ? 'Отпустите для обновления...' : 'Потяните вниз для обновления...';
        }
      }
    }, { passive: true });

    window.addEventListener('touchend', async () => {
      const bar = document.getElementById('ptr-indicator');
      if (pullDist > 95 && state.tab === 'feed' && !state.isRefreshing) {
        await refreshFeedManual();
      } else if (bar) {
        bar.style.height = '0px';
      }
      touchStartY = 0;
      pullDist = 0;
    });

    async function refreshFeedManual() {
      const bar = document.getElementById('ptr-indicator');
      const txt = document.getElementById('ptr-text');
      state.isRefreshing = true;
      if (bar) bar.style.height = '46px';
      if (txt) txt.textContent = 'Сканируем новые лоты...';
      await loadFeed(true);
      state.isRefreshing = false;
      if (bar) bar.style.height = '0px';
    }

    async function syncState() {
      render(); // Сразу рисуем стартовые карточки без ожидания!
      try {
        const res = await fetch(buildQueryUrl('/api/state'));
        if (res.ok) {
          const data = await res.json();
          state.user = data.user;
          if (data.user.refLink) {
            state.user.refLink = data.user.refLink;
          }
          state.monitors = data.monitors || [];
          render();
          await loadFeed(false);
        }
      } catch (e) {
        render();
      }
    }

    async function loadFeed(isManualRefresh = false) {
      if (!state.user.subActive) {
        state.listings = [];
        render();
        return;
      }
      try {
        const res = await fetch(buildQueryUrl('/api/live-feed', {
          category: state.category,
          source: state.source,
          q: state.searchQuery || ''
        }));
        if (res.ok) {
          const data = await res.json();
          if (data.items && data.items.length > 0) {
            state.listings = data.items;
          }
          state.lastUpdated = data.updatedAt || '';
          if (isManualRefresh) showToast(`⚡ Лента обновлена (${state.listings.length} лотов)`);
        }
      } catch (e) {}
      render();
    }

    function executeSearch() {
      const inp = document.getElementById('feed-search-input');
      if (inp) {
        state.searchQuery = inp.value.trim();
        loadFeed(true);
      }
    }

    function clearSearch() {
      state.searchQuery = '';
      loadFeed();
    }

    async function claimFreeTrial() {
      try {
        const res = await fetch(buildQueryUrl('/api/claim-trial'), { method: 'POST' });
        if (res.ok) {
          const data = await res.json();
          state.user = data.user;
          showToast('🎉 PRO-доступ на 3 дня активирован!');
          await loadFeed();
        }
      } catch (e) {
        state.user.trialClaimed = true;
        render();
      }
    }

    async function buyProWithStars() {
      showToast('Создаём счёт Telegram Stars...');
      try {
        const res = await fetch(buildQueryUrl('/api/create-invoice'), { method: 'POST' });
        const data = await res.json();
        if (data.invoiceUrl) {
          if (tg && tg.openInvoice) {
            tg.openInvoice(data.invoiceUrl, async (status) => {
              if (status === 'paid') {
                showToast('🎉 Оплата принята! +30 дней PRO активированы');
                state.showPaywall = false;
                await syncState();
              }
            });
          } else {
            window.open(data.invoiceUrl, '_blank');
          }
        } else {
          showToast(data.error || 'Ошибка создания счёта');
        }
      } catch (e) {
        showToast('Ошибка соединения с сервером');
      }
    }

    function copyRefLink() {
      const link = state.user.refLink;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(link);
      }
      showToast('🔗 Реферальная ссылка скопирована!');
    }

    function shareRefLink() {
      const link = state.user.refLink;
      const text = "🔥 Лови радар выгодных товаров Avito, Ozon и Wildberries! Переходи по ссылке и забирай 3 дня PRO бесплатно:";
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`;
      openExternalLink(shareUrl);
    }

    function openExternalLink(url) {
      if (tg && tg.openTelegramLink && url.startsWith('https://t.me/')) {
        tg.openTelegramLink(url);
      } else if (tg && tg.openLink) {
        tg.openLink(url);
      } else {
        window.open(url, '_blank');
      }
    }

    async function toggleSaveLot(lotId) {
      const idx = state.user.savedIds.indexOf(lotId);
      if (idx >= 0) state.user.savedIds.splice(idx, 1);
      else state.user.savedIds.push(lotId);
      render();
      await fetch(buildQueryUrl('/api/saved'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ savedIds: state.user.savedIds })
      });
    }

    async function createMonitorAction() {
      const t = document.getElementById('m-title').value.trim();
      const c = document.getElementById('m-city').value.trim() || 'Москва';
      const s = document.getElementById('m-source').value;
      const mg = parseInt(document.getElementById('m-margin').value) || 15;
      if (!t) { showToast('Введите название товара'); return; }
      const res = await fetch(buildQueryUrl('/api/monitors'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: t, city: c, source: s, minMargin: mg })
      });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        state.showNewMonitor = false;
        showToast('⚡ Монитор запущен!');
        render();
      } else {
        state.showNewMonitor = false;
        state.showPaywall = true;
        render();
      }
    }

    async function toggleMonitorAction(mid) {
      const res = await fetch(buildQueryUrl(`/api/monitors/${mid}/toggle`), { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        render();
      }
    }

    async function deleteMonitorAction(mid) {
      const res = await fetch(buildQueryUrl(`/api/monitors/${mid}`), { method: 'DELETE' });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        showToast('Монитор удалён');
        render();
      }
    }

    function renderLockedState() {
      return `
        <div class="glass-card" style="text-align:center; padding: 28px 18px; border-color: rgba(255,90,31,0.35);">
          <div style="width:56px; height:56px; border-radius:18px; background:var(--accent-soft); color:var(--accent); display:flex; align-items:center; justify-content:center; margin:0 auto 14px;">
            <i data-lucide="lock" style="width:28px; height:28px;"></i>
          </div>
          <h2 style="font-size:19px; font-weight:800; margin-bottom:8px;">Доступ к лотам закрыт</h2>
          <p style="font-size:13px; color:var(--text-secondary); line-height:1.5; margin-bottom:18px;">
            Для просмотра прямых карточек товаров <b>Wildberries, Ozon и Avito</b>, поиска и работы авто-мониторов необходима подписка <b>DealHunter PRO</b>.
          </p>
          <button class="btn btn-large btn-accent" style="margin-bottom:10px;" onclick="buyProWithStars()">
            <i data-lucide="zap" style="width:18px; height:18px;"></i>
            <span>Активировать PRO · 350 Stars</span>
          </button>
          <button class="btn btn-large btn-stars-bot" onclick="openExternalLink('${BUNNY_STARS_LINK}')">
            <i data-lucide="sparkles" style="width:18px; height:18px;"></i>
            <span>Купить Telegram Stars со скидкой</span>
          </button>
        </div>
      `;
    }

    function renderLotCard(item) {
      const isSaved = state.user.savedIds.includes(item.id);
      return `
        <div class="glass-card" onclick="state.detailId='${item.id}'; window.scrollTo(0,0); render();" style="cursor:pointer;">
          <div class="row-between" style="margin-bottom: 8px;">
            <div style="display:flex; align-items:center; gap: 7px;">
              <span class="src-badge src-${item.source}">${item.source}</span>
              <span style="font-size: 12px; color: var(--text-secondary);">${item.city} · ${item.timeAgo}</span>
            </div>
          </div>
          <h3 style="font-size: 15px; font-weight: 700; margin-bottom: 9px; line-height: 1.35;">${item.title}</h3>
          <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 12px;">
            ${(item.tags || []).map(t => `<span class="spec-tag"><i data-lucide="${t.icon}" style="width:12px; height:12px; color:var(--accent);"></i>${t.text}</span>`).join('')}
          </div>
          <div class="row-between" style="padding: 10px 12px; border-radius: 14px; background: var(--glass-pill); margin-bottom: 12px;">
            <div>
              <div class="t-num" style="font-size: 20px; font-weight: 800;">${fmt(item.price)} ₽</div>
              <div class="t-num" style="font-size: 11px; color: var(--text-muted);">Рынок ${fmt(item.market)} ₽</div>
            </div>
            <div class="profit-pill t-num">−${item.profitPct}% · ${fmt(item.profitRub)} ₽</div>
          </div>
          <div class="row-between">
            <span style="font-size: 12px; color: var(--text-secondary);">★ ${item.seller.rating} (${item.seller.reviews} отз.)</span>
            <div style="display:flex; gap:6px;">
              <button class="btn btn-icon" style="width:34px; height:34px;" onclick="event.stopPropagation(); toggleSaveLot('${item.id}');">
                <i data-lucide="heart" style="width:15px; height:15px; ${isSaved ? 'fill:var(--accent); stroke:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-pill btn-accent" style="height:34px; padding:0 13px;" onclick="event.stopPropagation(); openExternalLink('${item.url}');">
                <span>Забрать лот</span>
              </button>
            </div>
          </div>
        </div>
      `;
    }

    function render() {
      const app = document.getElementById('app');

      if (state.detailId) {
        const item = state.listings.find(x => x.id === state.detailId);
        if (item) {
          app.innerHTML = `
            <div class="top-header row-between">
              <button class="btn btn-pill btn-glass" onclick="state.detailId = null; render();">
                <i data-lucide="chevron-left" style="width:16px; height:16px;"></i><span>Назад</span>
              </button>
              <span class="src-badge src-${item.source}">${item.source}</span>
            </div>
            <div class="screen-content">
              <div class="glass-card">
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:6px;">${item.city} · ${item.timeAgo}</div>
                <h1 style="font-size:18px; font-weight:800; margin-bottom:12px; line-height:1.35;">${item.title}</h1>
                <div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px;">
                  ${(item.tags || []).map(t => `<span class="spec-tag"><i data-lucide="${t.icon}" style="width:12px; height:12px; color:var(--accent);"></i>${t.text}</span>`).join('')}
                </div>
                <div class="row-between" style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:14px;">
                  <div>
                    <div class="t-num" style="font-size:24px; font-weight:800;">${fmt(item.price)} ₽</div>
                    <div class="t-num" style="font-size:12px; color:var(--text-muted);">Рыночная медиана: ${fmt(item.market)} ₽</div>
                  </div>
                  <div class="profit-pill t-num">−${item.profitPct}%</div>
                </div>
                <div style="padding:10px 12px; border-radius:12px; background:var(--glass-pill); margin-bottom:12px; font-size:12px;">
                  <b>Продавец:</b> ${item.seller.name} · ★ ${item.seller.rating} (${item.seller.reviews} отзывов)
                </div>
                <p style="font-size:13px; line-height:1.55; color:var(--text-secondary); margin-bottom:16px;">${item.desc}</p>
                <button class="btn btn-large btn-accent" onclick="openExternalLink('${item.url}')">
                  <i data-lucide="external-link" style="width:17px; height:17px;"></i>
                  <span>Открыть карточку на ${item.source}</span>
                </button>
              </div>
            </div>
          `;
          lucide.createIcons();
          return;
        }
      }

      let contentHtml = '';
      const timerBadgeHtml = `
        <div id="live-timer-badge" onclick="state.showPaywall=true; render();" class="t-num" style="cursor:pointer; padding:5px 10px; border-radius:10px; font-size:11px; font-weight:800; background:${state.user.subActive ? 'var(--green-soft)' : 'var(--accent-soft)'}; color:${state.user.subActive ? 'var(--green)' : 'var(--accent)'};">
          ${state.user.subActive ? ('PRO · ' + formatCountdown(state.user.secondsLeft)) : 'PRO истёк'}
        </div>
      `;

      if (state.tab === 'feed') {
        contentHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap:8px;">
                <div style="width:32px; height:32px; border-radius:10px; background:var(--accent); display:flex; align-items:center; justify-content:center; color:#fff;">
                  <i data-lucide="radar" style="width:17px; height:17px;"></i>
                </div>
                <div>
                  <div style="font-size:16px; font-weight:800; line-height:1.1;">DealHunter</div>
                  <div style="font-size:10px; color:var(--text-muted);">Свайп вниз — обновить${state.lastUpdated ? ' · ' + state.lastUpdated : ''}</div>
                </div>
              </div>
              <div style="display:flex; align-items:center; gap:6px;">
                ${timerBadgeHtml}
                <button class="btn btn-icon" title="Обновить лоты" onclick="refreshFeedManual()">
                  <i data-lucide="refresh-cw" class="${state.isRefreshing ? 'spin' : ''}" style="width:15px; height:15px;"></i>
                </button>
                <button class="btn btn-icon" onclick="state.theme = state.theme === 'dark' ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', state.theme); render();">
                  <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:15px; height:15px;"></i>
                </button>
              </div>
            </div>

            <div class="search-wrap">
              <i data-lucide="search" class="search-icon" style="width:15px; height:15px;"></i>
              <input
                type="text"
                id="feed-search-input"
                class="search-input"
                placeholder="Поиск (напр. iPhone 16, RTX 4070, Dyson)..."
                value="${state.searchQuery.replace(/"/g, '&quot;')}"
                onkeydown="if(event.key==='Enter') executeSearch();"
              />
              ${state.searchQuery ? `<button class="search-clear" onclick="clearSearch()"><i data-lucide="x" style="width:12px; height:12px;"></i></button>` : ''}
              <button class="btn btn-pill btn-accent" style="height:38px; padding:0 14px;" onclick="executeSearch()">Найти</button>
            </div>

            <div class="pills-scroll">
              <button class="cat-pill ${state.source === 'all' ? 'active' : ''}" onclick="state.source='all'; loadFeed();">Все площадки</button>
              <button class="cat-pill ${state.source === 'Wildberries' ? 'active' : ''}" onclick="state.source='Wildberries'; loadFeed();">Wildberries Live</button>
              <button class="cat-pill ${state.source === 'Ozon' ? 'active' : ''}" onclick="state.source='Ozon'; loadFeed();">Ozon</button>
              <button class="cat-pill ${state.source === 'Avito' ? 'active' : ''}" onclick="state.source='Avito'; loadFeed();">Avito</button>
            </div>
            <div class="pills-scroll" style="margin-top:6px;">
              <button class="cat-pill ${state.category === 'all' ? 'active' : ''}" onclick="state.category='all'; state.searchQuery=''; loadFeed();">Все товары</button>
              <button class="cat-pill ${state.category === 'iphone' ? 'active' : ''}" onclick="state.category='iphone'; state.searchQuery=''; loadFeed();">iPhone</button>
              <button class="cat-pill ${state.category === 'macbook' ? 'active' : ''}" onclick="state.category='macbook'; state.searchQuery=''; loadFeed();">MacBook</button>
              <button class="cat-pill ${state.category === 'gpu' ? 'active' : ''}" onclick="state.category='gpu'; state.searchQuery=''; loadFeed();">Видеокарты</button>
              <button class="cat-pill ${state.category === 'ps5' ? 'active' : ''}" onclick="state.category='ps5'; state.searchQuery=''; loadFeed();">Консоли</button>
            </div>
          </div>
          <div class="screen-content">
            ${!state.user.subActive ? renderLockedState() : (state.listings.length ? state.listings.map(renderLotCard).join('') : '<div class="glass-card" style="text-align:center;">Лоты загружаются...</div>')}
          </div>
        `;
      }

      if (state.tab === 'monitors') {
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:17px; font-weight:800;">Авто-мониторы</div>
            <button class="btn btn-pill btn-accent" onclick="if(!state.user.subActive){state.showPaywall=true;}else{state.showNewMonitor=true;} render();">
              <i data-lucide="plus" style="width:14px; height:14px;"></i><span>Создать</span>
            </button>
          </div>
          <div class="screen-content">
            ${!state.user.subActive ? renderLockedState() : (
              state.monitors.length ? state.monitors.map(m => `
                <div class="glass-card">
                  <div class="row-between" style="margin-bottom:8px;">
                    <div>
                      <div style="font-size:15px; font-weight:800;">${m.title}</div>
                      <div style="font-size:12px; color:var(--text-secondary);">${m.source === 'all' ? 'WB + Ozon + Avito' : m.source} · ${m.city} · от ${m.minMargin}%</div>
                    </div>
                    <div class="ios-switch ${m.active ? 'on' : ''}" onclick="toggleMonitorAction('${m.id}')">
                      <div class="ios-switch-knob"></div>
                    </div>
                  </div>
                  <div class="row-between" style="padding-top:8px; border-top:1px solid var(--glass-stroke);">
                    <span style="font-size:12px; color:var(--green); font-weight:600;">⚡ Отправлено в бот: ${m.foundToday}</span>
                    <button class="btn btn-icon" style="width:30px; height:30px;" onclick="deleteMonitorAction('${m.id}')">
                      <i data-lucide="trash-2" style="width:14px; height:14px; color:var(--text-secondary);"></i>
                    </button>
                  </div>
                </div>
              `).join('') : '<div class="glass-card" style="text-align:center; padding:26px;">У вас пока нет активных мониторов. Нажмите «Создать», чтобы бот присылал прямые ссылки на лоты в Telegram!</div>'
            )}
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
            ${!state.user.subActive ? renderLockedState() : (savedList.length ? savedList.map(renderLotCard).join('') : '<div class="glass-card" style="text-align:center; padding:28px;">Сохранённых лотов пока нет</div>')}
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
                  <div style="font-size:17px; font-weight:800;">${state.user.name}</div>
                  <div style="font-size:12px; color:var(--text-secondary);">Telegram ID: ${state.user.user_id}</div>
                </div>
                <span class="profit-pill">${state.user.subActive ? 'PRO АКТИВЕН' : 'FREE'}</span>
              </div>

              <div style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:14px;">
                <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">Остаток времени PRO-доступа:</div>
                <div id="profile-timer-val" class="t-num" style="font-size:20px; font-weight:800; color:${state.user.subActive ? 'var(--green)' : 'var(--accent)'};">
                  ${formatCountdown(state.user.secondsLeft)}
                </div>
              </div>

              <button class="btn btn-large btn-accent" style="margin-bottom:10px;" onclick="buyProWithStars()">
                <i data-lucide="star" style="width:18px; height:18px;"></i>
                <span>Продлить PRO (+30 дней) · 350 Stars</span>
              </button>

              <button class="btn btn-large btn-stars-bot" onclick="openExternalLink('${BUNNY_STARS_LINK}')">
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
                    <div style="font-size:11px; color:var(--text-secondary);">+2 дня PRO за каждого друга</div>
                  </div>
                </div>
              </div>

              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin: 12px 0;">
                <div style="padding:10px 12px; border-radius:14px; background:var(--glass-pill); text-align:center;">
                  <div class="t-num" style="font-size:20px; font-weight:800;">${state.user.refCount || 0}</div>
                  <div style="font-size:11px; color:var(--text-secondary);">Приглашено друзей</div>
                </div>
                <div style="padding:10px 12px; border-radius:14px; background:var(--glass-pill); text-align:center;">
                  <div class="t-num" style="font-size:20px; font-weight:800; color:var(--green);">+${state.user.refDaysEarned || 0} дн.</div>
                  <div style="font-size:11px; color:var(--text-secondary);">Бонусных дней PRO</div>
                </div>
              </div>

              <div style="padding:10px 12px; border-radius:12px; background:var(--glass-pill); font-size:11px; color:var(--text-secondary); word-break:break-all; margin-bottom:10px;">
                ${state.user.refLink}
              </div>

              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <button class="btn btn-pill btn-glass" style="height:40px;" onclick="copyRefLink()">
                  <i data-lucide="copy" style="width:15px; height:15px;"></i>
                  <span>Скопировать</span>
                </button>
                <button class="btn btn-pill btn-accent" style="height:40px;" onclick="shareRefLink()">
                  <i data-lucide="share-2" style="width:15px; height:15px;"></i>
                  <span>Пригласить</span>
                </button>
              </div>
            </div>

            <div class="glass-card">
              <div style="font-size:13px; font-weight:700; margin-bottom:10px;">Правовая информация (РФ)</div>
              <div style="display:flex; flex-direction:column; gap:8px;">
                <button class="btn btn-glass" style="height:40px; border-radius:12px; justify-content:space-between; padding:0 12px; font-size:12px;" onclick="state.legalDoc='privacy'; render();">
                  <span>Политика конфиденциальности (152-ФЗ)</span>
                  <i data-lucide="chevron-right" style="width:15px; height:15px;"></i>
                </button>
                <button class="btn btn-glass" style="height:40px; border-radius:12px; justify-content:space-between; padding:0 12px; font-size:12px;" onclick="state.legalDoc='terms'; render();">
                  <span>Пользовательское соглашение (Публичная оферта)</span>
                  <i data-lucide="chevron-right" style="width:15px; height:15px;"></i>
                </button>
                <button class="btn btn-glass" style="height:40px; border-radius:12px; justify-content:space-between; padding:0 12px; font-size:12px;" onclick="state.legalDoc='disclaimer'; render();">
                  <span>Отказ от ответственности и правообладателям</span>
                  <i data-lucide="chevron-right" style="width:15px; height:15px;"></i>
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
                Мы начислили вам бесплатный доступ к радару находок <b>Wildberries, Ozon и Avito</b> и авто-уведомлениям в Telegram на 3 дня!
              </p>
              <button class="btn btn-large btn-accent" onclick="claimFreeTrial()">
                <i data-lucide="zap" style="width:18px; height:18px;"></i>
                <span>Забрать 3 дня PRO бесплатно</span>
              </button>
            </div>
          </div>
        `;
      } else if (state.showNewMonitor) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showNewMonitor = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <div style="font-size:17px; font-weight:800;">Новый авто-монитор</div>
                <button class="btn btn-icon" onclick="state.showNewMonitor = false; render();"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <input type="text" id="m-title" class="input-glass" placeholder="Что ищем (напр. iPhone 15 Pro)" />
              <input type="text" id="m-city" class="input-glass" placeholder="Город или регион" value="Москва" />
              <select id="m-source" class="input-glass">
                <option value="all">Все площадки (WB + Ozon + Avito)</option>
                <option value="Wildberries">Только Wildberries</option>
                <option value="Ozon">Только Ozon</option>
                <option value="Avito">Только Avito</option>
              </select>
              <input type="number" id="m-margin" class="input-glass" placeholder="Мин. выгода от рынка (%)" value="18" />
              <button class="btn btn-large btn-accent" onclick="createMonitorAction()">Запустить мониторинг</button>
            </div>
          </div>
        `;
      } else if (state.showPaywall) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showPaywall = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <span class="profit-pill">DealHunter PRO</span>
                <button class="btn btn-icon" onclick="state.showPaywall = false; render();"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <div class="t-num" style="font-size:26px; font-weight:800; margin-bottom:6px;">350 Stars <span style="font-size:13px; color:var(--text-muted);">/ 30 дней</span></div>
              <p style="font-size:13px; color:var(--text-secondary); line-height:1.45; margin-bottom:16px;">
                Полный доступ к прямым карточкам товаров Wildberries, Ozon и Avito, живому поиску и мгновенным пуш-уведомлениям в Telegram.
              </p>
              <button class="btn btn-large btn-accent" style="margin-bottom:10px;" onclick="buyProWithStars()">
                <i data-lucide="star" style="width:18px; height:18px;"></i>
                <span>Оплатить 350 Stars</span>
              </button>
              <button class="btn btn-large btn-stars-bot" onclick="openExternalLink('${BUNNY_STARS_LINK}')">
                <i data-lucide="external-link" style="width:17px; height:17px;"></i>
                <span>Где взять Stars? Купить в BunnyStars</span>
              </button>
            </div>
          </div>
        `;
      } else if (state.legalDoc) {
        const docs = {
          privacy: {
            title: 'Политика конфиденциальности (152-ФЗ)',
            body: '1. Общие положения: Настоящая Политика обработки персональных данных составлена в соответствии с Федеральным законом РФ от 27.07.2006 № 152-ФЗ «О персональных данных».\\n\\n2. Собираемые данные: Сервис обрабатывает исключительно публичный идентификатор аккаунта Telegram (Telegram ID), имя пользователя и технические настройки фильтров внутри мини-приложения.\\n\\n3. Цель обработки: Предоставление доступа к функционалу информационного мониторинга, учет реферальных бонусов и отправка уведомлений через Telegram-бота.\\n\\n4. Защита и удаление: Данные хранятся в защищенной СУБД. Пользователь вправе запросить полное удаление своих данных в любой момент.'
          },
          terms: {
            title: 'Пользовательское соглашение (Оферта)',
            body: '1. Предмет соглашения: Сервис DealHunter предоставляет Пользователю информационно-аналитический доступ к инструментам агрегации общедоступных карточек товаров и цен в соответствии со ст. 437 ГК РФ.\\n\\n2. Оплата и доступ: Расширенный доступ PRO предоставляется на условиях подписки сроком на 30 календарных дней с использованием внутренней валюты платформы Telegram Stars.\\n\\n3. Реферальная программа: За каждого нового пользователя, перешедшего по персональной ссылке, начисляется 2 бонусных дня подписки PRO.'
          },
          disclaimer: {
            title: 'Отказ от ответственности',
            body: '1. Информационный характер: DealHunter является независимым поисковым агрегатором и не является официальным приложением или представителем ООО «КЕХ еКоммерц» (Авито), ООО «Интернет Решения» (Ozon) или ООО «РВБ» / «Вайлдберриз».\\n\\n2. Все товарные знаки принадлежат их законным владельцам.\\n\\n3. Администрация сервиса не является стороной сделок купли-продажи и не несет ответственности за действия сторонних продавцов.'
          }
        };
        const curDoc = docs[state.legalDoc];
        modalHtml = `
          <div class="modal-backdrop" onclick="state.legalDoc = null; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <div style="font-size:15px; font-weight:800;">${curDoc.title}</div>
                <button class="btn btn-icon" onclick="state.legalDoc = null; render();"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <div style="font-size:12px; color:var(--text-secondary); line-height:1.6; white-space:pre-line; margin-bottom:16px;">${curDoc.body}</div>
              <button class="btn btn-large btn-glass" onclick="state.legalDoc = null; render();">Понятно</button>
            </div>
          </div>
        `;
      }

      const dockHtml = `
        <div class="floating-dock-wrap">
          <nav class="floating-dock">
            <button class="dock-btn ${state.tab === 'feed' ? 'active' : ''}" onclick="state.tab='feed'; render();"><i data-lucide="flame" style="width:19px; height:19px;"></i><span>Находки</span></button>
            <button class="dock-btn ${state.tab === 'monitors' ? 'active' : ''}" onclick="state.tab='monitors'; render();"><i data-lucide="radar" style="width:19px; height:19px;"></i><span>Мониторы</span></button>
            <button class="dock-btn ${state.tab === 'saved' ? 'active' : ''}" onclick="state.tab='saved'; render();"><i data-lucide="heart" style="width:19px; height:19px;"></i><span>Избранное</span></button>
            <button class="dock-btn ${state.tab === 'profile' ? 'active' : ''}" onclick="state.tab='profile'; render();"><i data-lucide="user" style="width:19px; height:19px;"></i><span>Профиль</span></button>
          </nav>
        </div>
      `;

      app.innerHTML = contentHtml + modalHtml + dockHtml;
      lucide.createIcons();
    }

    syncState();
  </script>
</body>
</html>
"""
    return html.replace("__INITIAL_LOTS_JSON__", initial_lots_json)
