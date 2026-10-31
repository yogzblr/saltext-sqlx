import datetime
import decimal
import os
import uuid

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy import text
from sqlalchemy.pool import NullPool

url = os.environ.get("SQLX_PROBE_URL", "postgresql+psycopg://postgres@127.0.0.1:54329/postgres")
eng = sa.create_engine(url, poolclass=NullPool)


def err(e):
    o = getattr(e, "orig", None)
    d = getattr(o, "diag", None)
    return (
        type(e).__name__,
        type(o).__name__,
        getattr(o, "sqlstate", None),
        (d.message_primary if d else str(o)).strip(),
    )


print("1. error mapping")
for q in ["selec 1", "select * from nope", "select 1/0"]:
    try:
        with eng.connect() as c:
            c.execute(text(q))
    except exc.DBAPIError as e:
        print("  ", q, "->", err(e))

print("2. read-only guard")
with eng.connect() as c:
    c.exec_driver_sql("create table if not exists t_probe(id int)")
    c.commit()
for mode in ["no guard", "SET TRANSACTION READ ONLY"]:
    try:
        with eng.connect() as c:
            if mode != "no guard":
                c.exec_driver_sql(mode)
            c.execute(text("insert into t_probe values (1)"))
            c.commit()
        print("  ", mode, "-> write succeeded")
    except exc.DBAPIError as e:
        print("  ", mode, "->", err(e))
# guard bypass attempt: statement tries to flip it back
try:
    with eng.connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("SET TRANSACTION READ WRITE")
        c.execute(text("insert into t_probe values (2)"))
        c.commit()
    print("   bypass READ WRITE -> write succeeded (guard is NOT sufficient alone)")
except exc.DBAPIError as e:
    print("   bypass READ WRITE ->", err(e))
# COMMIT; then write in same string
try:
    with eng.connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("COMMIT")
        c.exec_driver_sql("insert into t_probe values (3)")
        c.commit()
    print("   bypass COMMIT -> write succeeded (guard is NOT sufficient alone)")
except exc.DBAPIError as e:
    print("   bypass COMMIT ->", err(e))
# connection-level read-only
eng_ro = sa.create_engine(
    url, poolclass=NullPool, connect_args={"options": "-c default_transaction_read_only=on"}
)
try:
    with eng_ro.connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ WRITE")
        c.execute(text("insert into t_probe values (4)"))
        c.commit()
    print("   session default_transaction_read_only + READ WRITE -> write succeeded")
except exc.DBAPIError as e:
    print("   default_transaction_read_only ->", err(e))
with eng.connect() as c:
    print("   rows in t_probe now:", c.execute(text("select count(*) from t_probe")).scalar())

print("3. statement timeout")
try:
    with eng.connect() as c:
        c.exec_driver_sql("SET statement_timeout = 300")
        c.execute(text("select pg_sleep(3)"))
except exc.DBAPIError as e:
    print("  ", err(e))

print("4. types / limits")
with eng.connect() as c:
    r = c.exec_driver_sql(
        "select 1::numeric(5,2) n, now() ts, gen_random_uuid() u, '\\xdeadbeef'::bytea b, '{\"a\":1}'::jsonb j, array[1,2] a, interval '1 day' i"
    )
    print("  ", {k: (type(v).__name__) for k, v in r.mappings().first().items()})
    r = c.execute(text("select g from generate_series(1,1000) g"))
    print("   fetchmany(6) ->", len(r.fetchmany(6)), "rows; columns:", list(r.keys()))
    r = c.execute(text("select cast(:a as int) + cast(:b as int) as s"), {"a": 2, "b": 3})
    print("   bind params ->", r.scalar())
    c.execute(text("drop table t_probe"))
    c.commit()
print("4b. colon / percent literals")
with eng.connect() as c:
    for label, fn in [
        ("text()  colon  ", lambda: c.execute(text("select '{\"a\":1}'::jsonb j"))),
        ("text()  ::cast ", lambda: c.execute(text("select 1::int"))),
        ("text()  percent", lambda: c.execute(text("select 'abc' like '%b%' x"))),
        ("driver  colon  ", lambda: c.exec_driver_sql("select '{\"a\":1}'::jsonb j")),
        ("driver  percent", lambda: c.exec_driver_sql("select 'abc' like '%b%' x")),
        ("driver %s param", lambda: c.exec_driver_sql("select %s::int + 1 x", (4,))),
        (
            "driver pct+param",
            lambda: c.exec_driver_sql("select 'abc' like '%b%' x, %s::int y", (4,)),
        ),
    ]:
        try:
            print("  ", label, "->", fn().fetchall())
        except Exception as e:
            print("  ", label, "-> ERR", type(e).__name__, str(e).splitlines()[0][:90])
print("5. password in exception text?")
try:
    sa.create_engine(
        "postgresql+psycopg://postgres:S3cretPw@127.0.0.1:1/x", poolclass=NullPool
    ).connect()
except exc.DBAPIError as e:
    print("  ", "S3cretPw" in str(e), "|", str(e).splitlines()[0][:110])
