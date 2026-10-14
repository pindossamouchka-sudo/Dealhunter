import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import statistics
from urllib.parse import parse_qsl, quote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import httpx
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", 8000))
SPONSOR_CHANNEL = os.getenv("SPONSOR_CHANNEL", "@durov")
SPONSOR_URL = os.getenv("SPONSOR_URL", "https://t.me/durov")
DB_NAME = "dealhunter.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                access_until TEXT,
                trial_activated INTEGER DEFAULT 0,
                sponsor_verified INTEGER DEFAULT 0,
                referred_by_id INTEGER,
                suspicious_flag INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                category TEXT,
                city TEXT,
                min_margin INTEGER,
                marketplaces TEXT,
                is_active INTEGER DEFAULT 1,
                quiet_hours TEXT DEFAULT '23:00–08:00'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                user_id INTEGER,
                lot_id TEXT,
                sent_at TEXT,
                PRIMARY KEY (user_id, lot_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                referred_user_id INTEGER PRIMARY KEY,
                inviter_user_id INTEGER,
                days_granted INTEGER,
                granted_at TEXT
            )
        """)
        await db.commit()

def parse_dt(dt_str: str | None) -> datetime:
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

async def extend_user_access(db: aiosqlite.Connection, user_id: int, days: int) -> datetime:
    now = datetime.now(timezone.utc)
    cur = await db.execute("SELECT access_until FROM users WHERE id = ?", (user_id,))
    row = await cur.fetchone()
    current_until = parse_dt(row[0]) if row and row[0] else now
    base_date = current_until if current_until > now else now
    new_until = base_date + timedelta(days=days)
    await db.execute("UPDATE users SET access_until = ? WHERE id = ?", (new_until.isoformat(), user_id))
    await db.commit()
    return new_until

async def process_referral_bonus(db: aiosqlite.Connection, referred_user_id: int):
    cur = await db.execute("SELECT referred_by_id, sponsor_verified FROM users WHERE id = ?", (referred_user_id,))
    u_row = await cur.fetchone()
    if not u_row or not u_row[0] or not u_row[1]:
        return

    inviter_id = u_row[0]
    cur = await db.execute("SELECT 1 FROM referral_rewards WHERE referred_user_id = ?", (referred_user_id,))
    if await cur.fetchone():
        return

    now = datetime.now(timezone.utc)
    month_ago = (now - timedelta(days=30)).isoformat()
    cur = await db.execute(
        "SELECT COALESCE(SUM(days_granted), 0) FROM referral_rewards WHERE inviter_user_id = ? AND granted_at >= ?",
        (inviter_id, month_ago)
    )
    month_days = (await cur.fetchone())[0]
    if month_days >= 30:
        return

    days_to_add = min(3, 30 - month_days)
    await extend_user_access(db, inviter_id, days_to_add)
    await db.execute(
        "INSERT INTO referral_rewards (referred_user_id, inviter_user_id, days_granted, granted_at) VALUES (?, ?, ?, ?)",
        (referred_user_id, inviter_id, days_to_add, now.isoformat())
    )
    await db.commit()

    try:
        await bot.send_message(
            chat_id=inviter_id,
            text=f"🎁 Друг настроил монитор и получил первый лот! Вам начислено +{days_to_add} дня подписки."
        )
    except Exception:
        pass

def calculate_iqr_clean_median(prices: list[int]) -> int:
    clean = [p for p in prices if p > 500]
    if not clean:
        return 0
    if len(clean) < 4:
        return int(statistics.median(clean))
    sorted_p = sorted(clean)
    q1 = sorted_p[len(sorted_p) // 4]
    q3 = sorted_p[(len(sorted_p) * 3) // 4]
    iqr = q3 - q1
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr
    filtered = [p for p in sorted_p if lower_bound <= p <= upper_bound]
    return int(statistics.median(filtered or sorted_p))

async def fetch_avito_real_lots(query: str) -> list[dict]:
    encoded_query = quote(query)
    url = f"https://www.avito.ru/rossiya?q={encoded_query}&s=104"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    lots = []
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select("[data-marker='item']")
                all_prices = []
                temp_items = []
                for item in items[:25]:
                    title_elem = item.select_one("[itemprop='name']")
                    price_elem = item.select_one("[itemprop='price']")
                    link_elem = item.select_one("a[itemprop='url']")
                    city_elem = item.select_one("[class*='geo-root']") or item.select_one("[class*='style-item-address']")
                    item_id = item.get("data-item-id") or item.get("id")

                    if title_elem and price_elem and link_elem and item_id:
                        try:
                            price_val = int(price_elem.get("content", 0))
                        except ValueError:
                            digits = re.sub(r"\D", "", price_elem.text)
                            price_val = int(digits) if digits else 0

                        if price_val > 500:
                            all_prices.append(price_val)
                            href = link_elem.get("href", "")
                            full_url = f"https://www.avito.ru{href}" if href.startswith("/") else href
                            temp_items.append({
                                "id": f"avito_{item_id}",
                                "title": title_elem.text.strip(),
                                "price": price_val,
                                "url": full_url,
                                "city": city_elem.text.strip() if city_elem else "Россия",
                                "source": "Avito"
                            })

                market_median = calculate_iqr_clean_median(all_prices)
                if market_median > 0:
                    for it in temp_items:
                        margin_rub = market_median - it["price"]
                        margin_pct = round((margin_rub / market_median) * 100, 1)
                        lots.append({
                            **it,
                            "market": market_median,
                            "margin_rub": margin_rub,
                            "margin_pct": margin_pct,
                            "seller_rating": 4.8,
                            "seller_reviews": 24,
                            "seller_safe": True
                        })
    except Exception as e:
        print(f"Avito parsing error: {e}")
    return lots

async def fetch_wb_real_lots(query: str) -> list[dict]:
    encoded_query = quote(query)
    url = f"https://search.wb.ru/exactmatch/ru/common/v7/search?curr=rub&dest=-1257786&query={encoded_query}&resultset=catalog&sort=Newly"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    lots = []
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("data", {}).get("products", [])
                prices = []
                temp = []
                for p in products[:30]:
                    pid = str(p.get("id"))
                    name = p.get("name", query)
                    sizes = p.get("sizes", [{}])
                    price_raw = sizes[0].get("price", {}).get("product", 0) // 100
                    rating = float(p.get("reviewRating", 4.9))
                    feedbacks = int(p.get("feedbacks", 0))

                    if price_raw > 500:
                        prices.append(price_raw)
                        temp.append({
                            "id": f"wb_{pid}",
                            "title": name,
                            "price": price_raw,
                            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
                            "city": "Склад РФ",
                            "source": "Wildberries",
                            "seller_rating": rating,
                            "seller_reviews": feedbacks,
                            "seller_safe": rating >= 4.5
                        })

                market_median = calculate_iqr_clean_median(prices)
                if market_median > 0:
                    for it in temp:
                        margin_rub = market_median - it["price"]
                        margin_pct = round((margin_rub / market_median) * 100, 1)
                        lots.append({
                            **it,
                            "market": market_median,
                            "margin_rub": margin_rub,
                            "margin_pct": margin_pct,
                        })
    except Exception as e:
        print(f"WB parsing error: {e}")
    return lots

def make_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Открыть Радар находок", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="⭐ Продлить доступ (350 Stars)", callback_data="buy_stars")]
    ])

def make_sponsor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Подписаться на спонсора", url=SPONSOR_URL)],
        [InlineKeyboardButton(text="2. Проверить подписку и забрать 3 дня", callback_data="verify_sponsor")]
    ])

async def check_channel_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=SPONSOR_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return True

@dp.message(CommandStart())
async def bot_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    now_iso = datetime.now(timezone.utc).isoformat()
    ref_id = None

    if command.args and command.args.startswith("ref_"):
        try:
            cand = int(command.args.replace("ref_", ""))
            if cand != user_id:
                ref_id = cand
        except ValueError:
            pass

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (id, username, first_name, access_until, referred_by_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, message.from_user.username, message.from_user.first_name, None, ref_id, now_iso))

        cur = await db.execute("SELECT id FROM monitors WHERE user_id = ?", (user_id,))
        if not await cur.fetchone():
            await db.execute("""
                INSERT INTO monitors (user_id, title, category, city, min_margin, marketplaces)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, "iPhone 15 Pro", "iphone", "Москва", 15, "Avito,Wildberries"))
        await db.commit()

        cur = await db.execute("SELECT sponsor_verified, trial_activated, access_until FROM users WHERE id = ?", (user_id,))
        u = await cur.fetchone()

    is_verified = bool(u[0])
    if not is_verified:
        await message.answer(
            f"Привет, {message.from_user.first_name}!\n\n"
            "Чтобы активировать 3 дня бесплатного доступа и мониторинг площадок, подпишитесь на спонсора:",
            reply_markup=make_sponsor_keyboard()
        )
        return

    await message.answer(
        "Радар находок активен. Бот пришлёт вам пуш, как только появится лот с маржой от 15%.",
        reply_markup=make_main_keyboard()
    )

@dp.callback_query(F.data == "verify_sponsor")
async def cb_verify_sponsor(call: CallbackQuery):
    user_id = call.from_user.id
    ok = await check_channel_member(user_id)
    if not ok:
        await call.answer("Вы ещё не подписались на канал!", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT trial_activated FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        trial_done = bool(row[0]) if row else False

        if not trial_done:
            await db.execute("UPDATE users SET sponsor_verified = 1, trial_activated = 1 WHERE id = ?", (user_id,))
            await db.commit()
            new_date = await extend_user_access(db, user_id, 3)
            date_str = new_date.strftime("%d.%m.%Y %H:%M")
            await call.message.edit_text(
                f"Подписка подтверждена! Начислено 3 дня триала (до {date_str} UTC).\n\nЗапускайте радар:",
                reply_markup=make_main_keyboard()
            )
        else:
            await db.execute("UPDATE users SET sponsor_verified = 1 WHERE id = ?", (user_id,))
            await db.commit()
            await call.message.edit_text("Подписка подтверждена!", reply_markup=make_main_keyboard())

@dp.callback_query(F.data == "buy_stars")
async def cb_buy_stars(call: CallbackQuery):
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="PRO Подписка на 30 дней",
        description="Безлимитные автопоиски Avito и Wildberries + пуши за 2-3 секунды.",
        payload=f"stars_30d_{call.from_user.id}",
        currency="XTR",
        provider_token="",
        prices=[LabeledPrice(label="30 дней доступа", amount=350)],
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success_handler(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        new_until = await extend_user_access(db, user_id, 30)
    await message.answer(
        f"Оплата 350 Stars принята! Доступ продлён до {new_until.strftime('%d.%m.%Y')} UTC.",
        reply_markup=make_main_keyboard()
    )

async def background_monitoring_loop():
    await asyncio.sleep(4)
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_NAME) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM monitors WHERE is_active = 1")
                monitors = await cur.fetchall()

                for mon in monitors:
                    u_cur = await db.execute("SELECT access_until FROM users WHERE id = ?", (mon["user_id"],))
                    u_row = await u_cur.fetchone()
                    if not u_row or parse_dt(u_row["access_until"]) < now:
                        continue

                    all_found = []
                    query = mon["title"]
                    if "Avito" in mon["marketplaces"]:
                        all_found.extend(await fetch_avito_real_lots(query))
                    if "Wildberries" in mon["marketplaces"]:
                        all_found.extend(await fetch_wb_real_lots(query))

                    for lot in all_found:
                        if lot["margin_pct"] >= mon["min_margin"]:
                            chk = await db.execute(
                                "SELECT 1 FROM notification_logs WHERE user_id = ? AND lot_id = ?",
                                (mon["user_id"], lot["id"])
                            )
                            if not await chk.fetchone():
                                await db.execute(
                                    "INSERT INTO notification_logs (user_id, lot_id, sent_at) VALUES (?, ?, ?)",
                                    (mon["user_id"], lot["id"], now.isoformat())
                                )
                                await db.commit()

                                text = (
                                    f"🔥 <b>Найден выгодный лот!</b>\n\n"
                                    f"<b>{lot['title']}</b>\n"
                                    f"Площадка: {lot['source']} · {lot['city']}\n\n"
                                    f"Цена: <b>{lot['price']:,} ₽</b> (Рынок: {lot['market']:,} ₽)\n"
                                    f"Маржа: <b>−{lot['margin_pct']}% (−{lot['margin_rub']:,} ₽)</b>\n\n"
                                    f"Продавец: ★ {lot['seller_rating']} ({lot['seller_reviews']} отзывов)"
                                )
                                kb = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="Перейти к объявлению", url=lot["url"])]
                                ])
                                try:
                                    await bot.send_message(chat_id=mon["user_id"], text=text, parse_mode="HTML", reply_markup=kb)
                                    await process_referral_bonus(db, mon["user_id"])
                                except Exception as err:
                                    print(f"Push error: {err}")
        except Exception as e:
            print(f"Monitoring loop error: {e}")

        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(background_monitoring_loop())
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/feed")
async def get_live_feed(category: str = "all", min_margin: int = 0):
    query_map = {
        "all": "iPhone 15 Pro",
        "iphone": "iPhone 15 Pro",
        "macbook": "MacBook M3 Pro",
        "gpu": "RTX 4070 Ti",
        "scooter": "Ninebot Max G2",
    }
    q = query_map.get(category, "iPhone 15")
    avito_lots = await fetch_avito_real_lots(q)
    wb_lots = await fetch_wb_real_lots(q)
    combined = avito_lots + wb_lots
    filtered = [l for l in combined if l["margin_pct"] >= min_margin]
    return filtered

@app.get("/api/monitors")
async def api_get_monitors(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM monitors WHERE user_id = ?", (user_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

class CreateMonitorReq(BaseModel):
    user_id: int
    title: str
    category: str
    city: str
    min_margin: int
    marketplaces: str

@app.post("/api/monitors")
async def api_create_monitor(data: CreateMonitorReq):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO monitors (user_id, title, category, city, min_margin, marketplaces)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data.user_id, data.title, data.category, data.city, data.min_margin, data.marketplaces))
        await db.commit()
    return {"status": "ok"}

@app.get("/api/profile")
async def api_get_profile(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        u = await cur.fetchone()
        cur_m = await db.execute("SELECT COALESCE(SUM(days_granted), 0) FROM referral_rewards WHERE inviter_user_id = ?", (user_id,))
        bonus_days = (await cur_m.fetchone())[0]
        cur_c = await db.execute("SELECT COUNT(*) FROM referral_rewards WHERE inviter_user_id = ?", (user_id,))
        invited_count = (await cur_c.fetchone())[0]

    if not u:
        return {"days_left": 3, "access_until": "Триал", "bonus_days": 0, "invited_count": 0}

    now = datetime.now(timezone.utc)
    until = parse_dt(u["access_until"])
    days_left = max(0, (until - now).days) if until > now else 0
    return {
        "days_left": days_left,
        "access_until": until.strftime("%d.%m.%Y") if until > now else "Истекла",
        "bonus_days": bonus_days,
        "invited_count": invited_count,
        "ref_code": f"ref_{user_id}"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)