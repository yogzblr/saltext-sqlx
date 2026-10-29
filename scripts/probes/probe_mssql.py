"""T1 probe: SQL Server via pymssql. UNTESTED against a server: no SQL Server could be started in the T1 environment.
Only the client-side cases (unreachable port, blackhole, text() compile checks) were executed. The SQL below is written
for SQL Server 2022 (see tests-support/seed/mssql.sql) and has never run. Run: python -I scripts/probes/probe_mssql.py
"""

import importlib.metadata as md
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

P = int(os.environ.get("SQLX_MSSQL_PORT", "14331"))
URL = "mssql+pymssql://{a}:{pw}@127.0.0.1:%d/sqlx_test" % P
cfg = {
    "name": "mssql",
    "not_run_reason": "No server could be started: Docker Hub, MCR, Oracle and vendor package hosts return HTTP 403 to the egress proxy (policy), and there is no apt package. Only client-side cases ran.",
    "urls": {
        "ro": URL.format(a="ro", pw="Ro_pw_12345!"),
        "rw": URL.format(a="rw", pw="Rw_pw_12345!"),
    },
    "unknown_db_set": {"database": "nope"},
    "connect_timeout_args": {"login_timeout": 2},
    "cast_type": "int",
    "driver_versions": lambda: {"pymssql": md.version("pymssql")},
    "sql": {
        "ping": "select 1",
        "version": "select @@version",
        "syntax": "selec 1",
        "missing_table": "select * from nope",
        "missing_column": "select nope from scratch.orders",
        "div0": "select 1/0",
        "unique": "insert into scratch.uniq values ('a@example.com')",
        "perm_insert": "insert into scratch.orders (id) values (99)",
        "perm_select": "select * from private.secrets",
        "ro_update": "update scratch.orders set note = 'x'",
        "ro_delete": "delete from scratch.orders",
        "ro_ddl": "create table scratch.ddl_probe (i int)",
        "bad_cast": "insert into scratch.t_probe values (:x)",
        "bad_cast_acct": "rw",
        "write_probe": "insert into scratch.t_probe values (:i)",
        "count_probe": "select count(*) from scratch.t_probe",
        "delete_probe": "delete from scratch.t_probe",
        "types": "select * from scratch.orders order by id",
        "colon_cast": "select :a::int",
        "json_literal": "select '{\"a\":1}'",
        "json_literal_escaped": "select '{\"a\"\\:1}'",
        "percent": "select case when 'abc' like '%b%' then 1 else 0 end",
        "cast_bind": "select cast(:a as int) + cast(:a as int)",
    },
    "timeout": {
        "cases": {
            "pymssql_timeout_1s": {
                "engine_kw": {"connect_args": {"timeout": 1}},
                "sql": "waitfor delay '00:00:03'",
                "mechanism": "pymssql connect arg timeout=1 (s)",
            },
        }
    },
    "guard": {
        "guards": {},
        "flips": {},
    },  # SQL Server has no in-session read-only transaction; role only (unverified)
}
H.main(cfg)
