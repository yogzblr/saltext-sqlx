"""T1 probe: PostgreSQL database created as SQL_ASCII (the BUILD_PROMPT 'mistake #1'). Records what a caller sees.
Creates and drops database sqlx_ascii using the admin account. Run: python -I scripts/probes/probe_postgresql_sqlascii.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402
import psycopg  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

P = int(os.environ.get("SQLX_PG_PORT", "54329"))
ADMIN = os.environ.get("SQLX_ADMIN_PW", "admin_pw")
adm = psycopg.connect(
    f"host=127.0.0.1 port={P} user=postgres password={ADMIN} dbname=postgres", autocommit=True
)
adm.execute("drop database if exists sqlx_ascii")
adm.execute(
    "create database sqlx_ascii encoding 'SQL_ASCII' lc_collate 'C' lc_ctype 'C' template template0"
)
adm.execute("grant connect on database sqlx_ascii to rw")
out = []
for label, sql in [
    ("first connect + select 1", "select 1"),
    ("select text literal", "select 'abc'"),
    ("select 'café'", "select 'café'"),
]:
    eng = sa.create_engine(
        f"postgresql+psycopg://rw:rw_pw@127.0.0.1:{P}/sqlx_ascii", poolclass=NullPool
    )
    try:
        with eng.connect() as c:
            v = c.exec_driver_sql(sql).scalar()
            out.append(
                {
                    "step": label,
                    "status": "ok",
                    "python_type": type(v).__name__,
                    "value": repr(v),
                    "client_encoding": c.exec_driver_sql("show client_encoding").scalar(),
                    "server_encoding": c.exec_driver_sql("show server_encoding").scalar(),
                }
            )
    except Exception as e:  # pylint: disable=broad-except
        r = H.classify(e, ["rw_pw"])
        out.append(
            {
                "step": label,
                "status": "error",
                "sa_exc": r["sa_exc"],
                "driver_exc": r["driver_exc"],
                "message": r["message"],
                "dbapi_error": r["dbapi_error"],
            }
        )
    finally:
        eng.dispose()
adm.execute("drop database sqlx_ascii")
os.makedirs("docs/wire_shapes", exist_ok=True)
json.dump(
    {"results": out},
    open("docs/wire_shapes/postgresql_sqlascii.json", "w"),
    indent=1,
    ensure_ascii=False,
)
for r in out:
    print(r)
