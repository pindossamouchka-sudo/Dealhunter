"""Воркер Avito -> сервер DealHunter. Запускать НЕ на Render, а у себя/на VPS с чистым IP или резидентным прокси.
env: SERVER_URL, WORKER_SECRET, QUERIES="iphone 15 pro,macbook air m3", CITIES="Москва,Казань" (пусто = все),
     PROXY_URL, MIN_MARGIN=12, DELAY_MIN=5, DELAY_MAX=11, UA_MOBILE=1 (включить мобильную точку входа m.avito.ru)
Запуск: python worker.py   |   проверка slug'ов: python worker.py --check-cities
        дамп одной страницы для отладки парсера: python worker.py --dump [город] [запрос]
ВАЖНО: формат страниц Avito меняется, я не мог проверить парсер на живом сайте — смотрите логи первого запуска.
"""
import os, sys, json, re, time, random, statistics, urllib.parse, logging
from dotenv import load_dotenv

load_dotenv()
import httpx
from cities import CITIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")
SERVER = os.environ.get("SERVER_URL", "").rstrip("/")
SECRET = os.environ.get("WORKER_SECRET", "")
PROXY = os.environ.get("PROXY_URL") or None
QUERIES = [q.strip() for q in os.environ.get("QUERIES", "iphone 15 pro,macbook air m3,playstation 5").split(",") if q.strip()]
CITY_LIST = [c.strip() for c in os.environ.get("CITIES", "").split(",") if c.strip()] or list(CITIES)
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "12"))
DMIN, DMAX = float(os.environ.get("DELAY_MIN", "5")), float(os.environ.get("DELAY_MAX", "11"))
UA_MOBILE = os.environ.get("UA_MOBILE", "1") == "1"
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
UA_PHONE = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"


class Blocked(Exception):
    pass


def fetch(client, city, query):
    """Страница поиска Avito. Сначала мобильная точка входа (реже блокируют), затем десктоп если не мобильный режим."""
    q = urllib.parse.quote(query)
    endpoints = []
    if UA_MOBILE:
        endpoints.append(f"https://m.avito.ru/{CITIES[city]}?q={q}")
    endpoints.append(f"https://www.avito.ru/{CITIES[city]}?q={q}&s=104")  # s=104 — по дате

    last_err = None
    for url in endpoints:
        try:
            r = client.get(url, headers={"User-Agent": UA_PHONE if url.startswith("https://m.") else UA_DESKTOP,
                                         "Accept-Language": "ru-RU,ru;q=0.9",
                                         "Accept": "text/html,application/xhtml+xml"},
                            follow_redirects=True)
            if r.status_code in (403, 429) or "Доступ ограничен" in r.text[:5000]:
                last_err = Blocked(f"HTTP {r.status_code} на {url}")
                continue
            if r.status_code == 200:
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code} для {url}")
        except httpx.HTTPError as e:
            last_err = e
    raise last_err


def find_items(obj):
    if isinstance(obj, dict):
        it = obj.get("items")
        if isinstance(it, list) and it and isinstance(it[0], dict) and "urlPath" in it[0]:
            return it
        for v in obj.values():
            res = find_items(v)
            if res:
                return res
    elif isinstance(obj, list):
        for v in obj:
            res = find_items(v)
            if res:
                return res
    return None


def parse(html):
    """Три способа извлечения объявлений из страницы Avito. Возвращает (список, имя способа)."""
    # Способ 1: JSON в <script data-mfe-state="true">
    for m in re.finditer(r"<script[^>]*data-mfe-state=\"true\"[^>]*>(.*?)</script>", html, re.S):
        try:
            found = find_items(json.loads(m.group(1)))
        except Exception:
            continue
        if found:
            return _from_json_items(found), "data-mfe-state"
    # Способ 2: JSON в __initialData__
    m = re.search(r"__initialData__\s*=\s*(\{.*?\});", html, re.S)
    if m:
        try:
            found = find_items(json.loads(m.group(1)))
            if found:
                return _from_json_items(found), "__initialData__"
        except Exception:
            pass
    # Способ 3: HTML-карточки (data-marker="item") десктопной разметки
    cards = re.findall(r'data-marker="item"([\s\S]*?)(?=data-marker="item"|$)', html)
    if cards:
        items = []
        for card in cards:
            try:
                title = re.search(r'<a[^>]*item-title[^>]*>(?:<[^>]*>)*([^<]{2,})', card) or \
                        re.search(r'<a[^>]*>([^<]{2,})</a>', card)
                price = re.search(r'"value"\s*:\s*"?(\d[\d\s]*)', card) or \
                        re.search(r'data-marker="item-price"[^>]*>\s*(?:<[^>]*>)*([^<><]+)', card)
                path = re.search(r'href="(/[^"]+)"', card)
                if title and path:
                    items.append({"id": "", "title": title.group(1), "price": _to_int_price(price.group(1) if price else 0),
                                  "desc": "", "path": path.group(1)})
            except Exception:
                continue
        return _extract_ids(items), "HTML-карточки"
    return [], "НИ ОДИН"


def _to_int_price(v):
    """Цена может прийти числом, строкой «50 000» или словарём {"value": 50000}."""
    if isinstance(v, dict):
        v = v.get("value")
    try:
        n = int(re.sub(r"[^\d]", "", str(v))) if v is not None else 0
        return n
    except (TypeError, ValueError):
        return 0


def _from_json_items(items):
    out = []
    for it in items:
        try:
            price = _to_int_price(((it.get("priceDetailed") or {}).get("value")) or it.get("price") or 0)
            if price > 0 and it.get("title") and it.get("urlPath"):
                out.append({"id": str(it["id"]), "title": str(it["title"]), "price": price,
                            "desc": str(it.get("description") or "")[:600], "path": it["urlPath"]})
        except Exception:
            continue
    return out


def _extract_ids(items):
    for i, it in enumerate(items):
        if not it["id"]:
            m = re.search(r"/(\d{8,})", it["path"])
            it["id"] = m.group(1) if m else f"unknown_{i}"
    return items


def evaluate(city, query, items):
    words = [w for w in re.split(r"\s+", query.lower()) if len(w) > 1]
    rel = [i for i in items if all(w in i["title"].lower() for w in words)]
    if len(rel) < 10:
        return []
    market = int(statistics.median(i["price"] for i in rel))
    lots = []
    for i in rel:
        p = i["price"]
        margin = (market - p) / market * 100
        if margin < MIN_MARGIN or p < market * 0.45:  # слишком дёшево = запчасти/мошенник/«цена за аренду»
            continue
        lots.append({"id": f"av_{i['id']}", "title": i["title"][:200], "source": "Avito", "city": city,
                     "price": p, "market": market, "tags": [f"Медиана по {len(rel)} объявл."],
                     "sellerName": "Продавец Avito", "description": i["desc"],
                     "url": "https://www.avito.ru" + i["path"]})
    return lots


def push(client, lots):
    for k in range(0, len(lots), 100):
        r = client.post(f"{SERVER}/api/worker/ingest", json=lots[k:k + 100], headers={"X-Worker-Secret": SECRET})
        log.info("ingest: %s %s", r.status_code, r.text[:120])


def dump_mode(client):
    """Режим --dump: скачать одну страницу, сохранить dump.html и показать, какой способ извлечения сработал."""
    city = sys.argv[2] if len(sys.argv) > 2 else "Москва"
    query = sys.argv[3] if len(sys.argv) > 3 else (QUERIES[0] if QUERIES else "iphone")
    if city not in CITIES:
        sys.exit(f"Нет города {city} в cities.py")
    log.info("Дамп: город=%s запрос='%s'", city, query)
    html = fetch(client, city, query)
    with open("dump.html", "w", encoding="utf-8") as f:
        f.write(html)
    items, method = parse(html)
    log.info("Сохранено dump.html (%d символов). Способ извлечения: %s, объявлений: %d",
             len(html), method, len(items))
    for i in items[:5]:
        log.info("  пример: %s | %s ₽ | %s", i["title"][:50], i["price"], i["path"][:60])
    if method == "НИ ОДИН":
        log.error("Ни один способ не сработал — пришлите dump.html для разбора")


def run(client, once=False):
    backoff = 60
    while True:
        for city in CITY_LIST:
            if city not in CITIES:
                log.warning("нет города %s", city)
                continue
            for q in QUERIES:
                try:
                    items, _ = parse(fetch(client, city, q))
                    log.info("%s / %s: объявлений %d", city, q, len(items))
                    lots = evaluate(city, q, items)
                    if lots:
                        push(client, lots)
                    backoff = 60
                except Blocked as e:
                    log.warning("Avito блокирует (%s), пауза %ds — нужен другой IP/прокси", e, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 1800)
                except Exception as e:
                    log.warning("%s / %s: %s", city, q, e)
                time.sleep(random.uniform(DMIN, DMAX))
        if once:
            return
        time.sleep(60)


def client():
    kw = {"timeout": 20}
    if PROXY:
        kw["proxy"] = PROXY
        if PROXY.startswith("socks"):
            try:
                import socksio  # noqa: F401
            except ImportError:
                sys.exit("PROXY_URL использует SOCKS, установите библиотеку: pip install httpx[socks]")
    return httpx.Client(**kw)


def check_cities(client):
    ok, warn = 0, []
    for city in CITY_LIST:
        if city not in CITIES:
            log.warning("нет города %s", city)
            continue
        try:
            items, method = parse(fetch(client, city, QUERIES[0] if QUERIES else "iphone"))
            if items:
                ok += 1
                log.info("%s (%s): OK %d объявл. [%s]", city, CITIES[city], len(items), method)
            else:
                warn.append(city)
                log.error("slug %s не отдал объявлений (способ %s) — проверьте", CITIES[city], method)
        except Blocked as e:
            warn.append(city)
            log.warning("%s заблокирован (%s)", city, e)
        time.sleep(random.uniform(DMIN, DMAX))
    log.info("Итог: %d/%d городов работают", ok, len(CITY_LIST))


def main():
    dump_flag = "--dump" in sys.argv
    check = "--check-cities" in sys.argv
    if not SERVER or not SECRET:
        if not (dump_flag or check):
            sys.exit("Задайте SERVER_URL и WORKER_SECRET")
    with client() as cl:
        if dump_flag:
            dump_mode(cl)
        elif check:
            check_cities(cl)
        else:
            run(cl)


if __name__ == "__main__":
    main()
