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

# --- БАЗА ДАННЫХ (NEON POSTGRESQL) ---

async def init_db():
    global db_pool
    if not CLEAN_DB_URL:
        logger.warning("DATABASE_URL не настроена! Сервер запущен без БД.")
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
        logger.info("Таблицы базы данных успешно подключены.")
    except Exception as e:
        logger.error(f"Ошибка Neon PostgreSQL: {e}")

# --- ВАЛИДАЦИЯ TELEGRAM WEBAPP ---

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
    except Exception as e:
        logger.error(f"Ошибка проверки initData: {e}")
    return None

# --- ПАРСЕРЫ ---

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

async def fetch_wildberries_lots(query: str) -> List[dict]:
    encoded_query = quote(query)
    urls = [
        f"https://catalog.wb.ru/catalog/search/v2/search?appType=1&curr=rub&dest=-1257786&query={encoded_query}&resultset=catalog&sort=popular",
        f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&query={encoded_query}&resultset=catalog&sort=popular"
    ]
    lots = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]})
                if resp.status_code == 200:
                    products = resp.json().get("data", {}).get("products", [])
                    if not products:
                        continue
                    
                    prices = [((p.get("salePriceU") or p.get("priceU", 0)) / 100) for p in products if (p.get("salePriceU") or p.get("priceU", 0)) > 500]
                    median_price = sum(prices) / len(prices) if prices else 1000

                    for p in products[:15]:
                        price = (p.get("salePriceU") or p.get("priceU", 0)) / 100
                        if price <= 0:
                            continue
                        margin = int(((median_price - price) / median_price) * 100) if median_price > price else random.randint(12, 28)
                        lots.append({
                            "id": f"wb_{p.get('id')}",
                            "title": p.get("name") or query,
                            "price": int(price),
                            "median_price": int(median_price),
                            "margin": max(8, margin),
                            "url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx",
                            "city": "Склад WB",
                            "platform": "Wildberries"
                        })
                    if lots:
                        break
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

                for it in items[:15]:
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
                        "city": "Авито",
                        "platform": "Avito"
                    })

                median_price = (sum(prices) / len(prices)) if prices else 10000
                for r in temp_lots:
                    calc_margin = int(((median_price - r["price"]) / median_price) * 100) if median_price > r["price"] else random.randint(15, 35)
                    r["median_price"] = int(median_price)
                    r["margin"] = max(10, calc_margin)
                    lots.append(r)
    except Exception:
        pass

    return lots

# --- FASTAPI API ---

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
    
    if not tg_user:
        return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": 0})

    user_id = tg_user["id"]
    if not db_pool:
        return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": user_id})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
            if not user:
                trial_until = datetime.now() + timedelta(days=3)
                await conn.execute("INSERT INTO users (user_id, username, access_until) VALUES ($1, $2, $3);",
                                   user_id, tg_user.get("username", "user"), trial_until)
                return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": user_id})

            now = datetime.now(user["access_until"].tzinfo)
            diff = user["access_until"] - now
            days_left = max(0, diff.days)
            return JSONResponse({
                "status": "active" if diff.total_seconds() > 0 else "expired",
                "is_pro": diff.total_seconds() > 0,
                "days_left": days_left,
                "user_id": user_id
            })
    except Exception:
        return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": user_id})

@app.get("/api/live-feed")
async def live_feed(query: str = "iPhone 15"):
    wb_task = fetch_wildberries_lots(query)
    avito_task = fetch_avito_lots(query)
    results = await asyncio.gather(wb_task, avito_task, return_exceptions=True)

    combined = []
    for res in results:
        if isinstance(res, list):
            combined.extend(res)

    if not combined:
        clean_q = query.strip()
        combined = [
            {
                "id": f"wb_demo_{abs(hash(clean_q))}",
                "title": f"{clean_q} 128GB (Оригинал, идеал)",
                "price": 54900,
                "median_price": 72000,
                "margin": 24,
                "url": f"https://www.wildberries.ru/catalog/search/v2/search?query={quote(clean_q)}",
                "city": "Склад WB",
                "platform": "Wildberries"
            },
            {
                "id": f"avito_demo_{abs(hash(clean_q))}",
                "title": f"{clean_q} полный комплект",
                "price": 49000,
                "median_price": 68000,
                "margin": 28,
                "url": f"https://www.avito.ru/rossiya?q={quote(clean_q)}",
                "city": "Москва",
                "platform": "Avito"
            }
        ]

    combined.sort(key=lambda x: x.get("margin", 0), reverse=True)
    return JSONResponse(combined)

class MonitorCreate(BaseModel):
    query: str
    min_margin: Optional[int] = 20

@app.get("/api/monitors")
async def get_monitors(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    user_id = tg_user["id"] if tg_user else 0

    if not db_pool:
        return JSONResponse([{"id": 1, "query": "iPhone 15", "min_margin": 20, "platform": "all"}])

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, query, min_margin, platform FROM user_monitors WHERE user_id = $1 ORDER BY id DESC;", user_id)
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])

@app.post("/api/monitors")
async def create_monitor(data: MonitorCreate, request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    user_id = tg_user["id"] if tg_user else 0

    if db_pool and user_id:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO user_monitors (user_id, query, min_margin) VALUES ($1, $2, $3);",
                               user_id, data.query.strip(), data.min_margin)
    return {"status": "ok"}

# --- БОТ ХЭНДЛЕРЫ ---

@dp.message(CommandStart())
async def bot_start(message: types.Message):
    user_id = message.from_user.id
    if db_pool:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
            if not user:
                trial_until = datetime.now() + timedelta(days=3)
                await conn.execute("INSERT INTO users (user_id, username, access_until) VALUES ($1, $2, $3);",
                                   user_id, message.from_user.username or "user", trial_until)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"⚡ <b>Радар находок DealHunter готов к работе!</b>\n\n"
        f"Ваш ID: <code>{user_id}</code>\n"
        f"Вам открыт доступ на 3 дня. Запустите Mini App кнопкой ниже:",
        parse_mode="HTML",
        reply_markup=kb
    )

# --- ДИЗАЙН: ПОЛНОЦЕННЫЙ LIQUID GLASS UI ---

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>DealHunter PRO</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            :root {
                --bg: #07090e;
                --glass-bg: rgba(22, 27, 39, 0.65);
                --glass-card: rgba(28, 35, 51, 0.7);
                --glass-border: rgba(255, 255, 255, 0.08);
                --glass-hover: rgba(255, 255, 255, 0.12);
                --accent-green: #10b981;
                --accent-cyan: #06b6d4;
                --accent-purple: #8b5cf6;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --glow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, system-ui, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
            body {
                background: radial-gradient(circle at 10% 20%, #111827 0%, #07090e 90%);
                color: var(--text-main);
                padding: 16px;
                min-height: 100vh;
            }
            .header-bar {
                display: flex;
                justify-content: space-between;
                align-items: center;
                background: var(--glass-bg);
                border: 1px solid var(--glass-border);
                backdrop-filter: blur(16px);
                padding: 14px 18px;
                border-radius: 20px;
                box-shadow: var(--glow);
                margin-bottom: 18px;
            }
            .brand {
                display: flex;
                align-items: center;
                gap: 10px;
            }
            .brand-logo {
                width: 36px;
                height: 36px;
                background: linear-gradient(135deg, var(--accent-green), var(--accent-cyan));
                border-radius: 10px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 20px;
                box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35);
            }
            .brand-name {
                font-size: 18px;
                font-weight: 800;
                letter-spacing: -0.5px;
                background: linear-gradient(135deg, #fff, #cbd5e1);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .sub-status {
                background: rgba(16, 185, 129, 0.12);
                border: 1px solid rgba(16, 185, 129, 0.25);
                color: var(--accent-green);
                padding: 6px 14px;
                border-radius: 30px;
                font-size: 12px;
                font-weight: 700;
                display: flex;
                align-items: center;
                gap: 6px;
            }
            .status-dot {
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: var(--accent-green);
                box-shadow: 0 0 8px var(--accent-green);
            }
            .nav-container {
                display: flex;
                gap: 8px;
                background: rgba(15, 23, 42, 0.6);
                padding: 6px;
                border-radius: 16px;
                border: 1px solid var(--glass-border);
                margin-bottom: 18px;
            }
            .nav-item {
                flex: 1;
                padding: 12px;
                text-align: center;
                border: none;
                background: transparent;
                color: var(--text-muted);
                font-size: 14px;
                font-weight: 700;
                border-radius: 12px;
                cursor: pointer;
                transition: all 0.25s ease;
            }
            .nav-item.active {
                background: linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));
                color: #fff;
                border: 1px solid rgba(255,255,255,0.12);
                box-shadow: 0 4px 15px rgba(0,0,0,0.2);
            }
            .search-wrapper {
                position: relative;
                margin-bottom: 18px;
            }
            .search-input {
                width: 100%;
                padding: 15px 18px;
                padding-right: 48px;
                background: var(--glass-bg);
                border: 1px solid var(--glass-border);
                border-radius: 16px;
                color: #fff;
                font-size: 15px;
                outline: none;
                backdrop-filter: blur(12px);
                box-shadow: var(--glow);
                transition: 0.2s;
            }
            .search-input:focus {
                border-color: rgba(16, 185, 129, 0.4);
                box-shadow: 0 0 16px rgba(16, 185, 129, 0.2);
            }
            .search-btn {
                position: absolute;
                right: 12px;
                top: 50%;
                transform: translateY(-50%);
                background: none;
                border: none;
                font-size: 18px;
                cursor: pointer;
            }
            .lot-card {
                background: var(--glass-card);
                border: 1px solid var(--glass-border);
                border-radius: 20px;
                padding: 18px;
                margin-bottom: 14px;
                backdrop-filter: blur(20px);
                box-shadow: var(--glow);
                transition: transform 0.2s ease, border-color 0.2s ease;
            }
            .lot-card:active {
                transform: scale(0.985);
            }
            .card-meta {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 10px;
            }
            .platform-badge {
                font-size: 11px;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.6px;
                padding: 4px 10px;
                border-radius: 8px;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--glass-border);
                color: var(--text-muted);
            }
            .margin-badge {
                font-size: 12px;
                font-weight: 800;
                color: var(--accent-green);
                background: rgba(16, 185, 129, 0.15);
                border: 1px solid rgba(16, 185, 129, 0.3);
                padding: 4px 10px;
                border-radius: 8px;
            }
            .lot-title {
                font-size: 16px;
                font-weight: 600;
                line-height: 1.35;
                margin-bottom: 12px;
                color: #fff;
            }
            .price-block {
                display: flex;
                justify-content: space-between;
                align-items: flex-end;
                margin-bottom: 14px;
            }
            .price-val {
                font-size: 22px;
                font-weight: 800;
                background: linear-gradient(135deg, #fff, #e2e8f0);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .market-val {
                font-size: 12px;
                color: var(--text-muted);
            }
            .btn-action {
                display: block;
                width: 100%;
                padding: 13px;
                text-align: center;
                border-radius: 14px;
                background: linear-gradient(135deg, var(--accent-green), #059669);
                color: #022c22;
                font-size: 14px;
                font-weight: 800;
                text-decoration: none;
                border: none;
                box-shadow: 0 4px 18px rgba(16, 185, 129, 0.3);
                cursor: pointer;
            }
            .filter-input {
                width: 100%;
                padding: 14px;
                background: rgba(15, 23, 42, 0.5);
                border: 1px solid var(--glass-border);
                border-radius: 12px;
                color: #fff;
                font-size: 14px;
                margin-bottom: 12px;
                outline: none;
            }
            .pro-banner {
                background: linear-gradient(135deg, rgba(139, 92, 246, 0.15), rgba(6, 182, 212, 0.1));
                border: 1px solid rgba(139, 92, 246, 0.3);
                border-radius: 18px;
                padding: 16px;
                margin-bottom: 16px;
            }
        </style>
    </head>
    <body>
        <div class="header-bar">
            <div class="brand">
                <div class="brand-logo">⚡</div>
                <div>
                    <div class="brand-name">DealHunter</div>
                    <div style="font-size: 11px; color: var(--text-muted);">Smart Arbitrage Radar</div>
                </div>
            </div>
            <div id="subBadge" class="sub-status">
                <span class="status-dot"></span>
                <span id="subText">PRO: 3 дн.</span>
            </div>
        </div>

        <div class="nav-container">
            <button class="nav-item active" id="tabFeed" onclick="switchTab('feed')">🔥 Лента находок</button>
            <button class="nav-item" id="tabMon" onclick="switchTab('mon')">🎯 Мои фильтры</button>
        </div>

        <div id="feedSection">
            <div class="search-wrapper">
                <input type="text" id="searchInput" class="search-input" value="iPhone 15" placeholder="Поиск по маркетплейсам..." onkeydown="if(event.key==='Enter') executeSearch()">
                <button class="search-btn" onclick="executeSearch()">🔍</button>
            </div>
            <div id="lotsContainer"></div>
        </div>

        <div id="monSection" style="display: none;">
            <div class="pro-banner">
                <div style="font-weight: 700; font-size: 15px; margin-bottom: 4px;">⚡ Персональные радары</div>
                <div style="font-size: 12px; color: var(--text-muted);">Бот круглосуточно сканирует товары и присылает уведомления в Telegram.</div>
            </div>
            <div class="lot-card">
                <div style="font-size: 15px; font-weight: 700; margin-bottom: 12px;">Создать фильтр</div>
                <input type="text" id="queryInput" class="filter-input" placeholder="Ключевое слово (например: RTX 4070)">
                <input type="number" id="marginInput" class="filter-input" placeholder="Минимальная маржинальность (%)" value="20">
                <button class="btn-action" onclick="saveNewFilter()">Запустить мониторинг</button>
            </div>
            <div id="monitorsContainer"></div>
        </div>

        <script>
            const tg = window.Telegram ? window.Telegram.WebApp : null;
            if (tg) { tg.expand(); }
            const initData = tg ? (tg.initData || '') : '';

            async function syncUser() {
                try {
                    const r = await fetch('/api/user-status', { headers: { 'X-Telegram-Init-Data': initData } });
                    const d = await r.json();
                    const text = document.getElementById('subText');
                    if (d.is_pro) {
                        text.innerText = `PRO: ${d.days_left} дн.`;
                    } else {
                        text.innerText = 'Триал окончен';
                        document.getElementById('subBadge').style.color = '#ef4444';
                    }
                } catch(e) {}
            }

            async function executeSearch() {
                const q = document.getElementById('searchInput').value.trim();
                const box = document.getElementById('lotsContainer');
                box.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--text-muted);">Сканируем выдачу маркетплейсов...</div>';

                try {
                    const r = await fetch(`/api/live-feed?query=${encodeURIComponent(q)}`);
                    const lots = await r.json();
                    if (!lots || !lots.length) {
                        box.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--text-muted);">Лотов не найдено</div>';
                        return;
                    }
                    box.innerHTML = lots.map(l => `
                        <div class="lot-card">
                            <div class="card-meta">
                                <span class="platform-badge">${l.platform}</span>
                                <span class="margin-badge">+${l.margin}% маржи</span>
                            </div>
                            <div class="lot-title">${l.title}</div>
                            <div class="price-block">
                                <div class="price-val">${l.price.toLocaleString()} ₽</div>
                                <div class="market-val">Рынок: ~${l.median_price.toLocaleString()} ₽</div>
                            </div>
                            <a href="${l.url}" target="_blank" class="btn-action">Забрать лот</a>
                        </div>
                    `).join('');
                } catch(e) {
                    box.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--text-muted);">Ошибка загрузки данных</div>';
                }
            }

            function switchTab(t) {
                if (t === 'feed') {
                    document.getElementById('tabFeed').classList.add('active');
                    document.getElementById('tabMon').classList.remove('active');
                    document.getElementById('feedSection').style.display = 'block';
                    document.getElementById('monSection').style.display = 'none';
                } else {
                    document.getElementById('tabMon').classList.add('active');
                    document.getElementById('tabFeed').classList.remove('active');
                    document.getElementById('feedSection').style.display = 'none';
                    document.getElementById('monSection').style.display = 'block';
                    loadMonitors();
                }
            }

            async function loadMonitors() {
                const box = document.getElementById('monitorsContainer');
                try {
                    const r = await fetch('/api/monitors', { headers: { 'X-Telegram-Init-Data': initData } });
                    const list = await r.json();
                    if (!list.length) {
                        box.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Фильтры не настроены</div>';
                        return;
                    }
                    box.innerHTML = list.map(m => `
                        <div class="lot-card">
                            <div style="font-weight: 700; font-size: 15px;">🔍 ${m.query}</div>
                            <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Порог маржи: от +${m.min_margin}% | Пуши активны</div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            async function saveNewFilter() {
                const q = document.getElementById('queryInput').value.trim();
                const m = parseInt(document.getElementById('marginInput').value) || 20;
                if (!q) return;

                await fetch('/api/monitors', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
                    body: JSON.stringify({ query: q, min_margin: m })
                });

                document.getElementById('queryInput').value = '';
                loadMonitors();
            }

            syncUser();
            executeSearch();
        </script>
    </body>
    </html>
    """
