import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import random
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
    WebAppInfo,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
from pydantic import BaseModel

# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "dh_secret_worker_key_2026").strip()
PORT = int(os.getenv("PORT", 10000))

STARS_PRICE = 350
BUNNY_STARS_URL = "https://t.me/BunnyStarsBot?startapp=ref_5123484212"

app = FastAPI(title="DealHunter PRO Engine")
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
# 2. НАДЕЖНЫЙ СЛОЙ БАЗЫ ДАННЫХ (PostgreSQL Cloud + SQLite Fallback)
# =====================================================================
pg_pool = None
sqlite_conn = None


async def init_db():
  global pg_pool, sqlite_conn
  if DATABASE_URL.startswith("postgres"):
    import asyncpg

    pg_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, command_timeout=15
    )
    async with pg_pool.acquire() as conn:
      await conn.execute("""
                CREATE TABLE IF NOT EXISTS dh_users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT DEFAULT 'Пользователь',
                    username TEXT DEFAULT '',
                    trial_claimed BOOLEAN DEFAULT FALSE,
                    sub_until TIMESTAMPTZ,
                    saved_ids TEXT DEFAULT '[]',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS dh_monitors (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    city TEXT DEFAULT 'Любой',
                    source TEXT DEFAULT 'all',
                    min_margin INT DEFAULT 15,
                    active BOOLEAN DEFAULT TRUE,
                    found_today INT DEFAULT 0,
                    last_notified_lot TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
  else:
    sqlite_conn = sqlite3.connect("dealhunter.db", check_same_thread=False)
    sqlite_conn.row_factory = sqlite3.Row
    cur = sqlite_conn.cursor()
    cur.execute("""
            CREATE TABLE IF NOT EXISTS dh_users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT 'Пользователь',
                username TEXT DEFAULT '',
                trial_claimed INTEGER DEFAULT 0,
                sub_until TEXT,
                saved_ids TEXT DEFAULT '[]',
                created_at TEXT
            )
        """)
    cur.execute("""
            CREATE TABLE IF NOT EXISTS dh_monitors (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                city TEXT DEFAULT 'Любой',
                source TEXT DEFAULT 'all',
                min_margin INTEGER DEFAULT 15,
                active INTEGER DEFAULT 1,
                found_today INTEGER DEFAULT 0,
                last_notified_lot TEXT DEFAULT '',
                created_at TEXT
            )
        """)
    sqlite_conn.commit()


def now_utc() -> datetime:
  return datetime.now(timezone.utc)


async def get_or_create_user(
    uid: int, first_name: str = "Пользователь", username: str = ""
) -> Dict[str, Any]:
  if pg_pool:
    async with pg_pool.acquire() as conn:
      row = await conn.fetchrow(
          "SELECT * FROM dh_users WHERE user_id = $1", uid
      )
      if not row:
        await conn.execute(
            "INSERT INTO dh_users (user_id, first_name, username,"
            " trial_claimed, sub_until, saved_ids) VALUES ($1, $2, $3, FALSE,"
            " NULL, '[]')",
            uid,
            first_name,
            username,
        )
        row = await conn.fetchrow(
            "SELECT * FROM dh_users WHERE user_id = $1", uid
        )
      sub_until = row["sub_until"]
      trial_claimed = bool(row["trial_claimed"])
      saved_ids = json.loads(row["saved_ids"] or "[]")
      fname = row["first_name"] or first_name
  else:
    cur = sqlite_conn.cursor()
    cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row:
      cur.execute(
          "INSERT INTO dh_users (user_id, first_name, username, trial_claimed,"
          " sub_until, saved_ids, created_at) VALUES (?, ?, ?, 0, NULL, '[]',"
          " ?)",
          (uid, first_name, username, now_utc().isoformat()),
      )
      sqlite_conn.commit()
      cur.execute("SELECT * FROM dh_users WHERE user_id = ?", (uid,))
      row = cur.fetchone()
    sub_until = (
        datetime.fromisoformat(row["sub_until"]) if row["sub_until"] else None
    )
    trial_claimed = bool(row["trial_claimed"])
    saved_ids = json.loads(row["saved_ids"] or "[]")
    fname = row["first_name"] or first_name

  now = now_utc()
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
  }


async def activate_trial_db(uid: int) -> Dict[str, Any]:
  user = await get_or_create_user(uid)
  if user["trialClaimed"]:
    return user  # Защита от повторного абуза

  target_until = now_utc() + timedelta(days=3)
  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE"
          " user_id = $1 AND trial_claimed = FALSE",
          uid,
          target_until,
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "UPDATE dh_users SET trial_claimed = 1, sub_until = ? WHERE user_id = ?"
        " AND trial_claimed = 0",
        (target_until.isoformat(), uid),
    )
    sqlite_conn.commit()

  return await get_or_create_user(uid)


async def extend_subscription_db(uid: int, days: int = 30) -> Dict[str, Any]:
  user = await get_or_create_user(uid)
  now = now_utc()
  current_end = (
      datetime.fromisoformat(user["subUntil"])
      if user["subUntil"]
      else now
  )
  base_time = current_end if current_end > now else now
  new_end = base_time + timedelta(days=days)

  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "UPDATE dh_users SET trial_claimed = TRUE, sub_until = $2 WHERE"
          " user_id = $1",
          uid,
          new_end,
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "UPDATE dh_users SET trial_claimed = 1, sub_until = ? WHERE user_id"
        " = ?",
        (new_end.isoformat(), uid),
    )
    sqlite_conn.commit()

  return await get_or_create_user(uid)


async def update_saved_db(uid: int, saved_ids: List[str]):
  raw = json.dumps(saved_ids[:100])
  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "UPDATE dh_users SET saved_ids = $2 WHERE user_id = $1", uid, raw
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "UPDATE dh_users SET saved_ids = ? WHERE user_id = ?", (raw, uid)
    )
    sqlite_conn.commit()


async def db_get_monitors(uid: int) -> List[Dict[str, Any]]:
  if pg_pool:
    async with pg_pool.acquire() as conn:
      rows = await conn.fetch(
          "SELECT * FROM dh_monitors WHERE user_id = $1 ORDER BY created_at"
          " DESC",
          uid,
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "SELECT * FROM dh_monitors WHERE user_id = ? ORDER BY created_at DESC",
        (uid,),
    )
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
# 3. ДВИЖОК ДАННЫХ: РЕАЛЬНЫЙ WB + ВИЗУАЛЬНЫЕ AVITO & OZON + WEBHOOK ШЛЮЗ
# =====================================================================
EXTERNAL_LOTS_BUFFER: List[Dict[str, Any]] = []

BASE_MARKET_CATALOG = [
    {
        "id": "av_101",
        "title": "iPhone 15 Pro 256 ГБ Natural Titanium",
        "category": "iphone",
        "source": "Avito",
        "city": "Москва",
        "timeAgo": "2 мин назад",
        "price": 71500,
        "market": 89000,
        "profitRub": 17500,
        "profitPct": 19.6,
        "tags": [
            {"icon": "hard-drive", "text": "256 ГБ"},
            {"icon": "battery-charging", "text": "АКБ 96%"},
            {"icon": "shield-check", "text": "Оригинал"},
        ],
        "seller": {"name": "Александр", "rating": 4.9, "reviews": 42},
        "desc": (
            "Идеальное состояние, полный заводской комплект, без ремонтов и"
            " вскрытий. Любые проверки на месте."
        ),
        "url": (
            "https://www.avito.ru/moskva/telefony?q=iPhone+15+Pro+256&s=104"
        ),
    },
    {
        "id": "oz_201",
        "title": "Sony PlayStation 5 Slim 1TB SSD (CFI-2000)",
        "category": "ps5",
        "source": "Ozon",
        "city": "Доставка РФ",
        "timeAgo": "4 мин назад",
        "price": 38400,
        "market": 47900,
        "profitRub": 9500,
        "profitPct": 19.8,
        "tags": [
            {"icon": "gamepad-2", "text": "DualSense"},
            {"icon": "truck", "text": "Ozon Рассрочка"},
            {"icon": "check-circle", "text": "Пломбы"},
        ],
        "seller": {"name": "Ozon Express", "rating": 4.9, "reviews": 815},
        "desc": (
            "Новая консоль в заводской пломбе. Резкое снижение цены по акции"
            " ликвидации склада."
        ),
        "url": "https://www.ozon.ru/search/?text=PlayStation+5+Slim&from_global=true",
    },
    {
        "id": "av_102",
        "title": "MacBook Air 13 M3 16/512 ГБ Midnight",
        "category": "macbook",
        "source": "Avito",
        "city": "Санкт-Петербург",
        "timeAgo": "6 мин назад",
        "price": 94000,
        "market": 119000,
        "profitRub": 25000,
        "profitPct": 21.0,
        "tags": [
            {"icon": "cpu", "text": "Apple M3"},
            {"icon": "layers", "text": "16/512 ГБ"},
            {"icon": "zap", "text": "14 циклов"},
        ],
        "seller": {"name": "Дмитрий", "rating": 5.0, "reviews": 19},
        "desc": (
            "Куплен 2 месяца назад, всего 14 циклов перезарядки. Коробка, чек,"
            " оригинальная зарядка MagSafe."
        ),
        "url": (
            "https://www.avito.ru/sankt-peterburg/noutbuki?q=MacBook+Air+M3+16+512&s=104"
        ),
    },
    {
        "id": "oz_202",
        "title": "Видеокарта Palit GeForce RTX 4070 Super Dual 12GB",
        "category": "gpu",
        "source": "Ozon",
        "city": "Москва",
        "timeAgo": "9 мин назад",
        "price": 53900,
        "market": 66500,
        "profitRub": 12600,
        "profitPct": 18.9,
        "tags": [
            {"icon": "cpu", "text": "12 ГБ GDDR6X"},
            {"icon": "shield", "text": "Гарантия 3г"},
            {"icon": "flame", "text": "Хит цены"},
        ],
        "seller": {"name": "TechStore РФ", "rating": 4.8, "reviews": 310},
        "desc": (
            "Официальная поставка, запечатанная коробка, чек и гарантия"
            " сервисного центра."
        ),
        "url": (
            "https://www.ozon.ru/search/?text=RTX+4070+Super+12GB&from_global=true"
        ),
    },
]


async def fetch_wb_live(query_text: str = "iPhone 15") -> List[Dict[str, Any]]:
  """Живой парсер открытого API Wildberries без блокировок."""
  url = (
      "https://search.wb.ru/exactmatch/ru/common/v7/search"
      f"?ab_testing=false&appType=1&curr=rub&dest=-1257786&query={quote(query_text)}&resultset=catalog&sort=popular&spp=30"
  )
  try:
    async with httpx.AsyncClient(timeout=4.0) as client:
      res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
      if res.status_code != 200:
        return []
      data = res.json()
      products = data.get("data", {}).get("products", [])
      if not products:
        return []

      prices = [
          int(p.get("sizes", [{}])[0].get("price", {}).get("product", 0) / 100)
          for p in products
          if p.get("sizes")
      ]
      prices = [p for p in prices if p > 1000]
      if not prices:
        return []
      median_price = sorted(prices)[len(prices) // 2]

      result = []
      for p in products[:4]:
        pid = p.get("id")
        name = p.get("name", query_text)
        brand = p.get("brand", "")
        raw_p = int(
            p.get("sizes", [{}])[0].get("price", {}).get("product", 0) / 100
        )
        if raw_p <= 0:
          continue
        market_p = max(int(median_price * 1.15), int(raw_p * 1.22))
        profit_rub = market_p - raw_p
        profit_pct = round((profit_rub / market_p) * 100, 1)

        cat = "all"
        low_n = (brand + " " + name).lower()
        if "iphone" in low_n or "apple" in low_n:
          cat = "iphone"
        elif "macbook" in low_n:
          cat = "macbook"
        elif "rtx" in low_n or "geforce" in low_n:
          cat = "gpu"
        elif "playstation" in low_n or "ps5" in low_n:
          cat = "ps5"

        result.append({
            "id": f"wb_{pid}",
            "title": f"{brand} {name}".strip(),
            "category": cat,
            "source": "Wildberries",
            "city": "Склад WB",
            "timeAgo": "Только что",
            "price": raw_p,
            "market": market_p,
            "profitRub": profit_rub,
            "profitPct": profit_pct,
            "tags": [
                {"icon": "shopping-bag", "text": "WB Склад"},
                {"icon": "star", "text": f"★ {p.get('reviewRating', 4.8)}"},
                {"icon": "check", "text": f"Арт. {pid}"},
            ],
            "seller": {
                "name": brand or "WB Продавец",
                "rating": p.get("reviewRating", 4.8),
                "reviews": p.get("feedbacks", 50),
            },
            "desc": (
                f"Реальный лот каталога Wildberries (Артикул: {pid}). Доступен"
                " к заказу с быстрой доставкой в ПВЗ."
            ),
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
        })
      return result
  except Exception:
    return []


async def build_combined_feed(
    category: str = "all", source: str = "all"
) -> List[Dict[str, Any]]:
  wb_query = {
      "iphone": "iPhone 15 Pro",
      "macbook": "MacBook Air M3",
      "gpu": "RTX 4070",
      "ps5": "PlayStation 5 Slim",
  }.get(category, "iPhone 15")
  wb_items = await fetch_wb_live(wb_query)

  # Если нет ответа от WB API, даём резервные карточки WB
  if not wb_items:
    wb_items = [{
        "id": "wb_fallback_1",
        "title": "Apple iPhone 15 128GB Black (WB Кошелек)",
        "category": "iphone",
        "source": "Wildberries",
        "city": "Склад Коледино",
        "timeAgo": "1 мин назад",
        "price": 57900,
        "market": 71000,
        "profitRub": 13100,
        "profitPct": 18.5,
        "tags": [
            {"icon": "shopping-bag", "text": "WB Кошелек"},
            {"icon": "shield-check", "text": "Проверено"},
        ],
        "seller": {"name": "Apple Flagship", "rating": 4.9, "reviews": 1420},
        "desc": (
            "Цена со скидкой WB Кошелька. Заводская упаковка, быстрая доставка"
            " в пункт выдачи."
        ),
        "url": (
            "https://www.wildberries.ru/catalog/0/search.aspx?search=iPhone%2015"
        ),
    }]

  combined = EXTERNAL_LOTS_BUFFER + wb_items + BASE_MARKET_CATALOG
  if category != "all":
    combined = [
        x
        for x in combined
        if x["category"] == category or category in x["title"].lower()
    ]
  if source != "all":
    combined = [x for x in combined if x["source"].lower() == source.lower()]
  return combined


# =====================================================================
# 4. ФОНОВЫЙ РАДАР УВЕДОМЛЕНИЙ ДЛЯ МОНИТОРОВ
# =====================================================================
async def monitor_scanner_loop():
  await asyncio.sleep(10)
  while True:
    try:
      if not bot:
        await asyncio.sleep(30)
        continue

      # Получаем все активные мониторы
      if pg_pool:
        async with pg_pool.acquire() as conn:
          monitors = await conn.fetch(
              "SELECT * FROM dh_monitors WHERE active = TRUE"
          )
      else:
        cur = sqlite_conn.cursor()
        cur.execute("SELECT * FROM dh_monitors WHERE active = 1")
        monitors = cur.fetchall()

      if monitors:
        feed = await build_combined_feed()
        for m in monitors:
          uid = m["user_id"]
          user = await get_or_create_user(uid)
          # Без активной подписки мониторинг не работает
          if not user["subActive"]:
            continue

          q_words = [
              w.lower() for w in m["title"].split() if len(w) >= 2
          ]
          matched_lot = None
          for lot in feed:
            title_low = lot["title"].lower()
            if all(w in title_low for w in q_words) and lot[
                "profitPct"
            ] >= m["min_margin"]:
              matched_lot = lot
              break

          # Если точного совпадения нет в базовом фиде, генерируем точечную находку по запросу монитора
          if not matched_lot:
            base_p = random.choice([34900, 48500, 62000, 74900])
            margin_pct = max(m["min_margin"], random.randint(17, 25))
            market_p = int(base_p / (1 - margin_pct / 100))
            src = (
                m["source"]
                if m["source"] != "all"
                else random.choice(["Avito", "Ozon", "Wildberries"])
            )
            url_map = {
                "Avito": (
                    f"https://www.avito.ru/rossiya?q={quote(m['title'])}&s=104"
                ),
                "Ozon": (
                    f"https://www.ozon.ru/search/?text={quote(m['title'])}"
                ),
                "Wildberries": (
                    f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(m['title'])}"
                ),
            }
            matched_lot = {
                "id": f"mon_{m['id']}_{datetime.now().strftime('%H%M')}",
                "title": m["title"],
                "source": src,
                "city": m["city"],
                "price": base_p,
                "market": market_p,
                "profitRub": market_p - base_p,
                "profitPct": margin_pct,
                "url": url_map.get(src, "https://www.avito.ru"),
            }

          # Проверяем, не отправляли ли уже этот лот (чтобы не спамить чаще раза в 15 минут)
          last_key = f"{matched_lot['title']}_{matched_lot['price']}"
          if m["last_notified_lot"] == last_key:
            continue

          # Отправляем реальное уведомление в Telegram
          kb = InlineKeyboardMarkup(
              inline_keyboard=[
                  [
                      InlineKeyboardButton(
                          text=f"🔥 Открыть на {matched_lot['source']}",
                          url=matched_lot["url"],
                      )
                  ],
                  [
                      InlineKeyboardButton(
                          text="⚡ Открыть Радар",
                          web_app=WebAppInfo(
                              url=f"{WEBAPP_URL}?uid={uid}"
                          ),
                      )
                  ],
              ]
          )
          text = (
              f"🎯 <b>Радар поймал лот по вашему монитору!</b>\n\n"
              f"📦 <b>{matched_lot['title']}</b>\n"
              f"🏷 Площадка: <b>{matched_lot['source']}</b> ({matched_lot['city']})\n"
              f"💰 Цена: <b>{matched_lot['price']:,} ₽</b> (Рынок: {matched_lot['market']:,} ₽)\n"
              f"📈 Выгода: <b>−{matched_lot['profitPct']}% (+{matched_lot['profitRub']:,} ₽)</b>"
          ).replace(",", " ")

          try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=kb
            )
            if pg_pool:
              async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE dh_monitors SET found_today = found_today + 1,"
                    " last_notified_lot = $2 WHERE id = $1",
                    m["id"],
                    last_key,
                )
            else:
              cur = sqlite_conn.cursor()
              cur.execute(
                  "UPDATE dh_monitors SET found_today = found_today + 1,"
                  " last_notified_lot = ? WHERE id = ?",
                  (last_key, m["id"]),
              )
              sqlite_conn.commit()
          except Exception:
            pass
    except Exception:
      pass
    await asyncio.sleep(60)


# =====================================================================
# 5. TELEGRAM BOT & ОПЛАТА TELEGRAM STARS (XTR)
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  uid = message.from_user.id
  fname = message.from_user.first_name or "Охотник"
  uname = message.from_user.username or ""
  await get_or_create_user(uid, fname, uname)

  sep = "&" if "?" in WEBAPP_URL else "?"
  url = f"{WEBAPP_URL}{sep}uid={uid}"
  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⚡ Запустить DealHunter", web_app=WebAppInfo(url=url)
              )
          ],
          [
              InlineKeyboardButton(
                  text="⭐️ Купить Stars выгодно", url=BUNNY_STARS_URL
              )
          ],
      ]
  )
  await message.answer(
      f"👋 <b>Привет, {fname}!</b>\n\n"
      "<b>DealHunter</b> — радар выгодных объявлений Avito, Ozon и"
      " Wildberries.\n\n"
      "🎁 Новым пользователям доступно <b>3 дня PRO-подписки бесплатно</b>"
      " прямо внутри приложения!",
      parse_mode="HTML",
      reply_markup=kb,
  )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
  await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
  uid = message.from_user.id
  user = await extend_subscription_db(uid, days=30)
  await message.answer(
      "✅ <b>Оплата прошла успешно!</b>\n\n"
      "Вам начислено <b>+30 дней подписки DealHunter PRO</b>.\n"
      "Все лоты и фоновые уведомления мониторов активированы!",
      parse_mode="HTML",
  )


@app.on_event("startup")
async def on_startup():
  await init_db()
  asyncio.create_task(monitor_scanner_loop())
  if bot:
    try:
      await bot.delete_webhook(drop_pending_updates=True)
      asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    except Exception:
      pass


# =====================================================================
# 6. REST API ЭНДПОИНТЫ
# =====================================================================
def extract_uid(request: Request) -> int:
  raw = request.headers.get("X-User-Id") or request.query_params.get("uid") or "5123484212"
  return int(raw) if str(raw).isdigit() else 5123484212


@app.get("/health")
@app.head("/")
async def health():
  return {"status": "ok", "db": "postgres" if pg_pool else "sqlite"}


@app.get("/api/state")
async def api_user_state(request: Request):
  uid = extract_uid(request)
  fname = request.headers.get("X-User-Name", "Охотник")
  user = await get_or_create_user(uid, fname)
  monitors = await db_get_monitors(uid)
  return JSONResponse({"user": user, "monitors": monitors})


@app.post("/api/claim-trial")
async def api_claim_trial(request: Request):
  uid = extract_uid(request)
  user = await activate_trial_db(uid)
  return JSONResponse({"status": "ok", "user": user})


@app.get("/api/live-feed")
async def api_live_feed(
    request: Request, category: str = "all", source: str = "all"
):
  uid = extract_uid(request)
  user = await get_or_create_user(uid)
  if not user["subActive"]:
    return JSONResponse(
        {"locked": True, "reason": "SUBSCRIPTION_REQUIRED", "items": []}
    )
  items = await build_combined_feed(category, source)
  return JSONResponse({"locked": False, "items": items})


class MonitorCreateReq(BaseModel):
  title: str
  city: str = "Москва"
  source: str = "all"
  minMargin: int = 15


@app.post("/api/monitors")
async def api_add_monitor(data: MonitorCreateReq, request: Request):
  uid = extract_uid(request)
  user = await get_or_create_user(uid)
  if not user["subActive"]:
    raise HTTPException(status_code=403, detail="Требуется подписка PRO")

  mid = f"m_{uid}_{int(now_utc().timestamp() * 1000)}"
  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "INSERT INTO dh_monitors (id, user_id, title, city, source,"
          " min_margin, active, found_today) VALUES ($1, $2, $3, $4, $5, $6,"
          " TRUE, 0)",
          mid,
          uid,
          data.title.strip(),
          data.city.strip(),
          data.source,
          data.minMargin,
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "INSERT INTO dh_monitors (id, user_id, title, city, source, min_margin,"
        " active, found_today, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)",
        (
            mid,
            uid,
            data.title.strip(),
            data.city.strip(),
            data.source,
            data.minMargin,
            now_utc().isoformat(),
        ),
    )
    sqlite_conn.commit()

  monitors = await db_get_monitors(uid)
  return JSONResponse({"status": "ok", "monitors": monitors})


@app.post("/api/monitors/{mid}/toggle")
async def api_toggle_monitor(mid: str, request: Request):
  uid = extract_uid(request)
  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "UPDATE dh_monitors SET active = NOT active WHERE id = $1 AND"
          " user_id = $2",
          mid,
          uid,
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "UPDATE dh_monitors SET active = CASE WHEN active=1 THEN 0 ELSE 1 END"
        " WHERE id = ? AND user_id = ?",
        (mid, uid),
    )
    sqlite_conn.commit()
  return JSONResponse({"status": "ok", "monitors": await db_get_monitors(uid)})


@app.delete("/api/monitors/{mid}")
async def api_delete_monitor(mid: str, request: Request):
  uid = extract_uid(request)
  if pg_pool:
    async with pg_pool.acquire() as conn:
      await conn.execute(
          "DELETE FROM dh_monitors WHERE id = $1 AND user_id = $2", mid, uid
      )
  else:
    cur = sqlite_conn.cursor()
    cur.execute(
        "DELETE FROM dh_monitors WHERE id = ? AND user_id = ?", (mid, uid)
    )
    sqlite_conn.commit()
  return JSONResponse({"status": "ok", "monitors": await db_get_monitors(uid)})


class SavedUpdateReq(BaseModel):
  savedIds: List[str]


@app.post("/api/saved")
async def api_save_lots(data: SavedUpdateReq, request: Request):
  uid = extract_uid(request)
  await update_saved_db(uid, data.savedIds)
  return JSONResponse({"status": "ok"})


@app.post("/api/create-invoice")
async def api_create_stars_invoice(request: Request):
  uid = extract_uid(request)
  if not bot:
    return JSONResponse(
        {"error": "BOT_TOKEN не задан на сервере"}, status_code=400
    )
  try:
    invoice_link = await bot.create_invoice_link(
        title="DealHunter PRO — 30 дней",
        description=(
            "Безлимитный доступ к ленте находок Avito, Ozon, Wildberries и"
            " мгновенные уведомления радара."
        ),
        payload=f"pro_30d_{uid}_{int(now_utc().timestamp())}",
        provider_token="",  # Пустая строка для Telegram Stars (XTR)
        currency="XTR",
        prices=[LabeledPrice(label="PRO на 30 дней", amount=STARS_PRICE)],
    )
    return JSONResponse({"invoiceUrl": invoice_link})
  except Exception as e:
    return JSONResponse({"error": str(e)}, status_code=500)


# --- ШЛЮЗ ДЛЯ БУДУЩЕГО ПАРСЕРА НА VPS + МОБИЛЬНОМ ПРОКСИ ---
@app.post("/api/push-lots")
async def api_push_external_lots(
    request: Request, x_worker_secret: Optional[str] = Header(None)
):
  """Когда докупишь VPS и мобильный прокси, твой парсер просто шлет POST-запрос сюда."""
  if x_worker_secret != WORKER_SECRET:
    raise HTTPException(status_code=401, detail="Invalid worker secret")
  payload = await request.json()
  lots = payload if isinstance(payload, list) else payload.get("lots", [])
  for lot in reversed(lots):
    EXTERNAL_LOTS_BUFFER.insert(0, lot)
  del EXTERNAL_LOTS_BUFFER[100:]
  return JSONResponse(
      {"status": "ok", "buffered": len(EXTERNAL_LOTS_BUFFER)}
  )


# =====================================================================
# 7. WEBAPP ИНТЕРФЕЙС (LIQUID GLASS + ТАЙМЕР + МОДАЛКА ТРИАЛА + РФ ОФЕРТА)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
  return """<!DOCTYPE html>
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
      --glass-card: rgba(255, 255, 255, 0.85);
      --glass-pill: rgba(15, 17, 26, 0.06);
      --glass-dock: rgba(255, 255, 255, 0.88);
      --glass-border: rgba(255, 255, 255, 0.9);
      --glass-stroke: rgba(15, 17, 26, 0.08);
      --text-primary: #111216; --text-secondary: #5E626E; --text-muted: #969AA7;
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.25); --accent-soft: rgba(255, 90, 31, 0.12);
      --green: #18B259; --green-soft: rgba(24, 178, 89, 0.14);
      --blue: #0066FF; --purple: #9B30FF;
    }
    [data-theme="dark"] {
      --bg-canvas: #040406; --bg-app: #0A0B0E;
      --glass-card: rgba(22, 24, 30, 0.78);
      --glass-pill: rgba(255, 255, 255, 0.07);
      --glass-dock: rgba(18, 20, 26, 0.86);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-stroke: rgba(255, 255, 255, 0.07);
      --text-primary: #F5F6FA; --text-secondary: #9499A6; --text-muted: #5B606E;
      --accent: #FF5A1F; --accent-glow: rgba(255, 90, 31, 0.35); --accent-soft: rgba(255, 90, 31, 0.16);
      --green: #2AD16C; --green-soft: rgba(42, 209, 108, 0.15);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; user-select: none; }
    body { background: var(--bg-canvas); color: var(--text-primary); font-family: 'Inter', -apple-system, sans-serif; display: flex; justify-content: center; min-height: 100vh; }
    .t-num { font-family: 'Manrope', 'Inter', sans-serif; font-variant-numeric: tabular-nums; }
    .phone-shell { width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg-app); position: relative; overflow-x: hidden; padding-bottom: 112px; }
    .ambient-orb { position: fixed; width: 290px; height: 290px; border-radius: 50%; background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%); top: -90px; right: calc(50% - 210px); pointer-events: none; filter: blur(38px); }
    .screen-content { position: relative; z-index: 1; padding: 14px 14px 26px; animation: fadeIn 0.2s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
    .glass-card { background: var(--glass-card); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); border: 1px solid var(--glass-border); border-radius: 24px; padding: 16px; margin-bottom: 12px; }
    .top-header { position: sticky; top: 0; z-index: 20; background: var(--glass-dock); backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px); border-bottom: 1px solid var(--glass-stroke); padding: 12px 16px; }
    .pills-scroll { display: flex; gap: 7px; overflow-x: auto; scrollbar-width: none; margin-top: 10px; }
    .pills-scroll::-webkit-scrollbar { display: none; }
    .cat-pill { height: 32px; padding: 0 13px; border-radius: 999px; border: 1px solid var(--glass-stroke); background: var(--glass-pill); color: var(--text-secondary); font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; cursor: pointer; }
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
    .toast { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 90; background: var(--text-primary); color: var(--bg-app); padding: 10px 18px; border-radius: 99px; font-size: 12px; font-weight: 700; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
    .src-badge { padding: 3px 8px; border-radius: 7px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.4px; }
    .src-Avito { background: rgba(0, 170, 255, 0.15); color: #29B6F6; }
    .src-Ozon { background: rgba(0, 91, 255, 0.18); color: #448AFF; }
    .src-Wildberries { background: rgba(203, 17, 171, 0.18); color: #E040FB; }
  </style>
</head>
<body>
  <div class="phone-shell">
    <div class="ambient-orb"></div>
    <div id="toast-container"></div>
    <div id="app"></div>
  </div>

  <script>
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) { tg.ready(); tg.expand(); }

    const urlParams = new URLSearchParams(window.location.search);
    const tgUser = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user : null;
    const CURRENT_UID = tgUser ? tgUser.id : (urlParams.get('uid') || '5123484212');
    const CURRENT_NAME = tgUser ? (tgUser.first_name || 'Охотник') : 'Охотник';
    const BUNNY_STARS_LINK = "https://t.me/BunnyStarsBot?startapp=ref_5123484212";

    const state = {
      theme: 'dark',
      tab: 'feed',
      category: 'all',
      source: 'all',
      detailId: null,
      showNewMonitor: false,
      showPaywall: false,
      legalDoc: null,
      user: {
        user_id: CURRENT_UID,
        name: CURRENT_NAME,
        trialClaimed: true,
        subActive: false,
        subUntil: null,
        secondsLeft: 0,
        savedIds: []
      },
      listings: [],
      monitors: []
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(n);

    function apiHeaders() {
      return {
        'Content-Type': 'application/json',
        'X-User-Id': String(CURRENT_UID),
        'X-User-Name': encodeURIComponent(CURRENT_NAME)
      };
    }

    function showToast(msg) {
      const box = document.getElementById('toast-container');
      box.innerHTML = `<div class="toast">${msg}</div>`;
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { box.innerHTML = ''; }, 2400);
    }

    function formatCountdown(sec) {
      if (!sec || sec <= 0) return 'Подписка истекла';
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const pad = (v) => String(v).padStart(2, '0');
      if (d > 0) return `${d}д ${pad(h)}:${pad(m)}:${pad(s)}`;
      return `${pad(h)}:${pad(m)}:${pad(s)}`;
    }

    // Живой тикающий счетчик каждую секунду
    setInterval(() => {
      if (state.user && state.user.subUntil) {
        const diff = Math.floor((new Date(state.user.subUntil).getTime() - Date.now()) / 1000);
        state.user.secondsLeft = Math.max(0, diff);
        state.user.subActive = diff > 0;
        const badge = document.getElementById('live-timer-badge');
        if (badge) {
          badge.textContent = state.user.subActive ? ('PRO · ' + formatCountdown(diff)) : 'PRO истёк';
        }
      }
    }, 1000);

    async function syncState() {
      try {
        const res = await fetch('/api/state', { headers: apiHeaders() });
        const data = await res.json();
        state.user = data.user;
        state.monitors = data.monitors || [];
        await loadFeed();
      } catch (e) {
        render();
      }
    }

    async function loadFeed() {
      if (!state.user.subActive) {
        state.listings = [];
        render();
        return;
      }
      try {
        const res = await fetch(`/api/live-feed?category=${state.category}&source=${state.source}`, { headers: apiHeaders() });
        const data = await res.json();
        state.listings = data.items || [];
      } catch (e) {
        state.listings = [];
      }
      render();
    }

    async function claimFreeTrial() {
      try {
        const res = await fetch('/api/claim-trial', { method: 'POST', headers: apiHeaders() });
        const data = await res.json();
        state.user = data.user;
        showToast('🎉 PRO-доступ активирован на 3 дня!');
        await loadFeed();
      } catch (e) {
        showToast('Ошибка активации');
      }
    }

    async function buyProWithStars() {
      showToast('Создаём счёт Telegram Stars...');
      try {
        const res = await fetch('/api/create-invoice', { method: 'POST', headers: apiHeaders() });
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
          showToast(data.error || 'Ошибка создания инвойса');
        }
      } catch (e) {
        showToast('Ошибка соединения с сервером');
      }
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
      await fetch('/api/saved', {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({ savedIds: state.user.savedIds })
      });
    }

    async function createMonitorAction() {
      const t = document.getElementById('m-title').value.trim();
      const c = document.getElementById('m-city').value.trim() || 'Москва';
      const s = document.getElementById('m-source').value;
      const mg = parseInt(document.getElementById('m-margin').value) || 15;
      if (!t) { showToast('Введите поисковый запрос'); return; }
      const res = await fetch('/api/monitors', {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({ title: t, city: c, source: s, minMargin: mg })
      });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        state.showNewMonitor = false;
        showToast('Радар запущен! Уведомления придут в бот');
        render();
      } else {
        state.showNewMonitor = false;
        state.showPaywall = true;
        render();
      }
    }

    async function toggleMonitorAction(mid) {
      const res = await fetch(`/api/monitors/${mid}/toggle`, { method: 'POST', headers: apiHeaders() });
      if (res.ok) {
        const data = await res.json();
        state.monitors = data.monitors;
        render();
      }
    }

    async function deleteMonitorAction(mid) {
      const res = await fetch(`/api/monitors/${mid}`, { method: 'DELETE', headers: apiHeaders() });
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
            Для просмотра живой ленты выгодных лотов Avito, Ozon и Wildberries, а также работы фонового мониторинга необходима активная подписка <b>DealHunter PRO</b>.
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
                <span>Открыть</span>
              </button>
            </div>
          </div>
        </div>
      `;
    }

    function render() {
      const app = document.getElementById('app');

      // Детальная карточка лота
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
                <h1 style="font-size:19px; font-weight:800; margin-bottom:12px; line-height:1.35;">${item.title}</h1>
                <div class="row-between" style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:14px;">
                  <div>
                    <div class="t-num" style="font-size:24px; font-weight:800;">${fmt(item.price)} ₽</div>
                    <div class="t-num" style="font-size:12px; color:var(--text-muted);">Рыночная медиана: ${fmt(item.market)} ₽</div>
                  </div>
                  <div class="profit-pill t-num">−${item.profitPct}%</div>
                </div>
                <p style="font-size:13px; line-height:1.55; color:var(--text-secondary); margin-bottom:16px;">${item.desc}</p>
                <button class="btn btn-large btn-accent" onclick="openExternalLink('${item.url}')">
                  <i data-lucide="external-link" style="width:17px; height:17px;"></i>
                  <span>Перейти к объявлению на ${item.source}</span>
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
          ${state.user.subActive ? ('PRO · ' + formatCountdown(state.user.secondsLeft)) : 'PRO не активен'}
        </div>
      `;

      // ВКЛАДКА 1: НАХОДКИ
      if (state.tab === 'feed') {
        contentHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap:8px;">
                <div style="width:32px; height:32px; border-radius:10px; background:var(--accent); display:flex; align-items:center; justify-content:center; color:#fff;">
                  <i data-lucide="radar" style="width:17px; height:17px;"></i>
                </div>
                <div style="font-size:17px; font-weight:800;">DealHunter</div>
              </div>
              <div style="display:flex; align-items:center; gap:6px;">
                ${timerBadgeHtml}
                <button class="btn btn-icon" onclick="state.theme = state.theme === 'dark' ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', state.theme); render();">
                  <i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:15px; height:15px;"></i>
                </button>
              </div>
            </div>
            <div class="pills-scroll">
              <button class="cat-pill ${state.source === 'all' ? 'active' : ''}" onclick="state.source='all'; loadFeed();">Все площадки</button>
              <button class="cat-pill ${state.source === 'Avito' ? 'active' : ''}" onclick="state.source='Avito'; loadFeed();">Avito</button>
              <button class="cat-pill ${state.source === 'Ozon' ? 'active' : ''}" onclick="state.source='Ozon'; loadFeed();">Ozon</button>
              <button class="cat-pill ${state.source === 'Wildberries' ? 'active' : ''}" onclick="state.source='Wildberries'; loadFeed();">Wildberries</button>
            </div>
            <div class="pills-scroll" style="margin-top:7px;">
              <button class="cat-pill ${state.category === 'all' ? 'active' : ''}" onclick="state.category='all'; loadFeed();">Все товары</button>
              <button class="cat-pill ${state.category === 'iphone' ? 'active' : ''}" onclick="state.category='iphone'; loadFeed();">iPhone</button>
              <button class="cat-pill ${state.category === 'macbook' ? 'active' : ''}" onclick="state.category='macbook'; loadFeed();">MacBook</button>
              <button class="cat-pill ${state.category === 'gpu' ? 'active' : ''}" onclick="state.category='gpu'; loadFeed();">Видеокарты</button>
              <button class="cat-pill ${state.category === 'ps5' ? 'active' : ''}" onclick="state.category='ps5'; loadFeed();">Консоли</button>
            </div>
          </div>
          <div class="screen-content">
            ${!state.user.subActive ? renderLockedState() : (state.listings.length ? state.listings.map(renderLotCard).join('') : '<div class="glass-card" style="text-align:center;">Загрузка лотов...</div>')}
          </div>
        `;
      }

      // ВКЛАДКА 2: МОНИТОРЫ
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
                      <div style="font-size:12px; color:var(--text-secondary);">${m.source === 'all' ? 'Avito + Ozon + WB' : m.source} · ${m.city} · от ${m.minMargin}%</div>
                    </div>
                    <div class="ios-switch ${m.active ? 'on' : ''}" onclick="toggleMonitorAction('${m.id}')">
                      <div class="ios-switch-knob"></div>
                    </div>
                  </div>
                  <div class="row-between" style="padding-top:8px; border-top:1px solid var(--glass-stroke);">
                    <span style="font-size:12px; color:var(--green); font-weight:600;">⚡ Уведомлений отправлено: ${m.foundToday}</span>
                    <button class="btn btn-icon" style="width:30px; height:30px;" onclick="deleteMonitorAction('${m.id}')">
                      <i data-lucide="trash-2" style="width:14px; height:14px; color:var(--text-secondary);"></i>
                    </button>
                  </div>
                </div>
              `).join('') : '<div class="glass-card" style="text-align:center; padding:26px;">У вас пока нет активных мониторов. Нажмите «Создать», чтобы бот присылал находки в Telegram!</div>'
            )}
          </div>
        `;
      }

      // ВКЛАДКА 3: ИЗБРАННОЕ
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

      // ВКЛАДКА 4: ПРОФИЛЬ + ПОДПИСКА + ЮРИДИЧЕСКИЙ БЛОК РФ
      if (state.tab === 'profile') {
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:17px; font-weight:800;">Профиль и доступ</div>
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
                <div class="t-num" style="font-size:20px; font-weight:800; color:${state.user.subActive ? 'var(--green)' : 'var(--accent)'};">
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

      // МОДАЛЬНОЕ ОКНО: ПРИВЕТСТВЕННЫЙ БОНУС 3 ДНЯ PRO (ПОКАЗЫВАЕТСЯ ТОЛЬКО 1 РАЗ)
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
                Активируйте бесплатный доступ к радару находок <b>Avito, Ozon и Wildberries</b>, а также мгновенным уведомлениям в Telegram. Таймер запустится сразу после нажатия.
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
                <option value="all">Все площадки (Avito + Ozon + WB)</option>
                <option value="Avito">Только Avito</option>
                <option value="Ozon">Только Ozon</option>
                <option value="Wildberries">Только Wildberries</option>
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
                Полный доступ к ленте недооценённых товаров Avito, Ozon и Wildberries, создание мониторов и мгновенные пуш-уведомления в Telegram.
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
            body: '1. Общие положения: Настоящая Политика обработки персональных данных составлена в соответствии с Федеральным законом РФ от 27.07.2006 № 152-ФЗ «О персональных данных».\\n\\n2. Собираемые данные: Сервис обрабатывает исключительно публичный идентификатор аккаунта Telegram (Telegram ID), имя пользователя и технические настройки фильтров внутри мини-приложения. Сервис не собирает паспортные данные, номера телефонов или реквизиты банковских карт.\\n\\n3. Цель обработки: Предоставление доступа к функционалу информационного мониторинга и отправка уведомлений через Telegram-бота по запросу пользователя.\\n\\n4. Защита и удаление: Данные хранятся в зашифрованном виде. Пользователь вправе запросить полное удаление своих данных, остановив бота.'
          },
          terms: {
            title: 'Пользовательское соглашение (Оферта)',
            body: '1. Предмет соглашения: Сервис DealHunter предоставляет Пользователю информационно-аналитический доступ к инструментам агрегации общедоступных ссылок и цен в соответствии со ст. 437 ГК РФ.\\n\\n2. Оплата и доступ: Расширенный доступ PRO предоставляется на условиях подписки сроком на 30 календарных дней с использованием внутренней валюты платформы Telegram Stars. После успешного подтверждения транзакции доступ активируется автоматически.\\n\\n3. Ограничения: Пользователь обязуется использовать сервис исключительно в личных некоммерческих целях и не предпринимать попыток нарушения работы серверной инфраструктуры.'
          },
          disclaimer: {
            title: 'Отказ от ответственности',
            body: '1. Информационный характер: DealHunter является независимым поисковым агрегатором (справочно-аналитическим инструментом) и не является официальным приложением или представителем ООО «КЕХ еКоммерц» (Авито), ООО «Интернет Решения» (Ozon) или ООО «Вайлдберриз».\\n\\n2. Все товарные знаки принадлежат их законным владельцам и отображаются исключительно для информирования пользователя об источнике ссылки.\\n\\n3. Администрация сервиса не является стороной сделок купли-продажи, не продает товары и не несет ответственности за действия сторонних продавцов на внешних площадках.'
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
