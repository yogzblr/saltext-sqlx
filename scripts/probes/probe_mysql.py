"""T1 probe: MySQL 8 via PyMySQL. Run: python -I scripts/probes/probe_mysql.py (repo root as cwd)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402
import _mysqlfamily as M  # noqa: E402

T = {
    "max_execution_time_select": {
        "pre": ["SET SESSION MAX_EXECUTION_TIME=300"],
        "sql": "select sleep(3)",
        "mechanism": "SET SESSION MAX_EXECUTION_TIME=300 (ms)",
        "check": ["select @@max_execution_time"],
    },
    "max_execution_time_select_from_table": {
        "pre": ["SET SESSION MAX_EXECUTION_TIME=300"],
        "sql": "select sleep(3) from scratch.orders",
        "mechanism": "SET SESSION MAX_EXECUTION_TIME=300 (ms)",
    },
    "max_execution_time_dml": {
        "pre": ["SET SESSION MAX_EXECUTION_TIME=300"],
        "sql": "insert into scratch.t_probe select sleep(1)",
        "mechanism": "SET SESSION MAX_EXECUTION_TIME=300 (ms); non-SELECT",
    },
    "wrong_variable_max_statement_time": {
        "pre": ["SET SESSION max_statement_time=0.3"],
        "sql": "select 1",
        "mechanism": "MariaDB variable on MySQL",
    },
    "client_read_timeout_1s": {
        "engine_kw": {"connect_args": {"read_timeout": 1}},
        "sql": "select sleep(3)",
        "mechanism": "PyMySQL read_timeout=1 (s) connect arg",
    },
}
H.main(M.build("mysql", int(os.environ.get("SQLX_MYSQL_PORT", "33061")), T))
