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
# Пул прокси: PROXY_URLS="socks5://...,socks5://..." или текстовый файл proxy_urls.txt (строка = 1 прокси)
_raw_proxies = [p.strip() for p in os.environ.get("PROXY_URLS", "").replace("\n", ",").split(",") if p.strip()]
_proxy_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_urls.txt")
if os.path.exists(_proxy_file):
    with open(_proxy_file, encoding="utf-8") as f:
        _raw_proxies += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
PROXY_POOL = _raw_proxies or ([PROXY] if PROXY else [])
QUERIES = [q.strip() for q in os.environ.get("QUERIES", "iphone 15 pro,macbook air m3,playstation 5").split(",") if q.strip()]
CITY_LIST = [c.strip() for c in os.environ.get("CITIES", "").split(",") if c.strip()] or list(CITIES)
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "12"))
DMIN, DMAX = float(os.environ.get("DELAY_MIN", "5")), float(os.environ.get("DELAY_MAX", "11"))
UA_MOBILE = os.environ.get("UA_MOBILE", "1") == "1"
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
UA_PHONE = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"


def _headers(mobile: bool):
    """Полный набор заголовков, имитирующий настоящий Chrome (без них Avito часто отдаёт 429 боту)."""
    h = {
        "User-Agent": UA_PHONE if mobile else UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if mobile:
        h.update({"sec-ch-ua": '"Chromium";v="128", "Google Chrome";v="128", "Not-A-Brand";v="24"',
                  "sec-ch-ua-mobile": "?1", "sec-ch-ua-platform": '"Android"'})
    else:
        h.update({"sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not-A-Brand";v="24"',
                  "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'})
    return h


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
            r = client.get(url, headers=_headers(url.startswith("https://m.")), follow_redirects=True)
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
    """Четыре способа извлечения объявлений из страницы Avito. Возвращает (список, имя способа)."""
    # Способ 1: JSON в <script data-mfe-state="true">
    for m in re.finditer(r"<script[^>]*data-mfe-state=\"true\"[^>]*>(.*?)</script>", html, re.S):
        try:
            found = find_items(json.loads(m.group(1)))
        except Exception:
            continue
        if found:
            return _from_json_items(found), "data-mfe-state"
    # Способ 2: JSON в __initialData__ (прямой и двойная сериализация "экранированная строка")
    for pat in (r"__initialData__\s*=\s*(\{.*?\});", r'window\.__initialData__\s*=\s*"(.*)"\s*;?'):
        m = re.search(pat, html, re.S)
        if not m:
            continue
        raw = m.group(1)
        for _ in range(2):  # могло прийти {"..."} или "строка-JSON" (json.loads дважды)
            try:
                obj = json.loads(raw)
            except Exception:
                break
            if isinstance(obj, str):
                raw = obj
                continue
            found = find_items(obj)
            if found:
                return _from_json_items(found), "__initialData__"
            break
        # если JSON не распознан — идём дальше
    # Способ 3: microdata-карточки (новая разметка Avito): itemProp="url" — конец карточки,
    # всё видимое (картинка-заголовок, цены, описание) лежит между предыдущим url и текущим.
    urls = list(re.finditer(r'itemProp="url"\s+href="(/[^"]+)"', html))
    if len(urls) >= 3:
        items = []
        prev_end = 0
        for u in urls:
            window = html[prev_end:u.start()]
            path = u.group(1)
            try:
                alt = None
                alts = re.findall(r'itemProp="image"[^>]*alt="([^"]{2,200})"', window) or \
                       re.findall(r'itemProp="name"[^>]*content="([^"]{2,200})"', window)
                if alts:
                    alt = alts[-1]
                    body = re.sub(r"<script[\s\S]*?</script>", "\n", window)
                    lines = [s.strip() for s in re.sub(r"<[^>]+>", "\n", body).split("\n") if s.strip()]
                else:
                    # видимый текст: метка «Рыночная цена»/«Цена ниже рыночной» -> заголовок -> цена
                    body = re.sub(r"<script[\s\S]*?</script>", "\n", window)
                    lines = [s.strip() for s in re.sub(r"<[^>]+>", "\n", body).split("\n") if s.strip()]
                    for i, s in enumerate(lines):
                        if "рыночн" in s.lower() and "цен" in s.lower():
                            for nxt in lines[i + 1:i + 4]:
                                if nxt:
                                    alt = nxt
                                    break
                            break
                desc = re.findall(r'itemProp="description"[^>]*content="([^"]{2,600})"', window)
                prices = re.findall(r"([\d\u00a0 ]{2,12})₽", window)
                # продавец: имя идут перед строкой рейтинга «4,9», (кол-во отзывов) — сразу после
                seller, rating, reviews = "", None, 0
                for i, s in enumerate(lines):
                    m_r = re.fullmatch(r"(\d(?:[.,]\d)?)", s)
                    if m_r and i + 1 < len(lines) and re.fullmatch(r"\((\d{1,6})\)", lines[i + 1]):
                        rating = float(m_r.group(1).replace(",", "."))
                        reviews = int(lines[i + 1][1:-1])
                        seller = lines[i - 1] if i >= 1 else ""
                        break
                if alt:
                    base = path.split("?", 1)[0]
                    m_id = re.search(r"_(\d{6,})$", base) or re.search(r"(\d{8,})", base)
                    items.append({"id": m_id.group(1) if m_id else "",
                                  "title": str(alt).replace("&quot;", '"').strip(),
                                  "price": _to_int_price(prices[0] if prices else 0),
                                  "desc": (desc[-1] if desc else "")[:600],
                                  "path": path,
                                  "seller": seller[:60], "sellerRating": rating or 0, "sellerReviews": reviews})
            except Exception:
                continue
            prev_end = u.end()
        if items:
            return _extract_ids(items), "itemProp-карточки"
    # Способ 4: HTML-карточки (старая десктопная разметка)
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
            base = str(it["path"]).split("?", 1)[0]
            m = re.search(r"_(\d{6,})$", base) or re.search(r"(\d{8,})", base)
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
        if scam_suspect(i):
            continue
        lots.append({"id": f"av_{i['id']}", "title": i["title"][:200], "source": "Avito", "city": city,
                     "price": p, "market": market, "tags": [f"Медиана по {len(rel)} объявл."],
                     "sellerName": (i.get("seller") or "Продавец Avito")[:60],
                     "sellerRating": float(i.get("sellerRating") or 0),
                     "sellerReviews": int(i.get("sellerReviews") or 0),
                     "description": i["desc"],
                     "url": "https://www.avito.ru" + i["path"]})
    return lots


SCAM_WORDS = ("быстрый перевод", "залог", "займ", "скупка", "продам ништяк", "гарантия по переписке",
              "только перевод", "без осмотра", "крипт", "равновесие игров", "прокси",
              "возврат за счёт", "начала не смотри")


def scam_suspect(item):
    blob = (item.get("title", "") + " " + item.get("desc", "")).lower()
    return any(w in blob for w in SCAM_WORDS)


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
    cooldown = {p: 0.0 for p in PROXY_POOL}
    plain = client_for(None)
    while True:
        for city in CITY_LIST:
            if city not in CITIES:
                log.warning("нет города %s", city)
                continue
            for q in QUERIES:
                now = time.time()
                ready = [p for p in PROXY_POOL if cooldown.get(p, 0) <= now]
                if not ready:
                    wait = min(cooldown.get(p, now) for p in PROXY_POOL) - now
                    log.info("все прокси остывают, ждём %dс", max(0, int(wait)))
                    time.sleep(max(1, min(wait, 60)))
                    continue
                proxy = random.choice(ready)
                try:
                    items, _ = parse(fetch(client_for(proxy), city, q))
                    log.info("%s / %s [%s]: объявлений %d", city, q, proxy or "direct", len(items))
                    lots = evaluate(city, q, items)
                    if lots:
                        push(plain, lots)
                    backoff = 60
                except Blocked as e:
                    cooldown[proxy] = time.time() + 600
                    log.warning("Avito блокирует %s (%s), пауза 10 мин на этом IP", proxy or "direct", e)
                except Exception as e:
                    log.warning("%s / %s: %s", city, q, e)
                time.sleep(random.uniform(DMIN, DMAX))
        if once:
            return
        time.sleep(60)


def client(proxy=None):
    kw = {"timeout": 20}
    if proxy:
        kw["proxy"] = proxy
        if proxy.startswith("socks"):
            try:
                import socksio  # noqa: F401
            except ImportError:
                sys.exit("PROXY_URL использует SOCKS, установите библиотеку: pip install httpx[socks]")
    return httpx.Client(**kw)


inning_clients = {}


def client_for(proxy):
    """Один long-lived httpx-клиент на каждый прокси из пула."""
    if proxy not in inning_clients:
        inning_clients[proxy] = client(proxy)
    return inning_clients[proxy]


def check_cities():
    ok = 0
    for city in CITY_LIST:
        if city not in CITIES:
            log.warning("нет города %s", city)
            continue
        try:
            items, method = parse(fetch(client_for(random.choice(PROXY_POOL) if PROXY_POOL else None), city, QUERIES[0] if QUERIES else "iphone"))
            if items:
                ok += 1
                log.info("%s (%s): OK %d объявл. [%s]", city, CITIES[city], len(items), method)
            else:
                log.error("slug %s не отдал объявлений (способ %s) — проверьте", CITIES[city], method)
        except Blocked as e:
            log.warning("%s заблокирован (%s)", city, e)
        except Exception as e:
            log.warning("%s: %s", city, e)
        time.sleep(random.uniform(DMIN, DMAX))
    log.info("Итог: %d/%d городов работают", ok, len(CITY_LIST))


def main():
    dump_flag = "--dump" in sys.argv
    check = "--check-cities" in sys.argv
    once = "--once" in sys.argv
    if not SERVER or not SECRET:
        if not (dump_flag or check):
            sys.exit("Задайте SERVER_URL и WORKER_SECRET")
    log.info("Пул прокси: %d шт." % len(PROXY_POOL))
    if dump_flag:
        with client(PROXY_POOL[0] if PROXY_POOL else None) as cl:
            dump_mode(cl)
    elif check:
        check_cities()
    else:
        run(client_for(None), once=once)


if __name__ == "__main__":
    main()
