# -*- coding: utf-8 -*-
"""Тест миграционного скрипта на ПУСТОЙ базе (имитация asyncpg, без реальных БД).

Проверяет:
  1) без MIGRATE_CONFIRM=YES / без URL-переменных скрипт отказывается работать;
  2) на пустой базе migrate() не падает, создаёт схему, печатает по каждой
     таблице 0 скопировано / 0 осталось, возвращает 0;
  3) порядок копирования таблиц соответствует TABLES;
  4) скрипт НИЧЕГО не удаляет (в SQL-журнале нет DELETE/DROP/TRUNCATE).

Запуск: python test_migrate_transfer_data.py"""
import asyncio
import sys

sys.path.insert(0, "scripts")
import migrate_transfer_data as mig  # noqa: E402

cases = []


def case(name, ok, extra=""):
    cases.append((name, bool(ok)))
    if not ok and extra:
        print(f"      [детали] {extra}")


# --- 1) preflight-защита от случайного запуска ---
errs_none = mig.preflight({})
case("preflight: без всего отказывается", len(errs_none) == 3, str(errs_none))
errs_no_confirm = mig.preflight(
    {"SOURCE_DATABASE_URL": "postgres://a", "TARGET_DATABASE_URL": "postgres://b"})
case("preflight: есть URL, нет MIGRATE_CONFIRM=YES -> отказ",
     len(errs_no_confirm) == 1 and "MIGRATE_CONFIRM" in errs_no_confirm[0], str(errs_no_confirm))
errs_ok = mig.preflight({"SOURCE_DATABASE_URL": "postgres://a",
                         "TARGET_DATABASE_URL": "postgres://b", "MIGRATE_CONFIRM": "YES"})
case("preflight: все данные заданы -> разрешает", errs_ok == [])


# --- 2) пустая база: fake-pool ---
class FakeRecord:
    def __init__(self, d):
        self._d = d

    def values(self):
        return tuple(self._d.values())

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]


class FakeConn:
    def __init__(self, log, store):
        self.log, self.store = log, store

    def _exec(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, *a):
        self.log.append(("execute", sql))
        return "OK"

    async def fetch(self, sql, *a):
        self.log.append(("fetch", sql))
        return []

    async def fetchval(self, sql, *a):
        self.log.append(("fetchval", sql))
        return 0

    async def executemany(self, sql, args):
        self.log.append(("executemany", sql))
        return "OK"


class FakePool:
    def __init__(self, log):
        self.log = log

    def acquire(self):
        # каждая acquire — независимый fake-conn, но один общий журнал
        return FakeConn(self.log, None)

    async def close(self):
        pass


CREATED = []
LOG = []

async def fake_create_pool(url, **kw):
    CREATED.append(url)
    return FakePool(LOG)


async def run_migrate():
    saved = mig.asyncpg
    try:
        class FakeAsyncpg:
            create_pool = staticmethod(fake_create_pool)
        mig.asyncpg = FakeAsyncpg
        env = {"SOURCE_DATABASE_URL": "postgres://src",
               "TARGET_DATABASE_URL": "postgres://dst",
               "MIGRATE_CONFIRM": "YES"}
        rc = await mig.migrate(env)
    finally:
        mig.asyncpg = saved
    return rc


rc = asyncio.new_event_loop().run_until_complete(run_migrate())
case("migrate: пустая база, возврат 0", rc == 0)
case("migrate: подключился и к источнику и к цели",
     CREATED == ["postgres://src", "postgres://dst"], str(CREATED))

# схема создаётся на цели
schema_exec = [s for kind, s in LOG if kind == "execute"]
case("migrate: DDL схемы исполнен",
     any("CREATE TABLE IF NOT EXISTS dh_users" in s for s in schema_exec))

# порядок таблиц в SELECT-журнале
fetched_tables = [s.split("FROM ")[1].split()[0] for kind, s in LOG
                  if kind == "fetch" and "SELECT" in s and "COUNT" not in s]
expected = [t for t, _ in mig.TABLES]
case("migrate: порядок копирования = TABLES", fetched_tables == expected,
     f"{fetched_tables} vs {expected}")
case("migrate: копируются все 6 таблиц", len(fetched_tables) == len(mig.TABLES))

# count-запросы по каждой таблице в конце
counts = [s.split("FROM ")[1].split()[0] for kind, s in LOG
          if kind == "fetchval" and "COUNT" in s]
case("migrate: сверка остатков на источнике по каждой таблице",
     sorted(counts) == sorted(expected), f"{counts}")

# ничего не удаляется
all_sql = " ".join(s for _, s in LOG).upper()
case("migrate: в журнале нет DELETE/DROP/TRUNCATE",
     "DELETE" not in all_sql and "DROP" not in all_sql and "TRUNCATE" not in all_sql)

# executemany не вызывался ни разу (база пуста)
case("migrate: на пустой базе вставок нет",
     not [1 for kind, _s in LOG if kind == "executemany"])

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases)))
      if ok_cnt == len(cases) else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
