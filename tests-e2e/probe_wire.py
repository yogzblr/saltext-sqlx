"""Record the real error shapes of one dialect against the e2e Sakila servers (task T1b evidence).

Runs INSIDE the minion container with Salt's own Python and the installed drivers, straight through SQLAlchemy (no
sqlx code on the execution path), so that what it prints is what the drivers raise. For each case it records the
SQLAlchemy class, the driver class, the vendor code and the driver message (``exc.orig``; never ``str()`` of the
SQLAlchemy exception, which embeds the SQL), plus how ``saltext.sqlx.lib.errors.describe`` classifies it today.

    docker compose -f tests-e2e/docker-compose.yml exec -T minion \
        /opt/saltstack/salt/bin/python3 -I /src/tests-e2e/probe_wire.py oracle > tests-e2e/wire/oracle.json

Passwords come from /etc/salt/e2e-secrets.yaml and are replaced by <pw> in everything printed.
"""

import importlib.metadata as md
import json
import sys
import time

import sqlalchemy as sa
import yaml
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from saltext.sqlx.lib import errors

SECRETS = yaml.safe_load(open("/etc/salt/e2e-secrets.yaml", encoding="utf-8"))

HOST = {"oracle": "oracle", "mssql": "mssql"}
PORT = {"oracle": 1521, "mssql": 1433}


def url(dialect, user, password, host=None, port=None, database=None):
    if dialect == "oracle":
        return sa.engine.URL.create(
            "oracle+oracledb",
            username=user,
            password=password,
            host=host or HOST[dialect],
            port=port or PORT[dialect],
            query={"service_name": database or "SAKILA"},
        )
    return sa.engine.URL.create(
        "mssql+pymssql",
        username=user,
        password=password,
        host=host or HOST[dialect],
        port=port or PORT[dialect],
        database=database or "sakila",
    )


def scrub(value):
    s = value if isinstance(value, str) else repr(value)
    for pw in SECRETS.values():
        s = s.replace(pw, "<pw>")
    return s


def shape(exc, dialect):
    orig = getattr(exc, "orig", None)
    drv = orig if orig is not None else exc
    rec = {
        "sa_class": (
            f"{type(exc).__module__}.{type(exc).__qualname__}" if orig is not None else None
        ),
        "driver_class": f"{type(drv).__module__}.{type(drv).__qualname__}",
        "args": scrub(getattr(drv, "args", ())),
    }
    first = drv.args[0] if getattr(drv, "args", None) else None
    if dialect == "oracle" and first is not None and hasattr(first, "full_code"):
        rec.update(
            full_code=first.full_code,
            code=first.code,
            message=scrub(first.message),
            isrecoverable=first.isrecoverable,
        )
    if dialect == "mssql" and isinstance(first, tuple) and first and isinstance(first[0], int):
        rec.update(
            code=first[0],
            message=scrub(
                first[1].decode("utf-8", "replace") if isinstance(first[1], bytes) else first[1]
            ),
        )
    elif dialect == "mssql" and isinstance(first, int):
        second = drv.args[1] if len(drv.args) > 1 else b""
        rec.update(
            code=first,
            message=scrub(
                second.decode("utf-8", "replace") if isinstance(second, bytes) else second
            ),
        )
    described = errors.describe(exc, dialect)
    rec["sqlx_today"] = {
        "code": described.code,
        "native": described.native,
        "message": scrub(described.message),
    }
    return rec


def run(
    dialect,
    user,
    sql,
    params=None,
    *,
    setup=(),
    connect=None,
    engine_kw=None,
    commit=False,
    driver_sql=False,
):
    """Run ``sql`` once and return ('ok', result) or ('error', shape)."""
    pw = SECRETS.get(f"{user}_pw") if user in ("ro", "rw") else None
    kw = dict(connect or {})
    target = kw.pop("url", None) or url(dialect, user, pw, **kw)
    eng = sa.create_engine(target, poolclass=NullPool, hide_parameters=True, **(engine_kw or {}))
    started = time.perf_counter()
    try:
        with eng.connect() as conn:
            for step in setup:
                step(conn) if callable(step) else conn.exec_driver_sql(step)
            res = conn.exec_driver_sql(sql) if driver_sql else conn.execute(text(sql), params or {})
            out = {"returns_rows": res.returns_rows, "rowcount": res.rowcount}
            if res.returns_rows:
                rows = res.fetchall()
                out["columns"] = list(res.keys())
                out["rows"] = [[f"{type(v).__name__}:{scrub(v)}" for v in r] for r in rows[:5]]
            if commit:
                conn.commit()
            else:
                conn.rollback()
            return {
                "outcome": "ok",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                **out,
            }
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "outcome": "error",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **shape(exc, dialect),
        }
    finally:
        eng.dispose()


def oracle_cases():
    def call_timeout(ms):
        def step(conn):
            conn.connection.driver_connection.call_timeout = ms

        return step

    heavy = "select count(*) from payment a, payment b, payment c"
    return {
        "syntax": ("ro", "selec 1 from dual"),
        "syntax_from": ("ro", "select * fro film"),
        "missing_table": ("ro", "select * from nope"),
        "missing_column": ("ro", "select nope from film"),
        "division_by_zero": ("ro", "select 1/0 from dual"),
        "unique_violation": ("rw", "insert into scratch.e2e_notes (id, note) values (1, 'a') "),
        "unique_violation_2nd": (
            "rw",
            "insert all into scratch.e2e_notes values (900, 'a') into scratch.e2e_notes values (900, 'b') select * from dual",
        ),
        "fk_violation": (
            "rw",
            "insert into rental (rental_id, rental_date, inventory_id, customer_id, staff_id, last_update) values (999999, systimestamp, 1, 99999, 1, systimestamp)",
        ),
        "not_null_violation": ("rw", "insert into scratch.e2e_notes (id) values (901)"),
        "permission_denied_write": ("ro", "delete from film where film_id = -1"),
        "permission_denied_unseen": (
            "ro",
            "insert into scratch.e2e_notes (id, note) values (902, 'x')",
        ),
        "read_only_txn_insert": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (903, 'x')",
            None,
            {"setup": ["SET TRANSACTION READ ONLY"]},
        ),
        "read_only_bypass_plsql": (
            "rw",
            "begin commit; insert into scratch.e2e_notes (id, note) values (904, 'bypass'); commit; end;",
            None,
            {"setup": ["SET TRANSACTION READ ONLY"]},
        ),
        "read_only_bypass_check": ("rw", "select count(*) from scratch.e2e_notes where id = 904"),
        "read_only_bypass_cleanup": (
            "rw",
            "delete from scratch.e2e_notes where id = 904",
            None,
            {"commit": True},
        ),
        "set_read_write_after_ro": (
            "rw",
            "set transaction read write",
            None,
            {"setup": ["SET TRANSACTION READ ONLY"]},
        ),
        "multi_statement": ("ro", "select 1 from dual; select 2 from dual"),
        "multi_statement_dml": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (905, 'a'); insert into scratch.e2e_notes (id, note) values (906, 'b')",
        ),
        "trailing_semicolon": ("ro", "select 1 from dual;"),
        "nul_byte_literal": ("ro", "select 'a\x00b' from dual"),
        "nul_byte_bind": ("ro", "select :x from dual", {"x": "a\x00b"}),
        "cast_bind": ("ro", "select cast(:x as number) + 1 from dual", {"x": "41"}),
        "literal_colon_escaped": ("ro", "select 'a\\:b' from dual"),
        "call_timeout_500ms": ("ro", heavy, None, {"setup": [call_timeout(500)]}),
        "bad_cast": ("ro", "select cast('abc' as number) from dual"),
        "value_too_large": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (907, :n)",
            {"n": "x" * 200},
        ),
        "wrong_password": (
            "ro",
            "select 1 from dual",
            None,
            {"connect": {"url": url("oracle", "ro", SECRETS["bad_pw"])}},
        ),
        "no_password": (
            "ro",
            "select 1 from dual",
            None,
            {"connect": {"url": url("oracle", "ro", None)}},
        ),
        "unknown_user": (
            "ro",
            "select 1 from dual",
            None,
            {"connect": {"url": url("oracle", "nobody_e2e", SECRETS["bad_pw"])}},
        ),
        "unknown_service": (
            "ro",
            "select 1 from dual",
            None,
            {"connect": {"url": url("oracle", "ro", SECRETS["ro_pw"], database="NOPE")}},
        ),
        "closed_port": (
            "ro",
            "select 1 from dual",
            None,
            {"connect": {"url": url("oracle", "ro", SECRETS["ro_pw"], port=1599)}},
        ),
        "unknown_host": (
            "ro",
            "select 1 from dual",
            None,
            {
                "connect": {
                    "url": url("oracle", "ro", SECRETS["ro_pw"], host="no-such-host.invalid")
                }
            },
        ),
        "unreachable_host": (
            "ro",
            "select 1 from dual",
            None,
            {
                "connect": {"url": url("oracle", "ro", SECRETS["ro_pw"], host="10.254.254.254")},
                "engine_kw": {"connect_args": {"tcp_connect_timeout": 3}},
            },
        ),
        "types": (
            "ro",
            "select id, t_text, t_num, t_ts, t_uuid, t_bin, t_json, t_null from type_probe order by id",
        ),
        "count_film": ("ro", "select count(*) from film"),
        "version_attr": ("ro", "select 1 from dual"),
    }


def mssql_cases():
    return {
        "syntax": ("ro", "selec 1"),
        "syntax_from": ("ro", "select * fro film"),
        "missing_table": ("ro", "select * from nope"),
        "missing_column": ("ro", "select nope from film"),
        "division_by_zero": ("ro", "select 1/0"),
        "unique_violation": ("rw", "insert into scratch.e2e_notes (id, note) values (1, 'a')"),
        "fk_violation": (
            "rw",
            "insert into rental (rental_date, inventory_id, customer_id, staff_id) values (getdate(), 1, 99999, 1)",
        ),
        "not_null_violation": ("rw", "insert into scratch.e2e_notes (id) values (901)"),
        "permission_denied_write": ("ro", "delete from film where film_id = -1"),
        "permission_denied_scratch": (
            "ro",
            "insert into scratch.e2e_notes (id, note) values (902, 'x')",
        ),
        "multi_statement_select": ("ro", "select 1; select 2"),
        "multi_statement_dml": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (905, 'a'); insert into scratch.e2e_notes (id, note) values (906, 'b')",
            None,
            {"commit": True},
        ),
        "multi_statement_check": (
            "rw",
            "select count(*) from scratch.e2e_notes where id in (905, 906)",
        ),
        "multi_statement_no_separator": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (907, 'a') insert into scratch.e2e_notes (id, note) values (908, 'b')",
            None,
            {"commit": True},
        ),
        "multi_statement_no_sep_check": (
            "rw",
            "select count(*) from scratch.e2e_notes where id in (907, 908)",
        ),
        "commit_inside_batch": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (909, 'c'); commit",
            None,
            {"commit": False},
        ),
        "commit_inside_check": ("rw", "select count(*) from scratch.e2e_notes where id = 909"),
        "multi_cleanup": (
            "rw",
            "delete from scratch.e2e_notes where id between 900 and 999",
            None,
            {"commit": True},
        ),
        "trailing_semicolon": ("ro", "select 1;"),
        "nul_byte_literal": ("ro", "select 'a\x00b'"),
        "nul_byte_bind": ("ro", "select :x", {"x": "a\x00b"}),
        "cast_bind": ("ro", "select cast(:x as int) + 1", {"x": "41"}),
        "literal_percent_and_bind": (
            "ro",
            "select case when 'abc' like '%b%' then cast(:x as int) else 0 end",
            {"x": 1},
        ),
        "literal_colon_escaped": ("ro", "select 'a\\:b'"),
        "timeout_1s": (
            "ro",
            "waitfor delay '00:00:03'",
            None,
            {"engine_kw": {"connect_args": {"timeout": 1}}},
        ),
        "timeout_1s_select": (
            "ro",
            "select count(*) from payment a, payment b, payment c",
            None,
            {"engine_kw": {"connect_args": {"timeout": 1}}},
        ),
        "lock_timeout": ("ro", "select 1", None, {"setup": ["SET LOCK_TIMEOUT 100"]}),
        "bad_cast": ("ro", "select cast('abc' as int)"),
        "value_too_large": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (910, :n)",
            {"n": "x" * 200},
        ),
        "read_only_intent_attempt": (
            "rw",
            "select 1",
            None,
            {"setup": ["SET TRANSACTION ISOLATION LEVEL SNAPSHOT"]},
        ),
        "wrong_password": (
            "ro",
            "select 1",
            None,
            {"connect": {"url": url("mssql", "ro", SECRETS["bad_pw"])}},
        ),
        "no_password": ("ro", "select 1", None, {"connect": {"url": url("mssql", "ro", None)}}),
        "unknown_database": (
            "ro",
            "select 1",
            None,
            {"connect": {"url": url("mssql", "ro", SECRETS["ro_pw"], database="nope")}},
        ),
        "closed_port": (
            "ro",
            "select 1",
            None,
            {"connect": {"url": url("mssql", "ro", SECRETS["ro_pw"], port=1499)}},
        ),
        "unknown_host": (
            "ro",
            "select 1",
            None,
            {"connect": {"url": url("mssql", "ro", SECRETS["ro_pw"], host="no-such-host.invalid")}},
        ),
        "unreachable_host": (
            "ro",
            "select 1",
            None,
            {
                "connect": {"url": url("mssql", "ro", SECRETS["ro_pw"], host="10.254.254.254")},
                "engine_kw": {"connect_args": {"login_timeout": 3}},
            },
        ),
        "types": (
            "ro",
            "select id, t_text, t_num, t_ts, t_uuid, t_bin, t_json, t_null from type_probe order by id",
        ),
        "count_film": ("ro", "select count(*) from film"),
    }


def t1_cases(dialect):
    """The T1 case list of docs/WIRE_SHAPES.md sections 1-5, on the Sakila objects of this suite."""
    d = " from dual" if dialect == "oracle" else ""
    sleep = (
        "begin dbms_session.sleep(3); end;" if dialect == "oracle" else "waitfor delay '00:00:03'"
    )
    cases = {
        "t1_div0_dml": ("rw", f"insert into scratch.e2e_notes (id, note) select 1/0, 'x'{d}"),
        "t1_ro_select_other_schema": ("ro", "select * from scratch.e2e_notes"),
        "t1_ro_update": ("ro", "update film set title = title where film_id = -1"),
        "t1_ro_delete": ("ro", "delete from film where film_id = -1"),
        "t1_ro_ddl": ("ro", "create table ddl_probe (i int)"),
        "t1_bad_bound_value": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (:x, 'a')",
            {"x": "zz_secret_bound_value"},
        ),
        "t1_rw_write_then_rollback": (
            "rw",
            "insert into scratch.e2e_notes (id, note) values (950, 'rolled back')",
        ),
        "t1_rw_write_then_rollback_check": (
            "rw",
            "select count(*) from scratch.e2e_notes where id = 950",
        ),
        "t1_text_trap_colon_cast": ("ro", f"select :a::int{d}", {"a": 5}),
        "t1_text_trap_json_colon_literal": ("ro", f"select '{{\"a\":1}}'{d}"),
        "t1_text_trap_json_colon_escaped": ("ro", f"select '{{\"a\"\\:1}}'{d}"),
        "t1_text_percent_in_text": (
            "ro",
            f"select case when 'abc' like '%b%' then 1 else 0 end{d}",
        ),
        "t1_text_cast_bind": ("ro", f"select cast(:a as int) + cast(:a as int){d}", {"a": 5}),
        "t1_driver_sql_percent_no_params": (
            "ro",
            f"select case when 'abc' like '%b%' then 1 else 0 end{d}",
            None,
            {"driver_sql": True},
        ),
        "t1_timeout_sleep": (
            "ro",
            sleep,
            None,
            (
                {"setup": [_call_timeout(300)]}
                if dialect == "oracle"
                else {"engine_kw": {"connect_args": {"timeout": 1}}}
            ),
        ),
    }
    if dialect == "oracle":
        ro_txn = {"setup": ["SET TRANSACTION READ ONLY"]}
        cases.update(
            {
                "t1_guard_control_no_guard": (
                    "rw",
                    "insert into scratch.e2e_notes (id, note) values (951, 'g')",
                ),
                "t1_guard_set_transaction_read_only_write": (
                    "rw",
                    "insert into scratch.e2e_notes (id, note) values (952, 'g')",
                    None,
                    ro_txn,
                ),
                "t1_bypass_set_transaction_read_write": (
                    "rw",
                    "set transaction read write",
                    None,
                    ro_txn,
                ),
                "t1_bypass_commit_then_write": ("rw", "commit", None, ro_txn),
                "t1_bypass_plsql_block": (
                    "rw",
                    "begin commit; insert into scratch.e2e_notes (id, note) values (953, 'bypass'); end;",
                    None,
                    ro_txn,
                ),
                "t1_bypass_plsql_block_check": (
                    "rw",
                    "select count(*) from scratch.e2e_notes where id = 953",
                ),
                "t1_bypass_cleanup": (
                    "rw",
                    "delete from scratch.e2e_notes where id = 953",
                    None,
                    {"commit": True},
                ),
            }
        )
    else:
        cases.update(
            {
                "t1_guard_control_no_guard": (
                    "rw",
                    "insert into scratch.e2e_notes (id, note) values (951, 'g')",
                ),
                "t1_snapshot_isolation_not_allowed": (
                    "rw",
                    "select 1",
                    None,
                    {"setup": ["SET TRANSACTION ISOLATION LEVEL SNAPSHOT"]},
                ),
            }
        )
    return cases


def _call_timeout(ms):
    def step(conn):
        conn.connection.driver_connection.call_timeout = ms

    return step


def main():
    dialect = sys.argv[1]
    only = set(sys.argv[2:])
    cases = oracle_cases() if dialect == "oracle" else mssql_cases()
    cases.update(t1_cases(dialect))
    out = {
        "dialect": dialect,
        "observed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "versions": {
            p: md.version(p)
            for p in ("SQLAlchemy", "oracledb" if dialect == "oracle" else "pymssql")
        },
        "python": sys.version.split()[0],
        "cases": {},
    }
    for name, spec in cases.items():
        if only and name not in only:
            continue
        user, sql = spec[0], spec[1]
        params = spec[2] if len(spec) > 2 else None
        opts = spec[3] if len(spec) > 3 else {}
        out["cases"][name] = run(dialect, user, sql, params, **opts)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
