# -*- coding: utf-8 -*-
"""
gifts_scan — «gift sniper» для DealHunter.

Мониторим NFT-подарки Telegram сразу на трёх маркетах:
  • Tonnel   (market.tonnel.network) — gifts2.tonnel.network/api/pageGifts
  • Portals  (portal-market.com)     — порталовский REST
  • Fragment (fragment.com)          — каталог без кошелька + HTML карточек

Честная «рыночная планка» = МЕДИАНА цен всех текущих листингов модели,
собранных из трёх маркетов (Tonnel + Portals + Fragment вместе).
Лот объявляем выгодным, если его цена ≤ (100 - DEAL_PCT)% от медианы
и выборка по модели достаточно велика (>= MIN_SAMPLES лотов).
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
TONNEL_PAGES = 3
TONNEL_LIMIT = 30       # сервер Tonnel не принимает limit > 30
PORTALS_PAGES = 3
PORTALS_LIMIT = 30

# Порог «выгодности»: цена ≤ медианы минус DEAL_PCT%
# 18% — чтобы после комиссий перепродажи (~8%) сделка оставалась в плюсе
DEAL_PCT = 12.0
# Оценка комиссий при перепродаже (доля от цены продажи)
RESELL_FEE_PCT = 10.0
# Минимальная чистая прибыль в ₽ после вычета комиссий
MIN_NET_RUB = 500

# Методика B «перебей соседа»: скидка от живого соседа ≥ 15%
DEAL_PCT_B = 15.0

# Методика C «строго по продажам»: скидка ≥ 10%, чистыми ≥ 800 ₽
DEAL_PCT_C = 10.0
MIN_NET_C = 800
# Минимальное число листингов в выборке модели, чтобы сравнить с соседом
MIN_SAMPLES = 2
# Антибот-пауза между запросами
SLEEP_MIN, SLEEP_MAX = 2.0, 4.5
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

_fragment_slugs = None


def fragment_slugs():
    """{norm_имени_коллекции: fragment_slug} — тянем один раз за прогон."""
    global _fragment_slugs
    if _fragment_slugs is not None:
        return _fragment_slugs
    try:
        import asyncio
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        from fragment_api import FragmentCatalog
        cols = asyncio.run(FragmentCatalog().list_collections())
        out = {}
        for c in cols:
            slug = (c.get("slug") or "").strip()
            nm = _norm(c.get("name") or "")
            if slug:
                out.setdefault(nm, slug)
        # на Fragment коллекции во множ. числе: снимем 's' и наоборот
        for nm, slug in list(out.items()):
            if nm:
                out.setdefault(nm + "s", slug)
                out.setdefault(nm + "es", slug)
                if nm.endswith("s"):
                    out.setdefault(nm[:-1], slug)
                    if nm.endswith("es"):
                        out.setdefault(nm[:-2], slug)
        _fragment_slugs = out
        return out
    except Exception as e:
        print(f"  [!] Fragment каталог: {type(e).__name__} {e}")
        _fragment_slugs = {}
        return {}


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

# ---------------- Оценка ----------------
# Правило «следующего лота»:
# 1) референс лота = минимальная цена ДРУГИХ лотов той же модели (сам лот не учитываем),
#    в той же валюте, что цена лота (TON);
# 2) discount = (reference - price) / reference;
# 3) чистая цена продажи = reference * (1 - комиссия маркета 10%), профит = чистая - покупка;
# 4) выгодный = цена <= reference * 0.90 И профит > 0;
# 5) редкость модели/фона/символа < 2% — отдельная метка (на планку не влияет);
# 6) причина у сделки: "-N% от следующего лота".

RARE_PCT = 2.0             # порог редкости модели/фона/символа (%)


def get_model_floor(mlots, model, exclude_lot=None):
    """Референс лота = минимальная цена ДРУГИХ лотов той же модели (TON).

    Сам лот исключается: иначе он не может быть дешевле собственной цены.
    Так «флор по модели» — это следующий по дешевизне сосед, а не минимум,
    в который всегда попадает проверяемый лот.
    Нет других лотов -> 0.0 (причина «нет флора»)."""
    prices = [
        l["price_ton"]
        for l in mlots
        if l is not exclude_lot
        and l.get("model") == model
        and l.get("price_ton", 0) > 0
    ]
    return float(min(prices)) if prices else 0.0


def evaluate_lot(price_ton: float, reference_ton: float, rate: float,
                 fee_pct: float = None, min_discount: float = 0.10):
    """Чистая оценка лота по правилу «следующего лота»:
    цена <= референс * 0.90 И профит после комиссии 10% > 0. None -> отбросить.
    reference_ton — минимальная цена ДРУГОГО лота той же модели."""
    if not reference_ton or reference_ton <= 0 or not price_ton or price_ton <= 0:
        return None
    fee_pct = RESELL_FEE_PCT if fee_pct is None else fee_pct
    if price_ton > reference_ton * (1.0 - min_discount) + 1e-9:
        return None
    discount = (reference_ton - price_ton) / reference_ton
    net_sell = reference_ton * (1.0 - fee_pct / 100.0)
    profit_ton = net_sell - price_ton
    if profit_ton <= 0:
        return None
    reason = "-%d%% от следующего лота" % round(discount * 100)
    return {
        "floor_model": reference_ton,
        "reference_ton": reference_ton,
        "fair_ton": reference_ton,
        "discount": round(discount, 3),
        "profit_ton": round(profit_ton, 2),
        "profit_rub": round(profit_ton * rate),
        "net_sell_ton": round(net_sell, 2),
        "reason": reason,
    }


def rare_reason(model: str, lot: dict, mlots: list):
    """Правило 6: редкость модели/фона/символа < 2% (доля во всей выборке).
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


def reject_reason(price_ton, reference_ton, fee_pct=None, min_discount=0.10):
    """Почему лот бракуется (для отладочного лога):
    нет флора / цена >= флора / скидка < порога / профит <= 0."""
    if reference_ton is None or reference_ton <= 0:
        return "нет флора"
    if price_ton is None or price_ton <= 0:
        return "нет цены"
    if price_ton >= reference_ton:
        return "цена >= флора"
    if price_ton > reference_ton * (1.0 - min_discount) + 1e-9:
        return "скидка < порога"
    net_sell = reference_ton * (1.0 - (RESELL_FEE_PCT if fee_pct is None else fee_pct) / 100.0)
    if net_sell - price_ton <= 0:
        return "профит <= 0"
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


def find_with_model_deals(all_lots, rate: float):
    """Оценка по правилу «следующего лота»: для каждого лота референс —
    минимальная цена ДРУГИХ лотов той же модели. Сделка: цена <= референс*0.9
    и профит после комиссии 10% > 0. Референс считается из текущего скана,
    поэтому он всегда свежий (не старше 10 минут)."""
    import collections as _c
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
            if len(mlots) < MIN_SAMPLES:
                diag['мало_образцов'] += 1
                continue
            prices = sorted(l["price_ton"] for l in mlots if l.get("price_ton", 0) > 0)
            if len(prices) < 2:
                diag['флора_нет'] += 1
                continue
            # диагностика: разрыв между двумя самыми дешёвыми лотами модели
            gap = (prices[1] - prices[0]) / prices[1] if prices[1] > 0 else 0.0
            if gap >= 0.10:
                diag['макс_скидка_>=10'] += 1
            elif gap >= 0.05:
                diag['макс_скидка_>=5'] += 1
            else:
                diag['макс_скидка_<5'] += 1
            rar = parse_model_rarity(model)
            floors = {}
            for l in mlots:
                floors.setdefault(l["venue"], l["price_ton"])
            for l in mlots:
                # референс: минимум ДРУГИХ лотов модели (сам лот не учитываем)
                reference_ton = get_model_floor(mlots, model, exclude_lot=l)
                rare = rare_reason(model, l, mlots)
                res = evaluate_lot(l["price_ton"], reference_ton, rate)
                if not res:
                    reason = reject_reason(l["price_ton"], reference_ton)
                    reject_cnt[reason] += 1
                    if REJECT_LOG:
                        debug_reject_line(gift, model, l, reference_ton, reason)
                    diag['отброшено'] += 1
                    continue
                d = {
                    "gift": gift, "model": model, "rarity": rar,
                    "price_ton": l["price_ton"], "fair_ton": res["fair_ton"],
                    "reference_ton": res["reference_ton"],
                    "fair_src": res["reason"], "rare": rare,
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
        extra += f" · чистыми ~{d['net_rub']:,} ₽ после комиссий".replace(",", " ")
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
            f"цена {d['price_ton']:.2f} TON, рыночная цена {d['fair_ton']:.2f} TON "
            f"(источник: {d.get('fair_src') or 'листинги'}; Tonnel+Portals+Fragment)"
            f" · скидка ~{pct}%{extra}"
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


def mark_gone_lots(all_lots: dict, fetch_ok: dict, lots: list):
    """Лоты GIFTS, которые были на проде, а теперь их НЕТ в полном снимке
    конкретной площадки — реально купили/сняли. Помечаем gone (уйдут с ленты).
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
        for row in prod_rows:
            lot_id = row.get("id", "")
            title = row.get("title", "")
            gift = title.split(" —")[0].strip()
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


def main():
    ping_server()
    watchlist = load_watchlist()
    print(f"[{datetime.utcnow().isoformat()}Z] gifts_scan старт: коллекций {len(watchlist)}")
    rate = ton_rub_rate()
    print(f"курс TON: {rate:.1f} ₽")
    all_lots = {}
    fetch_ok = {}  # gift -> {'Tonnel': полный_снимок, 'Portals': ...}
    ok = []
    for gift in watchlist:
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
    mark_gone_lots(all_lots, fetch_ok, lots)
    if "--no-push" not in sys.argv:
        saved, skipped = push_to_server(lots)
        print(f"пуш: saved={saved} skipped={skipped}")
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
    deals = find_with_model_deals(all_lots, rate)
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
