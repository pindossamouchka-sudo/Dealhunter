# -*- coding: utf-8 -*-
"""
gifts_scan — «gift sniper» для DealHunter.

Мониторим NFT-подарки Telegram сразу на трёх маркетах:
  • Tonnel   (market.tonnel.network) — gifts2.tonnel.network/api/pageGifts
  • Portals  (portal-market.com)     — порталовский REST
  • Fragment (fragment.com)          — каталог без кошелька + HTML карточек

Честная «рыночная планка» модели — приоритет реальным СДЕЛКАМ, а не листингам:
  1) история продаж Tonnel через /api/giftActivity (SALE/INTERNAL_SALE/AUCTION
     по предметам; опрашиваем самые дешёвые лоты коллекции) → медиана сделок
     с IQR-фильтром выбросов (порог MIN_SALES сделок);
  2) если сделок пока мало — добавляем цены «исчезнувших» лотов (маркировка gone:
     купили/сняли) и берём min(медиана, медиана ask-листингов);
  3) если данных всё ещё нет — медиана ask-листингов модели по ВСЕМ трём
     маркетам (Tonnel + Portals + Fragment), консервативный режим.
Лот объявляем выгодным, если его цена ≤ (100 - DEAL_PCT)% от планки
И чистый профит после комиссий ≥ MIN_NET_RUB. Спам-цены «минимального соседа»
больше не завышают оценку — перекос таких листингов глушится медианой и IQR.
Ссылки — прямые: Tonnel t.me/tonnel_network_bot/gift?startapp=<id>, Portals Store t.me/portals/market, Fragment /gift/<slug>.
"""
import os
import sys
import time
import json
import random
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
# (в quick-режиме обрезается до 1 страницы — см. apply_mode)
TONNEL_PAGES = int(os.getenv("GIFT_TONNEL_PAGES", "3"))
TONNEL_LIMIT = 30       # сервер Tonnel не принимает limit > 30
PORTALS_PAGES = int(os.getenv("GIFT_PORTALS_PAGES", "3"))
PORTALS_LIMIT = 30

# Порог «выгодности»: цена ≤ планки минус DEAL_PCT%
# планка = честная рыночная цена (медиана реальных сделок, см. докстринг)
DEAL_PCT = 12.0
# Оценка комиссий при перепродаже (доля от цены продажи)
RESELL_FEE_PCT = 10.0
# Минимальная чистая прибыль в ₽ после вычета комиссий
MIN_NET_RUB = 500

# Минимальное число РЕАЛЬНЫХ сделок (Sale-событий), чтобы считать медиану честной
MIN_SALES = 5
# Г) Минимум данных для оценки планки: если сделок нет — нужно ≥3 сопоставимых
# лотов той же модели (иначе «мало данных», лот не оцениваем)
MIN_SAMPLES = 3
# Д) Ловушки (защита): скидка больше этой доли = подозрительный лот (ошибка
# цены/приманка/устаревший), не сделка
MAX_DISCOUNT = 0.50
# Сколько самых дешёвых лотов коллекции опрашиваем на историю сделок (Tonnel)
ACTIVITY_SAMPLE = int(os.getenv("GIFT_ACT_SAMPLE", "8")) or 8
# Сколько дней храним точки истории сделок и макс. точек на модель
SALES_TTL_DAYS = 90
SALES_POINTS_MAX = 300
# Типы событий Tonnel giftActivity, считаемые реальными сделками ТОЛЬКО
# по фиксированной цене. Аукционы (AUCTION/BID) и премаркет сюда не входят:
# цена аукциона — результат торгов, а премаркет — не «купить сейчас».
ACTIVITY_SALE_TYPES = {"SALE", "INTERNAL_SALE"}
# Антибот-пауза между запросами
SLEEP_MIN, SLEEP_MAX = 2.0, 4.5
# Режимы прогонов: quick (короткий cron) / full (ручной или дефолт).
# Скольким коллекциям за quick-прогон (каталог обходит за несколько циклов)
QUICK_SLICE = int(os.getenv("GIFT_QUICK_SLICE", "8"))
# Сколько дешёвых лотов Fragment открываем по-детально (чтобы узнать модель)
FRAGMENT_TOP = 2

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
        if isinstance(d, dict):
            d.setdefault("floors", {})
            d.setdefault("samples", {})
            d.setdefault("sales", {})
            d.setdefault("cursor", 0)
            return d
    except Exception:
        pass
    return {"v": 3, "floors": {}, "samples": {}, "sales": {}, "cursor": 0, "last_run": None}


def save_state(st):
    st["v"] = 3
    st["last_run"] = datetime.utcnow().isoformat()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def prune_sales(state):
    """Чистим историю сделок: TTL SALES_TTL_DAYS, кому больше — отрезаем хвост."""
    cutoff = datetime.utcnow().timestamp() - SALES_TTL_DAYS * 86400
    for key in list(state.get("sales", {}).keys()):
        pts = state["sales"][key]
        keep = []
        for ts, price, kind in pts:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                t = datetime.utcnow().timestamp()  # неизвестное время — считаем свежим
            if t >= cutoff:
                keep.append((ts, price, kind))
        keep.sort(key=lambda p: p[0])
        keep = keep[-SALES_POINTS_MAX:]
        if keep:
            state["sales"][key] = [list(p) for p in keep]
        else:
            del state["sales"][key]


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
    out, pages_ok, complete_t = [], 0, False
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
            complete_t = True
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
            # исключаем НЕ листинги: аукционы (обычный и голландский), премаркет,
            # залог/аренда, рефанды. Оцениваем только «купить сейчас».
            if (it.get("auction") or it.get("dutchAuctionData")
                    or it.get("premarketData") or it.get("underLoan")
                    or it.get("refunded")):
                continue
            model = (it.get("model") or "").strip()
            if not model or model == "—":
                continue
            rar = parse_model_rarity(model)
            model_base = re.sub(r"\s*\([\d.,]+%\)", "", model).strip()
            if not model_base:
                continue
            out.append({
                "price_ton": float(price),
                "model": model_base,
                "rarity": rar,
                "backdrop": it.get("backdrop") or "—",
                "symbol": it.get("symbol") or "—",
                "gift_id": it.get("gift_id") or "",
                "gift_num": it.get("gift_num") or "",
                "venue": "Tonnel",
            })
            if len(out) >= 90:
                break
        if len(items) < TONNEL_LIMIT:
            complete_t = True
            break
        time.sleep(random.uniform(1.5, 3.0))
    return out, pages_ok, complete_t


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
        return [], 0, False
    cid, coll = got
    out, pages_ok, complete_p = [], 0, False
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
            complete_p = True
            break
        pages_ok += 1
        for it in results:
            try:
                price = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            if it.get("bundle_id") or it.get("premarket_price"):
                continue  # бандлы и премаркет не листинги «купить сейчас»
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
                "id": it.get("id") or "",   # UUID для deep-link Portals
                "venue": "Portals",
            })
            if len(out) >= 90:
                break
        if len(results) < PORTALS_LIMIT:
            complete_p = True
            break
        time.sleep(random.uniform(1.5, 3.0))
    return out, pages_ok, complete_p


def build_universe():
    """Э1.4: весь рынок = watchlist (приоритет в начале списка) + ВСЕ коллекции
    из API Portals + ВСЕ имена каталога Fragment. Возврат list[str]."""
    names, seen = [], set()

    def add(n):
        n = (n or "").strip()
        if n and _norm(n) not in seen:
            seen.add(_norm(n))
            names.append(n)

    for n in load_watchlist():      # приоритет: watchlist сканируем первым
        add(n)
    pcoll = portals_all_collections()
    for c in sorted(pcoll, key=lambda x: (x.get("name") or "")):
        add(c.get("name"))
    fraw = fragment_raw_collections()
    fnames = sorted(nm for nm, slug in fraw if nm and slug)
    for n in fnames:
        add(n)
    return names


def parse_model_rarity(model: str) -> float:
    """'Puppy Pug (1%)' -> 1.0"""
    m = re.search(r"\(([\d.,]+)%\)", model)
    if not m:
        return 0.0
    return float(m.group(1).replace(",", "."))


# ---------------- Fragment ----------------

_FRAGMENT_FIX = {
    # watchlist-имя (norm) -> fragment-slug, где автоматика не справляется
}

_fragment_raw = None
_fragment_slugs = None


def fragment_raw_collections():
    """[(name, slug)] весь каталог Fragment; заодно (один раз за прогон)
    строит {norm_имени: slug} с поправкой на ед./мн. число."""
    global _fragment_raw, _fragment_slugs
    if _fragment_raw is None:
        try:
            import asyncio
            if sys.platform.startswith("win"):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            from fragment_api import FragmentCatalog
            cols = asyncio.run(FragmentCatalog().list_collections())
            _fragment_raw = [((c.get("name") or "").strip(), (c.get("slug") or "").strip())
                             for c in cols]
        except Exception as e:
            print(f"  [!] Fragment каталог: {type(e).__name__} {e}")
            _fragment_raw = []
        out = {}
        for nm, slug in _fragment_raw:
            n = _norm(nm)
            if not n or not slug:
                continue
            out.setdefault(n, slug)
            out.setdefault(n + "s", slug)
            out.setdefault(n + "es", slug)
            if n.endswith("s"):
                out.setdefault(n[:-1], slug)
                if n.endswith("es"):
                    out.setdefault(n[:-2], slug)
        _fragment_slugs = out
    return _fragment_raw


def fragment_slugs():
    """{norm_имени_коллекции: fragment_slug} — тянем один раз за прогон."""
    if _fragment_slugs is None:
        fragment_raw_collections()
    return _fragment_slugs or {}


def fragment_gift_page_attrs(slug: str):
    """(model, backdrop, symbol) — парсим HTML карточки Fragment."""
    try:
        r = requests.get(f"https://fragment.com/gift/{slug}",
                         headers=UA_HEADERS, impersonate="chrome", timeout=40)
        if r.status_code != 200:
            return None
        rows = re.findall(
            r'<div class="table-cell">(Model|Backdrop|Symbol)</div>\s*</td>\s*<td>\s*'
            r'<div class="table-cell">\s*<div class="table-cell-value tm-value">\s*'
            r'<a[^>]*>([^<]+)</a>\s*<span class="tm-rarity">\s*([\d.,]+)%\s*</span>',
            r.text, re.S)
        d = {t: (v.strip(), float(rr.replace(",", ""))) for t, v, rr in rows}
        return d
    except Exception:
        return None


def fragment_fetch(gift_name: str):
    """list[dict] lot-ов Fragment для одной коллекции (дешёвые сверху)."""
    slugs = fragment_slugs()
    want = _norm(gift_name)
    slug = slugs.get(want) or _FRAGMENT_FIX.get(want)
    if not slug:
        print(f"  [i] Fragment: коллекция не найдена: {gift_name}")
        return []
    try:
        import asyncio
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        from fragment_api import FragmentCatalog
        catalog = FragmentCatalog()
        items = asyncio.run(catalog.list_gifts(collection_slug=slug, limit=20, sort="price"))
    except Exception as e:
        print(f"  [!] Fragment {gift_name}: {type(e).__name__} {e}")
        return []
    out = []
    attrs_by_slug = {}
    top = 0
    for it in items:
        try:
            price = float(it.get("price_ton"))
        except (TypeError, ValueError):
            continue
        fslug = it.get("slug") or ""
        if price <= 0 or not fslug:
            continue
        if top < FRAGMENT_TOP:
            attrs = fragment_gift_page_attrs(fslug)
            time.sleep(random.uniform(1.0, 2.0))
            if attrs and attrs.get("Model"):
                (mv, mr) = attrs["Model"]
                (bv, _br) = attrs.get("Backdrop", ("—", 0))
                (sv, _sr) = attrs.get("Symbol", ("—", 0))
                attrs_by_slug[fslug] = (mv, bv, sv, mr)
                top += 1
            else:
                # карточка не открылась — не бесконечно рвёмся, пробуем следующую
                continue
        if fslug not in attrs_by_slug:
            # знаем только цену — модель неизвестна, в медиану не пойдёт, идём мимо
            continue
        mv, bv, sv, mr = attrs_by_slug[fslug]
        out.append({
            "price_ton": price,
            "model": mv,
            "backdrop": bv or "—",
            "symbol": sv or "—",
            "rarity": mr,
            "gift_num": it.get("number") or "",
            "venue": "Fragment",
            "slug": fslug,
        })
    return out


def clean_rarity(rar: float) -> str:
    return f"{rar:.1f}".rstrip("0").rstrip(".")


# ---------------- История реальных сделок (Tonnel) ----------------

def tonnel_gift_activity(gift_name: str, gift_num):
    """История сделок конкретного ПРЕДМЕТА (по номеру): SALE/INTERNAL_SALE/AUCTION
    с ценой в TON и датой. Анонимный endpoint, должен отвечать 200.
    Возвращает [(iso_ts, price_ton, model), ...]."""
    if gift_num in (None, ""):
        return []
    try:
        r = requests.post(
            "https://gifts2.tonnel.network/api/giftActivity",
            data=json.dumps({"gift_name": gift_name, "gift_num": int(gift_num)}),
            headers=TONNEL_HEADERS, impersonate="chrome", timeout=40)
        if r.status_code != 200:
            return []
        acts = r.json().get("activities") or []
    except Exception:
        return []
    out = []
    for a in acts:
        if (a.get("type") or "").upper() not in ACTIVITY_SALE_TYPES:
            continue
        try:
            price = float(a.get("price") or 0)
        except (TypeError, ValueError):
            continue
        model = (a.get("model") or "").strip()
        if price > 0 and model and model != "—":
            out.append((str(a.get("timestamp") or ""), price, model))
    return out


def record_sale_points(state, gift: str, model: str, price_ton: float, ts: str, kind: str):
    """Один факт «по этой цене продавалась/исчезала вещь той же модели»."""
    key = f"{gift}|{model}".lower()
    bucket = state.setdefault("sales", {}).setdefault(key, [])
    rec = [ts, round(float(price_ton), 2), kind]
    if rec not in bucket:
        bucket.append(rec)
        return 1
    return 0


def collect_sales_history(all_lots: dict, state: dict, budget: int = None):
    """Опрашиваем историю сделок у ACTIVITY_SAMPLE самых дешёвых лотов Tonnel
    каждой коллекции: события SALE этого предмета = реальные цены сделок
    его модели (модели может отличаться от модели текущего листинга — пишем
    именно по модели из события). История копится между прогонами.
    budget — максимум опросов giftActivity за прогон (анти-бан, чтобы полный
    прогон по всему рынку не растягивался на час из-за истории)."""
    if budget is None:
        budget = int(os.getenv("GIFT_SAMPLE_BUDGET", "150"))
    added, new_gift_models, spent = 0, set(), 0
    for gift, lots in all_lots.items():
        if spent >= budget:
            print(f"  [i] история: бюджет {budget} опросов исчерпан, прогон продолжает без неё")
            break
        picked = sorted(
            [l for l in lots
             if l.get("venue") == "Tonnel" and l.get("gift_num") not in (None, "")
             and l.get("price_ton", 0) > 0],
            key=lambda l: l["price_ton"])[:ACTIVITY_SAMPLE]
        for l in picked:
            if spent >= budget:
                break
            spent += 1
            evs = tonnel_gift_activity(gift, l.get("gift_num"))
            time.sleep(random.uniform(1.0, 2.0))
            for ts, price, model in evs:
                if record_sale_points(state, gift, model, price, ts, "api"):
                    added += 1
                    new_gift_models.add((gift, model))
    prune_sales(state)
    return added, new_gift_models


def median_sample(vals):
    vs = sorted(vals)
    n = len(vs)
    if not n:
        return 0.0
    mid = n // 2
    return float(vs[mid]) if n % 2 else (vs[mid - 1] + vs[mid]) / 2.0


def iqr_median(vals):
    """Медиана с IQR-фильтром выбросов: отрезаем всё за пределами
    [Q1 - 1.5*IQR, Q3 + 1.5*IQR]; если после фильтра пусто — сырая медиана."""
    vs = sorted(float(v) for v in vals if v > 0)
    if not vs:
        return 0.0
    q1 = median_sample(vs[: max(1, len(vs) // 2)] if len(vs) > 3 else vs)
    q3 = median_sample(vs[len(vs) // 2:] if len(vs) > 3 else vs)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    core = [v for v in vs if lo <= v <= hi] or vs
    return median_sample(core)

# ---------------- Оценка ----------------
# Честная планка модели (market_reference) = min из двух источников:
#  1) флор соседа той же площадки — других лотов той же модели там, где
#     продаётся лот (≥2 соседей); лот должен быть дешевле ВТОРОГО по
#     минимальности — иначе undercut-продажа нереальна;
#  2) медиана РЕАЛЬНЫХ сделок (Tonnel giftActivity + исчезнувшие лоты) —
#     учитывается даже при 1-2 точках: продано по 280 — значит asks по 400
#     соседей ничего не стоят.
# Чужие дорогие asks (Tonnel 600 при Portals floor 420) планкой больше
# не служат: только соседи той же площадки или факты продаж.
# Выгодный лот: цена ≤ планка*(1-DEAL_PCT) И чистыми после комиссий ≥ MIN_NET_RUB.

RARE_PCT = 2.0             # порог редкости модели/фона/символа (%)
# Минимум других лотов той же модели на той же площадке, чтобы верить их флору
MIN_LOCAL_PEERS = 2


def market_reference(gift: str, model: str, mlots: list, state: dict,
                     exclude_lot=None):
    """(planca_ton | None, source_txt, quality) — качества: good/ok/weak.

    Планка = min из:
      • флор соседа той же площадки — других лотов той же модели там, где
        продаётся лот (нужно ≥2 соседей, чтобы верить); лот дешевле ВТОРОГО
        по минимальности только тогда, когда мы чуть уточнённо считаем
        соседа «рынком»;
      • медиана РЕАЛЬНЫХ сделок (Tonnel giftActivity API + исчезнувшие лоты)
        — учитывается при любом числе точек: даже пара свежих продаж
        задавливает завышенные asks соседей.

    exclude_lot — оцениваемый лот (в планку не входит)."""
    key = f"{gift}|{model}".lower()
    pts = state.get("sales", {}).get(key, [])
    api = [float(p) for ts, p, k in pts if k == "api" and p > 0]
    gone = [float(p) for ts, p, k in pts if k == "gone" and p > 0]

    others = [l for l in mlots
              if l is not exclude_lot
              and l.get("price_ton", 0) > 0]

    floor_ref, floor_src = None, ""
    same_venue = None
    if exclude_lot is not None:
        v = exclude_lot.get("venue")
        same_venue = sorted(float(l["price_ton"]) for l in others
                            if l.get("venue") == v)
        if len(same_venue) >= MIN_LOCAL_PEERS:
            floor_ref = same_venue[0]
            floor_src = f"флор соседа {v} ({len(same_venue)})"
    if not floor_ref and len(others) >= MIN_LOCAL_PEERS + 1:
        # соседей «своих» нет — самый дешёвый чужой лот той же модели
        floor_ref = min(float(l["price_ton"]) for l in others)
        floor_src = f"флор соседа другой площадки ({len(others)})"

    sale_vals = api + gone
    sale_ref = iqr_median(sale_vals) if sale_vals else 0.0

    # Г) минимальные требования к данным (MIN_SAMPLES): без РЕАЛЬНЫХ сделок
    # верим планке только если у модели есть хотя бы 3 сопоставимых лота.
    # Не изобретаем цену из двух сомнительных листингов.
    if not sale_vals and len(others) < MIN_SAMPLES:
        return None, "мало данных (нужно ≥%d лотов модели или продажи)" % MIN_SAMPLES, "weak"

    cand, labels = [], []
    if floor_ref:
        cand.append(floor_ref)
        labels.append(floor_src)
    if sale_ref > 0:
        cand.append(sale_ref)
        n_api, n_gone = len(api), len(gone)
        sale_label = (f"медиана сделок ({n_api})"
                      if not n_gone else f"сделки+исчезновения ({n_api}+{n_gone})")
        labels.append(sale_label)

    ref = min(cand) if cand else None
    if not ref:
        return None, "", ""
    src = labels[cand.index(ref)] if ref in cand else labels[-1]
    if ref == sale_ref and float(floor_ref or 0) > ref:
        src += " (продажи ниже соседа)"
    # жёсткий кэп: чужой (любой площадки) самый дешёвый лот той же модели.
    # Лот должен быть дешевле 2-го по минимальности вообще, без исключений —
    # иначе «флор соседа 400» врут, когда на Tonnel та же модель за 10 TON.
    if others:
        cross = min(float(l["price_ton"]) for l in others)
        if cross < ref:
            ref = round(cross, 2)
            src += " (гашено чужим флором)"
    quality = "good" if len(api) >= MIN_SALES else ("ok" if sale_vals else "weak")
    return round(ref, 2), src, quality


def evaluate_lot(price_ton: float, reference_ton: float, rate: float,
                 fee_pct: float = None, min_discount: float = None,
                 max_discount: float = None):
    """Чистая оценка против честной планки: дисконт ≥ DEAL_PCT (мало — не сделка),
    дисконт ≤ MAX_DISCOUNT (слишком велик — подозрительный лот), чистый профит
    после комиссий ≥ MIN_NET_RUB. None -> отбросить (с описанием в reject_reason)."""
    if not reference_ton or reference_ton <= 0 or not price_ton or price_ton <= 0:
        return None
    fee_pct = RESELL_FEE_PCT if fee_pct is None else fee_pct
    min_discount = DEAL_PCT / 100.0 if min_discount is None else min_discount
    max_discount = MAX_DISCOUNT if max_discount is None else max_discount
    if price_ton > reference_ton * (1.0 - min_discount) + 1e-9:
        return None
    discount = (reference_ton - price_ton) / reference_ton
    if discount > max_discount + 1e-9:
        # приманка/ошибка цены/устаревший лот: «рынок» не дешевле вдвое
        return None
    net_sell = reference_ton * (1.0 - fee_pct / 100.0)
    profit_ton = net_sell - price_ton
    if profit_ton * rate < MIN_NET_RUB:
        return None
    return {
        "reference_ton": reference_ton,
        "fair_ton": reference_ton,
        "discount": round(discount, 3),
        "profit_ton": round(profit_ton, 2),
        "profit_rub": int(round(profit_ton * rate)),
        "net_sell_ton": round(net_sell, 2),
    }


def trait_excuse(lot: dict, mlots: list) -> str:
    """В) Поправка на признаки: если дешевизна лота объясняется ЧАСТЫМ фоном,
    это законная дешевизна (фоновый лот и должен быть дешевле), а не сделка.

    Правило (все три условия):
      • фон лота встречается у ≥30% других лотов модели  → фон «частый»;
      • медиана цен лотов с этим же фоном системно ниже лотов других фонов
        этой же модели (разрыв ≥ 15%);
      • наш лот НЕ дешевле типичной цены своего фона (скидка к фоновой медиане
        меньше DEAL_PCT) — видимые «десятки процентов» происходят от более
        дорогих фонов, а не от выгодности этого лота.

    Возвращает текст-причину для отбраковки или '' (фон не объясняет
    дешевизну → может быть сделкой)."""
    others = [l for l in mlots
              if l is not lot and l.get("price_ton", 0) > 0]
    if len(others) < 2 * MIN_SAMPLES:
        return ""          # данных о фоне слишком мало, чтобы судить
    bg = (lot.get("backdrop") or "—").strip()
    if not bg or bg == "—":
        return ""
    same_bg = [float(l["price_ton"]) for l in others
               if (l.get("backdrop") or "—").strip() == bg]
    if len(same_bg) < MIN_SAMPLES:
        return ""          # фон нечастый: дешевизна им не объясняется
    share = len(same_bg) / float(len(others))
    if share < 0.30:
        return ""
    bg_med = median_sample(same_bg)
    rest_med = median_sample([float(l["price_ton"]) for l in others
                              if (l.get("backdrop") or "—").strip() != bg])
    if bg_med <= 0 or rest_med <= 0:
        return ""
    if bg_med / rest_med >= 0.85:
        return ""          # фон не дешевле остальной модели — не объясняет
    if lot["price_ton"] < bg_med * (1.0 - DEAL_PCT / 100.0):
        return ""          # лот дешевле даже фоновой нормы — реальная сделка
    return ("частый фон (%d%% лотов модели, фоновые ~%d%% дешевле) — "
            "дешевизна как у всех на этом фоне, а не сделка"
            % (int(round(share * 100)),
               int(round((1.0 - bg_med / rest_med) * 100))))


def rare_reason(model: str, lot: dict, mlots: list):
    """Правило: редкость модели/фона/символа < RARE_PCT% (доля в выборке).
    Возвращает ярлык или ''."""
    rar = parse_model_rarity(model)
    if rar and 0 < rar < RARE_PCT:
        return "редкая модель"
    n = max(1, len(mlots))
    bg_hits = sum(1 for l in mlots if l.get("backdrop") == lot.get("backdrop"))
    if (bg_hits / n * 100.0) < RARE_PCT:
        return "редкий фон"
    sym = lot.get("symbol", "—")
    if sym and sym != "—":
        sym_hits = sum(1 for l in mlots if l.get("symbol") == sym)
        if (sym_hits / n * 100.0) < RARE_PCT:
            return "редкий символ"
    return ""


def reject_reason(price_ton, reference_ton, rate: float = 0.0, fee_pct=None, min_discount=float(DEAL_PCT) / 100.0):
    """Почему лот бракуется (для отладочного лога):
    нет флора / цена >= флора / скидка < порога / профит мал."""
    if reference_ton is None or reference_ton <= 0:
        return "нет планки (мало данных)"
    if price_ton is None or price_ton <= 0:
        return "нет цены"
    if price_ton >= reference_ton:
        return "цена >= планки"
    if price_ton > reference_ton * (1.0 - min_discount) + 1e-9:
        return "скидка < порога"
    discount = (reference_ton - price_ton) / reference_ton
    if discount > MAX_DISCOUNT + 1e-9:
        return "подозрительная скидка >%d%%" % round(MAX_DISCOUNT * 100)
    net_profit_rub = (reference_ton * (1.0 - (RESELL_FEE_PCT if fee_pct is None else fee_pct) / 100.0)
                      - price_ton) * (rate if rate > 0 else TON_RUB_FALLBACK)
    if net_profit_rub < MIN_NET_RUB:
        return "профит < %d₽" % MIN_NET_RUB
    return "?"


REJECT_LOG = False  # включается в --debug-reject


def debug_reject_line(gift, model, l, reference_ton, reason):
    fl = ("НЕТ ФЛОРА" if not reference_ton or reference_ton <= 0
          else "%.2f TON" % reference_ton)
    if reference_ton and reference_ton > 0:
        d = (reference_ton - l["price_ton"]) / reference_ton * 100.0
        ds = "%+.1f%%" % d
    else:
        ds = "—"
    print("[отбраковано] %s | %s | %.2f TON | floor=%s | disc=%s | причина: %s"
          % (gift, model, l["price_ton"], fl, ds, reason))


def find_with_model_deals(all_lots, rate: float, state=None):
    """Оценка против честной планки модели: реальные сделки (приоритет),
    консервативная медиана ask-листингов при малой выборке. Ставка
    «перебей соседа» изъята — минимум листингом не измеряется."""
    import collections as _c
    state = state if state is not None else {}
    diag = _c.Counter()
    reject_cnt = _c.Counter()
    deals = []
    for gift, lots in all_lots.items():
        diag['lots'] += len(lots)
        by_model = {}
        for l in lots:
            if l.get("price_ton", 0) > 0 and l.get("model"):
                by_model.setdefault(l["model"], []).append(l)
        for model, mlots in by_model.items():
            rar = parse_model_rarity(model)
            floors = {}
            for l in mlots:
                floors.setdefault(l["venue"], l["price_ton"])
            for l in mlots:
                rare = rare_reason(model, l, mlots)
                # планка всегда считается БЕЗ оцениваемого лота (self-исключение):
                # иначе самый дешёвый лот сам себе задаёт «флор соседа».
                reference_ton, src, quality = market_reference(
                    gift, model, mlots, state, exclude_lot=l)
                if not reference_ton:
                    if REJECT_LOG:
                        debug_reject_line(gift, model, l, 0, "мало данных (нет соседей/сделок)")
                    reject_cnt['нет данных о рынке'] += 1
                    diag['отброшено'] += 1
                    continue
                res = evaluate_lot(l["price_ton"], reference_ton, rate)
                if not res:
                    reason = reject_reason(l["price_ton"], reference_ton, rate)
                    reject_cnt[reason] += 1
                    if REJECT_LOG:
                        debug_reject_line(gift, model, l, reference_ton, reason)
                    diag['отброшено'] += 1
                    continue
                # В) признаковая поправка: дешёвый лот на ЧАСТОМ дешёвом фоне —
                # это не сделка (дешевизна законная, у всех на фоне так)
                excuse = trait_excuse(l, mlots)
                if excuse:
                    reject_cnt["частый фон (не сделка)"] += 1
                    if REJECT_LOG:
                        debug_reject_line(gift, model, l, reference_ton, excuse)
                    diag['отброшено'] += 1
                    continue
                d = {
                    "gift": gift, "model": model, "rarity": rar,
                    "price_ton": l["price_ton"], "fair_ton": res["fair_ton"],
                    "reference_ton": res["reference_ton"],
                    "fair_src": "-%d%% от %s" % (round(res["discount"] * 100), src),
                    "fair_quality": quality,
                    "rare": rare,
                    "venue": l["venue"], "backdrop": l["backdrop"],
                    "symbol": l["symbol"], "gift_id": l.get("gift_id", ""),
                    "gift_num": l.get("gift_num", ""),
                    "tg_id": l.get("tg_id", ""),
                    "portals_id": l.get("id", ""),
                    "net_rub": res["profit_rub"],
                    "floors": floors,
                }
                deals.append(d)
    diag['сделок'] = len(deals)
    print('[diag]', dict(diag))
    if reject_cnt:
        print('[отбраковано по причинам]', dict(reject_cnt))
    deals.sort(key=lambda d: -d["net_rub"])
    if len(deals) > 40:
        deals = deals[:40]
    return deals


def lot_url(d: dict) -> str:
    if d["venue"] == "Fragment" and d.get("slug"):
        return f"https://fragment.com/gift/{d['slug']}"
    if d["venue"] == "Tonnel" and d.get("gift_id"):
        # прямой deep-link на конкретный лот в мини-аппе Tonnel (market.tonnel.network из РФ часто не грузится)
        return f"https://t.me/tonnel_network_bot/gift?startapp={d['gift_id']}"
    if d["venue"] == "Portals":
        # deep-link из самого клиента Portals: бот @portals_market_bot, /market?startapp=gift_<uuid>
        uid = d.get("portals_id") or ""
        if uid:
            return f"https://t.me/portals_market_bot/market?startapp=gift_{uid}"
        return "https://t.me/portals_market_bot/market"
    if d.get("tg_id"):
        return f"https://t.me/nft/{d['tg_id']}"
    return "https://t.me/tonnel_network_bot"


def to_lot_dict(d: dict, rate: float) -> dict:
    price_rub = int(round(d["price_ton"] * rate))
    market_rub = int(round(d["fair_ton"] * rate))
    if price_rub < 1:
        price_rub = 1
    if market_rub <= price_rub:
        market_rub = price_rub + 1
    profit = market_rub - price_rub
    pct = int(round(profit * 100 / market_rub))
    rar_txt = f" ({clean_rarity(d.get('rarity', 0))}%)" if d.get("rarity") else ""
    title = f"{d['gift']} — {d['model']}{rar_txt}"
    tags = ["Подарок", d["gift"], d["model"], d["venue"]]
    if d.get("backdrop") and d["backdrop"] != "—":
        tags.append(d["backdrop"])
    tags = tags[:8]
    if d["venue"] == "Fragment":
        lot_id = f"fragment-{d.get('slug', '') or d.get('gift_num', '')}"
    elif d["venue"] == "Tonnel":
        lot_id = f"tonnel-{d.get('gift_id', '') or d.get('gift_num', '')}"
    else:
        lot_id = f"portals-{d.get('tg_id', '') or d.get('gift_num', '')}"
    extra = ""
    if d["venue"] == "Portals":
        extra = " · покупка: кнопка «Забрать» открывает лот прямо в Portals"
    elif d["venue"] == "Tonnel":
        extra = " · покупка: кнопка «Забрать» открывает лот в боте Tonnel"
    if d.get("net_rub"):
        extra += (f" · ориентировочно чистыми ~{d['net_rub']:,} ₽ после "
                  f"комиссии площадки ~{RESELL_FEE_PCT:.0f}%").replace(",", " ")
    if d.get("fair_quality") in ("weak", "ok"):
        extra += " · оценка грубая (мало сделок по модели)"
    estimate_date = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
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
            f"символ {d['symbol']} · №{d.get('gift_num') or '—'} · "
            f"цена {d['price_ton']:.2f} TON, ориентир по рынку ~{d['fair_ton']:.2f} TON "
            f"(оценка DefHunt; источник: {d.get('fair_src') or 'медиана листингов'}; "
            f"дата расчёта {estimate_date}; площадки Tonnel+Portals+Fragment) "
            f"· скидка ~{pct}% (ориентировочно){extra}"
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


def ping_server():
    """Держим сервер тёплым (Render free засыпает без HTTP-запросов)."""
    import httpx
    try:
        httpx.get(f"{SERVER_URL}/health", timeout=20)
    except Exception:
        pass


def mark_gone_lots(all_lots: dict, fetch_ok: dict, lots: list, state=None, rate: float = 0.0):
    """Лоты GIFTS, которые были на проде, а теперь их НЕТ в полном снимке
    конкретной площадки — реально купили/сняли. Помечаем gone (уйдут с ленты),
    их цены записываем в историю как суррогат сделки (kind='gone').
    fetch_ok: gift -> {venue: снимок_прочитан_целиком} — если площадка не прочитана
    целиком, маркировку для её лотов не делаем (иначе заметём живые лоты)."""
    if "--no-push" in sys.argv or not WORKER_SECRET:
        return
    import httpx
    fresh_by_gift = {}
    for gift, gift_lots in all_lots.items():
        for l in gift_lots:
            v = l.get("venue", "")
            if v == "Tonnel":
                vid = f"tonnel-{l.get('gift_id', '') or l.get('gift_num', '')}"
            elif v == "Fragment":
                vid = f"fragment-{l.get('slug', '') or l.get('gift_num', '')}"
            else:
                vid = f"portals-{l.get('tg_id', '') or l.get('gift_num', '')}"
            fresh_by_gift.setdefault(gift, {}).setdefault(v, set()).add(vid[:100])
    try:
        r = httpx.get(
            f"{SERVER_URL}/api/worker/lots-ids",
            params={"source": "GIFTS"},
            headers={"X-Worker-Secret": WORKER_SECRET}, timeout=60,
        )
        if r.status_code != 200:
            return
        prod_rows = r.json().get("rows", [])
        gone = []
        now_iso = datetime.utcnow().isoformat() + "Z"
        for row in prod_rows:
            lot_id = row.get("id", "")
            title = row.get("title", "")
            gift = title.split(" —")[0].strip()
            model = title.split(" —", 1)[1].split(" (")[0].strip() if " —" in title else ""
            vk = {v for v in ("portals", "tonnel", "fragment") if str(lot_id).startswith(v + "-")}
            vk2 = next(iter(vk), "")
            venue = {"portals": "Portals", "tonnel": "Tonnel", "fragment": "Fragment"}.get(vk2)
            if not venue or venue == "Fragment":
                continue  # Fragment маркировать нечем (снимок без флага полноты)
            if not fetch_ok.get(gift, {}).get(venue):
                continue  # площадку не прочитали целиком — снимок неполный
            fresh_set = fresh_by_gift.get(gift, {}).get(venue, set())
            if lot_id not in fresh_set:
                gone.append(lot_id)
                # суррогат сделки: лот исчез по своей ask-цене (kind='gone')
                price_rub = float(row.get("price") or 0)
                if state is not None and model and price_rub > 0 and rate > 0:
                    record_sale_points(state, gift, model, price_rub / rate, now_iso, "gone")
        if not gone:
            return
        for k in range(0, len(gone), 100):
            rg = httpx.post(
                f"{SERVER_URL}/api/worker/lots-gone",
                headers={"X-Worker-Secret": WORKER_SECRET},
                json={"source": "GIFTS", "ids": gone[k:k + 100]}, timeout=60,
            )
            if rg.status_code == 200:
                print(f"пропавших лотов GIFTS помечено: {rg.json().get('gone', 0)}")
    except Exception as e:
        print(f"[!] маркировка gone не удалась: {e}")


def mark_replaced_lots(source: str, lots: list):
    """После успешного пуша: старые лоты этого источника, которых нет в новой
    пачке, помечаем gone. Лента показывает только результаты текущего прогона,
    а не накопленный хвост прошлых логик/сканов."""
    if "--no-push" in sys.argv or not WORKER_SECRET:
        return
    import httpx
    try:
        r = httpx.get(
            f"{SERVER_URL}/api/worker/lots-ids",
            headers={"X-Worker-Secret": WORKER_SECRET},
            params={"source": source}, timeout=60,
        )
        if r.status_code != 200:
            return
        new_ids = {str(l["id"]) for l in lots}
        stale = [str(row["id"]) for row in r.json().get("rows", [])
                 if str(row["id"]) not in new_ids]
        if not stale:
            return
        rg = httpx.post(
            f"{SERVER_URL}/api/worker/lots-gone",
            headers={"X-Worker-Secret": WORKER_SECRET},
            json={"source": source, "ids": stale[:2000]}, timeout=60,
        )
        if rg.status_code == 200:
            print(f"старых лотов {source} помечено: {rg.json().get('gone', 0)}")
    except Exception as e:
        print(f"[!] замена старых лотов {source} не удалась: {e}")


def apply_mode(mode: str):
    """quick: 1 страница Tonnel/Portals, 4 точки истории вместо 8 — чтобы
    прогон укладывался в ~10 минут (cron GitHub Actions каждые 15 мин).
    full: как раньше, весь каталог. Возвращает текст для лога."""
    global TONNEL_PAGES, PORTALS_PAGES, ACTIVITY_SAMPLE
    if mode == "quick":
        TONNEL_PAGES = 1
        PORTALS_PAGES = 1
        ACTIVITY_SAMPLE = 4
        return "quick (1 страница Tonnel/Portals, ACTIVITY_SAMPLE=4)"
    return "full (весь каталог, полный пейджинг)"


def main():
    is_debug = "--debug-reject" in sys.argv
    if is_debug:
        debug_reject_run()
        return
    mode = ("full" if "--full" in sys.argv
            else "quick" if "--quick" in sys.argv
            else os.getenv("GIFT_SCAN_MODE", "full"))
    mode_txt = apply_mode(mode)
    ping_server()
    universe = build_universe()
    print(f"[{datetime.utcnow().isoformat()}Z] gifts_scan старт: режим {mode_txt}, "
          f"рынок {len(universe)} коллекций (watchlist приоритет)")
    rate = ton_rub_rate()
    print(f"курс TON: {rate:.1f} ₽")
    state = load_state()
    scan = universe
    if mode == "quick" and len(universe) > QUICK_SLICE:
        start = int(state.get("cursor") or 0) % len(universe)
        scan = [universe[(start + i) % len(universe)] for i in range(QUICK_SLICE)]
        state["cursor"] = (start + QUICK_SLICE) % len(universe)
        print(f"quick-окно: {QUICK_SLICE} коллекций от позиции {start} (каталог обойдёт за "
              f"{-(-len(universe) // QUICK_SLICE)} прогонов)")
    all_lots = {}
    fetch_ok = {}  # gift -> {'Tonnel': полный_снимок, 'Portals': ...}
    ok = []
    for gift in scan:
        t_lots, _, t_full = tonnel_fetch_pages(gift)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        p_lots, _, p_full = portals_fetch_pages(gift)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        f_lots = fragment_fetch(gift)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        total = t_lots + p_lots + f_lots
        all_lots[gift] = total
        fetch_ok[gift] = {"Tonnel": bool(t_full), "Portals": bool(p_full)}
        venue_t = len(t_lots)
        venue_p = len(p_lots)
        venue_f = len(f_lots)
        ok.append((gift, len(total), venue_t, venue_p))
        print(f"  {gift}: {len(total)} лотов (Tonnel {venue_t} / Portals {venue_p} / Fragment {venue_f})")
    try:
        Path("gifts_snapshot.json").write_text(
            json.dumps({"ts": datetime.utcnow().isoformat() + "Z", "rate": rate,
                        "all_lots": {k: v for k, v in all_lots.items()}},
                       ensure_ascii=False), "utf-8")
    except Exception as e:
        print(f"[!] снапшот не сохранён: {type(e).__name__}")
    # история реальных сделок: опрос Tonnel giftActivity по дешёвым лотам
    try:
        added, _ = collect_sales_history(all_lots, state)
        n_pts = sum(len(v) for v in state.get("sales", {}).values())
        n_mod = len(state.get("sales", {}))
        print(f"история сделок: {n_pts} точек по {n_mod} моделям (+{added} новых) [api + gone]")
    except Exception as e:
        print(f"[!] история сделок не удалась: {type(e).__name__} {e}")
    deals = find_with_model_deals(all_lots, rate, state)
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
    # ТОП-10 с расчётом: цена/планка/источник планки/качество/чистая прибыль
    for i, d in enumerate(deals[:10], 1):
        print("[deal %d] %s | %s | %s | %.2f TON | планка %s TON (%s; %s) "
              "| чистыми ~%d ₽ (профит %.2f TON после комиссии %s%%) | лот №%s"
              % (i, d["gift"], d["model"], d["venue"], d["price_ton"],
                 d["fair_ton"], d["fair_src"], d["fair_quality"],
                 d["net_rub"], d["price_ton"] - d["fair_ton"] * (1 - RESELL_FEE_PCT / 100.0),
                 RESELL_FEE_PCT, d.get("gift_num") or "—"))
    lots = [to_lot_dict(d, rate) for d in deals]
    mark_gone_lots(all_lots, fetch_ok, lots, state, rate)
    save_state(state)
    if "--no-push" not in sys.argv:
        saved, skipped = push_to_server(lots)
        print(f"пуш: saved={saved} skipped={skipped}")
        # quick пушит только своё окно: глобальную замену делаем только в full,
        # иначе сметём лоты коллекций, не входящих в текущее окно
        if saved > 0 and mode != "quick":
            mark_replaced_lots("GIFTS", lots)
    else:
        print(f"пуш: off (--no-push), лотов к пушу: {len(lots)}")


def debug_reject_run(n: int = 50):
    """Отладка фильтра на первых N лотах снапшота — без сети.
    На каждый отброшенный лот одна строка + счётчик причин."""
    global REJECT_LOG
    REJECT_LOG = True
    snap_file = Path("gifts_snapshot.json")
    if not snap_file.exists():
        print("[!] нет gifts_snapshot.json — сначала сделай обычный прогон")
        return
    snap = json.loads(snap_file.read_text("utf-8"))
    rate = float(snap.get("rate") or 0)
    if rate <= 0:
        rate = ton_rub_rate()
    print(f"[debug] снапшот от {snap.get('ts')} курс {rate:.1f} ₽/TON", "| лотов в снапшоте:",
          sum(len(v) for v in snap["all_lots"].values()))
    budget = max(0, int(n))
    all_lots = {}
    for gift, lots in snap["all_lots"].items():
        if budget <= 0:
            break
        take = lots[:budget]
        all_lots[gift] = take
        budget -= len(take)
    print(f"[debug] взяли {sum(len(v) for v in all_lots.values())} лотов")
    state = load_state()
    try:
        n_pts = sum(len(v) for v in state.get("sales", {}).values())
        print(f"[debug] история сделок в состоянии: {n_pts} точек по {len(state.get('sales', {}))} моделям")
    except Exception:
        pass
    deals = find_with_model_deals(all_lots, rate, state)
    print(f"[debug] прошло фильтр: {len(deals)} из {sum(len(v) for v in all_lots.values())}")
    for d in deals[:5]:
        print("  [взят] %s | %s | %.2f TON | %s | +%.0f ₽" % (d["gift"], d["model"], d["price_ton"], d["fair_src"], d["net_rub"]))


if __name__ == "__main__":
    args = list(sys.argv[1:])
    if "--debug-reject" in args:
        i = args.index("--debug-reject")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 50
        debug_reject_run(n)
    else:
        main()
