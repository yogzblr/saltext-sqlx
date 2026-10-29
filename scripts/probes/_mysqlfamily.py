"""Shared config for MySQL and MariaDB probes (PyMySQL)."""

import importlib.metadata as md

FLIPS = {
    "SET SESSION TRANSACTION READ WRITE": ["SET SESSION TRANSACTION READ WRITE"],
    "SET TRANSACTION READ WRITE": ["SET TRANSACTION READ WRITE"],
    "START TRANSACTION READ WRITE": ["START TRANSACTION READ WRITE"],
    "SET SESSION transaction_read_only=0": ["SET SESSION transaction_read_only=0"],
    "SET SESSION tx_read_only=0": ["SET SESSION tx_read_only=0"],
}


def build(name, port, timeout_cases):
    url = "mysql+pymysql://{a}:{a}_pw@127.0.0.1:%d/scratch" % port
    return {
        "name": name,
        "urls": {a: url.format(a=a) for a in ("ro", "rw")},
        "unknown_db_set": {"database": "nope"},
        "connect_timeout_args": {"connect_timeout": 2},
        "cast_type": "signed",
        "driver_versions": lambda: {
            "PyMySQL": md.version("PyMySQL"),
            "cryptography": _opt("cryptography"),
        },
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
            "bad_cast": "insert into scratch.t_probe values (:x)",
            "bad_cast_acct": "rw",
            "write_probe": "insert into scratch.t_probe values (:i)",
            "count_probe": "select count(*) from scratch.t_probe",
            "delete_probe": "delete from scratch.t_probe",
            "types": "select * from scratch.orders order by id",
            "colon_cast": "select :a::int",
            "json_literal": "select '{\"a\":1}'",
            "json_literal_escaped": "select '{\"a\"\\:1}'",
            "percent": "select 'abc' like '%b%'",
            "cast_bind": "select cast(:a as signed) + cast(:a as signed)",
        },
        "timeout": {"cases": timeout_cases},
        "guard": {
            "guards": {
                "SET SESSION TRANSACTION READ ONLY": ["SET SESSION TRANSACTION READ ONLY"],
                "SET TRANSACTION READ ONLY": ["SET TRANSACTION READ ONLY"],
                "START TRANSACTION READ ONLY": ["START TRANSACTION READ ONLY"],
            },
            "flips": FLIPS,
            "connection_level": {
                "init_command=SET SESSION TRANSACTION READ ONLY": (
                    {"connect_args": {"init_command": "SET SESSION TRANSACTION READ ONLY"}},
                    FLIPS,
                )
            },
        },
    }


def _opt(pkg):
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return None
