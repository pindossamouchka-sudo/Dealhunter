# -*- coding: utf-8 -*-
"""Юнит-тесты честной планки и правил отбора (gifts_scan).
Правила: MIN_SAMPLES=3, MAX_DISCOUNT=0.50, trait_excuse, ACTIVITY_SALE_TYPES.
Запуск: python test_eval_lot.py (все тесты в проекте: см. test_gift_rules.py)
"""
import contextlib
import io

import gifts_scan as gs

RATE = 100.0  # 1 TON = 100 ₽ (фикс для теста)

cases = []


def case(name, ok):
    cases.append((name, bool(ok)))


def make_model_lots(prices, gift="ТестГифт", model="Тест Модель", venue="Tonnel",
                    backdrop="—", symbol="—"):
    return [
        {"model": model, "price_ton": float(p), "venue": venue,
         "backdrop": backdrop, "symbol": symbol, "gift_num": i + 1,
         "gift_id": "",
         "tg_id": "", "id": "", "slug": ""}
        for i, p in enumerate(prices)
    ]


# --- evaluate_lot против честной планки (без 'reason': используем reject_reason) ---

r1 = gs.evaluate_lot(100.0, 100.0, RATE)
case("лот1: цена = планка -> отбросить", r1 is None)

r2 = gs.evaluate_lot(120.0, 100.0, RATE)
case("лот2: цена выше планки -> отбросить", r2 is None)

# цена 15% ниже планки: профит = 100*0.9 - 85 = 5 TON = 500 ₽ -> ровно порог, берём
r3 = gs.evaluate_lot(85.0, 100.0, RATE)
case("лот3: -15% -> взяли; дисконт 0.15, профит 5 TON",
     r3 is not None and abs(r3["discount"] - 0.15) < 1e-9
     and abs(r3["profit_ton"] - 5.0) < 1e-9
     and r3["profit_rub"] == 500)

# ровно -10%: профит после комиссии 10% = 0 -> отбросить
r4 = gs.evaluate_lot(90.0, 100.0, RATE)
case("лот4: ровно -10% -> профит 0 -> отбросить", r4 is None)

# чуть ниже -10%, но профит 1 TON*100 = 100₽ < 500₽ -> отбросить
r5 = gs.evaluate_lot(89.9, 100.0, RATE)
case("лот5: -10.1%, но профит < 500₽ -> отбросить", r5 is None)

case("причина лот5 (скидка -10.1% < DEAL_PCT 12%): скидка < порога",
     gs.reject_reason(89.9, 100.0, RATE) == "скидка < порога")

# --- полный путь find_with_model_deals ---

with contextlib.redirect_stdout(io.StringIO()):
    deals_flat = gs.find_with_model_deals(
        {"ТестГифт": make_model_lots([135, 138, 142, 150, 158, 170, 185, 200])}, RATE)
case("полный путь: равные соседи, дешёвых нет -> 0 сделок", len(deals_flat) == 0)

with contextlib.redirect_stdout(io.StringIO()):
    deals_one = gs.find_with_model_deals(
        {"ТестГифт": make_model_lots([90, 135, 138, 142, 150])}, RATE)
case("полный путь: 90 против соседей -> ровно одна сделка", len(deals_one) == 1)
case("полный путь: сделка именно на лоте за 90",
     len(deals_one) == 1 and abs(deals_one[0]["price_ton"] - 90.0) < 1e-9)
case("полный путь: источник планки = флор соседа площадки",
     len(deals_one) == 1 and "флор соседа" in deals_one[0]["fair_src"])

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases)))
      if ok_cnt == len(cases) else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
