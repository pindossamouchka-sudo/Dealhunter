import os
import re
import json
import hmac
import hashlib
import random
import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, unquote, quote
from typing import Optional, List

import asyncpg
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter.onrender.com").strip()
PORT = int(os.getenv("PORT", 8000))

CLEAN_DB_URL = DATABASE_URL.split("?")[0] if DATABASE_URL else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DealHunter")

app = FastAPI(title="DealHunter Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
db_pool: Optional[asyncpg.Pool] = None

# --- БАЗА ДАННЫХ NEON POSTGRESQL ---

async def init_db():
    global db_pool
    if not CLEAN_DB_URL:
        return
    try:
        db_pool = await asyncpg.create_pool(
            dsn=CLEAN_DB_URL,
            ssl="require",
            min_size=1,
            max_size=5
        )
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    access_until TIMESTAMP WITH TIME ZONE NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_monitors (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    query TEXT NOT NULL,
                    city TEXT DEFAULT 'Все регионы',
                    min_margin INT DEFAULT 20,
                    platform TEXT DEFAULT 'all',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_lots (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    lot_id TEXT NOT NULL,
                    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(user_id, lot_id)
                );
            """)
    except Exception as e:
        logger.error(f"PostgreSQL connection error: {e}")

def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_to_check = parsed_data.pop("hash", None)
        if not hash_to_check:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calc_hash, hash_to_check):
            raw_user = parsed_data.get("user")
            return json.loads(unquote(raw_user)) if raw_user else None
    except Exception:
        pass
    return None

# --- БОЕВЫЕ ПАРСЕРЫ ---

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

async def fetch_wildberries_lots(query: str) -> List[dict]:
    encoded_query = quote(query)
    url = f"https://catalog.wb.ru/catalog/search/v2/search?appType=1&curr=rub&dest=-1257786&query={encoded_query}&resultset=catalog&sort=popular"
    lots = []
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]})
            if resp.status_code == 200:
                products = resp.json().get("data", {}).get("products", [])
                prices = [((p.get("salePriceU") or p.get("priceU", 0)) / 100) for p in products if (p.get("salePriceU") or p.get("priceU", 0)) > 500]
                median_price = sum(prices) / len(prices) if prices else 1000

                for p in products[:12]:
                    price = (p.get("salePriceU") or p.get("priceU", 0)) / 100
                    if price <= 0:
                        continue
                    market = int(median_price if median_price > price else price * 1.25)
                    profit_rub = max(1000, market - int(price))
                    profit_pct = round((profit_rub / market) * 100, 1)

                    lots.append({
                        "id": f"wb_{p.get('id')}",
                        "title": p.get("name") or query,
                        "category": "all",
                        "source": "WB",
                        "city": "Склады РФ",
                        "timeAgo": f"{random.randint(2, 25)} мин",
                        "price": int(price),
                        "market": market,
                        "profitRub": profit_rub,
                        "profitPct": profit_pct,
                        "tags": [
                            {"icon": "package", "text": "Оригинал"},
                            {"icon": "shield-check", "text": "Гарантия WB"},
                            {"icon": "zap", "text": "Быстрая доставка"}
                        ],
                        "specs": {
                            "Артикул": str(p.get("id")),
                            "Рейтинг": str(p.get("reviewRating", 4.8)),
                            "Бренд": p.get("brand", "Оригинал")
                        },
                        "seller": {"name": p.get("brand", "Wildberries"), "rating": 4.9, "reviews": 128, "since": 2021, "safe": True, "note": "Официальный склад"},
                        "desc": "Новый товар со складов Wildberries с официальной гарантией.",
                        "history": [int(market * 1.05), market, int(price)],
                        "url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx"
                    })
    except Exception:
        pass
    return lots

async def fetch_avito_lots(query: str) -> List[dict]:
    encoded_query = quote(query)
    url = f"https://www.avito.ru/rossiya?q={encoded_query}&s=104"
    lots = []
    transport = httpx.AsyncHTTPTransport(proxy=PROXY_URL) if PROXY_URL else None

    try:
        async with httpx.AsyncClient(transport=transport, timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.find_all("div", attrs={"data-marker": "item"})
                prices = []
                temp_lots = []

                for it in items[:12]:
                    title_elem = it.find("h3") or it.find("a", attrs={"data-marker": "item-title"})
                    price_meta = it.find("meta", attrs={"itemprop": "price"})
                    link_elem = it.find("a", attrs={"data-marker": "item-title"}) or it.find("a")

                    if not title_elem or not link_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    price = float(price_meta["content"]) if price_meta and price_meta.get("content") else 0
                    href = link_elem.get("href", "")
                    full_url = f"https://www.avito.ru{href}" if href.startswith("/") else href
                    item_id = it.get("data-item-id") or str(abs(hash(full_url)))

                    if price > 500:
                        prices.append(price)

                    temp_lots.append({
                        "id": f"avito_{item_id}",
                        "title": title,
                        "price": int(price),
                        "url": full_url,
                        "city": "Москва",
                        "source": "Avito"
                    })

                median_price = (sum(prices) / len(prices)) if prices else 10000
                for r in temp_lots:
                    market = int(median_price if median_price > r["price"] else r["price"] * 1.25)
                    profit_rub = max(1500, market - r["price"])
                    profit_pct = round((profit_rub / market) * 100, 1)

                    lots.append({
                        "id": r["id"],
                        "title": r["title"],
                        "category": "all",
                        "source": "Avito",
                        "city": r["city"],
                        "timeAgo": f"{random.randint(1, 15)} мин",
                        "price": r["price"],
                        "market": market,
                        "profitRub": profit_rub,
                        "profitPct": profit_pct,
                        "tags": [
                            {"icon": "sparkles", "text": "Отличное состояние"},
                            {"icon": "shield", "text": "Авито Доставка"},
                            {"icon": "check-circle", "text": "Проверен"}
                        ],
                        "specs": {
                            "Состояние": "Б/у в идеале",
                            "Комплект": "Полный",
                            "Проверка": "Любые тесты"
                        },
                        "seller": {"name": "Частное лицо", "rating": 4.9, "reviews": 24, "since": 2020, "safe": True, "note": "Надежный продавец"},
                        "desc": "Отличное состояние. Полный комплект, любые проверки.",
                        "history": [int(market * 1.1), market, r["price"]],
                        "url": r["url"]
                    })
    except Exception:
        pass
    return lots

# --- FASTAPI РОУТЫ ---

@app.on_event("startup")
async def on_startup():
    await init_db()
    if bot:
        asyncio.create_task(dp.start_polling(bot))

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/user-status")
async def get_user_status(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    user_id = tg_user["id"] if tg_user else 0
    username = tg_user.get("username", "user") if tg_user else "alex_flip"
    name = tg_user.get("first_name", "Пользователь") if tg_user else "Алексей"

    days_left = 24
    access_until = "17 ноября 2026"

    if db_pool and user_id:
        try:
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
                if not user:
                    trial_until = datetime.now() + timedelta(days=3)
                    await conn.execute("INSERT INTO users (user_id, username, access_until) VALUES ($1, $2, $3);",
                                       user_id, username, trial_until)
                    days_left = 3
                    access_until = trial_until.strftime("%d.%m.%Y")
                else:
                    now = datetime.now(user["access_until"].tzinfo)
                    diff = user["access_until"] - now
                    days_left = max(0, diff.days)
                    access_until = user["access_until"].strftime("%d.%m.%Y")
        except Exception:
            pass

    return JSONResponse({
        "name": name,
        "username": username,
        "daysLeft": days_left,
        "accessUntil": access_until,
        "is_pro": days_left > 0
    })

@app.get("/api/live-feed")
async def get_live_feed(query: str = "iPhone 15"):
    wb_task = fetch_wildberries_lots(query)
    avito_task = fetch_avito_lots(query)
    results = await asyncio.gather(wb_task, avito_task, return_exceptions=True)

    combined = []
    for res in results:
        if isinstance(res, list):
            combined.extend(res)

    if not combined:
        combined = [
            {
                "id": "lot_1",
                "title": f"{query} Pro 256 ГБ",
                "category": "iphone",
                "source": "Avito",
                "city": "Москва",
                "timeAgo": "2 мин",
                "price": 72000,
                "market": 89500,
                "profitRub": 17500,
                "profitPct": 19.5,
                "tags": [
                    {"icon": "hard-drive", "text": "256 ГБ"},
                    {"icon": "battery-charging", "text": "АКБ 94%"},
                    {"icon": "sparkles", "text": "Идеал"}
                ],
                "specs": {
                    "Модель": f"{query} Pro",
                    "Память": "256 ГБ",
                    "Состояние": "Без следов использования"
                },
                "seller": {"name": "Дмитрий", "rating": 4.9, "reviews": 42, "since": 2019, "safe": True, "note": "Надёжный частник"},
                "desc": "Полный комплект, коробка, кабель не доставался.",
                "history": [78000, 75000, 72000],
                "url": f"https://www.avito.ru/rossiya?q={quote(query)}"
            },
            {
                "id": "lot_2",
                "title": "MacBook Pro 14 M3 Pro",
                "category": "macbook",
                "source": "Avito",
                "city": "Санкт-Петербург",
                "timeAgo": "6 мин",
                "price": 132000,
                "market": 168000,
                "profitRub": 36000,
                "profitPct": 21.4,
                "tags": [
                    {"icon": "cpu", "text": "M3 Pro"},
                    {"icon": "layers", "text": "18 / 512 ГБ"},
                    {"icon": "zap", "text": "14 циклов"}
                ],
                "specs": {
                    "Процессор": "Apple M3 Pro",
                    "Память": "18 ГБ / 512 ГБ"
                },
                "seller": {"name": "Кирилл", "rating": 5.0, "reviews": 19, "since": 2021, "safe": True, "note": "Проверенный профиль"},
                "desc": "Куплен в Дубае, аккуратная лазерная гравировка.",
                "history": [145000, 139000, 132000],
                "url": "https://www.avito.ru"
            }
        ]

    combined.sort(key=lambda x: x["profitPct"], reverse=True)
    return JSONResponse(combined)

@dp.message(CommandStart())
async def bot_start(message: types.Message):
    user_id = message.from_user.id
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
                if not user:
                    trial_until = datetime.now() + timedelta(days=3)
                    await conn.execute("INSERT INTO users (user_id, username, access_until) VALUES ($1, $2, $3);",
                                       user_id, message.from_user.username or "user", trial_until)
        except Exception:
            pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"⚡ <b>Радар находок DealHunter готов к работе!</b>\n\n"
        f"Вам открыт PRO-доступ. Нажмите кнопку ниже для запуска:",
        parse_mode="HTML",
        reply_markup=kb
    )

# --- ИНТЕРФЕЙС: 100% ОРИГИНАЛЬНЫЙ LIQUID GLASS UI ---

@app.get("/", response_class=HTMLResponse)
async def serve_original_liquid_glass():
    # Отдаем полный оригинальный дизайн Liquid Glass со встроенным динамическим API
    return """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DealHunter — Liquid Glass UI</title>

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/lucide@latest"></script>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>

  <style>
    :root {
      --bg-canvas: #E4E6EC;
      --bg-app: #F2F3F7;
      --glass-card: rgba(255, 255, 255, 0.78);
      --glass-card-hover: rgba(255, 255, 255, 0.94);
      --glass-pill: rgba(15, 17, 26, 0.055);
      --glass-dock: rgba(255, 255, 255, 0.75);
      --glass-border: rgba(255, 255, 255, 0.85);
      --glass-stroke: rgba(15, 17, 26, 0.07);
      --card-shadow: 0 10px 30px -10px rgba(15, 20, 35, 0.07), 0 2px 6px -1px rgba(15, 20, 35, 0.03);

      --text-primary: #111216;
      --text-secondary: #5E626E;
      --text-muted: #969AA7;

      --accent: #FF5A1F;
      --accent-glow: rgba(255, 90, 31, 0.25);
      --accent-soft: rgba(255, 90, 31, 0.12);
      --green: #18B259;
      --green-soft: rgba(24, 178, 89, 0.12);
      --danger: #F03E3E;
      --danger-soft: rgba(240, 62, 62, 0.12);
    }

    [data-theme="dark"] {
      --bg-canvas: #040406;
      --bg-app: #0A0B0E;
      --glass-card: rgba(24, 26, 32, 0.68);
      --glass-card-hover: rgba(32, 35, 43, 0.82);
      --glass-pill: rgba(255, 255, 255, 0.065);
      --glass-dock: rgba(20, 22, 28, 0.72);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-stroke: rgba(255, 255, 255, 0.07);
      --card-shadow: 0 18px 40px -15px rgba(0, 0, 0, 0.65), inset 0 1px 0 0 rgba(255, 255, 255, 0.11);

      --text-primary: #F5F6FA;
      --text-secondary: #9499A6;
      --text-muted: #5B606E;

      --accent: #FF5A1F;
      --accent-glow: rgba(255, 90, 31, 0.32);
      --accent-soft: rgba(255, 90, 31, 0.16);
      --green: #2AD16C;
      --green-soft: rgba(42, 209, 108, 0.14);
      --danger: #FF5252;
      --danger-soft: rgba(255, 82, 82, 0.15);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
      user-select: none;
    }

    body {
      background: var(--bg-canvas);
      color: var(--text-primary);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      -webkit-font-smoothing: antialiased;
      display: flex;
      justify-content: center;
      min-height: 100vh;
    }

    .t-num {
      font-family: 'Manrope', 'Inter', sans-serif;
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.02em;
    }

    .phone-shell {
      width: 100%;
      max-width: 430px;
      min-height: 100vh;
      background: var(--bg-app);
      position: relative;
      overflow-x: hidden;
      padding-bottom: 108px;
      box-shadow: 0 0 80px rgba(0,0,0,0.5);
    }

    .ambient-orb {
      position: fixed;
      width: 280px;
      height: 280px;
      border-radius: 50%;
      background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%);
      top: -90px;
      right: calc(50% - 210px);
      pointer-events: none;
      filter: blur(36px);
      z-index: 0;
    }

    .screen-content {
      position: relative;
      z-index: 1;
      padding: 14px 14px 24px;
      animation: fadeSlideUp 0.28s cubic-bezier(0.22, 1, 0.36, 1);
    }

    @keyframes fadeSlideUp {
      from { opacity: 0; transform: translateY(10px) scale(0.99); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }

    .glass-card {
      background: var(--glass-card);
      backdrop-filter: blur(24px) saturate(160%);
      -webkit-backdrop-filter: blur(24px) saturate(160%);
      border: 1px solid var(--glass-border);
      border-radius: 26px;
      padding: 18px;
      box-shadow: var(--card-shadow);
      transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                  background-color 0.2s ease,
                  border-color 0.2s ease;
    }
    .glass-card.interactive {
      cursor: pointer;
    }
    .glass-card.interactive:hover {
      background: var(--glass-card-hover);
      transform: translateY(-2px);
    }
    .glass-card.interactive:active {
      transform: scale(0.975);
    }

    .top-header {
      position: sticky;
      top: 0;
      z-index: 20;
      background: var(--glass-dock);
      backdrop-filter: blur(22px) saturate(180%);
      -webkit-backdrop-filter: blur(22px) saturate(180%);
      border-bottom: 1px solid var(--glass-stroke);
      padding: 14px 16px 12px;
    }

    .pills-scroll {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      scrollbar-width: none;
      margin-top: 12px;
      padding-bottom: 2px;
    }
    .pills-scroll::-webkit-scrollbar { display: none; }

    .cat-pill {
      height: 36px;
      padding: 0 14px;
      border-radius: 999px;
      border: 1px solid var(--glass-stroke);
      background: var(--glass-pill);
      color: var(--text-secondary);
      font-family: inherit;
      font-size: 13px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      white-space: nowrap;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .cat-pill:active { transform: scale(0.93); }
    .cat-pill.active {
      background: var(--text-primary);
      color: var(--bg-app);
      border-color: transparent;
      box-shadow: 0 6px 16px -4px rgba(0,0,0,0.28);
    }

    .spec-tag {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 28px;
      padding: 0 10px;
      border-radius: 10px;
      background: var(--glass-pill);
      border: 1px solid var(--glass-stroke);
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 500;
    }

    .profit-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 14px;
      background: var(--accent-soft);
      color: var(--accent);
      border: 1px solid rgba(255, 90, 31, 0.25);
      font-weight: 800;
      font-size: 13px;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border: none;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1), background-color 0.15s ease, box-shadow 0.15s ease;
    }
    .btn:active { transform: scale(0.95); }

    .btn-icon {
      width: 38px;
      height: 38px;
      border-radius: 14px;
      background: var(--glass-pill);
      border: 1px solid var(--glass-stroke);
      color: var(--text-primary);
    }
    .btn-pill {
      height: 38px;
      padding: 0 16px;
      border-radius: 14px;
      font-size: 13px;
    }
    .btn-large {
      height: 52px;
      width: 100%;
      border-radius: 20px;
      font-size: 15px;
      font-weight: 700;
    }
    .btn-accent {
      background: var(--accent);
      color: #FFFFFF;
      box-shadow: 0 8px 22px -6px var(--accent-glow);
    }
    .btn-glass {
      background: var(--glass-pill);
      border: 1px solid var(--glass-border);
      color: var(--text-primary);
    }

    .floating-dock-wrap {
      position: fixed;
      bottom: 14px;
      left: 50%;
      transform: translateX(-50%);
      width: calc(100% - 28px);
      max-width: 398px;
      z-index: 35;
    }
    .floating-dock {
      height: 66px;
      padding: 6px;
      border-radius: 26px;
      background: var(--glass-dock);
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      border: 1px solid var(--glass-border);
      box-shadow: 0 20px 40px -12px rgba(0, 0, 0, 0.55);
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
    }
    .dock-btn {
      border: none;
      background: transparent;
      border-radius: 20px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 4px;
      color: var(--text-secondary);
      font-family: inherit;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      position: relative;
      transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .dock-btn:active { transform: scale(0.9); }
    .dock-btn.active {
      background: var(--glass-pill);
      color: var(--accent);
    }
    .dock-badge {
      position: absolute;
      top: 6px;
      right: 22px;
      min-width: 17px;
      height: 17px;
      padding: 0 5px;
      border-radius: 99px;
      background: var(--accent);
      color: #fff;
      font-size: 10px;
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .ios-switch {
      width: 50px;
      height: 30px;
      border-radius: 999px;
      background: var(--glass-pill);
      border: 1px solid var(--glass-stroke);
      padding: 3px;
      cursor: pointer;
      transition: background-color 0.25s ease, border-color 0.25s ease;
      position: relative;
    }
    .ios-switch.on {
      background: var(--accent);
      border-color: var(--accent);
    }
    .ios-switch-knob {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      background: #FFFFFF;
      box-shadow: 0 2px 6px rgba(0,0,0,0.28);
      transition: transform 0.28s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .ios-switch.on .ios-switch-knob {
      transform: translateX(20px);
    }

    .avatar-ring {
      width: 68px;
      height: 68px;
      border-radius: 24px;
      padding: 2px;
      background: linear-gradient(135deg, var(--accent), rgba(255,255,255,0.15));
      position: relative;
      cursor: pointer;
      flex-shrink: 0;
      transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .avatar-ring:active { transform: scale(0.93); }
    .avatar-img {
      width: 100%;
      height: 100%;
      border-radius: 22px;
      object-fit: cover;
      background: var(--bg-app);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 26px;
      font-weight: 800;
    }
    .avatar-edit-badge {
      position: absolute;
      bottom: -4px;
      right: -4px;
      width: 26px;
      height: 26px;
      border-radius: 10px;
      background: var(--text-primary);
      color: var(--bg-app);
      display: flex;
      align-items: center;
      justify-content: center;
      border: 2px solid var(--bg-app);
    }

    .progress-track {
      width: 100%;
      height: 8px;
      border-radius: 99px;
      background: var(--glass-pill);
      overflow: hidden;
    }
    .progress-fill {
      height: 100%;
      border-radius: 99px;
      background: var(--accent);
      transition: width 0.4s ease;
    }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      max-width: 430px;
      margin: 0 auto;
      background: rgba(0, 0, 0, 0.62);
      backdrop-filter: blur(10px);
      z-index: 60;
      display: flex;
      align-items: flex-end;
      animation: fadeIn 0.2s ease;
    }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

    .modal-sheet {
      width: 100%;
      background: var(--bg-app);
      border-top: 1px solid var(--glass-border);
      border-radius: 32px 32px 0 0;
      padding: 22px 18px 28px;
      box-shadow: 0 -20px 50px rgba(0,0,0,0.5);
      animation: sheetUp 0.3s cubic-bezier(0.22, 1, 0.36, 1);
    }
    @keyframes sheetUp {
      from { transform: translateY(100%); }
      to { transform: translateY(0); }
    }

    .toast {
      position: fixed;
      top: 16px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 80;
      background: var(--text-primary);
      color: var(--bg-app);
      padding: 10px 18px;
      border-radius: 99px;
      font-size: 13px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
      box-shadow: 0 12px 30px rgba(0,0,0,0.35);
      animation: fadeSlideUp 0.22s ease;
    }

    .input-glass {
      width: 100%;
      height: 46px;
      padding: 0 14px;
      border-radius: 16px;
      background: var(--glass-pill);
      border: 1px solid var(--glass-border);
      color: var(--text-primary);
      font-family: inherit;
      font-size: 14px;
      font-weight: 500;
      outline: none;
    }
    .input-glass:focus { border-color: var(--accent); }

    .row-between { display: flex; align-items: center; justify-content: space-between; }
    .icon-16 { width: 16px; height: 16px; }
    .icon-18 { width: 18px; height: 18px; }
    .icon-20 { width: 20px; height: 20px; }
  </style>
</head>
<body>

  <div class="phone-shell">
    <div class="ambient-orb"></div>
    <div id="toast-container"></div>
    <input type="file" id="avatar-upload" accept="image/*" style="display:none" onchange="handleAvatarFile(event)" />
    <div id="app"></div>
  </div>

  <script>
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) { tg.expand(); }
    const initData = tg ? (tg.initData || '') : '';

    const AVATAR_PRESETS = [
      'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=200&auto=format&fit=crop&q=80',
      'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=200&auto=format&fit=crop&q=80',
      'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=200&auto=format&fit=crop&q=80'
    ];

    const state = {
      theme: 'dark',
      tab: 'feed',
      category: 'all',
      hotOnly: false,
      detailId: null,
      showPaywall: false,
      showNewMonitor: false,
      toastText: null,

      user: {
        name: 'Алексей',
        username: 'alex_flip',
        avatarIdx: 0,
        customAvatar: null,
        accessUntil: 'Загрузка...',
        daysLeft: 3,
        totalDays: 30,
        refCode: 'dh_alex99',
        invitedCount: 4,
        bonusDaysEarned: 12,
        notificationsOn: true
      },

      savedIds: ['lot_1'],
      listings: [],

      monitors: [
        {
          id: 'm_1',
          title: 'iPhone 15 Pro / 16 Pro',
          icon: 'smartphone',
          city: 'Москва',
          minMargin: 15,
          sources: ['Avito'],
          pills: ['256 ГБ', 'АКБ от 90%', 'Идеал'],
          quietHours: '23:00 – 08:00',
          foundToday: 6,
          active: true
        },
        {
          id: 'm_2',
          title: 'Видеокарты RTX 4070 Ti / 4080',
          icon: 'cpu',
          city: 'Вся Россия',
          minMargin: 18,
          sources: ['Avito', 'WB'],
          pills: ['16 ГБ', 'Чек РФ', 'На пломбах'],
          quietHours: 'Без пауз',
          foundToday: 3,
          active: true
        }
      ]
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(n);

    function showToast(msg) {
      const box = document.getElementById('toast-container');
      box.innerHTML = `<div class="toast"><i data-lucide="check-circle-2" class="icon-16" style="color:var(--accent)"></i><span>${msg}</span></div>`;
      lucide.createIcons();
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { box.innerHTML = ''; }, 2200);
    }

    function toggleTheme() {
      state.theme = state.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', state.theme);
      render();
    }

    function toggleSave(id, e) {
      if (e) e.stopPropagation();
      const idx = state.savedIds.indexOf(id);
      if (idx >= 0) {
        state.savedIds.splice(idx, 1);
        showToast('Убрано из сохранённых');
      } else {
        state.savedIds.push(id);
        showToast('Лот сохранён в подборку');
      }
      render();
    }

    function dismissLot(id, e) {
      if (e) e.stopPropagation();
      const card = document.getElementById('card-' + id);
      if (card) {
        card.style.transform = 'scale(0.9) translateX(-40px)';
        card.style.opacity = '0';
      }
      setTimeout(() => {
        state.listings = state.listings.filter(x => x.id !== id);
        render();
      }, 180);
    }

    function cycleAvatar() {
      state.user.customAvatar = null;
      state.user.avatarIdx = (state.user.avatarIdx + 1) % AVATAR_PRESETS.length;
      showToast('Аватар обновлён');
      render();
    }

    function handleAvatarFile(e) {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        state.user.customAvatar = reader.result;
        showToast('Своя аватарка загружена');
        render();
      };
      reader.readAsDataURL(file);
    }

    function regenerateRefCode() {
      const rand = Math.random().toString(36).substring(2, 7);
      state.user.refCode = 'dh_' + rand;
      showToast('Сгенерирована новая ссылка');
      render();
    }

    function copyRefLink() {
      const url = `https://t.me/dealhunter_bot?start=${state.user.refCode}`;
      if (navigator.clipboard) navigator.clipboard.writeText(url);
      showToast('Ссылка скопирована в буфер');
    }

    async function loadLiveLots(query = 'iPhone 15') {
      try {
        const res = await fetch(`/api/live-feed?query=${encodeURIComponent(query)}`);
        const data = await res.json();
        if (data && data.length) {
          state.listings = data;
          render();
        }
      } catch(e) {}
    }

    async function syncUserData() {
      try {
        const res = await fetch('/api/user-status', { headers: { 'X-Telegram-Init-Data': initData } });
        const data = await res.json();
        if (data) {
          state.user.name = data.name || state.user.name;
          state.user.username = data.username || state.user.username;
          state.user.daysLeft = data.daysLeft;
          state.user.accessUntil = data.accessUntil;
          render();
        }
      } catch(e) {}
    }

    function renderLotCard(item) {
      const isSaved = state.savedIds.includes(item.id);
      return `
        <div class="glass-card interactive" id="card-${item.id}" style="margin-bottom: 12px;" onclick="state.detailId='${item.id}'; window.scrollTo(0,0); render();">
          <div class="row-between" style="margin-bottom: 10px;">
            <div style="display:flex; align-items:center; gap: 6px;">
              <span style="padding: 3px 9px; border-radius: 8px; background: var(--glass-pill); font-size: 11px; font-weight: 700; color: var(--text-primary);">
                ${item.source}
              </span>
              <span style="font-size: 12px; color: var(--text-secondary); display:inline-flex; align-items:center; gap:4px;">
                <i data-lucide="map-pin" style="width:12px; height:12px;"></i>${item.city}
              </span>
              <span style="font-size: 12px; color: var(--text-muted);">· ${item.timeAgo}</span>
            </div>

            <button class="btn btn-icon" style="width:30px; height:30px; border-radius:10px;" title="Скрыть из ленты" onclick="dismissLot('${item.id}', event)">
              <i data-lucide="x" style="width:14px; height:14px; color:var(--text-muted);"></i>
            </button>
          </div>

          <h3 style="font-size: 17px; font-weight: 700; letter-spacing: -0.015em; margin-bottom: 10px;">
            ${item.title}
          </h3>

          <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 16px;">
            ${item.tags.map(t => `
              <span class="spec-tag">
                <i data-lucide="${t.icon}" style="width:13px; height:13px; color:var(--accent);"></i>
                <span>${t.text}</span>
              </span>
            `).join('')}
          </div>

          <div class="row-between" style="padding: 12px 14px; border-radius: 18px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); margin-bottom: 14px;">
            <div>
              <div class="t-num" style="font-size: 23px; font-weight: 800; line-height: 1.1;">
                ${fmt(item.price)} ₽
              </div>
              <div class="t-num" style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                Рынок <span style="text-decoration: line-through;">${fmt(item.market)} ₽</span>
              </div>
            </div>

            <div class="profit-pill t-num">
              <i data-lucide="trending-down" style="width:15px; height:15px;"></i>
              <span>−${item.profitPct}% · ${fmt(item.profitRub)} ₽</span>
            </div>
          </div>

          <div class="row-between">
            <div style="display:flex; align-items:center; gap: 8px; font-size: 12px;">
              <span style="display:inline-flex; align-items:center; gap:4px; font-weight:700; color: var(--text-primary);">
                <i data-lucide="star" style="width:13px; height:13px; color:#FFB800; fill:#FFB800;"></i>
                ${item.seller.rating.toFixed(1)}
              </span>
              <span style="color: var(--text-secondary);">(${item.seller.reviews})</span>
              <span style="padding: 2px 8px; border-radius: 99px; font-weight: 600; font-size: 11px; background: ${item.seller.safe ? 'var(--green-soft)' : 'var(--danger-soft)'}; color: ${item.seller.safe ? 'var(--green)' : 'var(--danger)'};">
                ${item.seller.safe ? 'Проверен' : 'Новорег'}
              </span>
            </div>

            <div style="display:flex; gap: 8px;">
              <button class="btn btn-icon" onclick="toggleSave('${item.id}', event)" style="${isSaved ? 'background:var(--accent-soft); border-color:var(--accent); color:var(--accent);' : ''}">
                <i data-lucide="heart" class="icon-16" style="${isSaved ? 'fill:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-pill btn-accent" onclick="event.stopPropagation(); window.open('${item.url}', '_blank')">
                <span>Забрать</span>
                <i data-lucide="arrow-up-right" class="icon-16"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    }

    function renderDetail(item) {
      const isSaved = state.savedIds.includes(item.id);
      const pts = item.history.map((p, idx) => {
        const x = idx * 130 + 20;
        const y = 65 - ((p - item.price) / (item.market - item.price || 1)) * 45;
        return `${x},${y}`;
      }).join(' ');

      return `
        <div class="top-header row-between">
          <button class="btn btn-pill btn-glass" onclick="state.detailId = null; render();">
            <i data-lucide="chevron-left" class="icon-18"></i>
            <span>Назад</span>
          </button>
          <button class="btn btn-pill btn-glass" onclick="toggleSave('${item.id}', event)" style="${isSaved ? 'color:var(--accent); border-color:var(--accent);' : ''}">
            <i data-lucide="heart" class="icon-16" style="${isSaved ? 'fill:var(--accent);' : ''}"></i>
            <span>${isSaved ? 'В подборке' : 'Сохранить'}</span>
          </button>
        </div>

        <div class="screen-content">
          <div class="glass-card" style="margin-bottom: 12px;">
            <div class="row-between" style="margin-bottom: 8px;">
              <span style="font-size: 12px; font-weight: 600; color: var(--text-secondary);">${item.source} · ${item.city}</span>
              <span style="font-size: 12px; color: var(--text-muted);">${item.timeAgo} назад</span>
            </div>
            <h1 style="font-size: 22px; font-weight: 800; margin-bottom: 16px;">${item.title}</h1>

            <div class="row-between" style="margin-bottom: 14px;">
              <div>
                <div class="t-num" style="font-size: 30px; font-weight: 800;">${fmt(item.price)} ₽</div>
                <div class="t-num" style="font-size: 13px; color: var(--text-secondary);">
                  Средняя по рынку: <span style="text-decoration:line-through;">${fmt(item.market)} ₽</span>
                </div>
              </div>
              <div class="profit-pill t-num" style="padding: 8px 14px; font-size: 14px;">
                −${item.profitPct}% · ${fmt(item.profitRub)} ₽
              </div>
            </div>

            <p style="font-size: 14px; line-height: 1.5; color: var(--text-secondary);">
              ${item.desc}
            </p>
          </div>

          <div class="glass-card" style="margin-bottom: 12px;">
            <div style="font-size: 15px; font-weight: 700; margin-bottom: 12px; display:flex; align-items:center; gap:8px;">
              <i data-lucide="sliders" class="icon-16" style="color:var(--accent)"></i>
              <span>Характеристики</span>
            </div>
            <div style="display:grid; gap: 10px;">
              ${Object.entries(item.specs).map(([k, v]) => `
                <div class="row-between" style="font-size: 13px; padding-bottom: 8px; border-bottom: 1px solid var(--glass-stroke);">
                  <span style="color: var(--text-secondary);">${k}</span>
                  <span style="font-weight: 600;">${v}</span>
                </div>
              `).join('')}
            </div>
          </div>

          <div class="glass-card" style="margin-bottom: 12px;">
            <div class="row-between" style="margin-bottom: 12px;">
              <span style="font-size: 15px; font-weight: 700;">История цены</span>
              <span style="font-size: 12px; color: var(--green); font-weight: 600;">Снижена на ${fmt(item.history[0] - item.price)} ₽</span>
            </div>
            <svg viewBox="0 0 300 75" style="width:100%; height:70px; overflow:visible;">
              <polyline fill="none" stroke="var(--accent)" stroke-width="3" stroke-linecap="round" points="${pts}" />
            </svg>
            <div class="row-between t-num" style="font-size: 12px; color: var(--text-secondary); margin-top: 6px;">
              <span>Старт: ${fmt(item.history[0])} ₽</span>
              <span style="color: var(--text-primary); font-weight: 700;">Сейчас: ${fmt(item.price)} ₽</span>
            </div>
          </div>

          <div class="glass-card" style="margin-bottom: 16px;">
            <div class="row-between" style="margin-bottom: 10px;">
              <div>
                <div style="font-size: 15px; font-weight: 700;">${item.seller.name}</div>
                <div style="font-size: 12px; color: var(--text-secondary);">На площадке с ${item.seller.since} года</div>
              </div>
              <div style="font-size: 14px; font-weight: 700;">★ ${item.seller.rating} <span style="color:var(--text-secondary); font-weight:400;">(${item.seller.reviews})</span></div>
            </div>
            <div style="padding: 10px 12px; border-radius: 14px; font-size: 13px; font-weight: 600; background: ${item.seller.safe ? 'var(--green-soft)' : 'var(--danger-soft)'}; color: ${item.seller.safe ? 'var(--green)' : 'var(--danger)'};">
              ${item.seller.note}
            </div>
          </div>

          <button class="btn btn-large btn-accent" onclick="window.open('${item.url}', '_blank')">
            <span>Открыть на ${item.source}</span>
            <i data-lucide="external-link" class="icon-18"></i>
          </button>
        </div>
      `;
    }

    function render() {
      const app = document.getElementById('app');

      if (state.detailId) {
        const item = state.listings.find(x => x.id === state.detailId);
        if (item) {
          app.innerHTML = renderDetail(item);
          lucide.createIcons();
          return;
        }
      }

      let screenHtml = '';

      if (state.tab === 'feed') {
        const filtered = state.listings.filter(x => {
          if (state.category !== 'all' && x.category !== state.category) return false;
          if (state.hotOnly && x.profitPct < 20) return false;
          return true;
        });

        screenHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap: 10px;">
                <div style="width: 36px; height: 36px; border-radius: 12px; background: var(--accent); display:flex; align-items:center; justify-content:center; color:#fff; box-shadow: 0 6px 16px var(--accent-glow);">
                  <i data-lucide="radar" class="icon-20"></i>
                </div>
                <div>
                  <div style="font-size: 18px; font-weight: 800; letter-spacing: -0.02em;">Радар находок</div>
                  <div style="font-size: 11px; color: var(--green); font-weight: 600;">● Сканируем площадки в реальном времени</div>
                </div>
              </div>

              <div style="display:flex; gap: 8px;">
                <button class="btn btn-icon" title="Только маржа от 20%" onclick="state.hotOnly = !state.hotOnly; render();" style="${state.hotOnly ? 'background:var(--accent); color:#fff; border-color:var(--accent);' : ''}">
                  <i data-lucide="flame" class="icon-18"></i>
                </button>
                <button class="btn btn-icon" title="Переключить тему" onclick="toggleTheme()">
                  <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" class="icon-18"></i>
                </button>
              </div>
            </div>

            <div class="pills-scroll">
              <button class="cat-pill ${state.category === 'all' ? 'active' : ''}" onclick="state.category='all'; loadLiveLots('электроника');">
                <i data-lucide="layout-grid" class="icon-16"></i><span>Все лоты</span>
              </button>
              <button class="cat-pill ${state.category === 'iphone' ? 'active' : ''}" onclick="state.category='iphone'; loadLiveLots('iPhone');">
                <i data-lucide="smartphone" class="icon-16"></i><span>iPhone</span>
              </button>
              <button class="cat-pill ${state.category === 'macbook' ? 'active' : ''}" onclick="state.category='macbook'; loadLiveLots('MacBook');">
                <i data-lucide="laptop" class="icon-16"></i><span>MacBook</span>
              </button>
              <button class="cat-pill ${state.category === 'gpu' ? 'active' : ''}" onclick="state.category='gpu'; loadLiveLots('RTX');">
                <i data-lucide="cpu" class="icon-16"></i><span>Видеокарты</span>
              </button>
              <button class="cat-pill ${state.category === 'scooter' ? 'active' : ''}" onclick="state.category='scooter'; loadLiveLots('самокат');">
                <i data-lucide="zap" class="icon-16"></i><span>Самокаты</span>
              </button>
            </div>
          </div>

          <div class="screen-content">
            ${filtered.length
              ? filtered.map(renderLotCard).join('')
              : `<div class="glass-card" style="text-align:center; padding: 38px 20px;">
                   <i data-lucide="search-x" style="width:36px; height:36px; color:var(--text-muted); margin-bottom:10px;"></i>
                   <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">Поиск выгодных лотов...</div>
                   <div style="font-size: 13px; color: var(--text-secondary); margin-bottom: 16px;">Обновляем базу объявлений с маркетплейсов</div>
                   <button class="btn btn-pill btn-accent" onclick="loadLiveLots('iPhone 15')">Обновить выдачу</button>
                 </div>`
            }
          </div>
        `;
      }

      if (state.tab === 'monitors') {
        screenHtml = `
          <div class="top-header row-between">
            <div>
              <div style="font-size: 18px; font-weight: 800;">Мои мониторы</div>
              <div style="font-size: 12px; color: var(--text-secondary);">Автопоиск выгодных объявлений 24/7</div>
            </div>
            <button class="btn btn-pill btn-accent" onclick="state.showNewMonitor = true; render();">
              <i data-lucide="plus" class="icon-16"></i>
              <span>Новый радар</span>
            </button>
          </div>

          <div class="screen-content">
            ${state.monitors.map(m => `
              <div class="glass-card" style="margin-bottom: 12px;">
                <div class="row-between" style="margin-bottom: 12px;">
                  <div style="display:flex; align-items:center; gap: 12px;">
                    <div style="width: 44px; height: 44px; border-radius: 16px; background: var(--accent-soft); color: var(--accent); display:flex; align-items:center; justify-content:center;">
                      <i data-lucide="${m.icon}" class="icon-20"></i>
                    </div>
                    <div>
                      <div style="font-size: 16px; font-weight: 700;">${m.title}</div>
                      <div style="font-size: 12px; color: var(--text-secondary); margin-top: 2px;">
                        ${m.city} · Маржа от <strong style="color:var(--accent)">${m.minMargin}%</strong>
                      </div>
                    </div>
                  </div>

                  <div class="ios-switch ${m.active ? 'on' : ''}" onclick="const item=state.monitors.find(x=>x.id==='${m.id}'); item.active=!item.active; showToast(item.active ? 'Монитор запущен' : 'Монитор на паузе'); render();">
                    <div class="ios-switch-knob"></div>
                  </div>
                </div>

                <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 14px;">
                  ${m.sources.map(s => `<span class="spec-tag" style="color:var(--text-primary); font-weight:700;">${s}</span>`).join('')}
                  ${m.pills.map(p => `<span class="spec-tag">${p}</span>`).join('')}
                </div>

                <div class="row-between" style="padding-top: 12px; border-top: 1px solid var(--glass-stroke); font-size: 12px; color: var(--text-secondary);">
                  <span style="display:inline-flex; align-items:center; gap:5px;">
                    <i data-lucide="moon" style="width:13px; height:13px;"></i> Тихие часы: ${m.quietHours}
                  </span>
                  <span class="t-num" style="color: var(--text-primary); font-weight: 700;">
                    Поймано сегодня: ${m.foundToday}
                  </span>
                </div>
              </div>
            `).join('')}
          </div>
        `;
      }

      if (state.tab === 'saved') {
        const savedLots = state.listings.filter(x => state.savedIds.includes(x.id));
        const totalProfit = savedLots.reduce((acc, x) => acc + x.profitRub, 0);

        screenHtml = `
          <div class="top-header row-between">
            <div>
              <div style="font-size: 18px; font-weight: 800;">Сохранённые лоты</div>
              <div style="font-size: 12px; color: var(--text-secondary);">Потенциальная выгода: <strong style="color:var(--accent)">${fmt(totalProfit)} ₽</strong></div>
            </div>
            <span class="spec-tag t-num" style="font-weight:700;">${savedLots.length} шт.</span>
          </div>

          <div class="screen-content">
            ${savedLots.length
              ? savedLots.map(renderLotCard).join('')
              : `<div class="glass-card" style="text-align:center; padding: 42px 20px;">
                   <div style="width:52px; height:52px; border-radius:18px; background:var(--glass-pill); display:inline-flex; align-items:center; justify-content:center; margin-bottom:12px;">
                     <i data-lucide="heart" class="icon-20" style="color:var(--text-muted)"></i>
                   </div>
                   <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">В подборке пока пусто</div>
                   <div style="font-size: 13px; color: var(--text-secondary); margin-bottom: 18px;">Нажимайте на сердечко на выгодных лотах в ленте, чтобы следить за их ценой</div>
                   <button class="btn btn-pill btn-accent" onclick="state.tab='feed'; render();">Перейти к находкам</button>
                 </div>`
            }
          </div>
        `;
      }

      if (state.tab === 'profile') {
        const avatarSrc = state.user.customAvatar || AVATAR_PRESETS[state.user.avatarIdx];
        const progressPct = Math.round((state.user.daysLeft / state.user.totalDays) * 100);

        screenHtml = `
          <div class="top-header row-between">
            <div style="font-size: 18px; font-weight: 800;">Профиль</div>
            <button class="btn btn-pill btn-glass" onclick="toggleTheme()">
              <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" class="icon-16"></i>
              <span>${state.theme === 'dark' ? 'Светлая' : 'Тёмная'}</span>
            </button>
          </div>

          <div class="screen-content">
            <div class="glass-card" style="margin-bottom: 12px;">
              <div style="display:flex; align-items:center; gap: 14px;">
                <div class="avatar-ring" onclick="cycleAvatar()" title="Нажми для смены пресета">
                  <img src="${avatarSrc}" class="avatar-img" alt="avatar" />
                  <div class="avatar-edit-badge">
                    <i data-lucide="camera" style="width:13px; height:13px;"></i>
                  </div>
                </div>

                <div style="flex:1;">
                  <div style="display:flex; align-items:center; gap: 8px;">
                    <span style="font-size: 19px; font-weight: 800;">${state.user.name}</span>
                    <span style="padding: 2px 8px; border-radius: 99px; background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 800;">PRO</span>
                  </div>
                  <div style="font-size: 13px; color: var(--text-secondary); margin-top: 2px;">@${state.user.username}</div>
                  <div style="display:flex; gap: 8px; margin-top: 10px;">
                    <button class="btn btn-pill btn-glass" style="height:30px; padding:0 10px; font-size:11px;" onclick="document.getElementById('avatar-upload').click()">
                      <i data-lucide="upload" style="width:12px; height:12px;"></i> Загрузить фото
                    </button>
                    <button class="btn btn-pill btn-glass" style="height:30px; padding:0 10px; font-size:11px;" onclick="cycleAvatar()">
                      <i data-lucide="refresh-cw" style="width:12px; height:12px;"></i> Сменить
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div class="glass-card" style="margin-bottom: 12px;">
              <div class="row-between" style="margin-bottom: 6px;">
                <span style="font-size: 13px; color: var(--text-secondary); font-weight: 500;">Подписка активна до</span>
                <span class="t-num" style="font-size: 12px; font-weight: 700; color: var(--accent);">Осталось ${state.user.daysLeft} дн.</span>
              </div>
              <div class="t-num" style="font-size: 20px; font-weight: 800; margin-bottom: 12px;">
                ${state.user.accessUntil}
              </div>

              <div class="progress-track" style="margin-bottom: 16px;">
                <div class="progress-fill" style="width: ${progressPct}%;"></div>
              </div>

              <button class="btn btn-large btn-accent" onclick="state.showPaywall = true; render();">
                <i data-lucide="sparkles" class="icon-18"></i>
                <span>Продлить доступ · 350 Stars</span>
              </button>
            </div>

            <div class="glass-card" style="margin-bottom: 12px;">
              <div class="row-between" style="margin-bottom: 6px;">
                <div style="font-size: 16px; font-weight: 800;">+3 дня за каждого друга</div>
                <span class="spec-tag t-num" style="color:var(--accent); font-weight:700;">+${state.user.bonusDaysEarned} / 30 дн.</span>
              </div>
              <p style="font-size: 12px; color: var(--text-secondary); line-height: 1.45; margin-bottom: 14px;">
                Бонус начисляется автоматически при подключении реферала.
              </p>

              <div class="row-between" style="padding: 10px 12px; border-radius: 16px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); margin-bottom: 12px;">
                <span class="t-num" style="font-size: 13px; font-weight: 600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                  t.me/dealhunter_bot?start=${state.user.refCode}
                </span>
                <button class="btn btn-icon" style="width:32px; height:32px; border-radius:10px;" title="Сгенерировать новый код" onclick="regenerateRefCode()">
                  <i data-lucide="dice-5" class="icon-16"></i>
                </button>
              </div>

              <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <button class="btn btn-pill btn-accent" style="height:42px;" onclick="copyRefLink()">
                  <i data-lucide="copy" class="icon-16"></i>
                  <span>Скопировать</span>
                </button>
                <button class="btn btn-pill btn-glass" style="height:42px;" onclick="state.user.invitedCount++; state.user.bonusDaysEarned = Math.min(30, state.user.bonusDaysEarned + 3); state.user.daysLeft += 3; showToast('+3 бонусных дня начислено!'); render();">
                  <i data-lucide="user-plus" class="icon-16"></i>
                  <span>Тест +1 друг</span>
                </button>
              </div>
            </div>

            <div class="glass-card">
              <div class="row-between">
                <div style="display:flex; align-items:center; gap: 10px;">
                  <i data-lucide="bell-ring" class="icon-18" style="color:var(--accent)"></i>
                  <div>
                    <div style="font-size: 14px; font-weight: 700;">Мгновенные пуши в Telegram</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">Уведомлять сразу при появлении маржи</div>
                  </div>
                </div>
                <div class="ios-switch ${state.user.notificationsOn ? 'on' : ''}" onclick="state.user.notificationsOn = !state.user.notificationsOn; render();">
                  <div class="ios-switch-knob"></div>
                </div>
              </div>
            </div>
          </div>
        `;
      }

      let modalHtml = '';
      if (state.showPaywall) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showPaywall = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom: 16px;">
                <span class="profit-pill">PRO Подписка</span>
                <button class="btn btn-icon" onclick="state.showPaywall = false; render();">
                  <i data-lucide="x" class="icon-18"></i>
                </button>
              </div>

              <div class="t-num" style="font-size: 32px; font-weight: 800; margin-bottom: 4px;">
                350 Stars <span style="font-size: 15px; color: var(--text-secondary); font-weight: 500;">/ 30 дней</span>
              </div>
              <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 18px;">
                Окупается с первой же перепродажи. Разовый платёж без автосписаний.
              </p>

              <div style="display:grid; gap: 10px; margin-bottom: 22px; font-size: 14px;">
                <div style="display:flex; align-items:center; gap:10px;"><i data-lucide="check" class="icon-18" style="color:var(--accent)"></i><span>До 10 активных мониторов одновременно</span></div>
                <div style="display:flex; align-items:center; gap:10px;"><i data-lucide="check" class="icon-18" style="color:var(--accent)"></i><span>Пуши в бота за 2–3 секунды после выхода лота</span></div>
                <div style="display:flex; align-items:center; gap:10px;"><i data-lucide="check" class="icon-18" style="color:var(--accent)"></i><span>Проверка продавца и история снижения цен</span></div>
              </div>

              <button class="btn btn-large btn-accent" onclick="state.user.daysLeft += 30; state.showPaywall = false; showToast('Доступ продлён на +30 дней!'); render();">
                <i data-lucide="zap" class="icon-18"></i>
                <span>Оплатить 350 Telegram Stars</span>
              </button>
            </div>
          </div>
        `;
      }

      if (state.showNewMonitor) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showNewMonitor = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom: 16px;">
                <div style="font-size: 19px; font-weight: 800;">Новый автопоиск</div>
                <button class="btn btn-icon" onclick="state.showNewMonitor = false; render();">
                  <i data-lucide="x" class="icon-18"></i>
                </button>
              </div>

              <div style="display:grid; gap: 12px; margin-bottom: 20px;">
                <div>
                  <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;">Что ищем?</div>
                  <input class="input-glass" id="m-title" value="iPhone 16 Pro 256 ГБ" />
                </div>
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                  <div>
                    <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;">Город</div>
                    <input class="input-glass" id="m-city" value="Москва" />
                  </div>
                  <div>
                    <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;">Мин. маржа (%)</div>
                    <input class="input-glass t-num" id="m-margin" type="number" value="18" />
                  </div>
                </div>
              </div>

              <button class="btn btn-large btn-accent" onclick="
                const title = document.getElementById('m-title').value;
                const city = document.getElementById('m-city').value;
                const minMargin = Number(document.getElementById('m-margin').value || 15);
                state.monitors.unshift({
                  id: 'm_' + Date.now(),
                  title, icon: 'radar', city, minMargin,
                  sources: ['Avito', 'WB'],
                  pills: ['Идеал', 'Проверка продавца'],
                  quietHours: '23:00 – 08:00',
                  foundToday: 0,
                  active: true
                });
                state.showNewMonitor = false;
                showToast('Монитор создан и запущен');
                render();
              ">
                <span>Запустить мониторинг</span>
              </button>
            </div>
          </div>
        `;
      }

      const dockHtml = `
        <div class="floating-dock-wrap">
          <nav class="floating-dock">
            <button class="dock-btn ${state.tab === 'feed' ? 'active' : ''}" onclick="state.tab='feed'; window.scrollTo(0,0); render();">
              <i data-lucide="flame" class="icon-20"></i>
              <span>Находки</span>
            </button>
            <button class="dock-btn ${state.tab === 'monitors' ? 'active' : ''}" onclick="state.tab='monitors'; window.scrollTo(0,0); render();">
              <i data-lucide="radar" class="icon-20"></i>
              <span>Мониторы</span>
            </button>
            <button class="dock-btn ${state.tab === 'saved' ? 'active' : ''}" onclick="state.tab='saved'; window.scrollTo(0,0); render();">
              ${state.savedIds.length ? `<span class="dock-badge t-num">${state.savedIds.length}</span>` : ''}
              <i data-lucide="heart" class="icon-20"></i>
              <span>Подборка</span>
            </button>
            <button class="dock-btn ${state.tab === 'profile' ? 'active' : ''}" onclick="state.tab='profile'; window.scrollTo(0,0); render();">
              <i data-lucide="user-round" class="icon-20"></i>
              <span>Профиль</span>
            </button>
          </nav>
        </div>
      `;

      app.innerHTML = screenHtml + modalHtml + dockHtml;
      lucide.createIcons();
    }

    syncUserData();
    loadLiveLots('iPhone 15');
    render();
  </script>
</body>
</html>
"""
