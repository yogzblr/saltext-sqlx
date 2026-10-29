# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, box 1 and section 2: the DATABASE refuses writes for the read-only role, not the module.

Account ``ro`` has SELECT only on ``scratch``. The module's best-effort guard (``read_only: true``) is switched OFF in
the first test, so every refusal seen there comes from the database's privilege system (PostgreSQL 42501; MySQL and
MariaDB 1142 / 1044 / 1227). The second test re-runs the SECURITY section 2 bypass sequences on one connection that
carries the guard: the guard statement is bypassed by the caller, and the write that follows is still refused by the role.
Oracle and SQL Server: NOT RUN (T1b).
"""

import re

import pytest
from sqlalchemy import text

from saltext.sqlx.lib import engine
from saltext.sqlx.lib import errors
from saltext.sqlx.lib import profiles
from tests.security import harness
from tests.security.harness import observe

ROLE_REFUSAL = {
    "postgresql": re.compile(r"\b42501\b"),
    "mysql": re.compile(r"\b(1142|1044|1227)\b"),
}

WRITES = {
    "postgresql": [
        "INSERT INTO scratch.orders (id, note) VALUES (9001, 'x')",
        "UPDATE scratch.orders SET note = 'x'",
        "DELETE FROM scratch.orders",
        "TRUNCATE scratch.orders",
        "DROP TABLE scratch.orders",
        "ALTER TABLE scratch.orders ADD COLUMN t11_zz integer",
        "CREATE INDEX t11_i ON scratch.orders (note)",
        "CREATE TABLE scratch.t11_x (a integer)",
        "CREATE VIEW scratch.t11_v AS SELECT 1",
        "CREATE ROLE t11_evil",
        "SELECT v FROM private.secrets",
    ],
    "mysql": [
        "INSERT INTO scratch.orders (id, note) VALUES (9001, 'x')",
        "UPDATE scratch.orders SET note = 'x'",
        "DELETE FROM scratch.orders",
        "TRUNCATE scratch.orders",
        "DROP TABLE scratch.orders",
        "ALTER TABLE scratch.orders ADD COLUMN t11_zz integer",
        "CREATE INDEX t11_i ON scratch.orders (note)",
        "CREATE TABLE scratch.t11_x (a integer)",
        "CREATE TEMPORARY TABLE scratch.t11_tmp (a integer)",
        "CREATE VIEW scratch.t11_v AS SELECT 1",
        "CREATE DATABASE t11_db",
        "CREATE USER 't11_evil'@'%'",
        "GRANT ALL ON scratch.orders TO 'ro'@'%'",
        "SELECT v FROM private.secrets",
    ],
}


def orders_snapshot(srv):
    return srv.admin("SELECT id, note FROM scratch.orders ORDER BY id")


@pytest.fixture
def ro_no_guard(server):
    """Profile ``ro`` with the module's guard OFF: only the database role stands between the caller and a write."""
    with harness.injected({"ro": server.profile("ro", read_only=False)}) as module:
        yield module


def test_positive_control_rw_can_write_where_ro_cannot(server):
    """If this failed, the refusals below could be a broken fixture and not the role."""
    table = harness.unique("scratch.t11_ctl")
    server.admin(f"CREATE TABLE {table} (id integer)")
    if server.key == "postgresql":
        server.admin(f"GRANT SELECT, INSERT ON {table} TO rw")
    try:
        with harness.injected(
            {"rw": server.profile("rw", allow_execute=True, read_only=False)}
        ) as module:
            assert module.execute("rw", f"INSERT INTO {table} VALUES (1)")["committed"] is True
        assert server.admin(f"SELECT count(*) FROM {table}") == [(1,)]
    finally:
        server.admin(f"DROP TABLE IF EXISTS {table}")


def test_ro_writes_are_refused_by_the_database_role(server, ro_no_guard):
    before = orders_snapshot(server)
    for sql in WRITES["postgresql" if server.key == "postgresql" else "mysql"]:
        kind, value = harness.attempt(lambda s=sql: ro_no_guard.query("ro", s))
        observe(
            "readonly-role", f"{server.key} guard-off ro: {sql}", harness.describe((kind, value))
        )
        assert kind == "ERR", f"{sql!r} succeeded for the read-only role"
        message = str(value)
        assert ROLE_REFUSAL["postgresql" if server.key == "postgresql" else "mysql"].search(
            message
        ), message
        assert (
            "[E_READ_ONLY]" not in message
        ), "refusal came from a read-only transaction, not from the role"
    assert orders_snapshot(server) == before


GUARD_BYPASS = {
    "postgresql": [
        ["SET TRANSACTION READ WRITE"],
        ["COMMIT"],
        ["SET default_transaction_read_only = off", "SET TRANSACTION READ WRITE"],
        ["START TRANSACTION READ WRITE"],
    ],
    "mysql": [
        ["SET SESSION TRANSACTION READ WRITE"],
        ["SET TRANSACTION READ WRITE"],
        ["START TRANSACTION READ WRITE"],
        ["SET SESSION transaction_read_only = 0"],
        ["SET SESSION tx_read_only = 0"],
        ["COMMIT"],
    ],
}


@pytest.mark.parametrize("variant", range(6))
def test_guard_bypass_sequences_still_fail_at_the_role(server, variant):
    family = "postgresql" if server.key == "postgresql" else "mysql"
    if variant >= len(GUARD_BYPASS[family]):
        pytest.skip("no such variant for this dialect")
    steps = GUARD_BYPASS[family][variant]
    prof = profiles.validate_profile("g", server.profile("ro", read_only=True))
    before = orders_snapshot(server)
    results = []
    with engine.connection(prof, "ro_pw", None, True) as conn:  # the guard is applied here
        for sql in steps:
            try:
                conn.execute(text(sql))
                results.append("OK")
            except Exception as exc:  # pylint: disable=broad-except
                try:
                    errors.raise_for(exc, server.dialect)
                except Exception as translated:  # pylint: disable=broad-except
                    results.append(str(translated)[:80])
                conn.rollback()
        try:
            conn.execute(text("INSERT INTO scratch.orders (id, note) VALUES (9001, 'bypass')"))
            write = "WRITE SUCCEEDED"
        except Exception as exc:  # pylint: disable=broad-except
            try:
                errors.raise_for(exc, server.dialect)
            except Exception as translated:  # pylint: disable=broad-except
                write = str(translated)
        try:
            conn.commit()
        except Exception:  # pylint: disable=broad-except
            pass
    observe(
        "guard-bypass",
        f"{server.key} {steps}",
        f"bypass statement(s): {results}; then INSERT: {write[:110]}",
    )
    assert write != "WRITE SUCCEEDED"
    assert ROLE_REFUSAL[family].search(write) and "[E_READ_ONLY]" not in write, write
    assert orders_snapshot(server) == before


def test_unrelated_privileges_of_ro(server):
    """What the role still allows is data for the review, not a pass/fail: TEMP tables on PostgreSQL (PUBLIC default)."""
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    with harness.injected({"ro": server.profile("ro", read_only=False)}) as module:
        kind, value = harness.attempt(
            lambda: module.query("ro", "CREATE TEMPORARY TABLE t11_tmp (a integer)")
        )
    observe(
        "readonly-role",
        "postgresql guard-off ro: CREATE TEMPORARY TABLE",
        harness.describe((kind, value)),
    )
    assert (
        kind == "OK"
    ), "PUBLIC normally has TEMP on the database; if this is refused the DBA revoked it (good)"
