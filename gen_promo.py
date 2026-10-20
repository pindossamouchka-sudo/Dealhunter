#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор одноразовых промокодов для DealHunter PRO.
Код = DH-<8hex>-<8hex>; вторая часть — HMAC-SHA256(PROMO_SECRET, "DH"+rnd)[:8].
PROMO_SECRET читается из .env (в репо НЕ хранится). Генерация:  python gen_promo.py 5
"""
import os
import sys
import hmac
import secrets
import hashlib

from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
PROMO_SECRET = os.getenv("PROMO_SECRET", "")
if not PROMO_SECRET:
    print("[!] В .env нет PROMO_SECRET. Сгенерируй: python -c \"import secrets;print(secrets.token_hex(32))\"")
    sys.exit(1)

try:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
except ValueError:
    n = 1
n = max(1, min(50, n))

codes = []
for _ in range(n):
    rnd = secrets.token_hex(4)
    sig = hmac.new(PROMO_SECRET.encode(), ("DH" + rnd).encode(), hashlib.sha256).hexdigest()[:8]
    codes.append(f"DH-{rnd.upper()}-{sig.upper()}")

print("Новые коды (каждый — один раз на любого пользователя, +30 дней PRO):")
for c in codes:
    print(" ", c)
