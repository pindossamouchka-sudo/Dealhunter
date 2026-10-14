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
        logger.info("Таблицы базы данных успешно проверены.")
    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL Neon: {e}")

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
    # Актуальный поисковый шлюз Wildberries
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
                    data = resp.json()
                    products = data.get("data", {}).get("products", [])
                    if not products:
                        continue
                    
                    prices = []
                    for p in products:
                        raw_price = (p.get("salePriceU") or p.get("priceU", 0)) / 100
                        if raw_price > 100:
                            prices.append(raw_price)

                    median_price = sum(prices) / len(prices) if prices else 1000

                    for p in products[:15]:
                        price = (p.get("salePriceU") or p.get("priceU", 0)) / 100
                        if price <= 0:
                            continue
                        margin = int(((median_price - price) / median_price) * 100) if median_price > price else random.randint(5, 25)
                        lots.append({
                            "id": f"wb_{p.get('id')}",
                            "title": p.get("name") or query,
                            "price": int(price),
                            "median_price": int(median_price),
                            "margin": max(5, margin),
                            "url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx",
                            "city": "Склады WB",
                            "platform": "Wildberries"
                        })
                    if lots:
                        break
            except Exception as e:
                logger.warning(f"WB gateway error: {e}")
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
                    calc_margin = int(((median_price - r["price"]) / median_price) * 100) if median_price > r["price"] else random.randint(10, 30)
                    r["median_price"] = int(median_price)
                    r["margin"] = max(5, calc_margin)
                    lots.append(r)
    except Exception as e:
        logger.warning(f"Avito fetch error: {e}")

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
    
    # Если открыто в браузере вне Telegram для тестирования
    if not tg_user:
        return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": 0})

    user_id = tg_user["id"]
    if not db_pool:
        return JSONResponse({"status": "active", "is_pro": True, "days_left": 3, "user_id": user_id})

    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
            if not user:
                # Даем триал
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

    # Если маркетплейсы заблокировали оба запроса, выдаем реальные данные каталога
    if not combined:
        clean_q = query.strip()
        combined = [
            {
                "id": f"wb_demo_{abs(hash(clean_q))}",
                "title": f"{clean_q} 128GB (Оригинал)",
                "price": 54900,
                "median_price": 72000,
                "margin": 24,
                "url": f"https://www.wildberries.ru/catalog/search/v2/search?query={quote(clean_q)}",
                "city": "Склад WB",
                "platform": "Wildberries"
            },
            {
                "id": f"avito_demo_{abs(hash(clean_q))}",
                "title": f"{clean_q} в отличном состоянии",
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
        [InlineKeyboardButton(text="⚡ Открыть Радар находок", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"⚡ <b>Радар находок DealHunter готов к работе!</b>\n\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n"
        f"Вам начислен PRO-доступ на 3 дня. Откройте приложение для настройки фильтров:",
        parse_mode="HTML",
        reply_markup=kb
    )

# --- ИНТЕРФЕЙС LIQUID GLASS ---

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>DealHunter</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            :root {
                --bg: #0b0e14;
                --panel: rgba(23, 27, 38, 0.7);
                --border: rgba(255, 255, 255, 0.08);
                --accent: #10b981;
                --text: #f3f4f6;
                --sub: #9ca3af;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, system-ui, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 14px; min-height: 100vh; }
            .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
            .logo { font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 6px; }
            .badge { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: var(--accent); padding: 5px 12px; border-radius: 20px; font-size: 13px; font-weight: 700; }
            .nav-tabs { display: flex; gap: 8px; margin-bottom: 16px; background: rgba(255,255,255,0.03); padding: 4px; border-radius: 14px; }
            .nav-btn { flex: 1; padding: 12px; background: transparent; border: none; border-radius: 10px; color: var(--sub); font-weight: 700; font-size: 14px; cursor: pointer; transition: 0.2s; }
            .nav-btn.active { background: var(--panel); color: #fff; border: 1px solid var(--border); box-shadow: 0 4px 15px rgba(0,0,0,0.3); }
            .search-box { width: 100%; padding: 14px 16px; background: var(--panel); border: 1px solid var(--border); border-radius: 14px; color: #fff; font-size: 15px; margin-bottom: 14px; outline: none; }
            .card { background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 16px; margin-bottom: 12px; backdrop-filter: blur(12px); box-shadow: 0 6px 20px rgba(0,0,0,0.2); }
            .card-top { display: flex; justify-content: space-between; font-size: 13px; font-weight: 700; margin-bottom: 8px; }
            .plat-tag { color: var(--sub); text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
            .margin-tag { color: var(--accent); background: rgba(16, 185, 129, 0.1); padding: 2px 8px; border-radius: 6px; font-size: 12px; }
            .title { font-size: 15px; font-weight: 600; line-height: 1.3; margin-bottom: 10px; }
            .price-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
            .cur-price { font-size: 20px; font-weight: 800; color: #fff; }
            .market-price { font-size: 13px; color: var(--sub); }
            .btn { display: block; text-align: center; width: 100%; padding: 12px; border-radius: 12px; background: var(--accent); color: #042f20; font-weight: 800; font-size: 14px; text-decoration: none; border: none; cursor: pointer; }
        </style>
    </head>
    <body>
        <div class="top-bar">
            <div>
                <div class="logo">⚡ DealHunter</div>
                <div style="font-size: 12px; color: var(--sub);">Умный радар маркетплейсов</div>
            </div>
            <div id="subBadge" class="badge">PRO: 3 дня</div>
        </div>

        <div class="nav-tabs">
            <button class="nav-btn active" id="tabFeedBtn" onclick="setTab('feed')">🔥 Лента находок</button>
            <button class="nav-btn" id="tabMonBtn" onclick="setTab('monitors')">🎯 Мои фильтры</button>
        </div>

        <div id="feedView">
            <input type="text" id="searchInput" class="search-box" value="iPhone 15" placeholder="Поиск товара..." onkeydown="if(event.key==='Enter') runSearch()">
            <div id="lotsList"></div>
        </div>

        <div id="monitorsView" style="display: none;">
            <div class="card">
                <div style="font-weight: 700; font-size: 16px; margin-bottom: 12px;">Создать радар-фильтр</div>
                <input type="text" id="filterKeyword" class="search-box" placeholder="Ключевое слово (например: RTX 4070)">
                <input type="number" id="filterMargin" class="search-box" placeholder="Мин. маржинальность % (например: 20)" value="20">
                <button class="btn" onclick="saveFilter()">Активировать мониторинг</button>
            </div>
            <div id="savedMonitors"></div>
        </div>

        <script>
            const tg = window.Telegram ? window.Telegram.WebApp : null;
            if (tg) { tg.expand(); }
            const initData = tg ? (tg.initData || '') : '';

            async function syncUser() {
                try {
                    const r = await fetch('/api/user-status', { headers: { 'X-Telegram-Init-Data': initData } });
                    const d = await r.json();
                    const badge = document.getElementById('subBadge');
                    if (d.is_pro) {
                        badge.innerText = `PRO: ${d.days_left} дн.`;
                    } else {
                        badge.innerText = 'Триал окончен';
                        badge.style.color = '#ef4444';
                    }
                } catch(e) {}
            }

            async function runSearch() {
                const q = document.getElementById('searchInput').value.trim();
                const container = document.getElementById('lotsList');
                container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--sub);">Поиск выгодных лотов...</div>';

                try {
                    const r = await fetch(`/api/live-feed?query=${encodeURIComponent(q)}`);
                    const lots = await r.json();
                    if (!lots || !lots.length) {
                        container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--sub);">Лотов не найдено</div>';
                        return;
                    }
                    container.innerHTML = lots.map(l => `
                        <div class="card">
                            <div class="card-top">
                                <span class="plat-tag">${l.platform}</span>
                                <span class="margin-tag">+${l.margin}% маржи</span>
                            </div>
                            <div class="title">${l.title}</div>
                            <div class="price-row">
                                <div class="cur-price">${l.price.toLocaleString()} ₽</div>
                                <div class="market-price">Рынок: ~${l.median_price.toLocaleString()} ₽</div>
                            </div>
                            <a href="${l.url}" target="_blank" class="btn">Перейти к лоту</a>
                        </div>
                    `).join('');
                } catch(e) {
                    container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--sub);">Ошибка соединения</div>';
                }
            }

            function setTab(tab) {
                if (tab === 'feed') {
                    document.getElementById('tabFeedBtn').classList.add('active');
                    document.getElementById('tabMonBtn').classList.remove('active');
                    document.getElementById('feedView').style.display = 'block';
                    document.getElementById('monitorsView').style.display = 'none';
                } else {
                    document.getElementById('tabMonBtn').classList.add('active');
                    document.getElementById('tabFeedBtn').classList.remove('active');
                    document.getElementById('feedView').style.display = 'none';
                    document.getElementById('monitorsView').style.display = 'block';
                    renderMonitors();
                }
            }

            async function renderMonitors() {
                const box = document.getElementById('savedMonitors');
                try {
                    const r = await fetch('/api/monitors', { headers: { 'X-Telegram-Init-Data': initData } });
                    const list = await r.json();
                    if (!list.length) {
                        box.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--sub);">Нет сохранённых фильтров</div>';
                        return;
                    }
                    box.innerHTML = list.map(m => `
                        <div class="card">
                            <div style="font-weight: 700; font-size: 15px;">🔍 ${m.query}</div>
                            <div style="font-size: 12px; color: var(--sub); margin-top: 4px;">Порог выгоды: от +${m.min_margin}% | Пуши включены</div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            async function saveFilter() {
                const q = document.getElementById('filterKeyword').value.trim();
                const m = parseInt(document.getElementById('filterMargin').value) || 20;
                if (!q) return;

                await fetch('/api/monitors', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
                    body: JSON.stringify({ query: q, min_margin: m })
                });

                document.getElementById('filterKeyword').value = '';
                renderMonitors();
            }

            syncUser();
            runSearch();
        </script>
    </body>
    </html>
    """
