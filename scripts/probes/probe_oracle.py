"""T1 probe: Oracle via oracledb thin. UNTESTED against a server: no Oracle server could be started in the T1 environment.
Only the client-side cases (unreachable port, blackhole, text() compile checks) were executed. The SQL below is written
for Oracle Free (see tests-support/seed/oracle.sql) and has never run. Run: python -I scripts/probes/probe_oracle.py
"""

import importlib.metadata as md
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

P = int(os.environ.get("SQLX_ORACLE_PORT", "15211"))
URL = "oracle+oracledb://{a}:{a}_pw@127.0.0.1:%d/?service_name=FREEPDB1" % P
GUARD = ["SET TRANSACTION READ ONLY"]
FLIP = ["SET TRANSACTION READ WRITE"]


def _set_call_timeout(conn):
    conn.connection.dbapi_connection.call_timeout = 300


cfg = {
    "name": "oracle",
    "not_run_reason": "No server could be started: Docker Hub, MCR, Oracle and vendor package hosts return HTTP 403 to the egress proxy (policy), and there is no apt package. Only client-side cases ran.",
    "urls": {a: URL.format(a=a) for a in ("ro", "rw")},
    "unknown_db_set": {"query": {"service_name": "NOPE"}},
    "connect_timeout_args": {"tcp_connect_timeout": 2},
    "cast_type": "number",
    "driver_versions": lambda: {"oracledb": md.version("oracledb")},
    "sql": {
        "ping": "select 1 from dual",
        "version": "select banner from v$version fetch first 1 rows only",
        "syntax": "selec 1 from dual",
        "missing_table": "select * from nope",
        "missing_column": "select nope from scratch.orders",
        "div0": "select 1/0 from dual",
        "div0_dml": "insert into scratch.t_probe select 1/0 from dual",
        "unique": "insert into scratch.uniq values ('a@example.com')",
        "perm_insert": "insert into scratch.orders (id) values (99)",
        "perm_select": "select * from private.secrets",
        "ro_update": "update scratch.orders set note = 'x'",
        "ro_delete": "delete from scratch.orders",
        "ro_ddl": "create table scratch.ddl_probe (i number)",
        "bad_cast": "insert into scratch.t_probe values (:x)",
        "bad_cast_acct": "rw",
        "write_probe": "insert into scratch.t_probe values (:i)",
        "count_probe": "select count(*) from scratch.t_probe",
        "delete_probe": "delete from scratch.t_probe",
        "types": "select * from scratch.orders order by id",
        "colon_cast": "select :a::int from dual",
        "json_literal": "select '{\"a\":1}' from dual",
        "json_literal_escaped": "select '{\"a\"\\:1}' from dual",
        "percent": "select case when 'abc' like '%b%' then 1 else 0 end from dual",
        "cast_bind": "select cast(:a as number) + cast(:a as number) from dual",
    },
    "timeout": {
        "cases": {
            "call_timeout": {
                "hook": _set_call_timeout,
                "sql": "begin dbms_session.sleep(3); end;",
                "mechanism": "connection.call_timeout = 300 (ms)",
            },
        }
    },
    "guard": {
        "guards": {"SET TRANSACTION READ ONLY": GUARD},
        "flips": {"SET TRANSACTION READ WRITE": FLIP},
    },
}
H.main(cfg)
