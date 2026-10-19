# -*- coding: utf-8 -*-
"""
playerok_scan.py — сканер Playerok (playerok.com) через публичный каталог.

ТОЛЬКО публичные данные (без логина, без cookies, без чужих аккаунтов).
Анонимно площадка отдаёт «Топ» и «Официальные» лоты (пачками по 50).
Логика: собираем N пачек, группируем по (игра, категория), в группах от 5 лотов
считаем медианную цену = «рыночная планка». Лот дешевле планки на 15% и больше
— выгодный: пушим на сервер (source=PLAYEROK, market=медиана, price=цена).

Опциональный фильтр подробностей: playerok_dbg.txt не нужен;
строки вида «Genshin» в playerok_watchlist.txt оставляют только эти игры.

Запуск:
  python playerok_scan.py            # скан + пуш
  python playerok_scan.py --no-push  # только посмотреть
"""
import asyncio
import json
import os
import random
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

SERVER_URL = os.getenv("SERVER_URL", "https://dealhunter-i8w2.onrender.com").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "")

WATCHLIST_FILE = os.path.join(HERE, "playerok_watchlist.txt")

PAGES_TOP = 4        # по 50 лотов
PAGES_OFFICIAL = 2
DEAL_RATIO = 0.90    # лот должен быть дешевле медианы на 10%+
MIN_GROUP = 5        # минимум лотов в группе, чтобы считать медиану


def load_game_filter() -> list:
    """Строки-подстроки: если заданы, сканируем только эти игры. Пустой = все."""
    if os.path.exists(WATCHLIST_FILE):
        qs = []
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    qs.append(line)
        return qs
    return []


def to_lot_dict(it, median: float, group: str) -> dict:
    price = int(round(float(it.price or 0)))
    market = int(round(median))
    if price < 1:
        price = 1
    if market <= price:
        market = price + 1
    profit = market - price
    pct = int(round(profit * 100 / market))
    title = (it.name or "Лот Playerok")[:200]
    tags = ["Playerok"]
    if group:
        tags.append(group[:40])
    tags = tags[:8]
    reviews = 0
    try:
        reviews = int(getattr(it.user, "testimonial_count", 0) or 0) if it.user else 0
    except Exception:
        reviews = 0
    return {
        "id": f"playerok-{it.slug or it.id}"[:80],
        "title": title,
        "source": "PLAYEROK",
        "city": "Вся РФ",
        "price": price,
        "market": market,
        "tags": tags,
        "sellerName": "Playerok",
        "sellerRating": 4.8,
        "sellerReviews": reviews,
        "description": (
            f"Игровой маркет Playerok · группа {group} · "
            f"медиана по {MIN_GROUP}+ лотам {median:.0f}₽, лот дешевле на {pct}%"
        )[:1000],
        "url": it.url,
    }


async def fetch_all(acc) -> list:
    items = []
    seen = set()

    async def take(kind: str, pages: int):
        after = None
        for _ in range(pages):
            if kind == "top":
                page = await acc.items.top(page_size=50, after=after)
            else:
                page = await acc.items.official(page_size=50, after=after)
            got = list(page.items or [])
            fresh = 0
            for it in got:
                if it.id in seen:
                    continue
                seen.add(it.id)
                items.append(it)
                fresh += 1
            print(f"  {kind}: +{fresh} (всего {len(items)})")
            if not page.has_next_page or not page.end_cursor:
                break
            after = page.end_cursor
            await asyncio.sleep(random.uniform(1.5, 3.0))

    await take("top", PAGES_TOP)
    await take("official", PAGES_OFFICIAL)
    return items


async def scan() -> tuple:
    from PlayerokAPI import Account
    print(f"[{datetime.now(timezone.utc).isoformat()}Z] playerok_scan: топ+официальные")
    game_filter = [g.lower() for g in load_game_filter()]
    async with Account() as acc:
        items = await fetch_all(acc)

    groups = {}
    for it in items:
        if not it.price or it.price <= 0:
            continue
        game = (getattr(it.game, "name", "") or "").strip()
        cat = (getattr(it.category, "name", "") or "").strip()
        if game_filter and not any(f in game.lower() for f in game_filter):
            continue
        groups.setdefault(cat or "Другое", []).append(it)

    lots = []
    for group, its in sorted(groups.items()):
        if len(its) < MIN_GROUP:
            continue
        prices = [float(i.price) for i in its]
        med = statistics.median(prices)
        if med <= 0:
            continue
        deals = [i for i in its if i.price <= med * DEAL_RATIO]
        if deals:
            print(f"  [+] {group}: {len(its)} лотов, медиана {med:.0f}₽, выгодных {len(deals)}")
            for it in deals:
                lots.append(to_lot_dict(it, med, group))
    return lots, len(groups), len(items)


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


def main() -> int:
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    lots, groups, total = asyncio.run(scan())
    print(f"прочитано лотов: {total}, групп: {groups}, выгодных: {len(lots)}")
    if "--no-push" not in sys.argv:
        saved, skipped = push_to_server(lots)
        print(f"пуш: saved={saved} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
