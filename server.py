import os
import json
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
)

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dealhunter-i8w2.onrender.com").strip()
PORT = int(os.getenv("PORT", 10000))

app = FastAPI(title="DealHunter Stable Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# --- СТАБИЛЬНЫЕ ЛОТЫ ДЛЯ РАБОТЫ ИНТЕРФЕЙСА БЕЗ ВЫЛЕТОВ ---
MOCK_FEED = [
    {
        "id": "lot_1",
        "title": "iPhone 15 Pro 256 ГБ (Natural Titanium)",
        "category": "iphone",
        "source": "Avito",
        "city": "Москва",
        "timeAgo": "3 мин",
        "price": 71500,
        "market": 89000,
        "profitRub": 17500,
        "profitPct": 19.6,
        "tags": [
            {"icon": "hard-drive", "text": "256 ГБ"},
            {"icon": "battery-charging", "text": "АКБ 95%"},
            {"icon": "sparkles", "text": "Идеал"},
        ],
        "specs": {
            "Модель": "iPhone 15 Pro",
            "Память": "256 ГБ",
            "Цвет": "Natural Titanium",
            "Состояние": "Идеальное, полный комплект",
            "АКБ": "95%",
        },
        "seller": {
            "name": "Александр",
            "rating": 4.9,
            "reviews": 38,
            "since": 2020,
            "safe": True,
            "note": "Проверенный продавец",
        },
        "desc": "Ростест, куплен в М.Видео. Полный комплект, не вскрывался, без единой царапины.",
        "history": [77000, 74000, 71500],
        "url": "https://www.avito.ru",
    },
    {
        "id": "lot_2",
        "title": "MacBook Pro 14 M3 (16/512 ГБ)",
        "category": "macbook",
        "source": "Avito",
        "city": "Санкт-Петербург",
        "timeAgo": "8 мин",
        "price": 128000,
        "market": 159000,
        "profitRub": 31000,
        "profitPct": 19.5,
        "tags": [
            {"icon": "cpu", "text": "Apple M3"},
            {"icon": "layers", "text": "16 ГБ ОЗУ"},
            {"icon": "zap", "text": "28 циклов"},
        ],
        "specs": {
            "Процессор": "Apple M3",
            "Память": "16 ГБ RAM / 512 ГБ SSD",
            "Состояние": "Как новый",
            "Батарея": "100% ёмкость",
        },
        "seller": {
            "name": "Максим",
            "rating": 5.0,
            "reviews": 14,
            "since": 2021,
            "safe": True,
            "note": "Частное лицо",
        },
        "desc": "Использовался для веб-дизайна дома. Полный заводской комплект с коробкой и зарядкой.",
        "history": [140000, 134000, 128000],
        "url": "https://www.avito.ru",
    },
    {
        "id": "lot_3",
        "title": "GeForce RTX 4070 Ti 12GB Gaming",
        "category": "gpu",
        "source": "Avito",
        "city": "Казань",
        "timeAgo": "14 мин",
        "price": 58000,
        "market": 74000,
        "profitRub": 16000,
        "profitPct": 21.6,
        "tags": [
            {"icon": "box", "text": "12 ГБ GDDR6X"},
            {"icon": "file-check", "text": "Чек DNS"},
            {"icon": "shield", "text": "Пломбы"},
        ],
        "specs": {
            "Модель": "RTX 4070 Ti",
            "Память": "12 ГБ",
            "Комплект": "Коробка + чек магазина",
        },
        "seller": {
            "name": "Игорь",
            "rating": 4.8,
            "reviews": 22,
            "since": 2019,
            "safe": True,
            "note": "Рейтинг 4.8",
        },
        "desc": "Не греется, любые стресс-тесты Furmark/Superposition перед покупкой.",
        "history": [64000, 61000, 58000],
        "url": "https://www.avito.ru",
    },
    {
        "id": "lot_4",
        "title": "PlayStation 5 Slim Digital Edition",
        "category": "ps5",
        "source": "Avito",
        "city": "Екатеринбург",
        "timeAgo": "25 мин",
        "price": 37000,
        "market": 47000,
        "profitRub": 10000,
        "profitPct": 21.2,
        "tags": [
            {"icon": "gamepad-2", "text": "2 геймпада"},
            {"icon": "box", "text": "Slim 1TB"},
            {"icon": "check-circle", "text": "3 ревизия"},
        ],
        "specs": {
            "Модель": "PS5 Slim Digital",
            "Накопитель": "1 ТБ",
            "Аксессуары": "Второй DualSense в подарок",
        },
        "seller": {
            "name": "Денис",
            "rating": 4.7,
            "reviews": 11,
            "since": 2022,
            "safe": True,
            "note": "Без претензий",
        },
        "desc": "Покупалась полгода назад, состояние идеальное, не шумит, аккаунты отвязаны.",
        "history": [42000, 39000, 37000],
        "url": "https://www.avito.ru",
    },
]

USER_STORAGE: Dict[int, Dict[str, Any]] = {}
MONITORS_STORAGE: Dict[int, List[Dict[str, Any]]] = {}

# --- TELEGRAM ХЭНДЛЕРЫ ---
@dp.message(CommandStart())
async def bot_start(message: types.Message):
    uid = message.from_user.id
    fname = message.from_user.first_name or "Пользователь"
    uname = message.from_user.username or "user"

    if uid not in USER_STORAGE:
        USER_STORAGE[uid] = {
            "user_id": uid,
            "name": fname,
            "username": uname,
            "daysLeft": 3,
            "refCode": f"dh_{uid}",
        }

    sep = "&" if "?" in WEBAPP_URL else "?"
    url = f"{WEBAPP_URL}{sep}uid={uid}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Открыть DealHunter", web_app=WebAppInfo(url=url)
                )
            ]
        ]
    )
    await message.answer(
        f"👋 <b>Добро пожаловать в DealHunter, {fname}!</b>\n\n"
        "Радар мониторинга готов к поиску выгодных цен.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def success_payment_handler(message: types.Message):
    uid = message.from_user.id
    if uid in USER_STORAGE:
        USER_STORAGE[uid]["daysLeft"] += 30
    await message.answer(
        "🎉 <b>Подписка PRO активирована на 30 дней!</b>", parse_mode="HTML"
    )


# --- ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ ---
@app.on_event("startup")
async def on_startup():
    if bot:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        except Exception:
            pass


@app.get("/health")
@app.head("/")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# --- REST API ---
@app.get("/api/live-feed")
async def get_live_feed(query: Optional[str] = None):
    if not query or query.lower() in ("all", "все"):
        return JSONResponse(MOCK_FEED)
    q = query.lower()
    filtered = [
        item
        for item in MOCK_FEED
        if q in item["title"].lower() or q in item["category"].lower()
    ]
    return JSONResponse(filtered if filtered else MOCK_FEED)


class MonitorModel(BaseModel):
    title: str
    city: str = "Москва"
    minMargin: int = 15


@app.get("/api/monitors")
async def list_monitors(request: Request):
    uid_str = request.headers.get("X-User-Id", "5123484212")
    uid = int(uid_str) if uid_str.isdigit() else 5123484212
    return JSONResponse(MONITORS_STORAGE.get(uid, []))


@app.post("/api/monitors")
async def create_monitor(data: MonitorModel, request: Request):
    uid_str = request.headers.get("X-User-Id", "5123484212")
    uid = int(uid_str) if uid_str.isdigit() else 5123484212

    new_mon = {
        "id": f"m_{int(datetime.now().timestamp() * 1000)}",
        "title": data.title,
        "city": data.city,
        "minMargin": data.minMargin,
        "active": True,
        "foundToday": 2,
    }
    if uid not in MONITORS_STORAGE:
        MONITORS_STORAGE[uid] = []
    MONITORS_STORAGE[uid].insert(0, new_mon)
    return JSONResponse({"status": "ok", "monitor": new_mon})


# --- HTML ИНТЕРФЕЙС LIQUID GLASS ---
@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    return """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DealHunter</title>

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/lucide@latest"></script>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>

  <style>
    :root {
      --bg-canvas: #E4E6EC;
      --bg-app: #F2F3F7;
      --glass-card: rgba(255, 255, 255, 0.82);
      --glass-card-hover: rgba(255, 255, 255, 0.95);
      --glass-pill: rgba(15, 17, 26, 0.055);
      --glass-dock: rgba(255, 255, 255, 0.85);
      --glass-border: rgba(255, 255, 255, 0.9);
      --glass-stroke: rgba(15, 17, 26, 0.07);
      --text-primary: #111216;
      --text-secondary: #5E626E;
      --text-muted: #969AA7;
      --accent: #FF5A1F;
      --accent-glow: rgba(255, 90, 31, 0.25);
      --accent-soft: rgba(255, 90, 31, 0.12);
      --green: #18B259;
      --green-soft: rgba(24, 178, 89, 0.12);
      --danger: #F03E3E;
    }

    [data-theme="dark"] {
      --bg-canvas: #040406;
      --bg-app: #0A0B0E;
      --glass-card: rgba(24, 26, 32, 0.72);
      --glass-card-hover: rgba(32, 35, 43, 0.85);
      --glass-pill: rgba(255, 255, 255, 0.065);
      --glass-dock: rgba(20, 22, 28, 0.82);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-stroke: rgba(255, 255, 255, 0.07);
      --text-primary: #F5F6FA;
      --text-secondary: #9499A6;
      --text-muted: #5B606E;
      --accent: #FF5A1F;
      --accent-glow: rgba(255, 90, 31, 0.35);
      --accent-soft: rgba(255, 90, 31, 0.16);
      --green: #2AD16C;
      --green-soft: rgba(42, 209, 108, 0.14);
      --danger: #FF5252;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; user-select: none; }
    body { background: var(--bg-canvas); color: var(--text-primary); font-family: 'Inter', -apple-system, sans-serif; display: flex; justify-content: center; min-height: 100vh; }
    .t-num { font-family: 'Manrope', 'Inter', sans-serif; font-variant-numeric: tabular-nums; }
    .phone-shell { width: 100%; max-width: 430px; min-height: 100vh; background: var(--bg-app); position: relative; overflow-x: hidden; padding-bottom: 108px; }
    .ambient-orb { position: fixed; width: 280px; height: 280px; border-radius: 50%; background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%); top: -90px; right: calc(50% - 210px); pointer-events: none; filter: blur(36px); }
    .screen-content { position: relative; z-index: 1; padding: 14px 14px 24px; animation: fadeIn 0.22s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

    .glass-card { background: var(--glass-card); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); border: 1px solid var(--glass-border); border-radius: 26px; padding: 18px; margin-bottom: 12px; }
    .top-header { position: sticky; top: 0; z-index: 20; background: var(--glass-dock); backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px); border-bottom: 1px solid var(--glass-stroke); padding: 14px 16px 12px; }
    .pills-scroll { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; margin-top: 12px; }
    .pills-scroll::-webkit-scrollbar { display: none; }

    .cat-pill { height: 34px; padding: 0 14px; border-radius: 999px; border: 1px solid var(--glass-stroke); background: var(--glass-pill); color: var(--text-secondary); font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; cursor: pointer; }
    .cat-pill.active { background: var(--text-primary); color: var(--bg-app); border-color: transparent; }

    .spec-tag { display: inline-flex; align-items: center; gap: 5px; height: 28px; padding: 0 10px; border-radius: 10px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-secondary); font-size: 12px; font-weight: 500; }
    .profit-pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 14px; background: var(--accent-soft); color: var(--accent); font-weight: 800; font-size: 13px; }

    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border: none; font-family: inherit; font-weight: 600; cursor: pointer; }
    .btn-icon { width: 38px; height: 38px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); color: var(--text-primary); }
    .btn-pill { height: 38px; padding: 0 16px; border-radius: 14px; font-size: 13px; }
    .btn-large { height: 50px; width: 100%; border-radius: 18px; font-size: 15px; font-weight: 700; }
    .btn-accent { background: var(--accent); color: #FFFFFF; }
    .btn-glass { background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); }

    .floating-dock-wrap { position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%); width: calc(100% - 28px); max-width: 398px; z-index: 35; }
    .floating-dock { height: 66px; padding: 6px; border-radius: 26px; background: var(--glass-dock); backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px); border: 1px solid var(--glass-border); display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; box-shadow: 0 20px 40px -12px rgba(0,0,0,0.5); }
    .dock-btn { border: none; background: transparent; border-radius: 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; color: var(--text-secondary); font-size: 11px; font-weight: 600; cursor: pointer; }
    .dock-btn.active { background: var(--glass-pill); color: var(--accent); }

    .ios-switch { width: 50px; height: 30px; border-radius: 999px; background: var(--glass-pill); border: 1px solid var(--glass-stroke); padding: 3px; cursor: pointer; position: relative; transition: background 0.2s; }
    .ios-switch.on { background: var(--accent); }
    .ios-switch-knob { width: 22px; height: 22px; border-radius: 50%; background: #FFFFFF; transition: transform 0.2s; }
    .ios-switch.on .ios-switch-knob { transform: translateX(20px); }

    .modal-backdrop { position: fixed; inset: 0; max-width: 430px; margin: 0 auto; background: rgba(0, 0, 0, 0.65); backdrop-filter: blur(10px); z-index: 60; display: flex; align-items: flex-end; }
    .modal-sheet { width: 100%; background: var(--bg-app); border-top: 1px solid var(--glass-border); border-radius: 30px 30px 0 0; padding: 22px 18px 28px; }
    .input-glass { width: 100%; height: 46px; padding: 0 14px; border-radius: 14px; background: var(--glass-pill); border: 1px solid var(--glass-border); color: var(--text-primary); font-family: inherit; font-size: 14px; outline: none; margin-bottom: 12px; }
    .row-between { display: flex; align-items: center; justify-content: space-between; }
    .toast { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 80; background: var(--text-primary); color: var(--bg-app); padding: 10px 18px; border-radius: 99px; font-size: 13px; font-weight: 600; }
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
    if (tg) { tg.expand(); }

    const state = {
      theme: 'dark',
      tab: 'feed',
      category: 'all',
      detailId: null,
      showNewMonitor: false,
      showPaywall: false,
      savedIds: ['lot_1'],
      listings: [],
      monitors: [
        { id: 'm_1', title: 'iPhone 15 Pro / 16 Pro', city: 'Москва', minMargin: 18, active: true, foundToday: 4 }
      ]
    };

    const fmt = (n) => new Intl.NumberFormat('ru-RU').format(n);

    function showToast(msg) {
      const box = document.getElementById('toast-container');
      box.innerHTML = `<div class="toast"><span>${msg}</span></div>`;
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { box.innerHTML = ''; }, 2200);
    }

    function toggleTheme() {
      state.theme = state.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', state.theme);
      render();
    }

    async function loadFeed() {
      try {
        const res = await fetch('/api/live-feed');
        state.listings = await res.json();
      } catch(e) {
        state.listings = [];
      }
      render();
    }

    function renderLotCard(item) {
      const isSaved = state.savedIds.includes(item.id);
      return `
        <div class="glass-card" onclick="state.detailId='${item.id}'; window.scrollTo(0,0); render();" style="cursor:pointer;">
          <div class="row-between" style="margin-bottom: 8px;">
            <div style="display:flex; align-items:center; gap: 6px;">
              <span style="padding: 2px 8px; border-radius: 6px; background: var(--glass-pill); font-size: 11px; font-weight: 700;">${item.source}</span>
              <span style="font-size: 12px; color: var(--text-secondary);">${item.city} · ${item.timeAgo}</span>
            </div>
          </div>

          <h3 style="font-size: 16px; font-weight: 700; margin-bottom: 8px;">${item.title}</h3>

          <div style="display:flex; flex-wrap:wrap; gap: 6px; margin-bottom: 12px;">
            ${item.tags.map(t => `<span class="spec-tag"><i data-lucide="${t.icon}" style="width:13px; height:13px; color:var(--accent);"></i>${t.text}</span>`).join('')}
          </div>

          <div class="row-between" style="padding: 10px 12px; border-radius: 14px; background: var(--glass-pill); margin-bottom: 12px;">
            <div>
              <div class="t-num" style="font-size: 21px; font-weight: 800;">${fmt(item.price)} ₽</div>
              <div class="t-num" style="font-size: 11px; color: var(--text-muted);">Рынок ${fmt(item.market)} ₽</div>
            </div>
            <div class="profit-pill t-num">−${item.profitPct}% · ${fmt(item.profitRub)} ₽</div>
          </div>

          <div class="row-between">
            <span style="font-size: 12px; color: var(--text-secondary);">★ ${item.seller.rating} (${item.seller.reviews} отз.)</span>
            <div style="display:flex; gap:6px;">
              <button class="btn btn-icon" style="width:34px; height:34px;" onclick="event.stopPropagation(); const i=state.savedIds.indexOf('${item.id}'); if(i>=0){state.savedIds.splice(i,1);}else{state.savedIds.push('${item.id}');} render();">
                <i data-lucide="heart" style="width:16px; height:16px; ${isSaved ? 'fill:var(--accent); stroke:var(--accent);' : ''}"></i>
              </button>
              <button class="btn btn-pill btn-accent" style="height:34px; padding:0 12px;" onclick="event.stopPropagation(); window.open('${item.url}', '_blank');">
                <span>Забрать</span>
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
              <button class="btn btn-pill btn-accent" onclick="window.open('${item.url}', '_blank');">Открыть лот</button>
            </div>
            <div class="screen-content">
              <div class="glass-card">
                <div style="font-size:12px; color:var(--text-secondary); margin-bottom:6px;">${item.source} · ${item.city}</div>
                <h1 style="font-size:20px; font-weight:800; margin-bottom:12px;">${item.title}</h1>
                <div class="row-between" style="padding:12px; border-radius:14px; background:var(--glass-pill); margin-bottom:12px;">
                  <div>
                    <div class="t-num" style="font-size:26px; font-weight:800;">${fmt(item.price)} ₽</div>
                    <div class="t-num" style="font-size:12px; color:var(--text-muted);">Медиана: ${fmt(item.market)} ₽</div>
                  </div>
                  <div class="profit-pill t-num">-${item.profitPct}%</div>
                </div>
                <p style="font-size:13px; line-height:1.5; color:var(--text-secondary); margin-bottom:14px;">${item.desc}</p>
                <button class="btn btn-large btn-accent" onclick="window.open('${item.url}', '_blank');">Связаться с продавцом</button>
              </div>
            </div>
          `;
          lucide.createIcons();
          return;
        }
      }

      let contentHtml = '';

      if (state.tab === 'feed') {
        const filtered = state.category === 'all' ? state.listings : state.listings.filter(x => x.category === state.category);
        contentHtml = `
          <div class="top-header">
            <div class="row-between">
              <div style="display:flex; align-items:center; gap:8px;">
                <div style="width:34px; height:34px; border-radius:10px; background:var(--accent); display:flex; align-items:center; justify-content:center; color:#fff;">
                  <i data-lucide="radar" style="width:18px; height:18px;"></i>
                </div>
                <div style="font-size:18px; font-weight:800;">Радар находок</div>
              </div>
              <button class="btn btn-icon" onclick="toggleTheme()"><i data-lucide="${state.theme === 'dark' ? 'sun' : 'moon'}" style="width:16px; height:16px;"></i></button>
            </div>
            <div class="pills-scroll">
              <button class="cat-pill ${state.category === 'all' ? 'active' : ''}" onclick="state.category='all'; render();">Все</button>
              <button class="cat-pill ${state.category === 'iphone' ? 'active' : ''}" onclick="state.category='iphone'; render();">iPhone</button>
              <button class="cat-pill ${state.category === 'macbook' ? 'active' : ''}" onclick="state.category='macbook'; render();">MacBook</button>
              <button class="cat-pill ${state.category === 'gpu' ? 'active' : ''}" onclick="state.category='gpu'; render();">Видеокарты</button>
            </div>
          </div>
          <div class="screen-content">
            ${filtered.map(renderLotCard).join('')}
          </div>
        `;
      }

      if (state.tab === 'monitors') {
        contentHtml = `
          <div class="top-header row-between">
            <div style="font-size:18px; font-weight:800;">Мои мониторы</div>
            <button class="btn btn-pill btn-accent" onclick="state.showNewMonitor = true; render();">
              <i data-lucide="plus" style="width:14px; height:14px;"></i><span>Добавить</span>
            </button>
          </div>
          <div class="screen-content">
            ${state.monitors.map(m => `
              <div class="glass-card">
                <div class="row-between" style="margin-bottom:8px;">
                  <div>
                    <div style="font-size:16px; font-weight:800;">${m.title}</div>
                    <div style="font-size:12px; color:var(--text-secondary);">${m.city} · от ${m.minMargin}% маржи</div>
                  </div>
                  <div class="ios-switch ${m.active ? 'on' : ''}" onclick="m.active=!m.active; render();">
                    <div class="ios-switch-knob"></div>
                  </div>
                </div>
                <div style="font-size:12px; color:var(--text-muted);">Поймано сегодня: ${m.foundToday} лота</div>
              </div>
            `).join('')}
          </div>
        `;
      }

      if (state.tab === 'saved') {
        const saved = state.listings.filter(x => state.savedIds.includes(x.id));
        contentHtml = `
          <div class="top-header row-between"><div style="font-size:18px; font-weight:800;">Подборка (${saved.length})</div></div>
          <div class="screen-content">${saved.length ? saved.map(renderLotCard).join('') : '<div class="glass-card" style="text-align:center; padding:30px;">В подборке пока пусто</div>'}</div>
        `;
      }

      if (state.tab === 'profile') {
        contentHtml = `
          <div class="top-header row-between"><div style="font-size:18px; font-weight:800;">Профиль</div></div>
          <div class="screen-content">
            <div class="glass-card">
              <div style="font-size:18px; font-weight:800; margin-bottom:4px;">DealHunter PRO</div>
              <div style="font-size:13px; color:var(--text-secondary); margin-bottom:14px;">Доступ активен: осталось 3 дня</div>
              <button class="btn btn-large btn-accent" onclick="state.showPaywall = true; render();">Продлить доступ · 350 Stars</button>
            </div>
          </div>
        `;
      }

      let modalHtml = '';
      if (state.showNewMonitor) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showNewMonitor = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <div style="font-size:17px; font-weight:800;">Новый монитор</div>
                <button class="btn btn-icon" onclick="state.showNewMonitor = false; render();"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <input type="text" id="m-title" class="input-glass" placeholder="Что ищем (напр. iPhone 16 Pro)" />
              <input type="number" id="m-margin" class="input-glass" placeholder="Мин. выгода (%)" value="18" />
              <button class="btn btn-large btn-accent" onclick="
                const t = document.getElementById('m-title').value;
                const mg = parseInt(document.getElementById('m-margin').value) || 15;
                if(t) {
                  state.monitors.unshift({ id: 'm_' + Date.now(), title: t, city: 'Москва', minMargin: mg, active: true, foundToday: 0 });
                  state.showNewMonitor = false;
                  showToast('Монитор сохранён');
                  render();
                }
              ">Сохранить радар</button>
            </div>
          </div>
        `;
      }

      if (state.showPaywall) {
        modalHtml = `
          <div class="modal-backdrop" onclick="state.showPaywall = false; render();">
            <div class="modal-sheet" onclick="event.stopPropagation();">
              <div class="row-between" style="margin-bottom:12px;">
                <span class="profit-pill">PRO Тариф</span>
                <button class="btn btn-icon" onclick="state.showPaywall = false; render();"><i data-lucide="x" style="width:16px; height:16px;"></i></button>
              </div>
              <div class="t-num" style="font-size:28px; font-weight:800; margin-bottom:8px;">350 Stars <span style="font-size:14px; color:var(--text-muted);">/ 30 дней</span></div>
              <button class="btn btn-large btn-accent" onclick="state.showPaywall = false; showToast('Оплата Stars доступна через Telegram-бота');">Оплатить в Telegram</button>
            </div>
          </div>
        `;
      }

      const dockHtml = `
        <div class="floating-dock-wrap">
          <nav class="floating-dock">
            <button class="dock-btn ${state.tab === 'feed' ? 'active' : ''}" onclick="state.tab='feed'; render();"><i data-lucide="flame" style="width:20px; height:20px;"></i><span>Находки</span></button>
            <button class="dock-btn ${state.tab === 'monitors' ? 'active' : ''}" onclick="state.tab='monitors'; render();"><i data-lucide="radar" style="width:20px; height:20px;"></i><span>Мониторы</span></button>
            <button class="dock-btn ${state.tab === 'saved' ? 'active' : ''}" onclick="state.tab='saved'; render();"><i data-lucide="heart" style="width:20px; height:20px;"></i><span>Подборка</span></button>
            <button class="dock-btn ${state.tab === 'profile' ? 'active' : ''}" onclick="state.tab='profile'; render();"><i data-lucide="user" style="width:20px; height:20px;"></i><span>Профиль</span></button>
          </nav>
        </div>
      `;

      app.innerHTML = contentHtml + modalHtml + dockHtml;
      lucide.createIcons();
    }

    loadFeed();
  </script>
</body>
</html>
"""
