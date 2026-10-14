import os
import re
import json
import hmac
import hashlib
import random
import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, unquote
from typing import Optional, List

import asyncpg
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# --- КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter.onrender.com").strip()
PORT = int(os.getenv("PORT", 8000))

# Подготовка ссылки для asyncpg (убираем ?sslmode=... так как asyncpg управляет SSL отдельно)
CLEAN_DB_URL = DATABASE_URL.split("?")[0] if DATABASE_URL else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DealHunter")

app = FastAPI(title="DealHunter Pro Engine")
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
        logger.warning("DATABASE_URL не настроена! Сервер запущен без постоянного хранилища.")
        return

    db_pool = await asyncpg.create_pool(
        dsn=CLEAN_DB_URL,
        ssl="require",
        min_size=1,
        max_size=10
    )

    async with db_pool.acquire() as conn:
        # 1. Пользователи и подписки
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                access_until TIMESTAMP WITH TIME ZONE NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        # 2. Индивидуальные фильтры (мониторы)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_monitors (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                query TEXT NOT NULL,
                city TEXT DEFAULT 'Все регионы',
                min_margin INT DEFAULT 20,
                platform TEXT DEFAULT 'all',
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        # 3. Дедупликация отправленных лотов (чтобы не спамить одним и тем же товаром)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_lots (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                lot_id TEXT NOT NULL,
                sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(user_id, lot_id)
            );
        """)
    logger.info("Таблицы базы данных Neon PostgreSQL успешно инициализированы.")

# --- БЕЗОПАСНАЯ АВТОРИЗАЦИЯ TELEGRAM (HMAC-SHA256) ---

def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    """Проверяет подлинность данных Telegram Mini App и извлекает user_id."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_to_check = parsed_data.pop("hash", None)
        if not hash_to_check:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calculated_hash, hash_to_check):
            user_raw = parsed_data.get("user")
            return json.loads(unquote(user_raw)) if user_raw else None
    except Exception as e:
        logger.error(f"Ошибка валидации initData: {e}")
    return None

# --- МАТЕМАТИКА МАРЖИ (IQR + МЕДИАНА) ---

def calculate_clean_median(prices: List[float]) -> float:
    """Вычисляет медиану с отсечением аномалий (чехлы, поломки, фейки)."""
    valid = sorted([p for p in prices if p > 500])
    if not valid:
        return 0.0
    if len(valid) < 4:
        return sum(valid) / len(valid)

    q1 = valid[len(valid) // 4]
    q3 = valid[(len(valid) * 3) // 4]
    iqr = q3 - q1
    lower_bound = max(100.0, q1 - 1.5 * iqr)
    upper_bound = q3 + 1.5 * iqr

    filtered = [p for p in valid if lower_bound <= p <= upper_bound]
    if not filtered:
        filtered = valid

    mid = len(filtered) // 2
    return filtered[mid] if len(filtered) % 2 != 0 else (filtered[mid - 1] + filtered[mid]) / 2.0

# --- БОЕВЫЕ ПАРСЕРЫ ---

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

async def fetch_wildberries_lots(query: str) -> List[dict]:
    """Парсинг официального поискового JSON-API каталога WB."""
    url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&query={query}&resultset=catalog&sort=popular"
    lots = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]})
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("data", {}).get("products", [])
                prices = [p.get("salePriceU", 0) / 100 for p in products if p.get("salePriceU")]
                median_price = calculate_clean_median(prices)

                for p in products[:15]:
                    price = (p.get("salePriceU") or p.get("priceU", 0)) / 100
                    if price <= 0:
                        continue
                    margin = int(((median_price - price) / median_price) * 100) if median_price > price else 0
                    lots.append({
                        "id": f"wb_{p.get('id')}",
                        "title": p.get("name", query),
                        "price": price,
                        "median_price": int(median_price),
                        "margin": margin,
                        "url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx",
                        "image": f"https://basket-01.wbbasket.ru/vol{p.get('id')//100000}/part{p.get('id')//1000}/{p.get('id')}/images/big/1.webp",
                        "city": "Склады WB",
                        "platform": "WB"
                    })
    except Exception as e:
        logger.error(f"WB error for '{query}': {e}")
    return lots

async def fetch_avito_lots(query: str, city_code: str = "rossiya") -> List[dict]:
    """Парсинг Авито через Flame Proxies с рандомизацией пауз и защитой от ошибок."""
    url = f"https://www.avito.ru/{city_code}?q={query}&s=104"
    lots = []
    
    # Динамическая задержка от детекта
    await asyncio.sleep(random.uniform(1.2, 2.5))
    
    transport = httpx.AsyncHTTPTransport(proxy=PROXY_URL) if PROXY_URL else None

    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(transport=transport, timeout=12.0) as client:
                resp = await client.get(url, headers=HEADERS)
                if resp.status_code in (403, 429):
                    logger.warning(f"Авито блок ({resp.status_code}), ротируем прокси...")
                    await asyncio.sleep(0.5)
                    continue

                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    items = soup.find_all("div", attrs={"data-marker": "item"})
                    raw_lots = []
                    prices = []

                    for it in items[:15]:
                        title_tag = it.find("h3") or it.find("a", attrs={"data-marker": "item-title"})
                        price_meta = it.find("meta", attrs={"itemprop": "price"})
                        link_tag = it.find("a", attrs={"data-marker": "item-title"}) or it.find("a")
                        img_tag = it.find("img")

                        if not title_tag or not price_meta:
                            continue

                        price = float(price_meta.get("content", 0))
                        title = title_tag.text.strip()
                        href = link_tag.get("href", "")
                        full_url = f"https://www.avito.ru{href}" if href.startswith("/") else href
                        img_url = img_tag.get("src", "") if img_tag else ""
                        item_id = it.get("data-item-id") or str(abs(hash(full_url)))

                        prices.append(price)
                        raw_lots.append({
                            "id": f"avito_{item_id}",
                            "title": title,
                            "price": price,
                            "url": full_url,
                            "image": img_url,
                            "city": "Авито",
                            "platform": "Avito"
                        })

                    median_price = calculate_clean_median(prices)
                    for r in raw_lots:
                        margin = int(((median_price - r["price"]) / median_price) * 100) if median_price > r["price"] else 0
                        r["median_price"] = int(median_price)
                        r["margin"] = margin
                        lots.append(r)
                    return lots
        except Exception as e:
            logger.warning(f"Ошибка прокси/Авито: {e}")
            await asyncio.sleep(0.5)

    return lots

# --- ФОНОВЫЙ СКАНЕР И ДОСТАВКА ПЕРСОНАЛЬНЫХ ПУШЕЙ ---

async def background_monitoring_worker():
    """Фоновый поток: опрашивает площадки и шлет персональные пуши подписчикам."""
    while True:
        try:
            await asyncio.sleep(60) # Интервал проверки: 1 минута
            if not db_pool or not bot:
                continue

            async with db_pool.acquire() as conn:
                # Получаем активные фильтры пользователей с действующей подпиской
                monitors = await conn.fetch("""
                    SELECT m.id, m.user_id, m.query, m.min_margin, m.platform
                    FROM user_monitors m
                    JOIN users u ON m.user_id = u.user_id
                    WHERE m.is_active = TRUE AND u.access_until > NOW();
                """)

            if not monitors:
                continue

            # Группируем по уникальным поисковым запросам (пулинг трафика)
            unique_queries = {m["query"] for m in monitors}

            for query in unique_queries:
                wb_task = fetch_wildberries_lots(query)
                avito_task = fetch_avito_lots(query)
                results = await asyncio.gather(wb_task, avito_task, return_exceptions=True)

                found_lots = []
                for res in results:
                    if isinstance(res, list):
                        found_lots.extend(res)

                # Фильтруем и отправляем только тем пользователям, кто ждёт этот лот
                for lot in found_lots:
                    for mon in monitors:
                        if mon["query"].lower() not in lot["title"].lower():
                            continue
                        if lot["margin"] < mon["min_margin"]:
                            continue
                        if mon["platform"] != "all" and mon["platform"].lower() != lot["platform"].lower():
                            continue

                        # Проверяем дедупликацию в БД
                        async with db_pool.acquire() as conn:
                            already_sent = await conn.fetchval(
                                "SELECT 1 FROM sent_lots WHERE user_id = $1 AND lot_id = $2;",
                                mon["user_id"], lot["id"]
                            )
                            if already_sent:
                                continue

                            await conn.execute(
                                "INSERT INTO sent_lots (user_id, lot_id) VALUES ($1, $2);",
                                mon["user_id"], lot["id"]
                            )

                        # Отправка персонального уведомления
                        text = (
                            f"⚡ <b>Найден маржинальный лот!</b>\n\n"
                            f"📦 <b>{lot['title']}</b>\n"
                            f"💰 Цена: <b>{int(lot['price']):,} ₽</b> (Рынок: {int(lot['median_price']):,} ₽)\n"
                            f"📈 Выгода: <b>+{lot['margin']}%</b>\n"
                            f"🏷 Площадка: <b>{lot['platform']}</b>"
                        )
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🔎 Открыть лот", url=lot["url"])]
                        ])
                        try:
                            await bot.send_message(chat_id=mon["user_id"], text=text, parse_mode="HTML", reply_markup=kb)
                        except Exception as e:
                            logger.error(f"Не удалось доставить пуш пользователю {mon['user_id']}: {e}")

        except Exception as e:
            logger.error(f"Сбой в цикле мониторинга: {e}")
            await asyncio.sleep(10)

# --- TELEGRAM BOT ХЭНДЛЕРЫ ---

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Аноним"

    if db_pool:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
            if not user:
                # Начисляем 3 дня бесплатного триала новичку
                trial_until = datetime.now() + timedelta(days=3)
                await conn.execute("""
                    INSERT INTO users (user_id, username, access_until)
                    VALUES ($1, $2, $3);
                """, user_id, username, trial_until)
                # Базовый стартовый фильтр
                await conn.execute("""
                    INSERT INTO user_monitors (user_id, query, min_margin)
                    VALUES ($1, 'iPhone', 15);
                """, user_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Открыть DealHunter Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"👋 <b>Добро пожаловать в DealHunter Radar, {message.from_user.first_name}!</b>\n\n"
        f"Вам начислен бесплатный доступ на <b>3 дня</b>.\n"
        f"Настройте свои ключевые слова и фильтры маржинальности в Mini App:",
        parse_mode="HTML",
        reply_markup=kb
    )

# --- FASTAPI API ЭНДПОИНТЫ ---

@app.on_event("startup")
async def on_startup():
    await init_db()
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(background_monitoring_worker())

@app.get("/health")
async def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

class MonitorCreate(BaseModel):
    query: str
    city: Optional[str] = "Все регионы"
    min_margin: Optional[int] = 20
    platform: Optional[str] = "all"

@app.get("/api/user-status")
async def get_user_status(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user:
        return JSONResponse({"status": "guest", "is_pro": False, "days_left": 0})

    user_id = tg_user["id"]
    if not db_pool:
        return JSONResponse({"status": "guest", "is_pro": False, "days_left": 0})

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", user_id)
        if not user:
            return JSONResponse({"status": "unregistered", "is_pro": False, "days_left": 0})

        now = datetime.now(user["access_until"].tzinfo)
        diff = user["access_until"] - now
        days_left = max(0, diff.days)
        is_pro = diff.total_seconds() > 0

        return JSONResponse({
            "status": "active" if is_pro else "expired",
            "is_pro": is_pro,
            "days_left": days_left,
            "user_id": user_id
        })

@app.get("/api/monitors")
async def get_monitors(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or not db_pool:
        return JSONResponse([])

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, query, city, min_margin, platform, is_active 
            FROM user_monitors WHERE user_id = $1 ORDER BY id DESC;
        """, tg_user["id"])
        return JSONResponse([dict(r) for r in rows])

@app.post("/api/monitors")
async def add_monitor(data: MonitorCreate, request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or not db_pool:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_monitors (user_id, query, city, min_margin, platform)
            VALUES ($1, $2, $3, $4, $5);
        """, tg_user["id"], data.query.strip(), data.city, data.min_margin, data.platform)
    return {"status": "created"}

@app.get("/api/live-feed")
async def live_feed(query: str = "iPhone 15"):
    wb = await fetch_wildberries_lots(query)
    avito = await fetch_avito_lots(query)
    combined = sorted(wb + avito, key=lambda x: x["margin"], reverse=True)
    return JSONResponse(combined)

# --- ИНТЕРФЕЙС MINI APP (LIQUID GLASS UI) ---

@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>DealHunter Radar</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            :root {
                --bg: #090b10;
                --card-bg: rgba(255, 255, 255, 0.04);
                --card-border: rgba(255, 255, 255, 0.08);
                --accent: #38ef7d;
                --text: #ffffff;
                --subtext: #8e9aa8;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg); color: var(--text); padding: 16px; min-height: 100vh; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
            .badge { background: rgba(56, 239, 125, 0.15); color: var(--accent); padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; border: 1px solid rgba(56, 239, 125, 0.3); }
            .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
            .tab-btn { flex: 1; padding: 10px; background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; color: var(--text); font-weight: 600; cursor: pointer; }
            .tab-btn.active { background: rgba(255,255,255,0.12); border-color: rgba(255,255,255,0.25); }
            .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; padding: 16px; margin-bottom: 12px; backdrop-filter: blur(20px); }
            .lot-header { display: flex; justify-content: space-between; font-size: 15px; font-weight: 600; margin-bottom: 6px; }
            .lot-price { font-size: 18px; font-weight: 700; color: #fff; }
            .lot-margin { color: var(--accent); font-weight: 700; }
            .lot-sub { font-size: 13px; color: var(--subtext); margin-top: 4px; }
            .btn-action { display: block; text-align: center; width: 100%; padding: 12px; background: var(--accent); color: #000; border-radius: 12px; text-decoration: none; font-weight: 700; margin-top: 10px; }
            .input-box { width: 100%; padding: 12px; background: rgba(255,255,255,0.06); border: 1px solid var(--card-border); border-radius: 10px; color: #fff; margin-bottom: 10px; }
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h2 style="font-size: 20px;">⚡ DealHunter</h2>
                <div style="font-size: 12px; color: var(--subtext);">Радар цен и маржи</div>
            </div>
            <div id="statusBadge" class="badge">PRO: 3 дня</div>
        </div>

        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('feed')">🔥 Лента находок</button>
            <button class="tab-btn" onclick="switchTab('monitors')">🎯 Мои фильтры</button>
        </div>

        <div id="feedTab">
            <input type="text" id="liveSearch" class="input-box" placeholder="Поиск в реальном времени..." value="iPhone 15" onchange="loadFeed()">
            <div id="feedContainer"><div style="text-align:center; padding: 40px; color: var(--subtext);">Загрузка реальных лотов...</div></div>
        </div>

        <div id="monitorsTab" style="display: none;">
            <div class="card">
                <h3 style="margin-bottom: 12px; font-size: 16px;">Добавить персональный фильтр</h3>
                <input type="text" id="monQuery" class="input-box" placeholder="Фраза (например: RTX 4070)">
                <input type="number" id="monMargin" class="input-box" placeholder="Минимальная маржинальность (%)" value="20">
                <button class="btn-action" onclick="createMonitor()">Сохранить монитор</button>
            </div>
            <div id="monitorsList"></div>
        </div>

        <script>
            const tg = window.Telegram.WebApp;
            tg.expand();
            const initData = tg.initData || '';

            async function checkStatus() {
                try {
                    const r = await fetch('/api/user-status', { headers: { 'X-Telegram-Init-Data': initData } });
                    const d = await r.json();
                    const badge = document.getElementById('statusBadge');
                    if (d.is_pro) {
                        badge.innerText = `PRO: ${d.days_left} дн.`;
                    } else {
                        badge.innerText = 'Триал окончен';
                        badge.style.color = '#ff4b4b';
                    }
                } catch(e) {}
            }

            async function loadFeed() {
                const q = document.getElementById('liveSearch').value;
                const container = document.getElementById('feedContainer');
                container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--subtext);">Сканируем маркетплейсы...</div>';
                try {
                    const r = await fetch(`/api/live-feed?query=${encodeURIComponent(q)}`);
                    const lots = await r.json();
                    if (!lots.length) {
                        container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--subtext);">Лотов не найдено</div>';
                        return;
                    }
                    container.innerHTML = lots.map(l => `
                        <div class="card">
                            <div class="lot-header">
                                <span>${l.platform}</span>
                                <span class="lot-margin">+${l.margin}% маржи</span>
                            </div>
                            <div style="font-weight:600; margin: 4px 0;">${l.title}</div>
                            <div class="lot-price">${l.price.toLocaleString()} ₽</div>
                            <div class="lot-sub">Медиана рынка: ${l.median_price.toLocaleString()} ₽</div>
                            <a href="${l.url}" target="_blank" class="btn-action">Забрать лот</a>
                        </div>
                    `).join('');
                } catch(e) {
                    container.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--subtext);">Ошибка загрузки данных</div>';
                }
            }

            async function switchTab(tab) {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                if (tab === 'feed') {
                    document.querySelectorAll('.tab-btn')[0].classList.add('active');
                    document.getElementById('feedTab').style.display = 'block';
                    document.getElementById('monitorsTab').style.display = 'none';
                } else {
                    document.querySelectorAll('.tab-btn')[1].classList.add('active');
                    document.getElementById('feedTab').style.display = 'none';
                    document.getElementById('monitorsTab').style.display = 'block';
                    loadMonitors();
                }
            }

            async function loadMonitors() {
                const container = document.getElementById('monitorsList');
                try {
                    const r = await fetch('/api/monitors', { headers: { 'X-Telegram-Init-Data': initData } });
                    const list = await r.json();
                    container.innerHTML = list.map(m => `
                        <div class="card">
                            <div style="font-weight: 700; font-size: 16px;">${m.query}</div>
                            <div class="lot-sub">Порог выгоды: от ${m.min_margin}% | ${m.platform.toUpperCase()}</div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            async function createMonitor() {
                const query = document.getElementById('monQuery').value;
                const margin = parseInt(document.getElementById('monMargin').value) || 20;
                if (!query) return;
                await fetch('/api/monitors', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
                    body: JSON.stringify({ query: query, min_margin: margin })
                });
                document.getElementById('monQuery').value = '';
                loadMonitors();
            }

            checkStatus();
            loadFeed();
        </script>
    </body>
    </html>
    """
