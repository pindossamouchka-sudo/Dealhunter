# -*- coding: utf-8 -*-
"""Тесты правил отбора gifts_scan (Этап 1):
  Б) MIN_SAMPLES=3 — без сделок не оцениваем модель из <3 сопоставимых лотов;
  Д) MAX_DISCOUNT=0.50 — скидка >50% = подозрительный лот, не сделка;
  В) trait_excuse — «частый фоновый лот дешевле всех» не сделка;
  Э1.3) ACTIVITY_SALE_TYPES без AUCTION/PREMARKET_SALE.
Запуск: python test_gift_rules.py"""
import contextlib
import io
import os
from datetime import datetime, timedelta

import gifts_scan as gs

RATE = 100.0
cases = []


def case(name, ok, extra=""):
    cases.append((name, bool(ok)))
    if not ok and extra:
        print(f"      [детали] {extra}")


def lot(p, venue="Tonnel", bg="—", sym="—", num=0):
    return {"model": "Тест Модель", "price_ton": float(p), "venue": venue,
            "backdrop": bg, "symbol": sym, "gift_num": num or int(p),
            "gift_id": "", "tg_id": "", "id": "", "slug": ""}


MIN_BD = "мин. данных"

# ---------- Б) MIN_SAMPLES = 3 ----------

# 1) 2 лота, продаж нет -> «мало данных» для обоих
with contextlib.redirect_stdout(io.StringIO()):
    d2 = gs.find_with_model_deals({"T": [lot(100), lot(140)]}, RATE)
case("MIN_SAMPLES: 2 лота без сделок -> 0 сделок", len(d2) == 0)

# 2) 3 лота, продаж нет -> оценка есть (слабая), сделка при стагнации цен нет,
#    но планка уже вычисляется
with contextlib.redirect_stdout(io.StringIO()):
    d3 = gs.find_with_model_deals({"T": [lot(100), lot(140), lot(145)]}, RATE)
case("MIN_SAMPLES: 3 лота без сделок -> планка существует, но скидки нет -> 0",
     len(d3) == 0)

# 3) 3 лота + реальная сделка (1 точка продаж ниже соседей) -> сделка берётся
state = {"sales": {"t|тест модель": [
    [(datetime.utcnow() - timedelta(days=2)).isoformat() + "Z", 95.0, "api"]]}}
with contextlib.redirect_stdout(io.StringIO()):
    d3s = gs.find_with_model_deals(
        {"T": [lot(60), lot(100), lot(140), lot(145)]}, RATE, state)
case("MIN_SAMPLES: одна сделка по модели -> привязка планки настоящей ценой",
     len(d3s) == 1 and abs(d3s[0]["price_ton"] - 60.0) < 1e-9,
     str(d3s))

# 4) 2 лота + сделка -> сделок достаточно, MIN_SAMPLES не мешает
state2 = {"sales": {"t|тест модель": [
    [(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z", 100.0, "api"],
    [(datetime.utcnow() - timedelta(days=2)).isoformat() + "Z", 99.0, "api"],
    [(datetime.utcnow() - timedelta(days=3)).isoformat() + "Z", 101.0, "api"],
    [(datetime.utcnow() - timedelta(days=4)).isoformat() + "Z", 98.0, "api"],
    [(datetime.utcnow() - timedelta(days=5)).isoformat() + "Z", 100.5, "api"]]}}
with contextlib.redirect_stdout(io.StringIO()):
    d2s = gs.find_with_model_deals({"T": [lot(70), lot(150)]}, RATE, state2)
case("MIN_SAMPLES: 2 лота, но ≥5 продаж -> оценка через сделки", len(d2s) == 1
     and abs(d2s[0]["reference_ton"] - 100.0) < 1.0, str(len(d2s)))

# ---------- Д) MAX_DISCOUNT = 0.50 ----------

# скидка 49% против планки -> сделка берётся
ref_same = {"sales": {"t|тест модель": [
    [(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z", 1000.0, "api"] for _ in range(5)]}}
r49 = gs.evaluate_lot(510.0, 1000.0, RATE)
case("MAX_DISCOUNT: скидка 49% -> берём (51% чистыми)", r49 is not None)
# скидка 55% -> подозрительный лот, отбросить
r55 = gs.evaluate_lot(450.0, 1000.0, RATE)
case("MAX_DISCOUNT: скидка 55% -> отбросить (подозрительная)", r55 is None)
case("MAX_DISCOUNT: причина = 'подозрительная скидка'",
     gs.reject_reason(450.0, 1000.0, RATE).startswith("подозрительная скидка"))
# точная граница: скидка ровно 50% -> граница входит, берём
r50 = gs.evaluate_lot(500.0, 1000.0, RATE)
case("MAX_DISCOUNT: скидка ровно 50% -> берём (равенство не > )", r50 is not None)

# ---------- В) trait_excuse: частый фон ----------

def bg_lots(bg_rest_prices, bg_same_prices, lot_price, bg_rest="Красный", bg_same="Синий"):
    """лоты других фонов (bg_rest_prices), лоты фона 'Синий' (bg_same_prices),
    и наш лот на фоне 'Синий' (lot_price)."""
    out = [lot(p, bg=bg_rest) for p in bg_rest_prices]
    out += [lot(p, bg=bg_same) for p in bg_same_prices]
    our = lot(lot_price, bg=bg_same)
    return out + [our]

# 4.1) частый дешёвый фон, лот «как у всех» -> НЕ сделка
lots_freq = bg_lots([100, 105, 110], [85, 87, 89, 86], 88)
exc = gs.trait_excuse(lots_freq[-1], lots_freq)
case("trait_excuse: частый дешёвый фон, лот как у всех -> отбраковка", exc != "", exc)

# 4.2) тот же лот, НО фон редкий (2 шт., порог MIN_SAMPLES) -> не отбраковка
lots_rare = bg_lots([100, 105, 110, 102], [88, 87], 88)
exc2 = gs.trait_excuse(lots_rare[-1], lots_rare)
case("trait_excuse: фон нечастый (2 из 7) -> не отбраковка", exc2 == "", exc2)

# 4.3) частый фон, но лот ДОРОЖЕ фоновой нормы на 30% -> реальная сделка
lots_deep = bg_lots([100, 105, 110], [85, 87, 89, 86], 60)
exc3 = gs.trait_excuse(lots_deep[-1], lots_deep)
case("trait_excuse: лот ниже фоновой медианы -> не отбраковка", exc3 == "", exc3)

# 4.4) частый фон, различие фонов лишь 5% -> фон не объясняет -> не отбраковка
lots_close = bg_lots([100, 105, 110], [98, 99, 97, 96], 97)
exc4 = gs.trait_excuse(lots_close[-1], lots_close)
case("trait_excuse: фон дешевле лишь на ~5% -> не отбраковка", exc4 == "", exc4)

# 4.5) полный путь: лот-«фоновый» исключается из сделок find_with_model_deals
with contextlib.redirect_stdout(io.StringIO()):
    deals_bg = gs.find_with_model_deals({"T": lots_freq}, RATE)
case("полный путь: фоновый лот не пролез в сделки", len(deals_bg) == 0)

# ---------- Э1.3: ACTIVITY_SALE_TYPES ----------

case("sale types: только фиксированные сделки (без AUCTION/PREMARKET_SALE)",
     gs.ACTIVITY_SALE_TYPES == {"SALE", "INTERNAL_SALE"},
     str(gs.ACTIVITY_SALE_TYPES))

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases)))
      if ok_cnt == len(cases) else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
