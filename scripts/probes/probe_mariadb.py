"""T1 probe: MariaDB via PyMySQL. Run: python -I scripts/probes/probe_mariadb.py (repo root as cwd)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402
import _mysqlfamily as M  # noqa: E402

T = {
    "max_statement_time_select": {
        "pre": ["SET SESSION max_statement_time=0.3"],
        "sql": "select sleep(3)",
        "mechanism": "SET SESSION max_statement_time=0.3 (s)",
        "check": ["select @@max_statement_time"],
    },
    "max_statement_time_select_from_table": {
        "pre": ["SET SESSION max_statement_time=0.3"],
        "sql": "select sleep(3) from scratch.orders",
        "mechanism": "SET SESSION max_statement_time=0.3 (s)",
    },
    "max_statement_time_dml": {
        "pre": ["SET SESSION max_statement_time=0.3"],
        "sql": "insert into scratch.t_probe select sleep(1)",
        "mechanism": "SET SESSION max_statement_time=0.3 (s); non-SELECT",
    },
    "wrong_variable_max_execution_time": {
        "pre": ["SET SESSION MAX_EXECUTION_TIME=300"],
        "sql": "select 1",
        "mechanism": "MySQL variable on MariaDB",
    },
    "client_read_timeout_1s": {
        "engine_kw": {"connect_args": {"read_timeout": 1}},
        "sql": "select sleep(3)",
        "mechanism": "PyMySQL read_timeout=1 (s) connect arg",
    },
}
H.main(M.build("mariadb", int(os.environ.get("SQLX_MARIADB_PORT", "33062")), T))
