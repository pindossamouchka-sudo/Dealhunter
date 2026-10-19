# -*- coding: utf-8 -*-
"""
gifts_scan — «gift sniper» для DealHunter.

Мониторим NFT-подарки Telegram сразу на двух маркетах:
  • Tonnel  (market.tonnel.network) — API gifts2.tonnel.network/api/pageGifts
  • Portals (portal-market.com)    — API portal-market.com/api/nfts/search

Честная «рыночная планка» = МЕДИАНА цен всех текущих листингов модели,
случайных из двух маркетов (Тоннель + Portals вместе).
Лот объявляем выгодным, если его цена ≤ (100 - DEAL_PCT)% от медианы
и выборка по модели достаточно велика (>= MIN_SAMPLES лотов).
Ссылка на лот — прямая (Tonnel: /gift/<gift_id>, Portals: t.me/nft/<tg_id>).
"""
import os
import sys
import time
import json
import random
import statistics
from datetime import datetime
from pathlib import Path

import re
from curl_cffi import requests

HERE = str(Path(__file__).parent)
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
TONNEL_HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
}
PORTALS_BASE = "https://portal-market.com/api"

# Пейджинг по каждому маркету: 3 страницы × limit = сколько листингов читаем
TONNEL_PAGES = 3
TONNEL_LIMIT = 30       # сервер Tonnel не принимает limit > 30
PORTALS_PAGES = 3
PORTALS_LIMIT = 30

# Порог «выгодности»: цена ≤ медианы минус DEAL_PCT%
DEAL_PCT = 10.0
# Минимальное число листингов в выборке модели, чтобы ей верить
MIN_SAMPLES = 5
# Антибот-пауза между запросами
SLEEP_MIN, SLEEP_MAX = 2.0, 4.5

DEFAULT_WATCHLIST = [
    "Plush Pepe", "Lunar Snake", "Toy Bear", "Precious Peach", "Skull Flower",
    "Lucky Locket", "Astral Shard", "Nail Bracelet", "Diamond Rose", "Mini Oscar",
    "Cheeky Mouse", "Vintage Cigar", "Evil Eye", "Swiss Watch", "Heart Locket",
    "Homemade Cake", "Jester Hat", "Sleigh Bell", "Scared Cat", "Spy Agaric",
    "Berry Box", "Loot Bag", "Candle", "Party Sparkler", "Record Player",
]

UA_HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"}


def load_watchlist():
    try:
        with open(WATCHLIST_FILE, encoding="utf-8-sig") as f:
            ws = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        return ws or DEFAULT_WATCHLIST
    except FileNotFoundError:
        return DEFAULT_WATCHLIST


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("floors"), dict):
            d.setdefault("samples", {})
            return d
    except Exception:
        pass
    return {"v": 2, "floors": {}, "samples": {}, "last_run": None}


def save_state(st):
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
            timeout=15, impersonate="chrome",
        )
        rate = float(r.json()["the-open-network"]["rub"])
        if 10 < rate < 1000:
            return rate
    except Exception:
        pass
    return TON_RUB_FALLBACK


# ---------------- Tonnel ----------------

def tonnel_fetch_pages(gift_name: str):
    """list[dict]: {price_ton, model, backdrop, symbol, gift_id, gift_num, rarity}"""
    sort = json.dumps({"price": 1, "gift_id": 1})
    base_filter = {
        "price": {"$exists": True},
        "refunded": {"$ne": True},
        "buyer": {"$exists": False},
        "export_at": {"$exists": True},
        "gift_name": gift_name,
    }
    out, pages_ok = [], 0
    for page in range(1, TONNEL_PAGES + 1):
        jd = {
            "page": page, "limit": TONNEL_LIMIT, "sort": sort,
            "filter": json.dumps({**base_filter, "asset": "TON"}),
            "price_range": None, "user_auth": "",
        }
        try:
            r = requests.post(TONNEL_API, data=json.dumps(jd), headers=TONNEL_HEADERS,
                              impersonate="chrome", timeout=30)
        except Exception as e:
            print(f"  [!] Tonnel сеть {gift_name} p{page}: {type(e).__name__}")
            break
        if r.status_code != 200:
            print(f"  [!] Tonnel HTTP {r.status_code} на {gift_name} p{page}")
            time.sleep(random.uniform(40.0, 65.0))
            break
        try:
            items = r.json()
        except Exception:
            break
        if not isinstance(items, list) or not items:
            break
        pages_ok += 1
        for it in items:
            price = it.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            if it.get("status") not in (None, "forsale"):
                continue
            model = (it.get("model") or "").strip()
            if not model or model == "—":
                continue
            out.append({
                "price_ton": float(price),
                "model": model,
                "backdrop": it.get("backdrop") or "—",
                "symbol": it.get("symbol") or "—",
                "gift_id": it.get("gift_id") or "",
                "gift_num": it.get("gift_num") or "",
                "venue": "Tonnel",
            })
            if len(out) >= 90:
                break
        if len(items) < TONNEL_LIMIT:
            break
        time.sleep(random.uniform(1.5, 3.0))
    return out, pages_ok


# ---------------- Portals ----------------

_portals_coll_cache = {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_portals_all = None


def portals_all_collections():
    """Один раз тянем весь список коллекций Portals (~123 шт) и кешируем."""
    global _portals_all
    if _portals_all is not None:
        return _portals_all
    try:
        r = requests.get(f"{PORTALS_BASE}/collections", params={"limit": 1000, "offset": 0},
                         headers=UA_HEADERS, impersonate="chrome", timeout=60)
        if r.status_code == 200:
            _portals_all = r.json().get("collections", [])
            return _portals_all
    except Exception as e:
        print(f"  [!] Portals список коллекций: {type(e).__name__}")
    _portals_all = []
    return _portals_all


def portals_collection_id(gift_name: str):
    if gift_name in _portals_coll_cache:
        return _portals_coll_cache[gift_name]
    cands = portals_all_collections()
    want = _norm(gift_name)
    chosen = None
    for pr in (lambda n: n == want, lambda n: want in n or n in want):
        for c in cands:
            n = _norm(c.get("name") or "")
            if n and pr(n):
                chosen = c
                break
        if chosen:
            break
    if not chosen:
        print(f"  [i] Portals: коллекция не найдена: {gift_name}")
        return None
    _portals_coll_cache[gift_name] = (chosen.get("id"), chosen)
    return (chosen.get("id"), chosen)


def portals_fetch_pages(gift_name: str):
    """list[dict]: {price_ton, model, backdrop, symbol, tg_id}"""
    got = portals_collection_id(gift_name)
    if not got:
        print(f"  [i] Portals: коллекция не найдена: {gift_name}")
        return [], 0
    cid, coll = got
    out, pages_ok = [], 0
    for p in range(PORTALS_PAGES):
        params = {
            "offset": p * PORTALS_LIMIT, "limit": PORTALS_LIMIT,
            "sort_by": "price asc", "status": "listed",
            "collection_ids": cid, "exclude_bundled": "true",
            "premarket_status": "without_premarket",
        }
        try:
            r = requests.get(f"{PORTALS_BASE}/nfts/search", params=params,
                             headers=UA_HEADERS, impersonate="chrome", timeout=30)
        except Exception as e:
            print(f"  [!] Portals сеть {gift_name} p{p + 1}: {type(e).__name__}")
            break
        if r.status_code != 200:
            print(f"  [!] Portals HTTP {r.status_code} на {gift_name} p{p + 1}")
            break
        try:
            results = r.json().get("results", [])
        except Exception:
            break
        if not results:
            break
        pages_ok += 1
        for it in results:
            try:
                price = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            attrs = {a.get("type"): a for a in (it.get("attributes") or [])}
            m = attrs.get("model")
            if not m or not m.get("value"):
                continue
            rarity = float(m.get("rarity_per_mille") or 0) / 10.0
            out.append({
                "price_ton": price,
                "model": m["value"],
                "backdrop": (attrs.get("backdrop") or {}).get("value", "—"),
                "symbol": (attrs.get("symbol") or {}).get("value", "—"),
                "rarity": rarity,
                "tg_id": it.get("tg_id") or "",
                "venue": "Portals",
            })
            if len(out) >= 90:
                break
        if len(results) < PORTALS_LIMIT:
            break
        time.sleep(random.uniform(1.5, 3.0))
    return out, pages_ok


def parse_model_rarity(model: str) -> float:
    """'Puppy Pug (1%)' -> 1.0"""
    m = re.search(r"\(([\d.,]+)%\)", model)
    if not m:
        return 0.0
    return float(m.group(1).replace(",", "."))


def clean_rarity(rar: float) -> str:
    return f"{rar:.1f}".rstrip("0").rstrip(".")


# ---------------- Сравнение ----------------

def find_with_model_deals(all_lots, rate: float):
    """list[lot]: {venue, gift, model, rarity, backdrop, symbol, price_ton, fair_ton, gift_id, tg_id, gift_num}"""
    st = load_state()
    deals = []
    for gift, lots in all_lots.items():
        by_model = {}
        for l in lots:
            by_model.setdefault(l["model"], []).append(l)
        for model, mlots in by_model.items():
            if len(mlots) < MIN_SAMPLES:
                continue
            prices = [l["price_ton"] for l in mlots]
            median = statistics.median(prices)
            if median <= 0:
                continue
            mlots.sort(key=lambda x: x["price_ton"])
            floors = {}
            for l in mlots:
                floors.setdefault(l["venue"], l["price_ton"])
            for l in mlots:
                if l["price_ton"] * 100.0 <= median * (100.0 - DEAL_PCT):
                    rar = parse_model_rarity(model)
                    deals.append({
                        "gift": gift, "model": model, "rarity": rar,
                        "price_ton": l["price_ton"], "fair_ton": median,
                        "venue": l["venue"], "backdrop": l["backdrop"],
                        "symbol": l["symbol"], "gift_id": l.get("gift_id", ""),
                        "gift_num": l.get("gift_num", ""),
                        "tg_id": l.get("tg_id", ""),
                        "floors": floors,
                    })
                else:
                    break  # отсортировано — дальше дороже
    return deals


def lot_url(d: dict) -> str:
    if d["venue"] == "Tonnel" and d.get("gift_id"):
        return f"https://market.tonnel.network/gift/{d['gift_id']}"
    if d.get("tg_id"):
        return f"https://t.me/nft/{d['tg_id']}"
    return "https://market.tonnel.network/"


def to_lot_dict(d: dict, rate: float) -> dict:
    price_rub = int(round(d["price_ton"] * rate))
    market_rub = int(round(d["fair_ton"] * rate))
    if price_rub < 1:
        price_rub = 1
    if market_rub <= price_rub:
        market_rub = price_rub + 1
    profit = market_rub - price_rub
    pct = int(round(profit * 100 / market_rub))
    rar_txt = f" ({clean_rarity(d['rarity'])}%)" if d["rarity"] else ""
    title = f"{d['gift']} — {d['model']}{rar_txt}"
    tags = ["Подарок", d["gift"], d["model"], d["venue"]]
    if d.get("backdrop") and d["backdrop"] != "—":
        tags.append(d["backdrop"])
    tags = tags[:8]
    fl = d.get("floors") or {}
    fl_txt = " · ".join(f"{v}∈{k}" for k, v in fl.items()) if fl else ""
    lot_id = f"{d['venue'].lower()}-{d.get('gift_id', '') or d.get('tg_id', '') or d.get('gift_num', '')}"
    return {
        "id": lot_id[:100],
        "title": title[:200],
        "source": "GIFTS",
        "city": "Вся РФ",
        "price": price_rub,
        "market": market_rub,
        "tags": tags,
        "sellerName": d["venue"],
        "sellerRating": 4.8,
        "sellerReviews": 0,
        "description": (
            f"NFT-подарок Telegram · модель {d['model']} · фон {d['backdrop']} · "
            f"символ {d['symbol']} · №{d['gift_num'] or '—'} · "
            f"цена {d['price_ton']:.2f} TON, медиана рынка {d['fair_ton']:.2f} TON"
            + (f" ({d.get('floors') and len(d['floors']) or 0} маркетов)" if d.get('floors') else "")
            + (f" · floor: {fl_txt}" if fl_txt else "")
            + f" · скидка ~{pct}% от медианы"
        )[:1000],
        "url": lot_url(d),
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
            json=lots, timeout=90,
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
    watchlist = load_watchlist()
    print(f"[{datetime.utcnow().isoformat()}Z] gifts_scan старт: коллекций {len(watchlist)}")
    rate = ton_rub_rate()
    print(f"курс TON: {rate:.1f} ₽")
    all_lots = {}
    ok = []
    for gift in watchlist:
        t_lots, _ = tonnel_fetch_pages(gift)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        p_lots, _ = portals_fetch_pages(gift)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        total = t_lots + p_lots
        all_lots[gift] = total
        venue_t = sum(1 for l in t_lots)
        venue_p = sum(1 for l in p_lots)
        ok.append((gift, len(total), venue_t, venue_p))
        print(f"  {gift}: {len(total)} лотов (Tonnel {venue_t} / Portals {venue_p})")
    deals = find_with_model_deals(all_lots, rate)
    # за прогон не спамим ленту: топ-20 по размеру скидки, уникально по (gift|model|venue)
    deals.sort(key=lambda d: 1 - d["price_ton"] / d["fair_ton"])
    seen = set()
    uniq = []
    for d in deals:
        k = (d["gift"], d["model"], d["venue"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(d)
    deals = uniq[:20]
    print(f"выгодных: {len(deals)}")
    lots = [to_lot_dict(d, rate) for d in deals]
    if "--no-push" not in sys.argv:
        saved, skipped = push_to_server(lots)
        print(f"пуш: saved={saved} skipped={skipped}")
    else:
        print(f"пуш: off (--no-push), лотов к пушу: {len(lots)}")


if __name__ == "__main__":
    main()
