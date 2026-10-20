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

# «болтовня/объявления» вместо лотов: приветствия, новости, правила чата
NOISE_TITLE_WORDS = [
    "всем привет", "всем внимание", "дорогие друзья", "друзья!", "внимание",
    "объявление", "правила", "администрация", "напоминаю", "обращаюсь",
    "по наличию", "спешите", "планируется", "анонс", "опрос", "поздравля",
]

def _is_noise_title(title: str) -> bool:
    tl = (title or "").lower()
    # «всем привет!», «ДОРОГИЕ ДРУЗЬЯ!», «По наличию в России» и т.п.
    if any(w in tl for w in NOISE_TITLE_WORDS):
        return True
    # кончается на «!» и почти без слов — телеграфный заголовок-приветствие
    if tl.rstrip("!").strip().count(" ") <= 2 and tl.rstrip("!").count("!") >= 1 and len(tl) < 22:
        return True
    return False

# пост должен ПОЗВОНИТЬ, что sells: продаю / отдаю / продам / цена ... / есть ...
SALE_WORDS = [
    "продам", "продаю", "продаётся", "продается", "продажа", "продаж",
    "отдам", "отдаю", "отдаётся", "отдается", "содам", "могу отдать",
    "есть в наличии", "в наличии", "забрать", "заберёшь", "заберите",
    "термин: ", "торг", "цена:", "цена ", "цена -", "по цене",
    "свежий айфон", "новый, гаранти",
]

# работа/услуги/подработка — НЕ товарные лоты
NON_ITEM_WORDS = [
    "подработк", "работа", "ваканс", "зарплат", "требуются", "требуетс",
    "услуг", "отзыв", "тестиров", "сервис", "доставка груз", "написан",
    "решен", "диплом", "курсов", "реферат", "оценк", "занятост", "смена",
    "сотрудник", "резюме", "фриланс", "заставка", "резюме", "стажировк",
    "нужны люди", "ищем людей", "набираем", "на полную ставку", "оплата",
    "опыт", "установ", "настро", "автоматизац", "виндовс", "windows",
    "регион", "склад", "видеонаблюд",
]

def _glitch_ratio(text: str) -> float:
    """доля «космических» букв (стиль спам-обфускации). >15% — спам."""
    weird = 0
    letters = 0
    for ch in text:
        o = ord(ch)
        is_normal_letter = (
            0x0400 <= o <= 0x04FF or  # кириллица
            0x0041 <= o <= 0x005A or 0x0061 <= o <= 0x007A or  # латиница
            0x0030 <= o <= 0x0039  # цифры
        )
        if ch.isalpha():
            letters += 1
            if not is_normal_letter:
                weird += 1
    if letters == 0:
        return 0.0
    return weird / letters


# ---- название и «каталог продавца» ----

PRODUCT_PAT = re.compile(
    r"(iphone|айфон|samsung|galaxy|macbook|playstation|ps\d|airpods|наушник|"
    r"apple\s*watch|смарт[-\s]?часы|xiaomi|redmi|poco|honor|dyson|ноутбук|tablet|"
    r"ipad|планшет|монитор|видеокарта|гпу|geforce|radeon|компьютер|системник|"
    r"мотоцикл|мото\b|квадроцикл|скутер|самокат|телевизор|камера|фотопларат)",
    re.IGNORECASE,
)

# строки-«описания», которые не годятся для названия (реклама, условия,_State)
GENERIC_TITLE_WORDS = [
    "весь товар", "оригинал", "гаранти", "в наличии", "по наличию", "без торга",
    "доставка", "отправка", "самовывоз", "идеальн", "состояни", "все работает",
    "в комплект", "комплект", "всё работает", "оригинальный", "проверяйте",
    "не гарантирую", "осмотр", "проверка",
]

SALE_PREFIX_RE = re.compile(
    r"^(?:прода[мюяё]|продает[сся]|продается|продаж|отда[мюяё]|реализ[у]?|"
    r"обмен(?:\w{0,3})?|срочн|уступ[а-я]{0,3}|продам)\s*", re.IGNORECASE,
)


def _is_catalog_post(text: str) -> bool:
    """пост-«каталог» магазина (много разных товаров) — не частный лот"""
    low = text.lower()
    cats = set(m.group(0).lower() for m in PRODUCT_PAT.finditer(low))
    marks = len(cats)
    iphones = set(re.findall(r"(?:iphone|айфон)\s*(\d{1,2}|1[0-8]g?|x|se)", low, re.I))
    if marks >= 3 or len(iphones) >= 3:
        return True
    # «и др.» / «много ...» / перечисления через запятую с ценами
    if low.count(",") >= 6 and marks >= 2:
        return True
    return False


def _make_title(text: str) -> str:
    """вытащить из поста название самой вещи: строка/сегмент с продуктом + модель,
    не рекламная прелюдия («весь товар новый, оригинал…»)"""
    raw_lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    # разбиваем длинные строки на фразы (по . ; ! ?)
    lines = []
    for ln in raw_lines[:12]:
        parts = re.split(r"[.;!?]\s*", ln)
        lines.extend([p.strip() for p in parts if p.strip()])
    best, best_score = None, -99
    for ln in lines[:16]:
        if len(ln) > 95:
            continue
        tl = ln.lower()
        score = 0
        if PRODUCT_PAT.search(tl):
            score += 8
        if re.search(r"\d", tl):
            score += 2
        for g in GENERIC_TITLE_WORDS:
            if g in tl:
                score -= 5
        if 10 <= len(ln) <= 70:
            score += 2
        if score > best_score:
            best, best_score = ln, score
    title = (best if best is not None else (raw_lines[0] if raw_lines else text.strip()))
    # чистим: хэштеги, эмодзи, «продам/срочно», висячую пунктуацию
    title = re.sub(r"#\w+", "", title)
    title = "".join(
        ch for ch in title
        if not (0x1F000 <= ord(ch) <= 0x1FAFF) and ord(ch) not in (0x200D, 0xFE0F, 0x2764, 0x2B50, 0x2728, 0x2705)
    )
    for _ in range(3):
        t2 = SALE_PREFIX_RE.sub("", title).strip()
        if t2 == title:
            break
        title = t2
    title = re.sub(r"^[\W_]+", "", title).strip(" −–—-:,. ").strip()
    return title[:120]

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
    has_sale = any(w in low for w in SALE_WORDS)
    # работа/услуги/подработка — не лоты
    if any(w in low for w in NON_ITEM_WORDS):
        return None
    # обфусцированный («гламурный») текст — уловка спама, не лот
    if _glitch_ratio(text) > 0.15:
        return None
    # шум-заголовок («всем привет», «дорогие друзья», «по наличию») отсекаем только
    # если в посте нет явного «продаю/цена:» — иначе режем настоящие продажи
    first_line0 = text.strip().splitlines()[0]
    if _is_noise_title(re.sub(r"^#\w+\s*", "", first_line0).strip()[:120]) and not has_sale:
        return None
    # берём цену: сначала с валютой, иначе голое число в конце первой строки
    price = 0
    for m in PRICE_RE.finditer(text):
        cand = normalize_price(m.group(1))
        if 300 <= cand <= 999_999:
            price = cand
            break
    if price == 0:
        #------------------------------------------------------------------ fallback: цена без валюты в самом конце первой строки
        # 'iPhone 18 pro 256gb eSIM. 96000' / 'Продам айфон 13 … 26000'
        fl = text.strip().splitlines()[0]
        for m in re.finditer(r"(\d[\d\s\u00a0.,]{2,9})(?!\w)", fl):
            cand = normalize_price(m.group(1))
            if 300 <= cand <= 999_999:
                price = cand
    if price < 300:  # отсекаем «100р» болтовню и цены-пустышки
        return None    # каталог продавца (много разных телефонов/товаров) — не частный лот
    if _is_catalog_post(text):
        return None
    title = _make_title(text)
    if len(title) < 5:
        title = text.strip()[:100]
    tl = title.lower()
    if _is_noise_title(title) and not has_sale:
        return None
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
        chans = []
        for l in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            l = l.strip().lstrip("@")
            if not l or l.startswith("#"):
                continue
            chans.append(l.split()[0])  # «@имя # описание» -> имя
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
                import traceback
                print(f"[{ch}] пропуск: {name}")
                traceback.print_exc()
                continue
            got = []
            for m in msgs:
                if not m.message:
                    continue
                lot = None
                try:
                    lot = extract_lots(m.message, ch, m.id)
                except Exception as e:
                    print(f"[{ch}] extract_lots ошибка {type(e).__name__}: {e} | msg={m.message[:80]!r}")
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
