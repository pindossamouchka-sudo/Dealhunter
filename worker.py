"""Воркер Avito -> сервер DealHunter. Запускать НЕ на Render, а у себя/на VPS с чистым IP или резидентным прокси.
env: SERVER_URL, WORKER_SECRET, QUERIES="iphone 15 pro,macbook air m3", CITIES="Москва,Казань" (пусто = все),
     PROXY_URL, MIN_MARGIN=12, DELAY_MIN=5, DELAY_MAX=11
Запуск: python worker.py   |   проверка slug'ов: python worker.py --check-cities
ВАЖНО: формат страниц Avito меняется, я не мог проверить парсер на живом сайте — смотрите логи первого запуска.
"""
import os, sys, json, re, time, random, statistics, urllib.parse, logging
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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class Blocked(Exception):
    pass


def fetch(client, city, query):
    url = f"https://www.avito.ru/{CITIES[city]}?q={urllib.parse.quote(query)}&s=104"  # s=104 — по дате
    r = client.get(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}, follow_redirects=True)
    if r.status_code in (403, 429) or "Доступ ограничен" in r.text[:5000]:
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} для {url}")
    return r.text


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
    items = []
    for m in re.finditer(r"<script[^>]*data-mfe-state=\"true\"[^>]*>(.*?)</script>", html, re.S):
        try:
            found = find_items(json.loads(m.group(1)))
        except Exception:
            continue
        if found:
            items = found
            break
    out = []
    for it in items:
        try:
            price = int((it.get("priceDetailed") or {}).get("value") or it.get("price") or 0)
            if price > 0 and it.get("title") and it.get("urlPath"):
                out.append({"id": str(it["id"]), "title": str(it["title"]), "price": price,
                            "desc": str(it.get("description") or "")[:600], "path": it["urlPath"]})
        except Exception:
            continue
    return out


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


def main():
    if not SERVER or not SECRET:
        sys.exit("Задайте SERVER_URL и WORKER_SECRET")
    check = "--check-cities" in sys.argv
    kw = {"timeout": 20}
    if PROXY:
        kw["proxy"] = PROXY
    backoff = 60
    with httpx.Client(**kw) as client:
        while True:
            for city in CITY_LIST:
                if city not in CITIES:
                    log.warning("нет города %s", city)
                    continue
                for q in (QUERIES[:1] if check else QUERIES):
                    try:
                        items = parse(fetch(client, city, q))
                        log.info("%s / %s: объявлений %d", city, q, len(items))
                        if check:
                            if not items:
                                log.error("slug %s не отдал объявлений — проверьте", CITIES[city])
                        else:
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
            if check:
                return
            time.sleep(60)


if __name__ == "__main__":
    main()
