# -*- coding: utf-8 -*-
"""
gifts_scan.py — сканер Телеграм-подарков на Tonnel (публичная витрина, без логина).

Что делает:
  для каждой коллекции из watchlist — 1 запрос (лоты по возрастанию цены),
  группа по моделям, считается floor каждой модели, сравнивается с прошлым прогоном.
  Если новая цена floor упала минимум на DEAL_PCT — пушим как «выгодный лот»
  (source=GIFTS, market=старый floor, price=новая цена).

Запуск:
  python gifts_scan.py            # один прогон: scan + push
  python gifts_scan.py --no-push  # только скан, без публикации

Список коллекций — gifts_watchlist.txt (по одному названию в строке).
Состояние floors — gifts_state.json (создаётся сам).
"""
import json
import os
import random
import sys
import time
from datetime import datetime

from curl_cffi import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

SERVER_URL = os.getenv("SERVER_URL", "https://dealhunter-i8w2.onrender.com").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "")
TON_RUB_FALLBACK = float(os.getenv("TON_RUB", "200") or "200")

WATCHLIST_FILE = os.path.join(HERE, "gifts_watchlist.txt")
STATE_FILE = os.path.join(HERE, "gifts_state.json")

TONNEL_API = "https://gifts2.tonnel.network/api/pageGifts"
HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
}

# Сколько лотов берём по коллекции (page 1 = самые дешёвые, нам этого хватает)
PAGE_LIMIT = 60
# Порог «выгодности»: новый floor ниже старого минимум на столько %
DEAL_PCT = 8.0
# Пауза между запросами, сек
SLEEP_MIN, SLEEP_MAX = 2.5, 5.5

DEFAULT_WATCHLIST = [
    "Plush Pepe",
    "Lunar Snake",
    "Toy Bear",
    "Precious Peach",
    "Skull Flower",
    "Lucky Locket",
    "Astral Shard",
    "Nail Bracelet",
    "Diamond Rose",
    "Mini Oscar",
    "Cheeky Mouse",
    "Vintage Cigar",
    "Eternal Rose",
    "Million Dollar",
    "Grand Piano",
    "Voodoo Doll",
    "Swag Bag",
    "Sparkle Squirrel",
    "Magic Potion",
    "Homemade Cake",
    "Scared Cat",
    "Sharp Tongue",
    "Whip Love",
    "Signed Hampster",
    "Bunny Ears",
    "Jester's Hat",
    "Neko Helmet",
    "Bowed Tie",
]


def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        names = []
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
        if names:
            return names
    return list(DEFAULT_WATCHLIST)


def ensure_watchlist_file():
    if not os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            f.write("# Список коллекций, по которым ловим скидки относительно floor\n")
            f.write("# Одно название в строке. # — комментарий\n")
            for n in DEFAULT_WATCHLIST:
                f.write(n + "\n")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw.get("v") == 1 and isinstance(raw.get("floors"), dict):
                return raw
        except Exception:
            pass
    return {"v": 1, "floors": {}, "last_run": None}


def save_state(st: dict):
    st["last_run"] = datetime.utcnow().isoformat()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def ton_rub_rate() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "the-open-network", "vs_currencies": "rub"},
            timeout=15,
        )
        d = r.json()
        rate = float(d["the-open-network"]["rub"])
        if rate > 10:
            return rate
    except Exception:
        pass
    return TON_RUB_FALLBACK


def fetch_collection(gift_name: str):
    """Возвращает список лотов коллекции (page 1, дешевле — первыми). None, если 403/ошибка."""
    sort = json.dumps({"price": 1, "gift_id": 1})
    base_filter = {
        "price": {"$exists": True},
        "refunded": {"$ne": True},
        "buyer": {"$exists": False},
        "export_at": {"$exists": True},
        "gift_name": gift_name,
    }
    jd = {
        "page": 1,
        "limit": PAGE_LIMIT,
        "sort": sort,
        "filter": json.dumps({**base_filter, "asset": "TON"}),
        "price_range": None,
        "user_auth": "",
    }
    try:
        r = requests.post(TONNEL_API, data=json.dumps(jd), headers=HEADERS,
                          impersonate="chrome", timeout=30)
    except Exception as e:
        print(f"  [!] сеть: {gift_name}: {type(e).__name__}")
        return None
    if r.status_code == 403:
        print(f"  [!] анти бот 403 на {gift_name} — пропускаю (следующий прогон повторит)")
        return None
    if r.status_code != 200:
        print(f"  [!] HTTP {r.status_code} на {gift_name}")
        return None
    try:
        data = r.json()
    except Exception:
        print(f"  [!] не-JSON ответ на {gift_name}")
        return None
    return data if isinstance(data, list) else []


def collect_deals(watchlist: list):
    """Возвращает (list_deals, ок_коллекций, до_403_шт) и обновляет словарь новых floor."""
    st = load_state()
    floors_prev = st["floors"]  # ключ "Gift|Model" -> {"floor": float, "ts": iso}
    deals = []
    ok_colls = 0
    for gi, gift in enumerate(watchlist):
        lots = fetch_collection(gift)
        if lots is None:
            time.sleep(random.uniform(45.0, 70.0))  # антибот где-то — немного ждём
            continue
        ok_colls += 1
        # группируем по модели
        by_model = {}
        for lot in lots:
            model = (lot.get("model") or "").strip() or "—"
            price = lot.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            by_model.setdefault(model, []).append(lot)
        for model, mlots in by_model.items():
            mlots.sort(key=lambda x: x.get("price", 1e9))
            p_now = mlots[0].get("price")
            if not isinstance(p_now, (int, float)) or p_now <= 0:
                continue
            key = f"{gift}|{model}"
            prev_meta = floors_prev.get(key)
            prev_floor = prev_meta.get("floor") if isinstance(prev_meta, dict) else None
            floors_prev[key] = {"floor": float(p_now), "ts": datetime.utcnow().isoformat(),
                                "gift_num": mlots[0].get("gift_num")}
            if prev_floor is not None and prev_floor > 0 and (1 - p_now / prev_floor) * 100 >= DEAL_PCT:
                cheap = mlots[0]
                deals.append({
                    "gift": gift,
                    "model": model,
                    "backdrop": cheap.get("backdrop") or "—",
                    "price_ton": p_now,
                    "was_floor_ton": float(prev_floor),
                    "gift_id": cheap.get("gift_id") or "",
                    "gift_num": cheap.get("gift_num") or "",
                })
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
    st["floors"] = floors_prev
    save_state(st)
    return deals, ok_colls


def to_lot_dict(d: dict, rate: float) -> dict:
    price_rub = int(round(d["price_ton"] * rate))
    market_rub = int(round(d["was_floor_ton"] * rate))
    if price_rub < 1:
        price_rub = 1
    if market_rub <= price_rub:
        market_rub = price_rub + 1
    profit = market_rub - price_rub
    pct = int(round(profit * 100 / market_rub))
    model_txt = d["model"] if d["model"] != "—" else "базовая"
    title = f"{d['gift']} — {model_txt}"
    tags = ["Подарок", d["gift"], model_txt]
    if d.get("backdrop") and d["backdrop"] != "—":
        tags.append(d["backdrop"])
    tags = tags[:8]
    return {
        "id": f"tonnel-{d['gift_id']}" if d["gift_id"] else f"tonnel-{d['gift_num']}-{int(time.time())}",
        "title": title[:200],
        "source": "GIFTS",
        "city": "Вся РФ",
        "price": price_rub,
        "market": market_rub,
        "tags": tags,
        "sellerName": "Tonnel",
        "sellerRating": 4.8,
        "sellerReviews": 0,
        "description": (
            f"NFT-подарок Telegram · модель {model_txt} · фон {d.get('backdrop') or '—'} · "
            f"номер №{d['gift_num'] or '—'} · floor был {d['was_floor_ton']:.2f} TON, теперь {d['price_ton']:.2f} TON"
        )[:1000],
        "url": "https://market.tonnel.network/",
    }


def push_to_server(lots: list) -> tuple:
    if not lots:
        return 0, 0
    if not WORKER_SECRET:
        print("[!] WORKER_SECRET пустой в .env — пуш невозможен")
        return 0, len(lots)
    import httpx
    try:
        r = httpx.post(
            f"{SERVER_URL}/api/worker/ingest",
            headers={"X-Worker-Secret": WORKER_SECRET},
            json=lots,
            timeout=60,
        )
        if r.status_code == 200:
            d = r.json()
            return d.get("saved", 0), d.get("skipped", 0)
        print(f"[!] сервер {r.status_code}: {r.text[:200]}")
        return 0, len(lots)
    except Exception as e:
        print(f"[!] сервер недоступен: {e}")
        return 0, len(lots)


def main():
    ensure_watchlist_file()
    watchlist = load_watchlist()
    print(f"[{datetime.utcnow().isoformat()}Z] gifts_scan старт: коллекций {len(watchlist)}")
    rate = ton_rub_rate()
    print(f"курс TON: {rate:.0f} ₽")
    deals, ok_colls = collect_deals(watchlist)
    print(f"коллекций прочитано: {ok_colls}/{len(watchlist)}; сделок найдено: {len(deals)}")
    push_it = "--no-push" not in sys.argv
    lots = [to_lot_dict(d, rate) for d in deals] if push_it else []
    if push_it and lots:
        saved, skipped = push_to_server(lots)
        print(f"пуш на сервер: saved={saved} skipped={skipped}")
    elif push_it:
        print("выгодных лотов нет — нечего пушить")
    return 0


if __name__ == "__main__":
    sys.exit(main())
