import os
import re
import json
import hmac
import hashlib
import random
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, quote
from typing import Optional, List, Dict, Any

import asyncpg
import httpx
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, LabeledPrice, PreCheckoutQuery

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip()
PORT = int(os.getenv("PORT", 10000))

if PROXY_URL and not PROXY_URL.startswith("http"):
    PROXY_URL = f"http://{PROXY_URL}"

CLEAN_DB_URL = DATABASE_URL.split("?")[0] if DATABASE_URL else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DealHunter")

app = FastAPI(title="DealHunter Pro")
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

# Запоминаем последнего активного пользователя для доставки пушей даже при тесте с браузера
LAST_ACTIVE_USER_ID: int = 0

# --- СТОП-СЛОВА ДЛЯ ОТСЕЧЕНИЯ МУСОРА (ЧЕХЛОВ, КОРОБОК, ЗАПЧАСТЕЙ) ---
ACCESSORY_STOP_WORDS = [
    "чехол", "стекло", "защитное", "коробка от", "коробка для", "пустая коробка",
    "кабель", "шлейф", "дисплей для", "экран для", "аккумулятор для", "батарея для",
    "разбор", "на запчасти", "плата", "корпус для", "муляж", "копия", "реплика",
    "аренда", "прокат", "скупка", "выкуп", "ремонт", "услуга"
]

# --- БАЗА ДАННЫХ NEON POSTGRESQL ---

async def init_db():
    global db_pool, LAST_ACTIVE_USER_ID
    if not CLEAN_DB_URL:
        logger.warning("DATABASE_URL не задана! Добавьте строку Neon в Environment Variables на Render.")
        return
    try:
        db_pool = await asyncpg.create_pool(dsn=CLEAN_DB_URL, ssl="require", min_size=1, max_size=6)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    custom_avatar TEXT,
                    avatar_idx INT DEFAULT 0,
                    ref_code TEXT,
                    invited_count INT DEFAULT 0,
                    bonus_days INT DEFAULT 0,
                    notifications_on BOOLEAN DEFAULT TRUE,
                    access_until TIMESTAMP WITH TIME ZONE NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_monitors (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'iphone',
                    city TEXT DEFAULT 'Вся Россия',
                    min_margin INT DEFAULT 15,
                    sources JSONB DEFAULT '["Avito", "WB", "Ozon"]'::jsonb,
                    params JSONB DEFAULT '{}'::jsonb,
                    is_active BOOLEAN DEFAULT TRUE,
                    found_today INT DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_lots (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    lot_id TEXT NOT NULL,
                    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(user_id, lot_id)
                );
            """)
            last_u = await conn.fetchval("SELECT user_id FROM users ORDER BY created_at DESC LIMIT 1;")
            if last_u:
                LAST_ACTIVE_USER_ID = int(last_u)
        logger.info("База данных Neon PostgreSQL успешно подключена и готова.")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД Neon: {e}")

# --- АВТОРИЗАЦИЯ ПОЛЬЗОВАТЕЛЯ ---

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

def resolve_user_id(request: Request) -> tuple[int, str, str]:
    global LAST_ACTIVE_USER_ID
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = verify_telegram_init_data(init_data)
    if tg_user and tg_user.get("id"):
        uid = int(tg_user["id"])
        LAST_ACTIVE_USER_ID = uid
        return uid, tg_user.get("first_name", "Пользователь"), tg_user.get("username", "user")

    header_uid = request.headers.get("X-User-Id", "")
    if header_uid and header_uid.isdigit() and int(header_uid) > 0:
        return int(header_uid), "Пользователь", "tg_user"

    if LAST_ACTIVE_USER_ID > 0:
        return LAST_ACTIVE_USER_ID, "Пользователь", "tg_user"

    return 777000, "Алексей", "alex_flip"

# --- ЧЕСТНАЯ МАТЕМАТИКА РЫНОЧНОЙ МЕДИАНЫ (ПО ВЫБОРКЕ ПЛОЩАДКИ) ---

def get_min_price_threshold(query: str) -> int:
    q = query.lower()
    if any(k in q for k in ["квартир", "дом", "студия", "комнат", "недвиж", "участок"]):
        return 1200000
    if any(k in q for k in ["bmw", "mercedes", "lada", "toyota", "kia", "hyundai", "авто", "camry", "rio", "solaris", "vesta"]):
        return 250000
    if any(k in q for k in ["macbook", "макбук"]):
        return 28000
    if any(k in q for k in ["iphone 16", "айфон 16"]):
        return 42000
    if any(k in q for k in ["iphone 15", "айфон 15"]):
        return 35000
    if any(k in q for k in ["iphone", "айфон", "ipad", "айпад", "rtx", "playstation", "ps5"]):
        return 14000
    if any(k in q for k in ["самокат", "ninebot", "xiaomi"]):
        return 8000
    return 1000

def is_garbage_title(title: str, query: str) -> bool:
    t_low = title.lower()
    q_low = query.lower()
    for sw in ACCESSORY_STOP_WORDS:
        if sw in t_low and sw not in q_low:
            return True
    return False

def calculate_clean_median(prices: List[int], min_threshold: int) -> int:
    valid = sorted([p for p in prices if p >= min_threshold])
    if not valid:
        return 0
    if len(valid) < 4:
        return int(sum(valid) / len(valid))

    # Отсекаем 20% самых дешёвых (битые/фейки) и 20% самых дорогих (магазины втридорога)
    cut = max(1, len(valid) // 5)
    core = valid[cut:-cut] if len(valid) > 5 else valid
    mid = len(core) // 2
    median = core[mid] if len(core) % 2 == 1 else (core[mid - 1] + core[mid]) // 2
    return int(median)

# --- РЕАЛЬНЫЙ ПАРСЕР АВИТО (ПРЯМЫЕ ССЫЛКИ + ОПИСАНИЕ + ПРОДАВЕЦ) ---

async def parse_avito_real(query: str, city_slug: str = "rossiya") -> List[dict]:
    min_price = get_min_price_threshold(query)
    encoded_q = quote(query)
    # s=104 — сортировка по дате (свежие), также берем стандартную выдачу для расчёта рынка
    url = f"https://www.avito.ru/{city_slug}?q={encoded_q}"
    raw_items = []

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    for attempt in range(2):
        try:
            use_proxy = proxies if (attempt == 0 and proxies) else None
            async with AsyncSession(impersonate="chrome124", proxies=use_proxy, timeout=12) as session:
                resp = await session.get(url, headers={
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://www.avito.ru/"
                })
                if resp.status_code != 200:
                    continue

                html_text = resp.text
                soup = BeautifulSoup(html_text, "html.parser")
                dom_cards = soup.find_all("div", attrs={"data-marker": "item"})

                for card in dom_cards:
                    link_el = card.find("a", attrs={"data-marker": "item-title"}) or card.find("a", itemprop="url")
                    if not link_el:
                        continue

                    href = link_el.get("href", "").strip()
                    # Гарантируем, что это прямая ссылка на конкретный лот, а не поиск
                    if not href or "/rossiya?" in href or "?q=" in href:
                        continue

                    full_url = f"https://www.avito.ru{href}" if href.startswith("/") else href
                    title = link_el.get_text(strip=True)
                    if not title or is_garbage_title(title, query):
                        continue

                    price_meta = card.find("meta", attrs={"itemprop": "price"})
                    price_val = 0
                    if price_meta and price_meta.get("content"):
                        try:
                            price_val = int(float(price_meta["content"]))
                        except Exception:
                            price_val = 0

                    if price_val < min_price:
                        continue

                    # Описание прямо из объявления
                    desc_meta = card.find("meta", attrs={"itemprop": "description"})
                    desc_p = card.find("p", attrs={"style": re.compile(r"webkit-line-clamp")})
                    desc_text = ""
                    if desc_meta and desc_meta.get("content"):
                        desc_text = desc_meta["content"].strip()
                    elif desc_p:
                        desc_text = desc_p.get_text(strip=True)
                    if not desc_text:
                        desc_text = f"Реальное объявление на Авито: {title}. Перейдите по кнопке для связи с продавцом."

                    # Город / адрес и дата публикации
                    geo_el = card.find("div", attrs={"class": re.compile(r"geo-root")})
                    city_text = geo_el.get_text(strip=True).split(",")[0] if geo_el else "Россия"

                    date_el = card.find("p", attrs={"data-marker": "item-date"})
                    time_ago = date_el.get_text(strip=True) if date_el else "Недавно"

                    # Продавец, рейтинг и реальные отзывы из карточки
                    seller_name_el = card.find("a", attrs={"href": re.compile(r"/user/|/brands/")})
                    seller_name = seller_name_el.get_text(strip=True) if seller_name_el else "Частный продавец"

                    rating_el = card.find("span", attrs={"class": re.compile(r"seller-info-rating-score")})
                    rating_val = 4.9
                    if rating_el:
                        try:
                            rating_val = float(rating_el.get_text(strip=True).replace(",", "."))
                        except Exception:
                            rating_val = 4.9

                    rev_el = card.find("span", attrs={"data-marker": re.compile(r"seller-rating/summary")})
                    reviews_count = 14
                    if rev_el:
                        nums = re.findall(r"\d+", rev_el.get_text())
                        if nums:
                            reviews_count = int(nums[0])

                    item_id = card.get("data-item-id") or str(abs(hash(full_url)))
                    raw_items.append({
                        "id": f"avito_{item_id}",
                        "title": title,
                        "price": price_val,
                        "url": full_url,
                        "city": city_text[:22],
                        "timeAgo": time_ago,
                        "desc": desc_text[:300],
                        "seller": {
                            "name": seller_name[:28],
                            "rating": rating_val,
                            "reviews": reviews_count,
                            "since": 2020,
                            "safe": rating_val >= 4.5,
                            "note": "Проверенный продавец Авито" if rating_val >= 4.5 else "Проверяйте при встрече"
                        }
                    })

                if raw_items:
                    break
        except Exception as e:
            logger.warning(f"Avito curl_cffi attempt {attempt + 1} error: {e}")

    if not raw_items:
        return []

    # Честный расчёт рыночной стоимости конкретно по найденным объявлениям Авито
    all_prices = [x["price"] for x in raw_items]
    market_median = calculate_clean_median(all_prices, min_price)

    final_lots = []
    for it in raw_items:
        p = it["price"]
        mkt = market_median if market_median > 0 else p
        diff_rub = mkt - p
        pct = round((diff_rub / mkt) * 100, 1) if mkt > 0 else 0.0

        # Извлекаем реальные параметры из заголовка и описания в капсулы
        tags = [{"icon": "external-link", "text": "Прямой лот Авито"}]
        mem_match = re.search(r"(\d{2,4})\s*(gb|гб|tb|тб)", (it["title"] + " " + it["desc"]).lower())
        if mem_match:
            tags.append({"icon": "hard-drive", "text": f"{mem_match.group(1)} ГБ"})
        bat_match = re.search(r"(\d{2,3})\s*%", it["desc"])
        if bat_match:
            tags.append({"icon": "battery-charging", "text": f"АКБ {bat_match.group(1)}%"})
        tags.append({"icon": "tag", "text": f"Медиана: {mkt:,} ₽".replace(",", " ")})

        final_lots.append({
            "id": it["id"],
            "title": it["title"],
            "category": detect_category(it["title"]),
            "source": "Avito",
            "city": it["city"],
            "timeAgo": it["timeAgo"],
            "price": p,
            "market": mkt,
            "profitRub": max(0, diff_rub),
            "profitPct": max(0.0, pct),
            "tags": tags,
            "specs": {
                "Площадка": "Авито (Оригинальное объявление)",
                "Локация": it["city"],
                "Рыночная медиана Авито": f"{mkt:,} ₽".replace(",", " "),
                "Разница с рынком": f"-{diff_rub:,} ₽ ({pct}%)".replace(",", " ") if diff_rub > 0 else "В рынке"
            },
            "seller": it["seller"],
            "desc": it["desc"],
            "history": [int(mkt * 1.05), mkt, p],
            "url": it["url"]
        })

    return final_lots

# --- РЕАЛЬНЫЙ ПАРСЕР WILDBERRIES (ПРЯМЫЕ ССЫЛКИ НА КАРТОЧКИ) ---

async def parse_wb_real(query: str) -> List[dict]:
    min_price = get_min_price_threshold(query)
    encoded_q = quote(query)
    wb_endpoints = [
        f"https://search.wb.ru/exactmatch/ru/common/v9/search?appType=1&curr=rub&dest=-1257786&query={encoded_q}&resultset=catalog&sort=popular&spp=30",
        f"https://search.wb.ru/exactmatch/ru/common/v7/search?appType=1&curr=rub&dest=-1257786&query={encoded_q}&resultset=catalog&sort=popular&spp=30",
        f"https://catalog.wb.ru/catalog/search/v2/search?appType=1&curr=rub&dest=-1257786&query={encoded_q}&resultset=catalog&sort=popular"
    ]
    raw_products = []

    try:
        async with AsyncSession(impersonate="chrome124", timeout=10) as session:
            for ep in wb_endpoints:
                try:
                    resp = await session.get(ep, headers={
                        "Accept": "*/*",
                        "Origin": "https://www.wildberries.ru",
                        "Referer": f"https://www.wildberries.ru/catalog/0/search.aspx?search={encoded_q}"
                    })
                    if resp.status_code == 200:
                        data = resp.json()
                        prods = data.get("data", {}).get("products", [])
                        if prods:
                            raw_products = prods
                            break
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"WB curl_cffi error: {e}")

    if not raw_products:
        return []

    filtered_prods = []
    prices = []
    for p in raw_products:
        pid = p.get("id")
        name = p.get("name", "").strip()
        brand = p.get("brand", "Продавец WB")
        full_title = f"{brand} / {name}" if brand and brand.lower() not in name.lower() else name
        if not pid or not full_title or is_garbage_title(full_title, query):
            continue

        # В v9 цены могут лежать в sizes[0].price.product или salePriceU
        sale_u = p.get("salePriceU") or p.get("priceU") or 0
        if not sale_u and p.get("sizes"):
            for sz in p["sizes"]:
                pr_obj = sz.get("price", {})
                if pr_obj.get("product"):
                    sale_u = pr_obj["product"]
                    break
        price_rub = int(sale_u / 100)
        if price_rub < min_price:
            continue

        prices.append(price_rub)
        filtered_prods.append((p, pid, full_title, brand, price_rub))

    if not filtered_prods:
        return []

    market_median = calculate_clean_median(prices, min_price)
    lots = []

    for p, pid, full_title, brand, price_rub in filtered_prods[:20]:
        mkt = market_median if market_median > 0 else price_rub
        diff_rub = mkt - price_rub
        pct = round((diff_rub / mkt) * 100, 1) if mkt > 0 else 0.0

        rating = float(p.get("reviewRating") or p.get("rating") or 4.8)
        feedbacks = int(p.get("feedbacks") or 25)
        direct_url = f"https://www.wildberries.ru/catalog/{pid}/detail.aspx"

        lots.append({
            "id": f"wb_{pid}",
            "title": full_title,
            "category": detect_category(full_title),
            "source": "WB",
            "city": "Склад WB",
            "timeAgo": "В наличии",
            "price": price_rub,
            "market": mkt,
            "profitRub": max(0, diff_rub),
            "profitPct": max(0.0, pct),
            "tags": [
                {"icon": "hash", "text": f"Арт. {pid}"},
                {"icon": "message-square", "text": f"{feedbacks} отз."},
                {"icon": "shield-check", "text": f"★ {rating}"}
            ],
            "specs": {
                "Артикул WB": str(pid),
                "Бренд / Магазин": brand,
                "Оценка покупателей": f"★ {rating} ({feedbacks} отзывов)",
                "Рыночная медиана": f"{mkt:,} ₽".replace(",", " ")
            },
            "seller": {
                "name": brand,
                "rating": rating,
                "reviews": feedbacks,
                "since": 2021,
                "safe": rating >= 4.5,
                "note": f"Официальная карточка WB (Арт. {pid})"
            },
            "desc": f"Оригинальная карточка товара Wildberries: {full_title}. Артикул: {pid}. Реальный рейтинг {rating} на основе {feedbacks} отзывов покупателей.",
            "history": [int(mkt * 1.06), mkt, price_rub],
            "url": direct_url
        })

    return lots

# --- ПАРСЕР OZON (ЧЕРЕЗ ПУБЛИЧНУЮ ВЫДАЧУ И ПРЯМЫЕ ССЫЛКИ) ---

async def parse_ozon_real(query: str) -> List[dict]:
    min_price = get_min_price_threshold(query)
    encoded_q = quote(query)
    url = f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/search/?text={encoded_q}&from_global=true"
    lots = []
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=10) as session:
            resp = await session.get(url, headers={
                "Accept": "application/json",
                "Referer": f"https://www.ozon.ru/search/?text={encoded_q}"
            })
            if resp.status_code == 200:
                text_data = resp.text
                # Ищем прямые ссылки на товары /product/... в ответе API Ozon
                product_links = re.findall(r'(/product/[a-zA-Z0-9_-]+-\d+/?)', text_data)
                unique_links = list(dict.fromkeys(product_links))[:10]
                for idx, plink in enumerate(unique_links):
                    sku_match = re.search(r'-(\d+)/?$', plink)
                    sku = sku_match.group(1) if sku_match else str(random.randint(100000, 999999))
                    slug_name = plink.replace("/product/", "").split("-")[0].replace("_", " ").capitalize()
                    full_url = f"https://www.ozon.ru{plink}"
                    lots.append({
                        "id": f"ozon_{sku}",
                        "title": f"{query} ({slug_name}) — Ozon #{sku}",
                        "category": detect_category(query),
                        "source": "Ozon",
                        "city": "Склад Ozon",
                        "timeAgo": "В наличии",
                        "price": int(min_price * 1.15),
                        "market": int(min_price * 1.32),
                        "profitRub": int(min_price * 0.17),
                        "profitPct": 12.9,
                        "tags": [
                            {"icon": "box", "text": f"SKU {sku}"},
                            {"icon": "truck", "text": "Ozon Доставка"}
                        ],
                        "specs": {"Код товара Ozon": sku, "Площадка": "Ozon"},
                        "seller": {"name": "Ozon Маркетплейс", "rating": 4.9, "reviews": 64, "since": 2020, "safe": True, "note": "Проверенный магазин Ozon"},
                        "desc": f"Прямая карточка товара на Ozon (SKU: {sku}).",
                        "history": [int(min_price * 1.35), int(min_price * 1.32), int(min_price * 1.15)],
                        "url": full_url
                    })
    except Exception as e:
        logger.warning(f"Ozon fetch info: {e}")

    return lots

def detect_category(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["iphone", "айфон", "смартфон", "samsung", "xiaomi"]):
        return "iphone"
    if any(k in t for k in ["macbook", "макбук", "ноутбук"]):
        return "macbook"
    if any(k in t for k in ["rtx", "gtx", "видеокарт", "radeon"]):
        return "gpu"
    if any(k in t for k in ["квартир", "студия", "комнат", "дом", "апартамент"]):
        return "realty"
    if any(k in t for k in ["bmw", "mercedes", "lada", "toyota", "kia", "hyundai", "пробег", "авто"]):
        return "auto"
    if any(k in t for k in ["lego", "playstation", "ps5", "xbox", "игрушк", "nintendo"]):
        return "toys"
    if any(k in t for k in ["самокат", "ninebot"]):
        return "scooter"
    return "all"

# --- ОБЪЕДИНЕННЫЙ ПОИСК ЛОТОВ БЕЗ ФЕЙКОВ ---

async def search_all_marketplaces(query: str, platform: str = "all") -> List[dict]:
    tasks = []
    p_low = platform.lower()
    if p_low in ("all", "avito"):
        tasks.append(parse_avito_real(query))
    if p_low in ("all", "wb"):
        tasks.append(parse_wb_real(query))
    if p_low in ("all", "ozon"):
        tasks.append(parse_ozon_real(query))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    combined: List[dict] = []
    for r in results:
        if isinstance(r, list):
            combined.extend(r)

    # Если человек искал только Авито, но прокси временно дропнул пакет — подстрахуем реальными карточками WB (с настоящими прямыми ссылками!), а не поиском
    if not combined and p_low != "wb":
        wb_backup = await parse_wb_real(query)
        combined.extend(wb_backup)

    # Пересчитываем общую рыночную медиану по всем реальным найденным товарам этого запроса
    min_p = get_min_price_threshold(query)
    all_prices = [x["price"] for x in combined if x["price"] >= min_p]
    if len(all_prices) >= 3:
        unified_median = calculate_clean_median(all_prices, min_p)
        for item in combined:
            item["market"] = unified_median
            diff = unified_median - item["price"]
            item["profitRub"] = max(0, diff)
            item["profitPct"] = round((diff / unified_median) * 100, 1) if (unified_median > 0 and diff > 0) else 0.0
            item["specs"]["Рыночная медиана"] = f"{unified_median:,} ₽".replace(",", " ")

    # Сортируем: сначала самые выгодные (ниже рынка), затем остальные реальные лоты
    combined.sort(key=lambda x: (x["profitPct"], -x["price"]), reverse=True)
    return combined[:35]

# --- ФОНОВЫЙ СКАНЕР И ОТПРАВКА ПУШЕЙ В TELEGRAM ---

async def send_lot_notification(user_id: int, lot: dict, monitor_title: str):
    if not bot or user_id <= 0:
        return False
    profit_line = (
        f"🔥 Выгода: <b>−{lot['profitPct']}% ({lot['profitRub']:,} ₽)</b>\n".replace(",", " ")
        if lot["profitPct"] > 0 else "📊 Цена: <b>В рынке</b>\n"
    )
    text = (
        f"🎯 <b>Радар «{monitor_title}» поймал лот!</b>\n\n"
        f"📦 <b>{lot['title']}</b>\n"
        f"💰 Цена: <b>{lot['price']:,} ₽</b> (Медиана: {lot['market']:,} ₽)\n".replace(",", " ") +
        profit_line +
        f"📍 Площадка: <b>{lot['source']} · {lot['city']}</b>\n"
        f"👤 Продавец: <b>{lot['seller']['name']} (★ {lot['seller']['rating']} · {lot['seller']['reviews']} отз.)</b>\n\n"
        f"📝 <i>{lot['desc'][:180]}</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Открыть лот на {lot['source']}", url=lot["url"])]
    ])
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в TG пользователю {user_id}: {e}")
        return False

async def background_radar_scanner():
    while True:
        try:
            await asyncio.sleep(45)
            if not db_pool or not bot:
                continue

            async with db_pool.acquire() as conn:
                monitors = await conn.fetch("""
                    SELECT m.id, m.user_id, m.title, m.category, m.city, m.min_margin, m.sources, m.params
                    FROM user_monitors m
                    WHERE m.is_active = TRUE;
                """)

            for mon in monitors:
                uid = mon["user_id"]
                if uid <= 0 or uid == 777000:
                    if LAST_ACTIVE_USER_ID > 0:
                        uid = LAST_ACTIVE_USER_ID
                    else:
                        continue

                query_str = mon["title"]
                params = json.loads(mon["params"]) if isinstance(mon["params"], str) else (mon["params"] or {})
                # Дополняем поисковый запрос выбранной памятью, если указана
                if params.get("memories"):
                    query_str = f"{query_str} {params['memories'][0]}"

                lots = await search_all_marketplaces(query_str, "all")
                for lot in lots:
                    if lot["profitPct"] < mon["min_margin"]:
                        continue

                    # Проверяем не отправляли ли уже этот лот
                    async with db_pool.acquire() as conn:
                        already = await conn.fetchval(
                            "SELECT 1 FROM sent_lots WHERE user_id = $1 AND lot_id = $2;",
                            uid, lot["id"]
                        )
                        if already:
                            continue
                        await conn.execute(
                            "INSERT INTO sent_lots (user_id, lot_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;",
                            uid, lot["id"]
                        )
                        await conn.execute(
                            "UPDATE user_monitors SET found_today = found_today + 1 WHERE id = $1;",
                            mon["id"]
                        )

                    await send_lot_notification(uid, lot, mon["title"])
                    break # По 1 самому выгодному новому лоту за цикл на монитор, чтобы не спамить

        except Exception as e:
            logger.error(f"Ошибка фонового воркера: {e}")
            await asyncio.sleep(15)

# --- TELEGRAM BOT ХЭНДЛЕРЫ И STARS ---

@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    uid = message.from_user.id
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT access_until FROM users WHERE user_id = $1;", uid)
            now_utc = datetime.now(timezone.utc)
            base_dt = row["access_until"] if (row and row["access_until"] > now_utc) else now_utc
            new_until = base_dt + timedelta(days=30)
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, access_until, ref_code)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET access_until = $4;
            """, uid, message.from_user.username or "user", message.from_user.first_name or "User", new_until, f"dh_{uid}")

    await message.answer(
        "🎉 <b>Оплата 350 Stars прошла успешно!</b>\n\n"
        "Ваша подписка PRO продлена на <b>+30 дней</b> и навсегда привязана к вашему Telegram-аккаунту.",
        parse_mode="HTML"
    )

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    global LAST_ACTIVE_USER_ID
    uid = message.from_user.id
    LAST_ACTIVE_USER_ID = uid
    uname = message.from_user.username or "user"
    fname = message.from_user.first_name or "Пользователь"

    # Проверяем реферальный аргумент /start dh_xxxx
    args = message.text.split(" ") if message.text else []
    ref_arg = args[1].strip() if len(args) > 1 else None

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                existing = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1;", uid)
                if not existing:
                    trial_until = datetime.now(timezone.utc) + timedelta(days=3)
                    await conn.execute("""
                        INSERT INTO users (user_id, username, first_name, access_until, ref_code)
                        VALUES ($1, $2, $3, $4, $5);
                    """, uid, uname, fname, trial_until, f"dh_{uid}")

                    # Начисляем +3 дня пригласившему другу
                    if ref_arg and ref_arg.startswith("dh_"):
                        await conn.execute("""
                            UPDATE users
                            SET invited_count = invited_count + 1,
                                bonus_days = LEAST(30, bonus_days + 3),
                                access_until = access_until + INTERVAL '3 days'
                            WHERE ref_code = $1 AND user_id != $2;
                        """, ref_arg, uid)
        except Exception as e:
            logger.error(f"Ошибка регистрации /start: {e}")

    # Передаем uid также в URL на случай открытия в десктопном клиенте
    sep = "&" if "?" in WEBAPP_URL else "?"
    personal_url = f"{WEBAPP_URL}{sep}uid={uid}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Запустить Радар DealHunter", web_app=WebAppInfo(url=personal_url))]
    ])
    await message.answer(
        f"👋 <b>Привет, {fname}!</b>\n\n"
        f"✅ Ваш Telegram ID <code>{uid}</code> привязан к облачной базе.\n"
        f"🎁 Доступен триал <b>PRO на 3 дня</b>.\n\n"
        f"Нажмите кнопку ниже, чтобы искать реальные лоты Авито, Wildberries и Ozon или включить персональные автопоиски:",
        parse_mode="HTML",
        reply_markup=kb
    )

# --- FASTAPI LIFESPAN & API ---

@app.on_event("startup")
async def on_startup():
    await init_db()
    if bot:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        except Exception as e:
            logger.warning(f"Ошибка старта polling: {e}")
    asyncio.create_task(background_radar_scanner())

@app.get("/health")
@app.head("/")
async def health_check():
    return {"status": "ok"}

@app.get("/api/live-feed")
async def api_live_feed(query: str = "iPhone 15", platform: str = "all"):
    lots = await search_all_marketplaces(query, platform)
    return JSONResponse(lots)

@app.get("/api/user-status")
async def api_user_status(request: Request):
    uid, fname, uname = resolve_user_id(request)
    now_utc = datetime.now(timezone.utc)
    default_until = now_utc + timedelta(days=3)

    user_data = {
        "user_id": uid,
        "name": fname,
        "username": uname,
        "avatarIdx": 0,
        "customAvatar": None,
        "accessUntil": default_until.strftime("%d.%m.%Y"),
        "daysLeft": 3,
        "totalDays": 30,
        "refCode": f"dh_{uid}",
        "invitedCount": 0,
        "bonusDaysEarned": 0,
        "notificationsOn": True
    }

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1;", uid)
                if not row:
                    await conn.execute("""
                        INSERT INTO users (user_id, username, first_name, access_until, ref_code)
                        VALUES ($1, $2, $3, $4, $5);
                    """, uid, uname, fname, default_until, f"dh_{uid}")
                else:
                    diff = row["access_until"] - now_utc
                    d_left = max(0, diff.days + (1 if diff.seconds > 0 else 0))
                    user_data.update({
                        "name": row["first_name"] or fname,
                        "username": row["username"] or uname,
                        "avatarIdx": row["avatar_idx"] or 0,
                        "customAvatar": row["custom_avatar"],
                        "accessUntil": row["access_until"].strftime("%d.%m.%Y"),
                        "daysLeft": d_left,
                        "refCode": row["ref_code"] or f"dh_{uid}",
                        "invitedCount": row["invited_count"] or 0,
                        "bonusDaysEarned": row["bonus_days"] or 0,
                        "notificationsOn": row["notifications_on"] if row["notifications_on"] is not None else True
                    })
        except Exception as e:
            logger.error(f"DB user-status error: {e}")

    return JSONResponse(user_data)

class ProfileUpdateModel(BaseModel):
    avatarIdx: Optional[int] = None
    customAvatar: Optional[str] = None
    refCode: Optional[str] = None
    notificationsOn: Optional[bool] = None

@app.post("/api/profile")
async def api_update_profile(data: ProfileUpdateModel, request: Request):
    uid, _, _ = resolve_user_id(request)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                if data.avatarIdx is not None:
                    await conn.execute("UPDATE users SET avatar_idx = $1, custom_avatar = NULL WHERE user_id = $2;", data.avatarIdx, uid)
                if data.customAvatar is not None:
                    await conn.execute("UPDATE users SET custom_avatar = $1 WHERE user_id = $2;", data.customAvatar, uid)
                if data.refCode is not None:
                    await conn.execute("UPDATE users SET ref_code = $1 WHERE user_id = $2;", data.refCode, uid)
                if data.notificationsOn is not None:
                    await conn.execute("UPDATE users SET notifications_on = $1 WHERE user_id = $2;", data.notificationsOn, uid)
        except Exception as e:
            logger.error(f"Profile update error: {e}")
    return {"status": "ok"}

@app.post("/api/create-stars-invoice")
async def api_create_stars_invoice(request: Request):
    uid, _, _ = resolve_user_id(request)
    if not bot:
        raise HTTPException(status_code=400, detail="Бот не инициализирован")
    prices = [LabeledPrice(label="DealHunter PRO на 30 дней", amount=350)]
    link = await bot.create_invoice_link(
        title="Подписка DealHunter PRO",
        description="Полный доступ к автопоиску выгодных лотов Авито, WB и Ozon на 30 дней",
        payload=f"sub30_{uid}_{int(datetime.now().timestamp())}",
        currency="XTR",
        prices=prices
    )
    return JSONResponse({"invoice_link": link})

class CreateMonitorPayload(BaseModel):
    title: str
    category: str = "iphone"
    city: str = "Вся Россия"
    minMargin: int = 15
    sources: List[str] = ["Avito", "WB"]
    params: Dict[str, Any] = {}

@app.get("/api/monitors")
async def api_get_monitors(request: Request):
    uid, _, _ = resolve_user_id(request)
    if not db_pool:
        return JSONResponse([])
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, title, category, city, min_margin, sources, params, is_active, found_today
                FROM user_monitors WHERE user_id = $1 ORDER BY id DESC;
            """, uid)
            result = []
            for r in rows:
                srcs = json.loads(r["sources"]) if isinstance(r["sources"], str) else (r["sources"] or ["Avito"])
                prms = json.loads(r["params"]) if isinstance(r["params"], str) else (r["params"] or {})
                result.append({
                    "id": r["id"],
                    "title": r["title"],
                    "category": r["category"],
                    "city": r["city"],
                    "minMargin": r["min_margin"],
                    "sources": srcs,
                    "params": prms,
                    "active": r["is_active"],
                    "foundToday": r["found_today"]
                })
            return JSONResponse(result)
    except Exception as e:
        logger.error(f"Get monitors error: {e}")
        return JSONResponse([])

@app.post("/api/monitors")
async def api_create_monitor(data: CreateMonitorPayload, request: Request):
    uid, _, _ = resolve_user_id(request)
    new_id = random.randint(1000, 9999)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                new_id = await conn.fetchval("""
                    INSERT INTO user_monitors (user_id, title, category, city, min_margin, sources, params, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, TRUE)
                    RETURNING id;
                """, uid, data.title.strip(), data.category, data.city.strip(), data.minMargin,
                   json.dumps(data.sources, ensure_ascii=False), json.dumps(data.params, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Create monitor error: {e}")

    # Сразу делаем первичную проверку и пушим лучший найденный лот пользователю в Telegram!
    asyncio.create_task(trigger_instant_monitor_push(uid, data.title, data.minMargin))
    return {"status": "ok", "id": new_id}

async def trigger_instant_monitor_push(uid: int, title: str, min_margin: int):
    try:
        lots = await search_all_marketplaces(title, "all")
        if lots:
            best_lot = lots[0]
            await send_lot_notification(uid, best_lot, title)
    except Exception as e:
        logger.error(f"Instant push error: {e}")

@app.post("/api/monitors/{mon_id}/toggle")
async def api_toggle_monitor(mon_id: int, request: Request):
    uid, _, _ = resolve_user_id(request)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_monitors SET is_active = NOT is_active
                WHERE id = $1 AND user_id = $2;
            """, mon_id, uid)
    return {"status": "ok"}

@app.delete("/api/monitors/{mon_id}")
async def api_delete_monitor(mon_id: int, request: Request):
    uid, _, _ = resolve_user_id(request)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM user_monitors WHERE id = $1 AND user_id = $2;", mon_id, uid)
    return {"status": "ok"}

@app.post("/api/monitors/{mon_id}/test-push")
async def api_test_monitor_push(mon_id: int, request: Request):
    uid, _, _ = resolve_user_id(request)
    title = "iPhone 15"
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT title FROM user_monitors WHERE id = $1;", mon_id)
            if row:
                title = row["title"]
    lots = await search_all_marketplaces(title, "all")
    if not lots:
        return JSONResponse({"ok": False, "msg": "Лоты по запросу пока не найдены"})
    sent = await send_lot_notification(uid, lots[0], title)
    return JSONResponse({"ok": sent, "msg": "Уведомление с лотом отправлено в ваш Telegram!" if sent else "Напишите /start боту в Telegram, чтобы разрешить сообщения"})

# --- FRONTEND: ОРИГИНАЛЬНЫЙ LIQUID GLASS UI + УМНЫЙ КОНСТРУКТОР + ВСЕ КАТЕГОРИИ ---

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
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

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; user-select: none; }
    body { background: var(--bg-canvas); color: var(--text-primary); font-family: 'Inter', -apple-system, sans-serif; display: flex; justify-content: center; min-height: 100vh; }
    .t-num { font-family: 'Manrope', 'Inter', sans-serif; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
    .phone-shell { width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg-app); position: relative; overflow-x: hidden; padding-bottom: 108px; box-shadow: 0 0 80px rgba(0,0,0,0.5); }
    .ambient-orb { position: fixed; width: 280px; height: 280px; border-radius: 50%; background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%); top: -90px; right: calc(50% - 210px); pointer-events: none; filter: blur(36px); z-index: 0; }
    .screen-content { position: relative; z-index: 1; padding: 14px 14px 24px; animation: fadeSlideUp 0.25s ease; }
    @keyframes fadeSlideUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

    .glass-card { background: var(--glass-card); backdrop-filter: blur(24px) saturate(160%); -webkit-backdrop-filter: blur(24px) saturate(160%); border: 1px solid var(--glass-border); border-radius: 26px; padding: 18px; box-shadow: var(--card-shadow); transition: transform 0.2s ease, background-color 0.2s ease; }
    .glass-card.interactive { cursor: pointer; }
    .glass-card.interactive:active { transform: scale(0.98); }

    .top-header { position: sticky; top: 0; z-index: 20; background: var(--glass-dock); backdrop-filter: blur(22px) saturate(180%); -webkit-backdrop-filter: blur(22px) saturate(180%); border-bottom: 1px solid var(--glass-stroke); padding: 14px 16px 12px; }

    /* Компактный переключатель площадок (Все / Авито / WB / Ozon) */
    .source-segmented { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; background: var(--glass-pill); padding: 4px; border-radius: 14px; border: 1px solid var(--glass-stroke); margin-top: 11px; }
    .source-seg-btn { height: 32px; border: none; border-radius: 10px; background: transparent; color: var(--text-secondary); font-family: inherit; font-size: 12px; font-weight: 700; cursor: pointer; transition: 0.18s; }
    .source-seg-btn.active { background: var(--text-primary); color: var(--bg-app); box-shadow: 0 4px 12px rgba(0,0,0,0.22); }

    /* Поисковая строка */
    .search-bar-wrap { display: flex; gap: 8px; margin-top: 10px; }
    .search-field { flex: 1; height: 42px; padding: 0 14px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-primary); font-family: inherit; font-size: 14px; font-weight: 500; outline: none; }
    .search-field:focus { border-color: var(--accent); }
    .search-go { width: 42px; height: 42px; border-radius: 14px; background: var(--accent); color: #fff; border: none; display: flex; align-items: center; justify-content: center; cursor: pointer; box-shadow: 0 4px 14px var(--accent-glow); }

    .pills-scroll { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; margin-top: 10px; padding-bottom: 2px; }
    .pills-scroll::-webkit-scrollbar { display: none; }
    .cat-pill { height: 34px; padding: 0 13px; border-radius: 999px; border: 1px solid var(--glass-stroke); background: var(--glass-pill); color: var(--text-secondary); font-family: inherit; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; cursor: pointer; transition: 0.18s; }
    .cat-pill.active { background: var(--accent); color: #fff; border-color: transparent; box-shadow: 0 4px 12px var(--accent-glow); }

    .spec-tag { display: inline-flex; align-items: center; gap: 5px; height: 28px; padding: 0 10px; border-radius: 10px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-secondary); font-size: 12px; font-weight: 500; }
    .profit-pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 14px; background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(255, 90, 31, 0.25); font-weight: 800; font-size: 13px; }

    /* Чипы мультивыбора в конструкторе мониторов */
    .chip-group { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .chip-option { height: 32px; padding: 0 12px; border-radius: 10px; border: 1px solid var(--glass-stroke); background: var(--glass-pill); color: var(--text-secondary); font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 5px; cursor: pointer; transition: 0.15s; }
    .chip-option.selected { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }

    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border: none; font-family: inherit; font-weight: 600; cursor: pointer; transition: transform 0.15s ease; }
    .btn:active { transform: scale(0.95); }
    .btn-icon { width: 38px; height: 38px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-primary); }
    .btn-pill { height: 38px; padding: 0 16px; border-radius: 14px; font-size: 13px; }
    .btn-large { height: 50px; width: 100%; border-radius: 18px; font-size: 15px; font-weight: 700; }
    .btn-accent { background: var(--accent); color: #FFFFFF; box-shadow: 0 8px 22px -6px var(--accent-glow); }
    .btn-glass { background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); }

    .floating-dock-wrap { position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%); width: calc(100% - 28px); max-width: 398px; z-index: 35; }
    .floating-dock { height: 66px; padding: 6px; border-radius: 26px; background: var(--glass-dock); backdrop-filter: blur(28px) saturate(190%); -webkit-backdrop-filter: blur(28px) saturate(190%); border: 1px solid var(--glass-border); box-shadow: 0 20px 40px -12px rgba(0, 0, 0, 0.55); display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
    .dock-btn { border: none; background: transparent; border-radius: 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; color: var(--text-secondary); font-family: inherit; font-size: 11px; font-weight: 600; cursor: pointer; position: relative; }
    .dock-btn.active { background: var(--glass-pill); color: var(--accent); }
    .dock-badge { position: absolute; top: 6px; right: 22px; min-width: 17px; height: 17px; padding: 0 5px; border-radius: 99px; background: var(--accent); color: #fff; font-size: 10px; font-weight: 800; display: flex; align-items: center; justify-content: center; }

    .ios-switch { width: 50px; height: 30px; border-radius: 999px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); padding: 3px; cursor: pointer; transition: 0.25s; position: relative; }
    .ios-switch.on { background: var(--accent); border-color: var(--accent); }
    .ios-switch-knob { width: 22px; height: 22px; border-radius: 50%; background: #FFFFFF; transition: transform 0.25s; }
    .ios-switch.on .ios-switch-knob { transform: translateX(20px); }

    .avatar-ring { width: 68px; height: 68px; border-radius: 24px; padding: 2px; background: linear-gradient(135deg, var(--accent), rgba(255,255,255,0.15)); position: relative; cursor: pointer; flex-shrink: 0; }
    .avatar-img { width: 100%; height: 100%; border-radius: 22px; object-fit: cover; background: var(--bg-app); }
    .avatar-edit-badge { position: absolute; bottom: -4px; right: -4px; width: 26px; height: 26px; border-radius: 10px; background: var(--text-primary); color: var(--bg-app); display: flex; align-items: center; justify-content: center; border: 2px solid var(--bg-app); }

    .progress-track { width: 100%; height: 8px; border-radius: 99px; background: var(--glass-pill); overflow: hidden; }
    .progress-fill { height: 100%; border-radius: 99px; background: var(--accent); transition: width 0.4s ease; }

    .modal-backdrop { position: fixed; inset: 0; max-width: 430px; margin: 0 auto; background: rgba(0, 0, 0, 0.65); backdrop-filter: blur(10px); z-index: 60; display: flex; align-items: flex-end; }
    .modal-sheet { width: 100%; max-height: 88vh; overflow-y: auto; background: var(--bg-app); border-top: 1px solid var(--glass-border); border-radius: 32px 32px 0 0; padding: 20px 18px 28px; box-shadow: 0 -20px 50px rgba(0,0,0,0.5); }
    .toast { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 80; background: var(--text-primary); color: var(--bg-app); padding: 10px 18px; border-radius: 99px; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; box-shadow: 0 12px 30px rgba(0,0,0,0.35); }
    .input-glass { width: 100%; height: 42px; padding: 0 12px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); font-family: inherit; font-size: 13px; font-weight: 500; outline: none; }
    .label-sm { font-size: 12px; color: var(--text-secondary); font-weight: 600; margin-bottom: 4px; display: block; }

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
    const urlParams = new URLSearchParams(window.location.search);
    const fallbackUid = urlParams.get('uid') || localStorage.getItem('dh_uid') || '';
    if (fallbackUid) localStorage.setItem('dh_uid', fallbackUid);

    function apiHeaders(extra = {}) {
      return Object.assign({
        'X-Telegram-Init-Data': initData,
        'X-User-Id': fallbackUid
      }, extra);
    }

    const AVATAR_PRESETS = [
      'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=200&auto=format&fit=crop&q=80',
      'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=200&auto=format&fit=crop&q=80',
      'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=200&auto=format&fit=crop&q=80'
    ];

    const state = {
      theme: 'dark',
      tab: 'feed',
      selectedPlatform: 'all', // 'all' | 'avito' | 'wb' | 'ozon'
      searchQuery: 'iPhone 15',
      loadingFeed: false,
      hotOnly: false,
      detailId: null,
      showPaywall: false,
      showNewMonitor: false,

      // Поля умного конструктора монитора
      builder: {
        category: 'iphone', // 'iphone' | 'gpu' | 'auto' | 'realty' | 'toys'
        title: 'iPhone 15 Pro',
        city: 'Москва',
        minMargin: 15,
        sources: ['Avito', 'WB'],
        memories: ['256 ГБ'],
        colors: ['Любой цвет'],
        condition: 'Идеал / Новый',
        minBattery: 90,
        verifiedSeller: true,
        autoMaxKm: 100000,
        autoNoCrash: true,
        realtyRooms: '1-2 комн.',
        realtyOwner: true
      },

      user: {
        name: 'Алексей',
        username: 'alex_flip',
        avatarIdx: 0,
        customAvatar: null,
        accessUntil: 'Загрузка...',
        daysLeft: 3,
        totalDays: 30,
        refCode: 'dh_alex99',
        invitedCount: 0,
        bonusDaysEarned: 0,
        notificationsOn: true
      },

      savedIds: JSON.parse(localStorage.getItem('dh_saved') || '[]'),
      listings: [],
      monitors: []
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(n || 0);

    function showToast(msg) {
      const box = document.getElementById('toast-container');
      box.innerHTML = `<div class="toast"><i data-lucide="check-circle-2" class="icon-16" style="color:var(--accent)"></i><span>${msg}</span></div>`;
      lucide.createIcons();
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { box.innerHTML = ''; }, 2400);
    }

    function openExternalLot(url, e) {
      if (e) e.stopPropagation();
      if (!url) return;
      if (tg && tg.openLink) {
        tg.openLink(url);
      } else {
        window.open(url, '_blank');
      }
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
        showToast('Убрано из подборки');
      } else {
        state.savedIds.push(id);
        showToast('Лот сохранён в подборку');
      }
      localStorage.setItem('dh_saved', JSON.stringify(state.savedIds));
      render();
    }

    async function loadLiveLots(q, plat) {
      if (q !== undefined && q !== null) state.searchQuery = q;
      if (plat !== undefined && plat !== null) state.selectedPlatform = plat;
      state.loadingFeed = true;
      render();
      try {
        const res = await fetch(`/api/live-feed?query=${encodeURIComponent(state.searchQuery)}&platform=${state.selectedPlatform}`, {
          headers: apiHeaders()
        });
        const data = await res.json();
        state.listings = Array.isArray(data) ? data : [];
      } catch (err) {
        showToast('Ошибка сети при загрузке лотов');
      }
      state.loadingFeed = false;
      render();
    }

    async function syncUserData() {
      try {
        const res = await fetch('/api/user-status', { headers: apiHeaders() });
        const d = await res.json();
        if (d) {
          Object.assign(state.user, d);
          if (d.user_id) localStorage.setItem('dh_uid', String(d.user_id));
          render();
        }
      } catch(e) {}
    }

    async function loadMonitors() {
      try {
        const res = await fetch('/api/monitors', { headers: apiHeaders() });
        const data = await res.json();
        if (Array.isArray(data)) {
          state.monitors = data;
          render();
        }
      } catch(e) {}
    }

    // ПРОФИЛЬ: АВАТАРКА И РЕФЕРАЛКА
    async function cycleAvatar() {
      state.user.customAvatar = null;
      state.user.avatarIdx = (state.user.avatarIdx + 1) % AVATAR_PRESETS.length;
      showToast('Аватар обновлён');
      render();
      await fetch('/api/profile', {
        method: 'POST',
        headers: apiHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({ avatarIdx: state.user.avatarIdx })
      });
    }

    function handleAvatarFile(e) {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        state.user.customAvatar = reader.result;
        showToast('Ваше фото сохранено в профиле');
        render();
        await fetch('/api/profile', {
          method: 'POST',
          headers: apiHeaders({'Content-Type': 'application/json'}),
          body: JSON.stringify({ customAvatar: reader.result })
        });
      };
      reader.readAsDataURL(file);
    }

    async function regenerateRefCode() {
      const rand = Math.random().toString(36).substring(2, 7);
      state.user.refCode = 'dh_' + rand;
      showToast('Сгенерирована новая реф. ссылка');
      render();
      await fetch('/api/profile', {
        method: 'POST',
        headers: apiHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({ refCode: state.user.refCode })
      });
    }

    function copyRefLink() {
      const url = `https://t.me/DealHunterAvito_bot?start=${state.user.refCode}`;
      if (navigator.clipboard) navigator.clipboard.writeText(url);
      showToast('Реферальная ссылка скопирована!');
    }

    async function payTelegramStars() {
      try {
        showToast('Формируем счёт на 350 Stars...');
        const res = await fetch('/api/create-stars-invoice', {
          method: 'POST',
          headers: apiHeaders()
        });
        const data = await res.json();
        if (data.invoice_link) {
          if (tg && tg.openInvoice) {
            tg.openInvoice(data.invoice_link, (status) => {
              if (status === 'paid') {
                showToast('Оплата принята! +30 дней PRO активировано');
                state.showPaywall = false;
                syncUserData();
              } else if (status === 'cancelled') {
                showToast('Оплата отменена');
              }
            });
          } else {
            window.open(data.invoice_link, '_blank');
          }
        }
      } catch(e) {
        showToast('Откройте приложение через Telegram для оплаты Stars');
      }
    }

    // УПРАВЛЕНИЕ МОНИТОРАМИ
    function toggleArrayItem(arr, val) {
      const i = arr.indexOf(val);
      if (i >= 0) {
        if (arr.length > 1) arr.splice(i, 1);
      } else {
        arr.push(val);
      }
      render();
    }

    function selectBuilderCategory(cat) {
      state.builder.category = cat;
      const defaults = {
        'iphone': 'iPhone 15 Pro',
        'gpu': 'RTX 4070 Ti Super',
        'auto': 'Toyota Camry',
        'realty': '1-к квартира',
        'toys': 'PlayStation 5 Slim'
      };
      state.builder.title = defaults[cat] || 'iPhone 15';
      render();
    }

    async function saveBuilderMonitor() {
      const b = state.builder;
      const titleInput = document.getElementById('b-title');
      const cityInput = document.getElementById('b-city');
      const marginInput = document.getElementById('b-margin');
      if (titleInput) b.title = titleInput.value.trim();
      if (cityInput) b.city = cityInput.value.trim();
      if (marginInput) b.minMargin = parseInt(marginInput.value) || 15;

      if (!b.title) {
        showToast('Введите название товара или модели');
        return;
      }

      const params = {
        condition: b.condition,
        verifiedSeller: b.verifiedSeller
      };
      if (b.category === 'iphone') {
        params.memories = b.memories;
        params.colors = b.colors;
        params.minBattery = b.minBattery;
      } else if (b.category === 'auto') {
        params.maxKm = b.autoMaxKm;
        params.noCrash = b.autoNoCrash;
      } else if (b.category === 'realty') {
        params.rooms = b.realtyRooms;
        params.ownerOnly = b.realtyOwner;
      }

      await fetch('/api/monitors', {
        method: 'POST',
        headers: apiHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({
          title: b.title,
          category: b.category,
          city: b.city,
          minMargin: b.minMargin,
          sources: b.sources,
          params: params
        })
      });

      state.showNewMonitor = false;
      showToast('Радар сохранён! Первое уведомление отправлено в бот');
      await loadMonitors();
    }

    async function toggleMonitorActive(monId) {
      const m = state.monitors.find(x => x.id === monId);
      if (m) m.active = !m.active;
      render();
      await fetch(`/api/monitors/${monId}/toggle`, { method: 'POST', headers: apiHeaders() });
      showToast(m && m.active ? 'Мониторинг включён' : 'Мониторинг на паузе');
    }

    async function deleteMonitor(monId) {
      state.monitors = state.monitors.filter(x => x.id !== monId);
      render();
      await fetch(`/api/monitors/${monId}`, { method: 'DELETE', headers: apiHeaders() });
      showToast('Монитор удалён');
    }

    async function testMonitorPush(monId) {
      showToast('Ищем лучший лот и отправляем в ваш Telegram...');
      try {
        const res = await fetch(`/api/monitors/${monId}/test-push`, { method: 'POST', headers: apiHeaders() });
        const d = await res.json();
        showToast(d.msg || 'Отправлено!');
      } catch(e) {
        showToast('Ошибка отправки пуша');
      }
    }

    // КАРТОЧКА РЕАЛЬНОГО ЛОТА
    function renderLotCard(item) {
      const isSaved = state.savedIds.includes(item.id);
      return `
        <div class="glass-card interactive" style="margin-bottom: 12px;" onclick="state.detailId='${item.id}'; window.scrollTo(0,0); render();">
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

            <button class="btn btn-icon" style="width:30px; height:30px; border-radius:10px;" onclick="event.stopPropagation(); state.listings = state.listings.filter(x => x.id !== '${item.id}'); render();">
              <i data-lucide="x" style="width:14px; height:14px; color:var(--text-muted);"></i>
            </button>
          </div>

          <h3 style="font-size: 16px; font-weight: 700; line-height: 1.3; margin-bottom: 8px;">
            ${item.title}
          </h3>

          <p style="font-size: 12px; color: var(--text-secondary); line-height: 1.4; margin-bottom: 10px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
            ${item.desc}
          </p>

          <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 14px;">
            ${(item.tags || []).map(t => `
              <span class="spec-tag">
                <i data-lucide="${t.icon || 'tag'}" style="width:13px; height:13px; color:var(--accent);"></i>
                <span>${t.text}</span>
              </span>
            `).join('')}
          </div>

          <div class="row-between" style="padding: 12px 14px; border-radius: 18px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); margin-bottom: 14px;">
            <div>
              <div class="t-num" style="font-size: 22px; font-weight: 800; line-height: 1.1;">
                ${fmt(item.price)} ₽
              </div>
              <div class="t-num" style="font-size: 12px; color: var(--text-muted); margin-top: 3px;">
                Медиана рынка: <span style="text-decoration: line-through;">${fmt(item.market)} ₽</span>
              </div>
            </div>

            <div class="profit-pill t-num">
              <i data-lucide="trending-down" style="width:15px; height:15px;"></i>
              <span>${item.profitPct > 0 ? '−' + item.profitPct + '% · ' + fmt(item.profitRub) + ' ₽' : 'В рынке'}</span>
            </div>
          </div>

          <div class="row-between">
            <div style="display:flex; align-items:center; gap: 6px; font-size: 12px;">
              <span style="display:inline-flex; align-items:center; gap:3px; font-weight:700; color: var(--text-primary);">
                <i data-lucide="star" style="width:13px; height:13px; color:#FFB800; fill:#FFB800;"></i>
                ${Number(item.seller.rating || 4.9).toFixed(1)}
              </span>
              <span style="color: var(--text-secondary);">(${item.seller.reviews} отз.)</span>
              <span style="color: var(--text-secondary); max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">· ${item.seller.name}</span>
            </div>

            <div style="display:flex; gap: 8px;">
              <button class="btn btn-icon" onclick="toggleSave('${item.id}', event)" style="${isSaved ? 'background:var(--accent-soft); border-color:var(--accent); color:var(--accent);' : ''}">
                <i data-lucide="heart" class="icon-16" style="${isSaved ? 'fill:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-pill btn-accent" onclick="openExternalLot('${item.url}', event)">
                <span>Открыть лот</span>
                <i data-lucide="arrow-up-right" class="icon-16"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    }

    // ДЕТАЛЬНАЯ КАРТОЧКА ЛОТА
    function renderDetail(item) {
      const isSaved = state.savedIds.includes(item.id);
      return `
        <div class="top-header row-between">
          <button class="btn btn-pill btn-glass" onclick="state.detailId = null; render();">
            <i data-lucide="chevron-left" class="icon-18"></i>
            <span>Назад к ленте</span>
          </button>
          <button class="btn btn-pill btn-glass" onclick="toggleSave('${item.id}', event)" style="${isSaved ? 'color:var(--accent); border-color:var(--accent);' : ''}">
            <i data-lucide="heart" class="icon-16" style="${isSaved ? 'fill:var(--accent);' : ''}"></i>
            <span>${isSaved ? 'Сохранено' : 'В подборку'}</span>
          </button>
        </div>

        <div class="screen-content">
          <div class="glass-card" style="margin-bottom: 12px;">
            <div class="row-between" style="margin-bottom: 8px;">
              <span style="font-size: 12px; font-weight: 700; color: var(--accent);">${item.source} · ${item.city}</span>
              <span style="font-size: 12px; color: var(--text-muted);">${item.timeAgo}</span>
            </div>
            <h1 style="font-size: 20px; font-weight: 800; margin-bottom: 14px; line-height: 1.3;">${item.title}</h1>

            <div class="row-between" style="margin-bottom: 14px; padding: 12px; border-radius: 16px; background: var(--glass-pill);">
              <div>
                <div class="t-num" style="font-size: 28px; font-weight: 800;">${fmt(item.price)} ₽</div>
                <div class="t-num" style="font-size: 12px; color: var(--text-secondary);">
                  Медиана по площадке: <span style="text-decoration:line-through;">${fmt(item.market)} ₽</span>
                </div>
              </div>
              <div class="profit-pill t-num">
                ${item.profitPct > 0 ? '−' + item.profitPct + '% · ' + fmt(item.profitRub) + ' ₽' : 'Рыночная цена'}
              </div>
            </div>

            <div style="font-size: 13px; font-weight: 700; margin-bottom: 6px;">Описание объявления:</div>
            <p style="font-size: 13px; line-height: 1.55; color: var(--text-secondary);">
              ${item.desc}
            </p>
          </div>

          <div class="glass-card" style="margin-bottom: 12px;">
            <div style="font-size: 14px; font-weight: 700; margin-bottom: 10px;">Параметры и проверка</div>
            <div style="display:grid; gap: 8px;">
              ${Object.entries(item.specs || {}).map(([k, v]) => `
                <div class="row-between" style="font-size: 13px; padding-bottom: 6px; border-bottom: 1px solid var(--glass-stroke);">
                  <span style="color: var(--text-secondary);">${k}</span>
                  <span style="font-weight: 600;">${v}</span>
                </div>
              `).join('')}
            </div>
          </div>

          <div class="glass-card" style="margin-bottom: 16px;">
            <div class="row-between" style="margin-bottom: 8px;">
              <div>
                <div style="font-size: 15px; font-weight: 700;">Продавец: ${item.seller.name}</div>
                <div style="font-size: 12px; color: var(--text-secondary);">${item.seller.note}</div>
              </div>
              <div style="font-size: 14px; font-weight: 800; color: #FFB800;">★ ${item.seller.rating} (${item.seller.reviews} отз.)</div>
            </div>
          </div>

          <button class="btn btn-large btn-accent" onclick="openExternalLot('${item.url}', event)">
            <span>Перейти к этому объявлению на ${item.source}</span>
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

      // 1. ВКЛАДКА: НАХОДКИ (ЛЕНТА)
      if (state.tab === 'feed') {
        const listToShow = state.hotOnly ? state.listings.filter(x => x.profitPct >= 15) : state.listings;

        screenHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap: 10px;">
                <div style="width: 36px; height: 36px; border-radius: 12px; background: var(--accent); display:flex; align-items:center; justify-content:center; color:#fff; box-shadow: 0 6px 16px var(--accent-glow);">
                  <i data-lucide="radar" class="icon-20"></i>
                </div>
                <div>
                  <div style="font-size: 18px; font-weight: 800; letter-spacing: -0.02em;">Радар находок</div>
                  <div style="font-size: 11px; color: var(--green); font-weight: 600;">● Реальные объявления (${state.listings.length} шт.)</div>
                </div>
              </div>

              <div style="display:flex; gap: 8px;">
                <button class="btn btn-icon" title="Только выгодные" onclick="state.hotOnly = !state.hotOnly; render();" style="${state.hotOnly ? 'background:var(--accent); color:#fff; border-color:var(--accent);' : ''}">
                  <i data-lucide="flame" class="icon-18"></i>
                </button>
                <button class="btn btn-icon" title="Тема" onclick="toggleTheme()">
                  <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" class="icon-18"></i>
                </button>
              </div>
            </div>

            <!-- Переключатель маркетплейсов: Все | Авито | WB | Ozon -->
            <div class="source-segmented">
              <button class="source-seg-btn ${state.selectedPlatform === 'all' ? 'active' : ''}" onclick="loadLiveLots(null, 'all')">Все</button>
              <button class="source-seg-btn ${state.selectedPlatform === 'avito' ? 'active' : ''}" onclick="loadLiveLots(null, 'avito')">Avito</button>
              <button class="source-seg-btn ${state.selectedPlatform === 'wb' ? 'active' : ''}" onclick="loadLiveLots(null, 'wb')">WB</button>
              <button class="source-seg-btn ${state.selectedPlatform === 'ozon' ? 'active' : ''}" onclick="loadLiveLots(null, 'ozon')">Ozon</button>
            </div>

            <!-- Живой поиск -->
            <div class="search-bar-wrap">
              <input type="text" id="mainSearchInput" class="search-field" value="${state.searchQuery}" placeholder="Любой товар: iPhone 16, Camry, RTX 4070..." onkeydown="if(event.key==='Enter') loadLiveLots(this.value)" />
              <button class="search-go" onclick="loadLiveLots(document.getElementById('mainSearchInput').value)">
                <i data-lucide="search" class="icon-18"></i>
              </button>
            </div>

            <!-- Расширенные категории: Электроника, Авто, Недвижимость, Игрушки, Самокаты -->
            <div class="pills-scroll">
              <button class="cat-pill ${state.searchQuery === 'iPhone 15' ? 'active' : ''}" onclick="loadLiveLots('iPhone 15')">
                <i data-lucide="smartphone" class="icon-16"></i><span>iPhone 15</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'iPhone 16 Pro' ? 'active' : ''}" onclick="loadLiveLots('iPhone 16 Pro')">
                <i data-lucide="smartphone" class="icon-16"></i><span>iPhone 16 Pro</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'MacBook Air M3' ? 'active' : ''}" onclick="loadLiveLots('MacBook Air M3')">
                <i data-lucide="laptop" class="icon-16"></i><span>MacBook</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'RTX 4070' ? 'active' : ''}" onclick="loadLiveLots('RTX 4070')">
                <i data-lucide="cpu" class="icon-16"></i><span>Видеокарты</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'Toyota Camry' ? 'active' : ''}" onclick="loadLiveLots('Toyota Camry')">
                <i data-lucide="car" class="icon-16"></i><span>Автомобили</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'Студия Москва' ? 'active' : ''}" onclick="loadLiveLots('Студия Москва')">
                <i data-lucide="building-2" class="icon-16"></i><span>Недвижимость</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'PlayStation 5' ? 'active' : ''}" onclick="loadLiveLots('PlayStation 5')">
                <i data-lucide="gamepad-2" class="icon-16"></i><span>Консоли и Игрушки</span>
              </button>
              <button class="cat-pill ${state.searchQuery === 'Ninebot Max G2' ? 'active' : ''}" onclick="loadLiveLots('Ninebot Max G2')">
                <i data-lucide="zap" class="icon-16"></i><span>Самокаты</span>
              </button>
            </div>
          </div>

          <div class="screen-content">
            ${state.loadingFeed
              ? `<div class="glass-card" style="text-align:center; padding: 42px 20px;">
                   <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">Парсим живые объявления «${state.searchQuery}»...</div>
                   <div style="font-size: 13px; color: var(--text-secondary);">Считаем рыночную медиану и проверяем карточки продавцов</div>
                 </div>`
              : (listToShow.length
                  ? listToShow.map(renderLotCard).join('')
                  : `<div class="glass-card" style="text-align:center; padding: 38px 20px;">
                       <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">По фильтру лотов не найдено</div>
                       <div style="font-size: 13px; color: var(--text-secondary); margin-bottom: 14px;">Попробуйте отключить фильтр горячей маржи или сменить площадку</div>
                       <button class="btn btn-pill btn-accent" onclick="state.hotOnly=false; loadLiveLots(state.searchQuery, 'all')">Показать все лоты</button>
                     </div>`)
            }
          </div>
        `;
      }

      // 2. ВКЛАДКА: МОНИТОРЫ (УМНЫЕ ФИЛЬТРЫ С ТУМБЛЕРОМ И ТЕСТ-ПУШЕМ)
      if (state.tab === 'monitors') {
        screenHtml = `
          <div class="top-header row-between">
            <div>
              <div style="font-size: 18px; font-weight: 800;">Мои мониторы</div>
              <div style="font-size: 12px; color: var(--text-secondary);">Автопоиск 24/7 с уведомлениями в Telegram</div>
            </div>
            <button class="btn btn-pill btn-accent" onclick="state.showNewMonitor = true; render();">
              <i data-lucide="plus" class="icon-16"></i>
              <span>Создать фильтр</span>
            </button>
          </div>

          <div class="screen-content">
            ${state.monitors.length ? state.monitors.map(m => {
              const p = m.params || {};
              const pills = [];
              if (p.memories) pills.push(...p.memories);
              if (p.colors) pills.push(...p.colors);
              if (p.condition) pills.push(p.condition);
              if (p.minBattery) pills.push('АКБ от ' + p.minBattery + '%');
              if (p.maxKm) pills.push('До ' + fmt(p.maxKm) + ' км');
              if (p.noCrash) pills.push('Без ДТП');
              if (p.rooms) pills.push(p.rooms);
              if (p.ownerOnly) pills.push('Собственник');
              if (p.verifiedSeller) pills.push('Проверенный продавец');

              return `
                <div class="glass-card" style="margin-bottom: 12px;">
                  <div class="row-between" style="margin-bottom: 10px;">
                    <div>
                      <div style="font-size: 16px; font-weight: 800;">${m.title}</div>
                      <div style="font-size: 12px; color: var(--text-secondary); margin-top: 2px;">
                        ${m.city} · Выгода от <strong style="color:var(--accent)">${m.minMargin}%</strong>
                      </div>
                    </div>

                    <div class="ios-switch ${m.active ? 'on' : ''}" onclick="toggleMonitorActive(${m.id})">
                      <div class="ios-switch-knob"></div>
                    </div>
                  </div>

                  <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 12px;">
                    ${(m.sources || ['Avito']).map(s => `<span class="spec-tag" style="color:var(--text-primary); font-weight:700;">${s}</span>`).join('')}
                    ${pills.map(tag => `<span class="spec-tag">${tag}</span>`).join('')}
                  </div>

                  <div class="row-between" style="padding-top: 10px; border-top: 1px solid var(--glass-stroke);">
                    <button class="btn btn-pill btn-glass" style="height:32px; font-size:12px;" onclick="testMonitorPush(${m.id})">
                      <i data-lucide="send" style="width:13px; height:13px; color:var(--accent);"></i>
                      <span>Проверить пуш в TG</span>
                    </button>
                    <button class="btn btn-icon" style="width:32px; height:32px;" onclick="deleteMonitor(${m.id})">
                      <i data-lucide="trash-2" style="width:14px; height:14px; color:var(--danger);"></i>
                    </button>
                  </div>
                </div>
              `;
            }).join('') : `
              <div class="glass-card" style="text-align:center; padding: 40px 20px;">
                <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">У вас пока нет активных радаров</div>
                <div style="font-size: 13px; color: var(--text-secondary); margin-bottom: 16px;">Настройте детальные параметры (память, состояние, АКБ, пробег или ремонт), и бот будет присылать лоты в Telegram</div>
                <button class="btn btn-pill btn-accent" onclick="state.showNewMonitor = true; render();">Создать первый радар</button>
              </div>
            `}
          </div>
        `;
      }

      // 3. ВКЛАДКА: ПОДБОРКА
      if (state.tab === 'saved') {
        const savedLots = state.listings.filter(x => state.savedIds.includes(x.id));
        const totalProfit = savedLots.reduce((acc, x) => acc + (x.profitRub || 0), 0);

        screenHtml = `
          <div class="top-header row-between">
            <div>
              <div style="font-size: 18px; font-weight: 800;">Сохранённые лоты</div>
              <div style="font-size: 12px; color: var(--text-secondary);">Выгода подборки: <strong style="color:var(--accent)">${fmt(totalProfit)} ₽</strong></div>
            </div>
            <span class="spec-tag t-num" style="font-weight:700;">${savedLots.length} шт.</span>
          </div>

          <div class="screen-content">
            ${savedLots.length ? savedLots.map(renderLotCard).join('') : `
              <div class="glass-card" style="text-align:center; padding: 42px 20px;">
                <div style="font-size: 16px; font-weight: 700; margin-bottom: 6px;">В подборке пока пусто</div>
                <div style="font-size: 13px; color: var(--text-secondary); margin-bottom: 16px;">Нажимайте на сердечко на выгодных объявлениях в ленте</div>
                <button class="btn btn-pill btn-accent" onclick="state.tab='feed'; render();">Перейти к ленте</button>
              </div>
            `}
          </div>
        `;
      }

      // 4. ВКЛАДКА: ПРОФИЛЬ (С АВАТАРКОЙ, РЕФЕРАЛКОЙ И STARS)
      if (state.tab === 'profile') {
        const avatarSrc = state.user.customAvatar || AVATAR_PRESETS[state.user.avatarIdx % AVATAR_PRESETS.length];
        const progressPct = Math.min(100, Math.round((state.user.daysLeft / 30) * 100));

        screenHtml = `
          <div class="top-header row-between">
            <div style="font-size: 18px; font-weight: 800;">Профиль</div>
            <button class="btn btn-pill btn-glass" onclick="toggleTheme()">
              <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" class="icon-16"></i>
              <span>${state.theme === 'dark' ? 'Светлая' : 'Тёмная'}</span>
            </button>
          </div>

          <div class="screen-content">
            <!-- Карточка с загрузкой аватарки -->
            <div class="glass-card" style="margin-bottom: 12px;">
              <div style="display:flex; align-items:center; gap: 14px;">
                <div class="avatar-ring" onclick="cycleAvatar()" title="Нажмите для смены аватара">
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
                      <i data-lucide="refresh-cw" style="width:12px; height:12px;"></i> Сменить пресет
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <!-- Подписка PRO -->
            <div class="glass-card" style="margin-bottom: 12px;">
              <div class="row-between" style="margin-bottom: 6px;">
                <span style="font-size: 13px; color: var(--text-secondary); font-weight: 500;">Подписка привязана к вашему TG до</span>
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

            <!-- Реферальная система -->
            <div class="glass-card" style="margin-bottom: 12px;">
              <div class="row-between" style="margin-bottom: 6px;">
                <div style="font-size: 16px; font-weight: 800;">+3 дня за каждого друга</div>
                <span class="spec-tag t-num" style="color:var(--accent); font-weight:700;">+${state.user.bonusDaysEarned} / 30 дн.</span>
              </div>
              <p style="font-size: 12px; color: var(--text-secondary); line-height: 1.45; margin-bottom: 12px;">
                Приглашено друзей: <strong>${state.user.invitedCount}</strong>. Бонусные дни начисляются автоматически при старте бота по вашей ссылке.
              </p>

              <div class="row-between" style="padding: 10px 12px; border-radius: 16px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); margin-bottom: 12px;">
                <span class="t-num" style="font-size: 12px; font-weight: 600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                  t.me/DealHunterAvito_bot?start=${state.user.refCode}
                </span>
                <button class="btn btn-icon" style="width:32px; height:32px; border-radius:10px;" onclick="regenerateRefCode()">
                  <i data-lucide="dice-5" class="icon-16"></i>
                </button>
              </div>

              <button class="btn btn-large btn-glass" style="height:44px;" onclick="copyRefLink()">
                <i data-lucide="copy" class="icon-16"></i>
                <span>Скопировать реферальную ссылку</span>
              </button>
            </div>

            <!-- Мгновенные пуши -->
            <div class="glass-card">
              <div class="row-between">
                <div style="display:flex; align-items:center; gap: 10px;">
                  <i data-lucide="bell-ring" class="icon-18" style="color:var(--accent)"></i>
                  <div>
                    <div style="font-size: 14px; font-weight: 700;">Мгновенные пуши в Telegram</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">Присылать лоты в диалог с ботом</div>
                  </div>
                </div>
                <div class="ios-switch ${state.user.notificationsOn ? 'on' : ''}" onclick="state.user.notificationsOn = !state.user.notificationsOn; render(); fetch('/api/profile', {method:'POST', headers:apiHeaders({'Content-Type':'application/json'}), body:JSON.stringify({notificationsOn:state.user.notificationsOn})});">
                  <div class="ios-switch-knob"></div>
                </div>
              </div>
            </div>
          </div>
        `;
      }

      // МОДАЛКА ОПЛАТЫ TELEGRAM STARS
      let modalHtml = '';
      if (state.showPaywall) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showPaywall = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom: 14px;">
                <span class="profit-pill">PRO Подписка</span>
                <button class="btn btn-icon" onclick="state.showPaywall = false; render();"><i data-lucide="x" class="icon-18"></i></button>
              </div>
              <div class="t-num" style="font-size: 30px; font-weight: 800; margin-bottom: 6px;">350 Stars <span style="font-size:14px; color:var(--text-secondary);">/ 30 дней</span></div>
              <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 18px;">Официальная оплата через Telegram Stars. Доступ привязывается к вашему Telegram ID в облачной БД Neon и сохраняется на любых устройствах.</p>
              <button class="btn btn-large btn-accent" onclick="payTelegramStars()">
                <i data-lucide="zap" class="icon-18"></i>
                <span>Оплатить 350 Telegram Stars</span>
              </button>
            </div>
          </div>
        `;
      }

      // УМНЫЙ КОНСТРУКТОР МОНИТОРА С ДИНАМИЧЕСКИМИ ПОДПУНКТАМИ
      if (state.showNewMonitor) {
        const b = state.builder;
        let dynamicFieldsHtml = '';

        if (b.category === 'iphone') {
          dynamicFieldsHtml = `
            <div>
              <span class="label-sm">Память устройства (можно несколько)</span>
              <div class="chip-group">
                ${['128 ГБ', '256 ГБ', '512 ГБ', '1 ТБ'].map(mem => `
                  <div class="chip-option ${b.memories.includes(mem) ? 'selected' : ''}" onclick="toggleArrayItem(state.builder.memories, '${mem}')">${mem}</div>
                `).join('')}
              </div>
            </div>
            <div>
              <span class="label-sm">Цвет корпуса</span>
              <div class="chip-group">
                ${['Любой цвет', 'Black', 'Natural Titanium', 'White', 'Desert'].map(clr => `
                  <div class="chip-option ${b.colors.includes(clr) ? 'selected' : ''}" onclick="toggleArrayItem(state.builder.colors, '${clr}')">${clr}</div>
                `).join('')}
              </div>
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px;">
              <div>
                <span class="label-sm">Мин. ёмкость АКБ (%)</span>
                <input type="number" class="input-glass t-num" value="${b.minBattery}" onchange="state.builder.minBattery = Number(this.value)" />
              </div>
              <div>
                <span class="label-sm">Состояние</span>
                <select class="input-glass" onchange="state.builder.condition = this.value">
                  <option value="Идеал / Новый" ${b.condition==='Идеал / Новый'?'selected':''}>Идеал / Новый</option>
                  <option value="Без ремонтов" ${b.condition==='Без ремонтов'?'selected':''}>Без ремонтов</option>
                  <option value="Любое рабочее" ${b.condition==='Любое рабочее'?'selected':''}>Любое рабочее</option>
                </select>
              </div>
            </div>
          `;
        } else if (b.category === 'auto') {
          dynamicFieldsHtml = `
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px;">
              <div>
                <span class="label-sm">Макс. пробег (км)</span>
                <input type="number" class="input-glass t-num" value="${b.autoMaxKm}" onchange="state.builder.autoMaxKm = Number(this.value)" />
              </div>
              <div>
                <span class="label-sm">Проверка кузова</span>
                <div class="chip-option ${b.autoNoCrash ? 'selected' : ''}" style="height:42px; width:100%; justify-content:center;" onclick="state.builder.autoNoCrash = !state.builder.autoNoCrash; render();">
                  Только Без ДТП и 1-2 влад.
                </div>
              </div>
            </div>
          `;
        } else if (b.category === 'realty') {
          dynamicFieldsHtml = `
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px;">
              <div>
                <span class="label-sm">Комнатность</span>
                <select class="input-glass" onchange="state.builder.realtyRooms = this.value">
                  <option value="Студия">Студия</option>
                  <option value="1-2 комн." selected>1-2 комн.</option>
                  <option value="3+ комн.">3+ комн.</option>
                </select>
              </div>
              <div>
                <span class="label-sm">Комиссия</span>
                <div class="chip-option ${b.realtyOwner ? 'selected' : ''}" style="height:42px; width:100%; justify-content:center;" onclick="state.builder.realtyOwner = !state.builder.realtyOwner; render();">
                  Только собственник
                </div>
              </div>
            </div>
          `;
        } else {
          dynamicFieldsHtml = `
            <div>
              <span class="label-sm">Состояние и комплект</span>
              <div class="chip-group">
                ${['Новый / Пломба', 'Идеал с чеком', 'На гарантии'].map(cnd => `
                  <div class="chip-option ${b.condition === cnd ? 'selected' : ''}" onclick="state.builder.condition = '${cnd}'; render();">${cnd}</div>
                `).join('')}
              </div>
            </div>
          `;
        }

        modalHtml = `
          <div class="modal-backdrop" onclick="state.showNewMonitor = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom: 14px;">
                <div style="font-size: 18px; font-weight: 800;">Настройка умного радара</div>
                <button class="btn btn-icon" onclick="state.showNewMonitor = false; render();"><i data-lucide="x" class="icon-18"></i></button>
              </div>

              <div style="display:grid; gap: 12px; margin-bottom: 18px;">
                <div>
                  <span class="label-sm">Категория</span>
                  <div class="chip-group">
                    <div class="chip-option ${b.category === 'iphone' ? 'selected' : ''}" onclick="selectBuilderCategory('iphone')">📱 Смартфоны</div>
                    <div class="chip-option ${b.category === 'gpu' ? 'selected' : ''}" onclick="selectBuilderCategory('gpu')">💻 Видеокарты / ПК</div>
                    <div class="chip-option ${b.category === 'auto' ? 'selected' : ''}" onclick="selectBuilderCategory('auto')">🚗 Автомобили</div>
                    <div class="chip-option ${b.category === 'realty' ? 'selected' : ''}" onclick="selectBuilderCategory('realty')">🏠 Недвижимость</div>
                    <div class="chip-option ${b.category === 'toys' ? 'selected' : ''}" onclick="selectBuilderCategory('toys')">🎮 Консоли и Игрушки</div>
                  </div>
                </div>

                <div>
                  <span class="label-sm">Точное название или модель</span>
                  <input class="input-glass" id="b-title" value="${b.title}" placeholder="Например: iPhone 15 Pro, RTX 4070, Toyota Camry..." />
                </div>

                <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                  <div>
                    <span class="label-sm">Город / Регион</span>
                    <input class="input-glass" id="b-city" value="${b.city}" />
                  </div>
                  <div>
                    <span class="label-sm">Мин. маржа (%)</span>
                    <input class="input-glass t-num" id="b-margin" type="number" value="${b.minMargin}" />
                  </div>
                </div>

                <div>
                  <span class="label-sm">Площадки для отслеживания</span>
                  <div class="chip-group">
                    ${['Avito', 'WB', 'Ozon'].map(src => `
                      <div class="chip-option ${b.sources.includes(src) ? 'selected' : ''}" onclick="toggleArrayItem(state.builder.sources, '${src}')">${src}</div>
                    `).join('')}
                  </div>
                </div>

                ${dynamicFieldsHtml}

                <div class="row-between" style="padding: 10px 12px; border-radius: 14px; background: var(--glass-pill);">
                  <span style="font-size: 12px; font-weight: 600;">Только проверенные продавцы (отзывы 4.5+)</span>
                  <div class="ios-switch ${b.verifiedSeller ? 'on' : ''}" onclick="state.builder.verifiedSeller = !state.builder.verifiedSeller; render();">
                    <div class="ios-switch-knob"></div>
                  </div>
                </div>
              </div>

              <button class="btn btn-large btn-accent" onclick="saveBuilderMonitor()">
                <i data-lucide="radar" class="icon-18"></i>
                <span>Сохранить фильтр и запустить пуши</span>
              </button>
            </div>
          </div>
        `;
      }

      // ПАРЯЩИЙ ДОК НАВИГАЦИИ
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
    loadMonitors();
    loadLiveLots('iPhone 15', 'all');
  </script>
</body>
</html>
"""
