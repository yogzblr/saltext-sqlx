"""T1 probe: PostgreSQL via psycopg 3. Run: python -I scripts/probes/probe_postgresql.py (repo root as cwd)."""

import importlib.metadata as md
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

P = int(os.environ.get("SQLX_PG_PORT", "54329"))
URL = "postgresql+psycopg://{a}:{a}_pw@127.0.0.1:%d/sqlx_test" % P
SLEEP_GUARD = ["SET TRANSACTION READ ONLY"]
FLIP = ["SET TRANSACTION READ WRITE"]
cfg = {
    "name": "postgresql",
    "urls": {a: URL.format(a=a) for a in ("ro", "rw")},
    "unknown_db_set": {"database": "nope"},
    "connect_timeout_args": {"connect_timeout": 2},
    "cast_type": "int",
    "driver_versions": lambda: {"psycopg": md.version("psycopg")},
    "sql": {
        "ping": "select 1",
        "version": "select version()",
        "syntax": "selec 1",
        "missing_table": "select * from nope",
        "missing_column": "select nope from scratch.orders",
        "div0": "select 1/0",
        "div0_dml": "insert into scratch.t_probe select 1/0",
        "unique": "insert into scratch.uniq values ('a@example.com')",
        "perm_insert": "insert into scratch.orders (id) values (99)",
        "perm_select": "select * from private.secrets",
        "ro_update": "update scratch.orders set note = 'x'",
        "ro_delete": "delete from scratch.orders",
        "ro_ddl": "create table scratch.ddl_probe (i int)",
        "bad_cast": "select cast(:x as int)",
        "write_probe": "insert into scratch.t_probe values (:i)",
        "count_probe": "select count(*) from scratch.t_probe",
        "delete_probe": "delete from scratch.t_probe",
        "types": "select * from scratch.orders order by id",
        "colon_cast": "select :a::int",
        "json_literal": "select '{\"a\":1}'::jsonb",
        "json_literal_escaped": "select '{\"a\"\\:1}'::jsonb",
        "percent": "select 'abc' like '%b%'",
        "cast_bind": "select cast(:a as int) + cast(:a as int)",
    },
    "timeout": {
        "cases": {
            "statement_timeout": {
                "pre": ["SET statement_timeout = 300"],
                "sql": "select pg_sleep(3)",
                "mechanism": "SET statement_timeout = 300 (ms)",
                "check": ["show statement_timeout"],
            },
        }
    },
    "guard": {
        "guards": {"SET TRANSACTION READ ONLY": SLEEP_GUARD},
        "flips": {"SET TRANSACTION READ WRITE": FLIP},
        "connection_level": {
            "default_transaction_read_only=on": (
                {"connect_args": {"options": "-c default_transaction_read_only=on"}},
                {"SET TRANSACTION READ WRITE": FLIP},
            )
        },
    },
}
H.main(cfg)
