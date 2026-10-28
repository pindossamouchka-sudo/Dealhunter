# -*- coding: utf-8 -*-
"""Юнит-тесты оценки «по следующему лоту».
Референс лота = минимальная цена ДРУГИХ лотов той же модели.
Запуск: python test_eval_lot.py"""
import contextlib
import io

import gifts_scan as gs

RATE = 100.0  # 1 TON = 100 RUB (фикс для теста)

cases = []


def case(name, ok):
    cases.append((name, bool(ok)))


def make_lots(prices, gift="TestGift", model="Test Model"):
    return {gift: [
        {"model": model, "price_ton": float(p), "venue": "Tonnel",
         "backdrop": "—", "symbol": "—", "id": str(i)}
        for i, p in enumerate(prices)
    ]}


# --- чистая функция evaluate_lot ---

# цена = референс -> отбросить
r1 = gs.evaluate_lot(100.0, 100.0, RATE)
case("лот1: цена = референс -> отбросить", r1 is None)

# цена выше референса -> отбросить
r2 = gs.evaluate_lot(120.0, 100.0, RATE)
case("лот2: цена выше референса -> отбросить", r2 is None)

# цена -15% -> взять; профит = 100*0.9 - 85 = 5 TON
r3 = gs.evaluate_lot(85.0, 100.0, RATE)
case("лот3: цена -15% -> взять", r3 is not None
     and r3["reason"] == "-15% от следующего лота"
     and abs(r3["discount"] - 0.15) < 1e-9
     and abs(r3["profit_ton"] - 5.0) < 1e-9
     and abs(r3["profit_rub"] - 500.0) < 1e-6)

# ровно -10% -> профит после комиссии = 0 -> отбросить (нужно > 0)
r4 = gs.evaluate_lot(90.0, 100.0, RATE)
case("лот4: ровно -10% -> профит 0 -> отбросить", r4 is None)

# чуть ниже -10% -> взять
r5 = gs.evaluate_lot(89.9, 100.0, RATE)
case("лот5: -10.1% -> взять", r5 is not None
     and r5["reason"] == "-10% от следующего лота")

# --- референс исключает сам лот ---

mlots = [{"model": "M", "price_ton": 90.0}, {"model": "M", "price_ton": 135.0}]
ref_a = gs.get_model_floor(mlots, "M", exclude_lot=mlots[0])
ref_b = gs.get_model_floor(mlots, "M", exclude_lot=mlots[1])
case("референс: для лота 90 -> 135", abs(ref_a - 135.0) < 1e-9)
case("референс: для лота 135 -> 90", abs(ref_b - 90.0) < 1e-9)

# два лота по одной цене: у каждого референс = 90 -> сделки нет
mlots_dup = [{"model": "M", "price_ton": 90.0}, {"model": "M", "price_ton": 90.0}]
ref_c = gs.get_model_floor(mlots_dup, "M", exclude_lot=mlots_dup[0])
case("референс: дубль 90/90 -> 90 (сделки нет)", abs(ref_c - 90.0) < 1e-9)

# --- полный путь через find_with_model_deals ---

with contextlib.redirect_stdout(io.StringIO()):
    deals_flat = gs.find_with_model_deals(
        make_lots([135, 138, 142, 150, 158, 170, 185, 200]), RATE)
case("полный путь: 135,138,142,150,158,170,185,200 -> 0 сделок",
     len(deals_flat) == 0)

with contextlib.redirect_stdout(io.StringIO()):
    deals_one = gs.find_with_model_deals(
        make_lots([90, 135, 138, 142, 150]), RATE)
case("полный путь: 90,135,138,142,150 -> ровно одна сделка", len(deals_one) == 1)
case("полный путь: сделка именно на лоте за 90",
     len(deals_one) == 1 and abs(deals_one[0]["price_ton"] - 90.0) < 1e-9)
case("полный путь: причина '-33% от следующего лота'",
     len(deals_one) == 1 and deals_one[0]["fair_src"] == "-33% от следующего лота")

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases))) if ok_cnt == len(cases)
      else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
