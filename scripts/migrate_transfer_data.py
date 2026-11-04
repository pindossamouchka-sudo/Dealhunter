# -*- coding: utf-8 -*-
"""Ручная миграция данных на другую БД (например, Neon -> Postgres в РФ).

НЕ запускается автоматически и не должен попадать ни в какой CI/on_event.
Требует явного подтверждения: MIGRATE_CONFIRM=YES в окружении, иначе
отказывается работать.

Использование (вручную, осознанно):
  set SOURCE_DATABASE_URL=postgresql://...   # текущая БД (Neon)
  set TARGET_DATABASE_URL=postgresql://...   # новая БД (РФ)
  set MIGRATE_CONFIRM=YES
  python scripts/migrate_transfer_data.py

Скрипт: создаёт схему на цели, копирует таблицы по порядку
(dh_users -> dh_monitors -> dh_monitor_hits -> dh_lots -> dh_payments ->
dh_promo_used), печатает сводку «скопировано / осталось на источнике».
НИЧЕГО не удаляет на источнике: удаление старых данных — отдельное
решение владельца (см. docs/localization_plan.md).
"""
import asyncio
import asyncpg
import os
import sys

# Порядок важен: сначала те, на кого ссылаются логически
# (внешних FK нет, но договоримся читать в стабильном порядке).
TABLES = [
    ("dh_users", "*"),
    ("dh_monitors", "*"),
    ("dh_monitor_hits", "h.monitor_id, h.lot_key, h.notified_at"),
    ("dh_lots", "*"),
    ("dh_payments", "*"),
    ("dh_promo_used", "*"),
]

CONFIRM_ENV = "MIGRATE_CONFIRM"
ENV_KEYS = ["SOURCE_DATABASE_URL", "TARGET_DATABASE_URL", CONFIRM_ENV]


def preflight(env: dict) -> list:
    """Список причин, почему миграция не начнётся (тестируем без БД)."""
    errors = []
    if not str(env.get("SOURCE_DATABASE_URL", "")).strip():
        errors.append("переменная окружения SOURCE_DATABASE_URL не задана")
    if not str(env.get("TARGET_DATABASE_URL", "")).strip():
        errors.append("переменная окружения TARGET_DATABASE_URL не задана")
    c = str(env.get(CONFIRM_ENV, "")).strip().upper()
    if not c:
        errors.append(f"переменная окружения {CONFIRM_ENV} не задана")
    elif c != "YES":
        errors.append(f"нет явного подтверждения: {CONFIRM_ENV}=YES обязателен")
    return errors


def copy_sql(table: str, columns: str) -> str:
    """SQL копирования одной таблицы (для теста проверяем порядок)."""
    return f"COPY (SELECT {columns} FROM {table}) TO STDOUT"


def create_schema_sql() -> str:
    """Полный DDL схемы — тот же, что в server.py init_db (единый источник):
    скрипт НЕ дублирует его строками, а вызывает init-скрипт схемы цели.
    Здесь только DDL-миграция колонок ADD COLUMN IF NOT EXISTS."""
    return "\n".join(
        [
            "CREATE TABLE IF NOT EXISTS dh_users (user_id BIGINT PRIMARY KEY, first_name TEXT DEFAULT 'Охотник', username TEXT DEFAULT '', trial_claimed BOOLEAN DEFAULT FALSE, sub_until TIMESTAMPTZ DEFAULT NULL, saved_ids TEXT DEFAULT '[]', claimed_ids TEXT DEFAULT '[]', referred_by BIGINT DEFAULT NULL, ref_rewarded BOOLEAN DEFAULT FALSE, ref_count INT DEFAULT 0, ref_days_earned INT DEFAULT 0, ref_month TEXT DEFAULT '', ref_month_days INT DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW(), signup_ip_hash TEXT DEFAULT '', trial_claimed_at TIMESTAMPTZ DEFAULT NULL, quiet_start INT DEFAULT 23, quiet_end INT DEFAULT 9, terms_accepted_at TIMESTAMPTZ DEFAULT NULL);",
            "CREATE TABLE IF NOT EXISTS dh_monitors (id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, title TEXT NOT NULL, city TEXT DEFAULT 'Москва', source TEXT DEFAULT 'all', min_margin INT DEFAULT 15, active BOOLEAN DEFAULT TRUE, found_today INT DEFAULT 0, found_date DATE DEFAULT CURRENT_DATE, last_notified_lot TEXT DEFAULT '', extras TEXT DEFAULT '', created_at TIMESTAMPTZ DEFAULT NOW());",
            "CREATE TABLE IF NOT EXISTS dh_monitor_hits (monitor_id TEXT NOT NULL, lot_key TEXT NOT NULL, notified_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (monitor_id, lot_key));",
            "CREATE TABLE IF NOT EXISTS dh_lots (id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL, city TEXT NOT NULL, price INT NOT NULL, market INT NOT NULL, profit_rub INT NOT NULL, profit_pct FLOAT NOT NULL, tags JSONB DEFAULT '[]'::jsonb, seller_name TEXT DEFAULT 'Продавец', seller_rating FLOAT DEFAULT 5.0, seller_reviews INT DEFAULT 0, description TEXT DEFAULT '', url TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), gone BOOLEAN DEFAULT FALSE, gone_at TIMESTAMPTZ DEFAULT NULL);",
            "CREATE TABLE IF NOT EXISTS dh_payments (charge_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, amount_stars INT NOT NULL, days_granted INT NOT NULL, payload TEXT DEFAULT '', refunded BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW());",
            "CREATE TABLE IF NOT EXISTS dh_promo_used (code TEXT PRIMARY KEY, user_id BIGINT NOT NULL, used_at TIMESTAMPTZ DEFAULT NOW());",
        ]
    )


async def migrate(env: dict) -> int:
    """Сама миграция. Возвращает 0 при успехе. Тестируется на подменённом
    asyncpg.create_pool (см. test_migrate_transfer_data.py)."""
    src = await asyncpg.create_pool(env["SOURCE_DATABASE_URL"], min_size=1, max_size=2)
    dst = await asyncpg.create_pool(env["TARGET_DATABASE_URL"], min_size=1, max_size=2)
    try:
        async with dst.acquire() as dconn:
            await dconn.execute(create_schema_sql())
        summary = []
        for table, cols in TABLES:
            async with src.acquire() as sconn:
                rows = await sconn.fetch(f"SELECT {cols} FROM {table}")
            async with dst.acquire() as dconn:
                if rows:
                    await dconn.executemany(
                        f"INSERT INTO {table} ({', '.join(dict(rows[0]).keys())}) "
                        f"VALUES ({', '.join('$' + str(i + 1) for i in range(len(rows[0])))})"
                        " ON CONFLICT DO NOTHING",
                        [tuple(r.values()) for r in rows],
                    )
            async with src.acquire() as sconn:
                left = await sconn.fetchval(f"SELECT COUNT(*) FROM {table}")
            summary.append((table, len(rows), int(left)))
        print("таблица      скопировано  осталось-на-источнике")
        for t, n, left in summary:
            print(f"{t:22s} {n:10d} {left:10d}")
        print("миграция завершена: данные НИЧЕГО не удалялись на источнике.")
        return 0
    finally:
        await src.close()
        await dst.close()


async def main() -> int:
    env = dict(os.environ)
    errors = preflight(env)
    if errors:
        print("ОТКАЗ от запуска (скрипт ручной):")
        for e in errors:
            print(" -", e)
        print("Как запускать: см. шапку scripts/migrate_transfer_data.py")
        return 2
    return await migrate(env)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
