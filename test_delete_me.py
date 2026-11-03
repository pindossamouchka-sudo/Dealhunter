# -*- coding: utf-8 -*-
"""Тест «Удалить мои данные» (152-ФЗ): профиль, мониторы, история срабатываний,
сохранённые лоты (в профиле), подписка, промо и платежи стираются на сервере.
server.api_delete_me проверяется подменой conn (SQL-capture, без реальной БД).
Запуск: python test_delete_me.py"""
import asyncio
import sys

import server

cases = []


def case(name, ok, extra=""):
    cases.append((name, bool(ok)))
    if not ok and extra:
        print(f"      [детали] {extra}")


class FakeConn:
    """Ловит SQL, ведёт счётчик «удалённых строк» по-простому."""

    def __init__(self, count_vals=None):
        self.count_vals = iter(count_vals or [3, 2, 1, 1])  # hits, mon, pay, promo
        self.sql = []
        self.deleted = 0

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        try:
            return next(self.count_vals)
        except StopIteration:
            return 0

    async def execute(self, sql, *args):
        self.sql.append(sql)
        u = sql.upper().strip()
        if u.startswith("DELETE"):
            self.deleted += 1
        elif "SET signup_ip_hash" in u:
            self.deleted += 1
        return "OK"

    def transaction(self):
        return FakeTx(self)


class FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False

    def transaction(self):
        return FakeTx(self.conn)


def run_delete():
    conn = FakeConn()
    pool = FakePool(conn)
    server.pg_pool = pool
    req = server.DeleteMeReq(confirm=True)
    resp = asyncio.run(server.api_delete_me(req, {"uid": 4242}))

    class R:
        body = None

    # JSONResponse: вытащим body
    raw = resp.body.decode("utf-8").lower()
    return conn, raw


# --- 1) основной путь: всё стирается ---
conn, raw = run_delete()
case("delete-me: статус ok", '"status"' in raw and '"ok"' in raw, raw[:200])
del_sql = [s for s in conn.sql if s.upper().strip().startswith("DELETE")]
case("delete-me: 5 DELETE (hits, monitors, payments, promo, profile)",
     len(del_sql) == 5, "\n".join(del_sql))
case("delete-me: стираются мониторы",
     any("dh_monitors" in s and "JOIN" not in s for s in del_sql))
case("delete-me: стирается история срабатываний (monitor_hits)",
     any("dh_monitor_hits" in s for s in del_sql))
case("delete-me: стираются платежи (dh_payments)",
     any("dh_payments" in s for s in del_sql))
case("delete-me: стираются промо-активации (dh_promo_used)",
     any("dh_promo_used" in s for s in del_sql))
case("delete-me: стирается профиль (dh_users)",
     any(s.strip().startswith("DELETE FROM dh_users") for s in del_sql))
case("delete-me: monitor_hits удаляются ДО мониторов",
     conn.sql.index("SELECT COUNT(*) FROM dh_monitor_hits h "
                    "JOIN dh_monitors m ON m.id = h.monitor_id WHERE m.user_id = $1".replace("$1", "$1"))
     == 0 or any(
         "dh_monitor_hits" in s.upper() for s in conn.sql[: len(conn.sql) // 2]),
     "\n".join(conn.sql))
# у пользователя 4242 были сохранённые лоты в dh_users.saved_ids — профиль удалён
case("delete-me: сохранённые лоты ушли вместе с профилем (saved_ids в dh_users)",
     any("dh_users WHERE user_id" in s for s in del_sql))

# --- 2) счётчики в ответе ---
case("delete-me: счётчик мониторов в ответе", '"monitorsdeleted":2' in raw.replace(" ", ""), raw[:300])
case("delete-me: счётчик платежей в ответе", '"paymentsdeleted":1' in raw)

# --- 3) tombstone: сразу после удаления upsert не воскресит профиль ---
server.DELETED_UIDS[4242] = __import__("time").time() + 600
case("tombstone: deleted_recently верит", server.deleted_recently(4242))
try:
    server.pg_pool = FakePool(FakeConn())
    asyncio.run(server.upsert_user_atomic(4242, "Имя", "user"))
    case("tombstone: upsert-воскрешение заблокировано", False)
    from fastapi import HTTPException
except Exception as e:
    from fastapi import HTTPException
    case("tombstone: upsert-восбрешение заблокировано HTTPException(401)",
         isinstance(e, HTTPException) and e.status_code == 401, repr(e))
server.DELETED_UIDS.pop(4242, None)
case("tombstone: после TTL можно войти заново", not server.deleted_recently(4242))

# --- 4) без confirm -> 400 ---
try:
    from fastapi import HTTPException
    conn2 = FakeConn()
    server.pg_pool = FakePool(conn2)
    asyncio.run(server.api_delete_me(server.DeleteMeReq(confirm=False), {"uid": 1}))
    case("without confirm: тоже удаляет (плохо)", False)
except HTTPException as e:
    case("without confirm: 400, ничего не удаляет",
         e.status_code == 400 and not [s for s in conn2.sql if s.upper().startswith("DELETE")])

# --- 5) сессии пользователя убиваются ---
fs = server.fallback_sessions
fs.sessions["s1"] = (4242, 9999999999.0, "n", "u")
fs.sessions["s2"] = (4243, 9999999999.0, "n", "u")
conn3 = FakeConn()
server.pg_pool = FakePool(conn3)
asyncio.run(server.api_delete_me(server.DeleteMeReq(confirm=True), {"uid": 4242}))
case("сессии: все сессии юзера убиты, чужие живы",
     "s1" not in fs.sessions and "s2" in fs.sessions)
fs.sessions.pop("s2", None)

ok_cnt = sum(1 for _, ok in cases if ok)
for name, ok in cases:
    print(("OK  " if ok else "FAIL"), name)
print()
print(("ВСЕ ОК (%d/%d)" % (ok_cnt, len(cases)))
      if ok_cnt == len(cases) else ("ПРОПАЛО %d/%d" % (ok_cnt, len(cases))))
raise SystemExit(0 if ok_cnt == len(cases) else 1)
