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

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_FILE = "tg_session"  # -> tg_session.session (не коммитится)
CHANNELS_FILE = BASE / "tg_channels.txt"
STATE_FILE = BASE / "tg_state.json"
LOTS_FILE = BASE / "tg_lots.json"

SCAM_WORDS = [
    "быстрый перевод", "залог", "заложу", "крипт", "сатош", "bitcoin", "btc",
    "без осмотра", "перевод на карту до", "только полный перевод", "оплатите заранее",
    "не нужно приближаться", "гарантий нет",
]

PRICE_RE = re.compile(r"(\d[\d\s\u00a0.,]{2,13})\s*(руб|₽|р\.|rub)", re.IGNORECASE)


def clean_price(raw: str):
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return 0
    val = int(digits)
    # страховка от цен формата «15.000» -> 15 или «1 500 000» -> мусор
    if val >= 1_000_000:
        return 0
    return val


def extract_lots(text: str, channel: str, msg_id: int):
    if not text or len(text) < 8:
        return None
    low = text.lower()
    if any(w in low for w in SCAM_WORDS):
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    price = clean_price(m.group(1))
    if price < 300:  # отсекаем «100р» болтовню и цены-пустышки
        return None
    first_line = text.strip().splitlines()[0]
    title = re.sub(r"^#\w+\s*", "", first_line).strip()[:120]
    if len(title) < 5:
        title = text.strip()[:100]
    return {
        "id": f"tg_{channel}_{msg_id}",
        "title": title,
        "price": price,
        "url": f"https://t.me/{channel}/{msg_id}",
        "channel": channel[:40],
        "excerpt": text.strip()[:160],
    }


async def run():
    from telethon.sync import TelegramClient

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--scan"
    if not API_ID or not API_HASH:
        print("В .env нужны TG_API_ID и TG_API_HASH (см. my.telegram.org)")
        return

    with TelegramClient(SESSION_FILE, int(API_ID), API_HASH) as client:
        if cmd == "--login":
            me = client.get_me()
            print(f"Вход ок: {me.first_name} (@{me.username or '—'})")
            return

        if cmd == "--list":
            print("Каналы/группы, в которых ты состоишь:")
            for d in client.iter_dialogs(limit=100):
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
                msgs = client.get_messages(ch, limit=100 if full else 50, min_id=min_id)
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


if __name__ == "__main__":
    print("TG-скан DealHunter — читаю только публичные каналы, по своему аккаунту.")
    run()
