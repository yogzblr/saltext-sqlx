"""Functional tests for modules/sqlx_mod.py against real servers (TEST_PLAN section 4, module half of T7/T8/T9).

The module functions run for real (engine, runner, driver); only ``__salt__`` is injected: ``config.get`` returns the
profiles below and ``sdb.get`` resolves ``sdb://`` references from a dict. Servers and accounts are those of
``tests-support/env.sh`` (see ``test_runner_functional`` for the overrides). Scratch tables are ``scratch.t7_<random>``
and are dropped afterwards. A server that is not reachable is skipped with the reason.
"""

import json
import logging
import uuid

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_runner_functional import DIALECTS
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = [pytest.mark.pg, pytest.mark.mysql, pytest.mark.mariadb]

SDB = {"sdb://vault/ro": "ro_pw"}


@pytest.fixture(scope="module", params=DIALECTS)
def server(request):
    srv, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    srv.table = f"scratch.t7_{uuid.uuid4().hex[:10]}"
    srv.admin_run(f"CREATE TABLE {srv.table} (k varchar(20) PRIMARY KEY, n integer)")
    if srv.key == "postgresql":
        srv.admin_run(f"GRANT SELECT ON {srv.table} TO ro")
        srv.admin_run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {srv.table} TO rw")
    try:
        yield srv
    finally:
        srv.admin_run(f"DROP TABLE IF EXISTS {srv.table}")


@pytest.fixture
def salt(server, monkeypatch):
    """Inject ``__salt__`` with profiles ro, ro_sdb, rw (execute allowed) and bad (wrong password)."""
    base = {
        "dialect": server.dialect,
        "host": HOST,
        "port": server.cfg["port"],
        "database": server.cfg["database"],
        "connect_timeout_s": 5,
    }
    profiles = {
        "ro": dict(base, username="ro", password="ro_pw"),
        "ro_sdb": dict(base, username="ro", password="sdb://vault/ro"),
        "rw": dict(base, username="rw", password="rw_pw", allow_execute=True, read_only=False),
        "bad": dict(base, username="ro", password="not-the-password"),
    }
    functions = {
        "config.get": lambda key, default=None: (
            {"profiles": profiles} if key == "sqlx" else default
        ),
        "sdb.get": SDB.__getitem__,
    }
    monkeypatch.setattr(mod, "__salt__", functions, raising=False)
    server.admin_run(f"DELETE FROM {server.table}")
    server.admin_run(f"INSERT INTO {server.table} (k, n) VALUES ('a', 1), ('b', 2)")
    return server


def test_query_good_and_sdb_profile(salt):
    out = mod.query("ro", f"SELECT k, n FROM {salt.table} ORDER BY k")
    assert out["rows"] == [["a", 1], ["b", 2]] and out["profile"] == "ro"
    assert out["columns"] == ["k", "n"] and out["truncated"] is False
    json.dumps(out, allow_nan=False)
    assert mod.query("ro_sdb", "SELECT 1 AS one")["rows"] == [[1]]


@pytest.mark.parametrize(
    "sql,native,text",
    [
        (
            "/* T7MARK */ selec 1",
            {"postgresql": "42601", "mysql": "1064", "mariadb": "1064"},
            "syntax",
        ),
        (
            "/* T7MARK */ SELECT * FROM scratch.t7_missing_table",
            {"postgresql": "42P01", "mysql": "1146", "mariadb": "1146"},
            "t7_missing_table",
        ),
        (
            "/* T7MARK */ SELECT t7_no_such_col FROM scratch.orders WHERE note = :v",
            {"postgresql": "42703", "mysql": "1054", "mariadb": "1054"},
            "t7_no_such_col",
        ),
    ],
    ids=["syntax", "missing-table", "missing-column"],
)
def test_query_error_fidelity(salt, sql, native, text):
    with pytest.raises(CommandExecutionError) as exc:
        mod.query("ro", sql, params={"v": "BOUND-t7-SECRET"})
    message = str(exc.value)
    assert message.startswith(f"[E_SQL] {salt.dialect} {native[salt.key]}: ")
    assert text in message
    # the server may quote the fragment at the error position (MySQL "near ..."), never the whole statement
    for forbidden in ("T7MARK", "BOUND-t7-SECRET", "ro_pw", "[SQL:"):
        assert forbidden not in message


def test_wrong_password_is_e_auth_failed_without_the_password(salt):
    with pytest.raises(CommandExecutionError) as exc:
        mod.query("bad", "SELECT 1")
    assert str(exc.value).startswith(f"[E_AUTH_FAILED] {salt.dialect}")
    assert "not-the-password" not in str(exc.value)


def test_execute_disabled_then_dry_run_then_commit(salt):
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute("ro", f"UPDATE {salt.table} SET n = 9")
    assert str(exc.value).startswith("[E_EXECUTE_DISABLED]")
    sql = f"UPDATE {salt.table} SET n = 9 WHERE k = :k"
    dry = mod.execute("rw", sql, params={"k": "a"}, dry_run=True)
    assert (dry["rows_affected"], dry["committed"], dry["dry_run"]) == (1, False, True)
    assert salt.verify(f"SELECT n FROM {salt.table} WHERE k = 'a'") == [(1,)]
    done = mod.execute("rw", sql, params={"k": "a"})
    assert (done["rows_affected"], done["committed"], done["profile"]) == (1, True, "rw")
    assert salt.verify(f"SELECT n FROM {salt.table} WHERE k = 'a'") == [(9,)]


def test_execute_verb_outside_execute_verbs_is_refused(salt):
    with pytest.raises(SaltInvocationError) as exc:
        mod.execute("rw", f"DROP TABLE {salt.table}")
    assert str(exc.value).startswith("[E_POLICY]")


def test_check_query_result_ok_and_not_ok(salt):
    sql = f"SELECT n FROM {salt.table} WHERE k = 'b'"
    assert mod.check_query_result("ro", sql, "==", expected=2)["ok"] is True
    bad = mod.check_query_result("ro", sql, "==", expected=3)
    assert bad["ok"] is False and bad["actual"] == 2
    assert mod.check_query_result("ro", "SELECT NULL", "is_null")["ok"] is True


def test_check_row_count(salt):
    sql = f"SELECT k FROM {salt.table}"
    assert mod.check_row_count("ro", sql, "==", expected=2)["ok"] is True
    assert mod.check_row_count("ro", sql, ">", expected=5)["ok"] is False


def test_ping(salt):
    out = mod.ping("ro")
    assert out["ok"] is True and out["dialect"] == salt.dialect and out["server_version"]
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping("bad")
    assert str(exc.value).startswith("[E_AUTH_FAILED]")


def test_table_list(salt):
    name = salt.table.split(".", 1)[1]
    out = mod.table_list("ro", schema="scratch", pattern="t7_%", include_views=True)
    assert name in out["tables"] and out["schema"] == "scratch" and out["views"] == []
    assert "orders" not in out["tables"]


def test_audit_line_has_no_statement_text(salt, caplog):
    caplog.set_level(logging.INFO)
    mod.query("ro", f"SELECT k FROM {salt.table}", __pub_user="alice")
    lines = [r.getMessage() for r in caplog.records if r.name == mod.log.name]
    assert len(lines) == 1 and "user=alice" in lines[0] and "rows=2" in lines[0]
    assert salt.table not in lines[0] and "ro_pw" not in lines[0]
