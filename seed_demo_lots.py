# -*- coding: utf-8 -*-
"""Заливает демо-лоты через рабочий эндпоинт деплоя /api/worker/ingest.

Запуск из папки проекта:  .venv\\Scripts\\python.exe seed_demo_lots.py
Читает DATABASE_URL/WORKER_SECRET/WEBAPP_URL из .env. Повторный запуск обновляет те же лоты (id demo-*).
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

try:
    import asyncpg
except ImportError:
    asyncpg = None


def load_env(path=".env"):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def row(id_, title, source, city, price, market, tags, seller, rating, reviews, desc, url):
    pct = round((market - price) * 100.0 / market, 1)
    return (id_, title, source, city, int(price), int(market), int(market - price), pct,
            json.dumps(tags, ensure_ascii=False), seller, float(rating), int(reviews), desc, url)


FRAME = "https://market.tonnel.network/market"

GIFTS = [
    ("Plush Pepe — Red Menace", 21900, 26900, ["nft", "редкий", "шутка"], "KRYPTON", 4.9, 3812),
    ("Precious Peach — Sunset", 7300, 9200, ["nft", "популярный"], "NFT_HUNTER", 4.8, 1544),
    ("Doodlz — Neon Mood", 1600, 2300, ["nft", "популярный"], "AVATAR_AI", 4.7, 903),
    ("Diamond Ring — Brilliant", 15400, 18900, ["nft", "статус"], "OKC_Ry", 5.0, 1271),
    ("Vintage — Cuckle Dress", 12600, 15900, ["nft", "модель", "редкий"], "ToLIOM", 4.9, 812),
    ("Hex Pot — Aurora", 9400, 12400, ["nft", "популярный"], "SALVADOR768", 4.6, 468),
    ("Astral Shard — Prime", 4500, 6200, ["nft", "модель"], "yusichur", 4.8, 210),
    ("Birthday Candle — Sparkle", 900, 1450, ["nft", "мелкий"], "margarita.4", 4.9, 355),
    ("Jester Hat — Party Mode", 1200, 1750, ["nft", "мелкий", "шутка"], "ChainKeeper", 4.7, 512),
    ("Skull Flower — Poison Rose", 6100, 8300, ["nft", "модель"], "crush101", 4.8, 229),
    ("Vaccine — Blue Charm", 2800, 3900, ["nft", "популярный"], "TiNA_tg", 4.6, 640),
    ("Legendary Square — Gold", 38900, 45900, ["nft", "статус", "редкий"], "MISHKA.X", 5.0, 1877),
]

TG_LOTS = [
    ("iPhone 13 128 ГБ, чёрный, аккумулятор 84%", "Москва", 31500,
     ["б/у", "самовывоз"], "Alex_M", 4.5, 23,
     "Продаю iPhone 13 в хорошем состоянии: всё время в чехле, сколов нет, аккумулятор 84%. Самовывоз у метро Южная, проверка при встрече."),
    ("Гитара Fender Stratocaster (японская копия)", "Санкт-Петербург", 12400,
     ["б/у", "музыка"], "GuitarMax", 4.0, 12,
     "Стратокастер, японская линейка. Струны новые, прогиб ровный. Меняю только при личной встрече."),
    ("Диван раскладной + 2 подушки, отдаю срочно", "Москва", 9000,
     ["б/у", "мебель", "срочно"], "Ольга", 4.2, 7,
     "Отдаю срочно из-за переезда: раскладной диван, без пятен, механизм исправен. Самовывоз в течение пары дней."),
    ("Велосипед Stels Navigator 26\" (подростковый)", "Казань", 8300,
     ["б/у", "спорт"], "Radik78", 4.3, 15,
     "Велосипед без падений, летом смазывал цепь. Покрышки целые, тормоза отрегулированы."),
    ("Кровать 140×200 с ортопедическим матрасом", "Екатеринбург", 15800,
     ["б/у", "мебель"], "PureVibe", 4.6, 31,
     "Каркас цельный, без скрипов. Матрас покупали два года назад, состояние идеальное. Разберу при передаче."),
]

MARKET_LOTS = [
    ("Apple AirPods Pro 2 (USB-C), новые", "Wildberries", "Москва", 14700, 21900,
     ["новое", "розница"], "Wildberries", 4.9, 8421,
     "Оригинал, коробка запечатана. Доставка на пункт выдачи завтра."),
    ("Dyson V12 Slim Fluffy — фильтр, док-станция", "Wildberries", "Санкт-Петербург", 26700, 34900,
     ["новое", "техника"], "Wildberries", 4.8, 1560,
     "Полная комплектация: щётка, валик для паркета и док. Гарантия 12 месяцев."),
    ("Sony DualSense Edge — геймпад для PS5", "Ozon", "Москва", 8700, 11900,
     ["новое", "гейминг"], "Ozon", 4.7, 942,
     "Игровая редакция геймпада: сменные стики, набор накладок, кейс. Проверить при получении."),
    ("Nintendo Switch OLED белая + док-станция", "Wildberries", "Москва", 28400, 36400,
     ["новое", "гейминг"], "Wildberries", 4.9, 2331,
     "Новая консоль, полная комплектация, доставка завтра. Чек прилагается."),
    ("LEGO Technic McLaren 720S (1240 деталей)", "Ozon", "Казань", 6400, 8600,
     ["новое", "конструктор"], "Ozon", 4.8, 268,
     "Актуальный набор: пакеты с номерами по этапам, инструкция внутри, наклейки целые."),
    ("Xiaomi Redmi Note 13 Pro 12/512 ГБ", "Wildberries", "Новосибирск", 19600, 26700,
     ["новое", "смартфон"], "Wildberries", 4.7, 5110,
     "Официальная гарантия РСТ, полный комплект, чек. Цена ниже обычной для этой недели."),
]


def build_rows():
    rows = []
    for i, (title, price, market, tags, seller, rating, reviews) in enumerate(GIFTS, 1):
        desc = ("NFT-подарок из Telegram: кнопка «Забрать» открывает лот у продавца. Проверь, что название "
                "совпадает один в один, и что итоговая цена с комиссией всё ещё в твою пользу.")
        rows.append(row("demo-g%02d" % i, title, "GIFTS", "Вся РФ", price, market,
                        tags, seller, rating, reviews, desc, FRAME))
    for i, (title, city, price, tags, seller, rating, reviews, desc) in enumerate(TG_LOTS, 1):
        rows.append(("demo-t%02d" % i, title, "TG", city, int(price), int(price), 0, 0.0,
                     tags, seller, float(rating), int(reviews), desc,
                     "https://t.me/avito_msk_posts/%d" % (2000 + i)))
    for i, (title, source, city, price, market, tags, seller, rating, reviews, desc) in enumerate(MARKET_LOTS, 1):
        rowid = "demo-w%02d" % i
        url = ("https://www.wildberries.ru/catalog/demo/%d/detail.aspx" if source == "Wildberries"
               else "https://www.ozon.ru/product/demo/%d") % (3000 + i)
        rows.append(row(rowid, title, source, city, price, market, tags, seller, rating, reviews, desc, url))
    return rows


def as_ingest_lot(r):
    (id_, title, source, city, price, market, _profit, _pct, tags, seller, rating, reviews, desc, url) = r
    return {
        "id": id_, "title": title, "source": source, "city": city,
        "price": price, "market": market, "tags": tags,
        "sellerName": seller, "sellerRating": rating, "sellerReviews": reviews,
        "description": desc, "url": url,
    }


def push_via_worker(env):
    base = env.get("WEBAPP_URL", "").rstrip("/")
    secret = env.get("WORKER_SECRET", "").strip()
    if not base or not secret:
        return False, "нет WEBAPP_URL или WORKER_SECRET в .env"
    proxy = env.get("PROXY_URL", "").strip()
    orig_socket = None
    if proxy and proxy.startswith("socks5"):
        try:
            import socks
            import socket as _so
            from urllib.parse import urlparse
            u = urlparse(proxy)
            socks.set_default_proxy(socks.SOCKS5, addr=u.hostname, port=u.port or 1080)
            orig_socket = _so.socket
            _so.socket = socks.socksocket
            print("использую SOCKS-прокси", u.hostname)
        except Exception as e:
            print("прокси недоступен (%s), иду напрямую" % str(e)[:80])
    url = base + "/api/worker/ingest"
    payload = json.dumps([as_ingest_lot(r) for r in build_rows()], ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "x-worker-secret": secret,
                                          "User-Agent": "DealHunterSeed/1.0", "Accept": "application/json"})
    try:
        for attempt in range(1, 5):
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    print("worker ingest:", body)
                    return True, None
            except Exception as e:
                print("попытка %d сорвалась: %s" % (attempt, str(e)[:140]))
                time.sleep(4)
        return False, "не удалось залить за 4 попыток"
    finally:
        if orig_socket is not None:
            import socket as _so
            _so.socket = orig_socket


INSERT = """
INSERT INTO dh_lots (id, title, source, city, price, market, profit_rub, profit_pct, tags,
                     seller_name, seller_rating, seller_reviews, description, url, created_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14, NOW() AT TIME ZONE 'utc')
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title, source = EXCLUDED.source, city = EXCLUDED.city,
    price = EXCLUDED.price, market = EXCLUDED.market,
    profit_rub = EXCLUDED.profit_rub, profit_pct = EXCLUDED.profit_pct,
    tags = EXCLUDED.tags, seller_name = EXCLUDED.seller_name,
    seller_rating = EXCLUDED.seller_rating, seller_reviews = EXCLUDED.seller_reviews,
    description = EXCLUDED.description, url = EXCLUDED.url,
    gone = FALSE, gone_at = NULL, created_at = NOW() AT TIME ZONE 'utc'
"""


async def push_via_db(env):
    dsn = env.get("DATABASE_URL", "").strip()
    if not asyncpg or not dsn:
        return False, "нет asyncpg или DATABASE_URL"
    rows = build_rows()
    for attempt in range(1, 4):
        try:
            conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=30)
            for r in rows:
                await conn.execute(INSERT, *r)
            await conn.close()
            return True, None
        except Exception as e:
            print("DB попытка %d сорвалась: %s" % (attempt, str(e)[:140]))
            await asyncio.sleep(4)
    return False, "не удалось залить в БД за 3 попыток"


def main():
    env = load_env(".env")
    ok, err = push_via_worker(env)
    if ok:
        print("готово: демо-лоты залиты через воркер")
        return
    print("воркер не сработал (%s), пробую прямую запись в БД" % err)
    ok2, err2 = asyncio.run(push_via_db(env))
    if ok2:
        print("готово: демо-лоты залиты напрямую в базу")
    else:
        print("не получилось: %s" % err2)
        sys.exit(1)


if __name__ == "__main__":
    main()
