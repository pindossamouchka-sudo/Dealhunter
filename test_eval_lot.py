# -*- coding: utf-8 -*-
"""Юнит-тесты новой оценки (правила ТЗ 1-8). Запуск: python test_eval_lot.py"""
import time
import random

# подставляем чистую функцию без сети: Gifts scan импортирует кучу модулей,
# поэтому тянем только текст нужных функций? Нет — импортируем модуль целиком:
# он безопасен (весь сети теперь в других функциях).
import gifts_scan as gs

RATE = 100.0  # 1 TON = 100 RUB (фикс для теста)
FEE = 10.0    # комиссия маркета 10%

cases = []


def case(name, ok):
    cases.append((name, bool(ok)))


# --- молитва末期бность: чистые подстановки — floor_model=100 TON ---

# Лот 1: цена = флор (100) → отбросить (правила 4/5: discount=0)
r1 = gs.evaluate_lot(100.0, 100.0, RATE, rare="")
case("лот1: цена = флор -> отбросить", r1 is None)

# Лот 2: цена выше флора (120) → отбросить
r2 = gs.evaluate_lot(120.0, 100.0, RATE, rare="")
case("лот2: цена выше флора -> отбросить", r2 is None)

# Лот 3: цена -15% от флора (85) → взять; профит = 100*0.9 - 85 = 5 TON
r3 = gs.evaluate_lot(85.0, 100.0, RATE, rare="")
case("лот3: цена -15% -> взять", r3 is not None
     and r3["reason"] == "-15% от флора"
     and abs(r3["discount"] - 0.15) < 1e-9
     and abs(r3["profit_ton"] - 5.0) < 1e-9
     and abs(r3["profit_rub"] - 500.0) < 1e-6)

# Лот 4: редкий фон (<2%), цена = флоро модели → взять, планка = 100*1.25 = 125
# discount = (125-100)/125 = 0.20 >= 0.10; профит = 125*0.9 - 100 = 12.5 TON (=1250 RUB)
r4 = gs.evaluate_lot(100.0, 100.0, RATE, rare="редкий фон")
case("лот4: редкий фон -> взять", r4 is not None
     and r4["reason"] == "редкий фон"
     and abs(r4["discount"] - 0.20) < 1e-9
     and abs(r4["profit_ton"] - 12.5) < 1e-9
     and abs(r4["profit_rub"] - 1250.0) < 1e-6)

# правило 5 для редких: редкий лот с ценой >= планки редкого (125) → отбросить
r4b = gs.evaluate_lot(126.0, 100.0, RATE, rare="редкий фон")
case("лот4б: редкий, цена >= планка редкого -> отбросить", r4b is None)

# --- правило 7: флор старше 10 минут → пересчитать из листингов ---
cached_old = {"value": 100.0, "ts": time.time() - 900.0}  # 15 минут назад
mlots = [
    {"model": "Test (1%)", "price_ton": 92.0},
    {"model": "Test (1%)", "price_ton": 110.0},
]
floor, age, recomputed = gs.get_model_floor(mlots, "Test (1%)", cached=cached_old, now=time.time())
case("правило 7: флор старше 10 мин пересчитывается (кеш 100 -> медиана 101)", recomputed and abs(floor - 101.0) < 1e-9)

# свежий кеш (2 мин) не пересчитывается
cached_new = {"value": 100.0, "ts": time.time() - 120.0}
floor2, age2, recomputed2 = gs.get_model_floor(mlots, "Test (1%)", cached=cached_new, now=time.time())
case("правило 7: свежий флор (2 мин) берётся из кеша", not recomputed2 and abs(floor2 - 100.0) < 1e-9)

# --- редкость: детектор ярлыков ---
# модель (1%) → редкая модель
mlots_bg = [{"model": "Rare Thing (1%)", "backdrop": "Blue", "symbol": "S", "price_ton": 10.0} for _ in range(60)]
rr = gs.rare_reason("Rare Thing (1%)", mlots_bg[0], mlots_bg)
case("редкость: модель <2% -> 'редкая модель'", rr == "редкая модель")

# фон = 1 из 5 лотов (20%) → не редкий по фону
mlots_mix = [{"model": "M", "backdrop": "Green" if i else "Blue", "symbol": "S", "price_ton": 10.0} for i in range(5)]
rr2 = gs.rare_reason("M", mlots_mix[0], mlots_mix)  # Blue один из 5 (20%)
case("редкость: фон 20% -> не редкий", rr2 == "")

frac = gs.rare_reason("M", {"backdrop": "Blue", "symbol": "S"}, mlots_mix)
case("редкость: фон 20% (1/5) -> не редкий", frac == "")

mlots_big = [{"model": "M", "backdrop": "Green" if i else "Blue", "symbol": "S", "price_ton": 10.0} for i in range(180)]
frac2 = gs.rare_reason("M", mlots_big[0], mlots_big)
case("редкость: фон ~0.5% (1/180) -> 'редкий фон'", frac2 == "редкий фон")

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases))) if ok_cnt == len(cases) else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
