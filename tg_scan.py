# -*- coding: utf-8 -*-
"""DealHunter TG-скан: читает публичные б/у-каналы через официальный MTProto API (Telethon).

Команды:
  python tg_scan.py --login      первый вход (спросит номер и код из Telegram)
  python tg_scan.py --list       показать каналы, в которых состоит аккаунт
  python tg_scan.py --scan       инкрементальный скан каналов из tg_channels.txt
  python tg_scan.py --scan --full  без инкремента (вытащить последние 100 постов)

Ключи берёт из .env: TG_API_ID, TG_API_HASH. Никогда не коммитятся.
"""
import os
import re
import sys
import json
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "")
SESSION_FILE = "tg_session"  # -> tg_session.session (не коммитится)
CHANNELS_FILE = BASE / "tg_channels.txt"
STATE_FILE = BASE / "tg_state.json"
LOTS_FILE = BASE / "tg_lots.json"

SCAM_WORDS = [
    "быстрый перевод", "залог", "заложу", "крипт", "сатош", "bitcoin", "btc",
    "без осмотра", "перевод на карту до", "только полный перевод", "оплатите заранее",
    "не нужно приближаться", "гарантий нет",
]

# посты-спрос («куплю», «ищу», «сколько стоит?») — НЕ лоты
DEMAND_WORDS = [
    "куплю", "покуплю", "скуп", "ищу", "нужен ", "нужна ", "нужно ", "подскажите",
    "посоветуйте", "кто может", "кто продаст", "сколько стоит", "актуально?",
    "порекомендуйте", "ищется", "ищеться",
]

# пост должен ПОЗВОНИТЬ, что sells: продаю / отдаю / продам / цена ... / есть ...
SALE_WORDS = [
    "продам", "продаю", "продаётся", "продается", "продажа", "продаж",
    "отдам", "отдаю", "отдаётся", "отдается", "содам", "могу отдать",
    "есть в наличии", "в наличии", "забрать", "заберёшь", "заберите",
    "термин: ", "торг", "цена:", "цена ", "цена -", "по цене",
    "свежий айфон", "новый, гаранти",
]

# минимальные правдоподобные цены (защита от «запчастей» и цен «со скупа»)
PRICE_FLOORS = {
    "iphone": 3500, "айфон": 3500, "galaxy": 3500, "samsung": 3500,
    "macbook": 14000, "ноутбук": 8000, "playstation": 7000, "ps5": 7000, "ps4": 4500,
    "airpods": 900, "наушники": 700, "watch": 1600, "часы": 1600,
    "dyson": 4000, "redmi": 2500, "xiaomi": 2500, "honor": 2000, "poco": 2500,
}
DEFAULT_FLOOR = 900

PRICE_RE = re.compile(r"(\d[\d\s\u00a0.,]{2,13})\s*(руб|₽|р\.|р(?!\w)|rub)", re.IGNORECASE)


def normalize_price(raw: str):
    """«1 800 000» -> 1800000; «m1 180000 ₽» -> 180000 (что­бы приставка «m1» не приросла к цене)."""
    groups = re.split(r"[\s\u00a0]+", (raw or "").strip())
    digits = re.sub(r"[^\d]", "", (raw or "").replace(",", "").replace(".", ""))
    if not digits:
        return 0
    val = int(digits)
    if val >= 1_000_000 and len(groups) >= 2:
        # «1 180000» / «1 800 000» / «m1 180000»: похоже, одна лишняя ведущая группа
        alt = int(re.sub(r"[^\d]", "", " ".join(groups[1:])))
        if 0 < alt < val:
            val = alt
    if val >= 1_000_000:
        return 0
    return val


def clean_price(raw: str):
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return 0
    return int(re.sub(r"^0+", "", digits) or "0")


def extract_lots(text: str, channel: str, msg_id: int):
    if not text or len(text) < 8:
        return None
    low = text.lower()
    if any(w in low for w in SCAM_WORDS):
        return None
    if any(w in low for w in DEMAND_WORDS):
        return None
    if not any(w in low for w in SALE_WORDS):
        # без явного «продаю/отдаю/цена:» — болтовня в чате, не лот
        return None
    # берём первую цену-кандидат, которая похожа на правду (иначе пропускаем)
    if not PRICE_RE.search(text):
        return None
    price = 0
    for m in PRICE_RE.finditer(text):
        cand = normalize_price(m.group(1))
        if 300 <= cand <= 999_999:
            price = cand
            break
    if price < 300:  # отсекаем «100р» болтовню и цены-пустышки
        return None
    first_line = text.strip().splitlines()[0]
    title = re.sub(r"^#\w+\s*", "", first_line).strip()[:120]
    title = re.sub(r"^[\W_]+", "", title).strip()
    if len(title) < 5:
        title = text.strip()[:100]
    tl = title.lower()
    floor = DEFAULT_FLOOR
    for brand, fl in PRICE_FLOORS.items():
        if brand in tl:
            floor = fl
            break
    if price < floor:
        return None
    if "запчаст" in tl:
        return None
    return {
        "id": f"tg_{channel}_{msg_id}",
        "title": title,
        "price": price,
        "url": f"https://t.me/{channel}/{msg_id}",
        "channel": channel[:40],
        "excerpt": text.strip()[:160],
    }


def push_to_server(lots):
    """Отправка лотов в DealHunter (source=TG). Возвращает сколько принято."""
    if not SERVER_URL or not WORKER_SECRET:
        print("push: нет SERVER_URL/WORKER_SECRET в .env")
        return 0
    payload = [{
        "id": l["id"],
        "title": l["title"][:200],
        "source": "TG",
        "city": "Москва",
        "price": l["price"],
        "market": l["price"],
        "tags": ["барахолка"],
        "sellerName": "Автор поста",
        "sellerRating": 0,
        "sellerReviews": 0,
        "description": l["excerpt"],
        "url": l["url"],
    } for l in lots]
    saved = 0
    with httpx.Client(timeout=60) as c:
        for k in range(0, len(payload), 100):
            try:
                r = c.post(f"{SERVER_URL}/api/worker/ingest", json=payload[k:k + 100],
                           headers={"X-Worker-Secret": WORKER_SECRET})
                print(f"push: {r.status_code} {r.text[:100]}")
                if r.status_code == 200:
                    saved += r.json().get("saved", 0)
            except Exception as e:
                print(f"push: ошибка {type(e).__name__}")
    return saved


async def run():
    from telethon import TelegramClient

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--scan"
    if not API_ID or not API_HASH:
        print("В .env нужны TG_API_ID и TG_API_HASH (см. my.telegram.org)")
        return

    client = TelegramClient(SESSION_FILE, int(API_ID), API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERR: сессии нет — сначала выполни вход (tg_login.py)")
        await client.disconnect()
        return
    try:
        if cmd == "--login":
            me = client.get_me()
            print(f"Вход ок: {me.first_name} (@{me.username or '—'})")
            return

        if cmd == "--list":
            print("Каналы/группы, в которых ты состоишь:")
            async for d in client.iter_dialogs(limit=100):
                if d.is_channel or d.is_group:
                    username = getattr(d.entity, "username", None)
                    mark = "public" if username else "private"
                    print(f"  [{mark}] {d.name[:40]:40}  @{'б' if not username else username}")
            return

        # --scan
        if not CHANNELS_FILE.exists():
            print(f"Нет {CHANNELS_FILE.name}. Создай файл, по одному @каналу на строку.")
            print("Подсказка: python tg_scan.py --list  покажет твои каналы.")
            return
        chans = [l.strip().lstrip("@") for l in CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if not chans:
            print("tg_channels.txt пустой.")
            return

        state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
        full = "--full" in sys.argv

        all_lots = []
        for ch in chans:
            try:
                min_id = 0 if full else state.get(ch, 0)
                msgs = await client.get_messages(ch, limit=100 if full else 50, min_id=min_id)
            except Exception as e:
                name = type(e).__name__
                if name == "FloodWaitError":
                    wait = getattr(e, "seconds", 60)
                    print(f"[{ch}] FloodWait {wait}с — ждём (правило белой зоны)")
                    time.sleep(min(wait, 300))
                    continue
                print(f"[{ch}] пропуск: {name}")
                continue
            got = []
            for m in msgs:
                if not m.message:
                    continue
                lot = extract_lots(m.message, ch, m.id)
                if lot:
                    got.append(lot)
            last_id = max([m.id for m in msgs], default=None)
            if last_id and not full:
                state[ch] = max(state.get(ch, 0), last_id)
            print(f"[{ch}] постов прочитано: {len(msgs)}, лотов с ценой: {len(got)}")
            all_lots.extend(got)

        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")

        # местная витрина: чтение -> merger c прошлым (последние 300 лотов)
        old = json.loads(LOTS_FILE.read_text("utf-8")) if LOTS_FILE.exists() else []
        by_id = {l["id"]: l for l in old}
        for l in all_lots:
            by_id[l["id"]] = l
        merged = sorted(by_id.values(), key=lambda x: x["id"], reverse=True)[:300]
        LOTS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")
        print(f"\nИтого свежих лотов: {len(all_lots)}. Всего в витрине: {len(merged)}. Файл: {LOTS_FILE.name}")

        if all_lots and ("--push" in sys.argv or cmd == "--push"):
            saved = push_to_server(all_lots)
            print(f"Принято на сервер: {saved}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    print("TG-скан DealHunter — читаю только публичные каналы, по своему аккаунту.")
    import asyncio
    try:
        asyncio.run(run())
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
