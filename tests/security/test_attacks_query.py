# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""T11 part 2, attack classes (1) and (3): make ``sqlx.query`` persist a write, read a file, or reach beyond the profile.

Hostile-reviewer tests through the PUBLIC module functions against the live servers (see ``harness.injected``). Profiles:

* ``rw_query``  account ``rw`` (DML), guard OFF   - a ``query`` profile that was wrongly given a write-capable account
* ``rw_guard``  account ``rw`` (DML), guard ON    - the default (``read_only: true``) with a write-capable account
* ``ro``        account ``ro`` (SELECT only), guard ON

A test that asserts the SAFE behaviour and fails today is ``xfail(strict=True)`` and names the defect (D1 ... in
``docs/SECURITY_REVIEW.md``). A test whose name says ``documented`` asserts a limit that the security docs already state
(the database role is the control) so that a change in behaviour is noticed.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from tests.security import harness
from tests.security.harness import observe

W = "scratch.t11_w"


def defect(request, server, key, reason, only=None):
    """Mark the running test as an expected failure for the reported defect when it applies to this server."""
    if only is None or server.key in only:
        request.applymarker(pytest.mark.xfail(strict=True, reason=f"{key}: {reason}"))


@pytest.fixture(scope="module")
def wtab(server):
    k = server.key
    drops = [f"DROP TABLE IF EXISTS {W}"]
    server.admin(drops[0])
    if k == "postgresql":
        for s in [
            f"CREATE TABLE {W} (id integer PRIMARY KEY, v text)",
            f"GRANT SELECT ON {W} TO ro",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {W} TO rw",
            "DROP SEQUENCE IF EXISTS scratch.t11_seq",
            "CREATE SEQUENCE scratch.t11_seq",
            "CREATE SEQUENCE scratch.t11_seq2",
            "GRANT USAGE ON SEQUENCE scratch.t11_seq2 TO rw",
            "DROP FUNCTION IF EXISTS scratch.t11_fn()",
            f"CREATE FUNCTION scratch.t11_fn() RETURNS integer LANGUAGE sql AS 'INSERT INTO {W} VALUES (500, ''fn'') RETURNING id'",
            "DROP PROCEDURE IF EXISTS scratch.t11_proc()",
            f"CREATE PROCEDURE scratch.t11_proc() LANGUAGE plpgsql AS $$ BEGIN INSERT INTO {W} VALUES (501, 'p'); COMMIT; END $$",
        ]:
            server.admin(s)
    else:
        for s in [
            f"CREATE TABLE {W} (id integer PRIMARY KEY, v text) ENGINE=InnoDB",
            "DROP TABLE IF EXISTS scratch.t11_myisam",
            "CREATE TABLE scratch.t11_myisam (id integer PRIMARY KEY, v text) ENGINE=MyISAM",
            "DROP TABLE IF EXISTS scratch.t11_auto",
            "CREATE TABLE scratch.t11_auto (id integer AUTO_INCREMENT PRIMARY KEY, v text) ENGINE=InnoDB",
            "DROP PROCEDURE IF EXISTS scratch.t11_proc",
            "DROP PROCEDURE IF EXISTS scratch.t11_proc_nogrant",
            f"CREATE PROCEDURE scratch.t11_proc() BEGIN INSERT INTO {W} VALUES (501, 'p'); COMMIT; END",
            f"CREATE PROCEDURE scratch.t11_proc_nogrant() BEGIN INSERT INTO {W} VALUES (502, 'p'); COMMIT; END",
            "GRANT EXECUTE ON PROCEDURE scratch.t11_proc TO 'rw'@'%'",
        ]:
            server.admin(s)
    try:
        yield server
    finally:
        for s in [
            f"DROP TABLE IF EXISTS {W}",
            "DROP TABLE IF EXISTS scratch.t11_new",
        ] + (
            [
                "DROP SEQUENCE IF EXISTS scratch.t11_seq",
                "DROP SEQUENCE IF EXISTS scratch.t11_seq2",
                "DROP FUNCTION IF EXISTS scratch.t11_fn()",
                "DROP PROCEDURE IF EXISTS scratch.t11_proc()",
            ]
            if k == "postgresql"
            else [
                "DROP TABLE IF EXISTS scratch.t11_myisam",
                "DROP TABLE IF EXISTS scratch.t11_auto",
                "DROP PROCEDURE IF EXISTS scratch.t11_proc",
                "DROP PROCEDURE IF EXISTS scratch.t11_proc_nogrant",
            ]
        ):
            server.admin(s)


@pytest.fixture
def q(wtab, salt):
    """``q(profile, sql, **kw)`` -> ``(outcome, persisted_rows)``; clears the table afterwards."""
    srv = wtab

    def run(profile, sql, label=None, table=W, **kw):
        outcome = harness.attempt(lambda: salt.query(profile, sql, **kw))
        persisted = srv.admin(f"SELECT count(*) FROM {table}")[0][0]
        if persisted:
            srv.admin(f"DELETE FROM {table}")
        observe(
            "query-write",
            f"{srv.key} {profile}: {label or sql[:90]}",
            f"persisted={persisted} {harness.describe(outcome)}",
        )
        return outcome, persisted

    srv.admin(f"DELETE FROM {W}")
    return run


def ins(i):
    return f"INSERT INTO {W} VALUES ({i}, 'x')"


# ---- baseline ----------------------------------------------------------------------------------------------------
def test_single_insert_is_rolled_back_even_without_the_guard(q):
    outcome, persisted = q("rw_query", ins(1))
    assert outcome[0] == "OK" and persisted == 0


def test_guard_on_refuses_a_single_insert(q):
    outcome, persisted = q("rw_guard", ins(1))
    assert outcome[0] == "ERR" and "[E_READ_ONLY]" in str(outcome[1]) and persisted == 0


# ---- multi-statement strings, BEGIN/COMMIT inside the statement ---------------------------------------------------
MULTI = [
    ("insert-commit", "{i}; COMMIT"),
    ("commit-insert-commit", "COMMIT; {i}; COMMIT"),
    ("begin-insert-commit", "BEGIN; {i}; COMMIT"),
    ("start-transaction-insert-commit", "START TRANSACTION; {i}; COMMIT"),
    ("set-rw-insert-commit", "SET TRANSACTION READ WRITE; {i}; COMMIT"),
    ("start-rw-insert-commit", "START TRANSACTION READ WRITE; {i}; COMMIT"),
    (
        "cte-write-commit",
        "WITH x AS (INSERT INTO " + W + " VALUES ({n}, 'c') RETURNING id) SELECT * FROM x; COMMIT",
    ),
]


@pytest.mark.parametrize("profile", ["rw_query", "rw_guard"])
@pytest.mark.parametrize("label,template", MULTI, ids=[m[0] for m in MULTI])
def test_multi_statement_string_does_not_persist_a_write(
    request, server, q, profile, label, template
):
    """D1: PostgreSQL runs a multi-statement string (simple protocol) and the caller's COMMIT makes the write permanent."""
    if label == "cte-write-commit" and server.key != "postgresql":
        pytest.skip("data-modifying CTE is PostgreSQL syntax")
    sql = template.format(i=ins(7), n=7)
    outcome, persisted = q(profile, sql, label=f"{label}")
    assert (
        persisted == 0
    ), f"{label} persisted a write through sqlx.query as rw ({harness.describe(outcome)})"


def test_mysql_family_rejects_multi_statements_by_driver_default(server, q):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, persisted = q("rw_query", f"{ins(8)}; COMMIT")
    assert outcome[0] == "ERR" and "1064" in str(outcome[1]) and persisted == 0


# ---- statements that persist or reach outside the transaction ------------------------------------------------------
def test_data_modifying_cte_is_rolled_back_or_refused(server, q):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL syntax")
    outcome, persisted = q(
        "rw_query", f"WITH i AS (INSERT INTO {W} VALUES (6, 'c') RETURNING id) SELECT * FROM i"
    )
    assert outcome[0] == "OK" and outcome[1]["rows"] == [[6]] and persisted == 0
    outcome, persisted = q(
        "rw_guard", f"WITH i AS (INSERT INTO {W} VALUES (6, 'c') RETURNING id) SELECT * FROM i"
    )
    assert outcome[0] == "ERR" and persisted == 0


@pytest.mark.parametrize(
    "sql",
    [
        f"COPY {W} FROM PROGRAM 'echo 9,x' CSV",
        "COPY (SELECT 1) TO PROGRAM 'touch /tmp/t11_prog'",
        f"COPY {W} TO '/tmp/t11_copy_out'",
        f"COPY {W} FROM '/etc/passwd'",
        f"SELECT id INTO scratch.t11_new FROM {W}",
        "CREATE TABLE scratch.t11_new AS SELECT 1 AS a",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/hostname')",
        "SELECT pg_ls_dir('/')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT lo_export(1, '/tmp/t11_lo')",
        "CREATE EXTENSION dblink",
        "ALTER SYSTEM SET work_mem = '1MB'",
        "SELECT pg_reload_conf()",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'postgres'",
        "SET ROLE postgres",
        "SET SESSION AUTHORIZATION postgres",
    ],
)
def test_pg_file_program_and_privileged_operations_are_refused_by_the_role(server, q, sql):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    for profile in ("rw_query", "rw_guard"):
        outcome, persisted = q(profile, sql)
        assert outcome[0] == "ERR" and persisted == 0
        assert any(
            code in str(outcome[1]) for code in ("42501", "25006", "25001", "42704")
        ), outcome[1]
    assert not __import__("os").path.exists("/tmp/t11_prog") and not __import__("os").path.exists(
        "/tmp/t11_copy_out"
    )


@pytest.mark.parametrize(
    "sql,codes",
    [
        ("SELECT 1 INTO OUTFILE '/tmp/t11_out'", ("1227", "1045")),
        ("SELECT 1 INTO DUMPFILE '/tmp/t11_out2'", ("1227", "1045")),
        (f"LOAD DATA INFILE '/etc/passwd' INTO TABLE {W}", ("1045", "1227")),
        (f"LOAD DATA LOCAL INFILE '/etc/passwd' INTO TABLE {W}", ("3948", "4166")),
        ("CREATE TABLE scratch.t11_new AS SELECT 1 AS a", ("1142",)),
        ("CREATE TEMPORARY TABLE t11_tmp (a integer)", ("1044",)),
        ("SET GLOBAL max_connections = 5", ("1227",)),
        ("FLUSH PRIVILEGES", ("1227",)),
        ("LOCK TABLES scratch.t11_w WRITE", ("1044",)),
    ],
)
def test_mysql_file_and_privileged_operations_are_refused(server, q, sql, codes):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, persisted = q("rw_query", sql)
    assert (
        outcome[0] == "ERR" and persisted == 0 and any(c in str(outcome[1]) for c in codes)
    ), outcome[1]


def test_mysql_load_file_returns_null_without_the_file_privilege(server, q):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, _ = q("rw_query", "SELECT LOAD_FILE('/etc/passwd')")
    assert outcome[0] == "OK" and outcome[1]["rows"] == [[None]]


# ---- stored routines ---------------------------------------------------------------------------------------------
def test_pg_function_that_inserts_is_rolled_back_or_refused(server, q):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    outcome, persisted = q("rw_query", "SELECT scratch.t11_fn()")
    assert outcome[0] == "OK" and persisted == 0
    outcome, persisted = q("rw_guard", "SELECT scratch.t11_fn()")
    assert outcome[0] == "ERR" and persisted == 0


def test_pg_procedure_cannot_commit_inside_the_callers_transaction(server, q):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    outcome, persisted = q("rw_query", "CALL scratch.t11_proc()")
    assert outcome[0] == "ERR" and "2D000" in str(outcome[1]) and persisted == 0
    outcome, persisted = q("rw_guard", "CALL scratch.t11_proc()")
    assert outcome[0] == "ERR" and persisted == 0


def test_documented_mysql_procedure_with_execute_grant_commits_for_a_profile_without_the_guard(
    server, q
):
    """SECURITY section 2: the role must not hold EXECUTE on write-capable routines. If it does, COMMIT inside persists."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, persisted = q("rw_query", "CALL scratch.t11_proc()")
    assert outcome[0] == "OK" and persisted == 1
    outcome, persisted = q("rw_guard", "CALL scratch.t11_proc()")
    assert persisted == 0 and (
        outcome[0] == "ERR"
    )  # the guard does stop it here (single statement, no multi-statement)
    outcome, persisted = q("rw_query", "CALL scratch.t11_proc_nogrant()")
    assert outcome[0] == "ERR" and "1370" in str(outcome[1]) and persisted == 0


# ---- storage-engine and counter side effects the rollback cannot undo -------------------------------------------------
def test_documented_myisam_write_is_not_rolled_back(server, q):
    """MyISAM has no transactions: ``query`` as a write-capable account without the guard persists. The role is the control."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, persisted = q(
        "rw_query", "INSERT INTO scratch.t11_myisam VALUES (1, 'm')", table="scratch.t11_myisam"
    )
    assert outcome[0] == "OK" and persisted == 1
    outcome, persisted = q(
        "rw_guard", "INSERT INTO scratch.t11_myisam VALUES (1, 'm')", table="scratch.t11_myisam"
    )
    assert outcome[0] == "ERR" and persisted == 0


def test_documented_postgres_sequence_advance_is_not_rolled_back(server, q):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    before = server.admin("SELECT last_value, is_called FROM scratch.t11_seq2")[0]
    outcome, _ = q("rw_query", "SELECT nextval('scratch.t11_seq2')")
    after = server.admin("SELECT last_value, is_called FROM scratch.t11_seq2")[0]
    assert outcome[0] == "OK" and after != before, "nextval is not transactional"
    outcome, _ = q("rw_query", "SELECT nextval('scratch.t11_seq')")
    assert outcome[0] == "ERR" and "42501" in str(
        outcome[1]
    )  # no USAGE on the sequence: refused by the role


# ---- account self-service DDL (persists without a COMMIT on MySQL) ---------------------------------------------------------
def _login_works(server, user, password):
    url = URL.create(
        server.cfg["drivername"],
        username=user,
        password=password,
        host=harness.HOST,
        port=server.cfg["port"],
        database=server.cfg["database"],
    )
    eng = sa.create_engine(url, poolclass=NullPool)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pylint: disable=broad-except
        return False
    finally:
        eng.dispose()


@pytest.fixture
def victim(server):
    """A SELECT-only throwaway account ``t11_u`` that may change its own password (every database allows that)."""
    if server.key == "postgresql":
        server.admin("DROP ROLE IF EXISTS t11_u")
        server.admin("CREATE ROLE t11_u LOGIN PASSWORD 't11_pw'")
        server.admin("GRANT USAGE ON SCHEMA scratch TO t11_u")
        server.admin("GRANT SELECT ON scratch.orders TO t11_u")
        change = "ALTER ROLE t11_u PASSWORD 'hacked'"
    else:
        server.admin("DROP USER IF EXISTS 't11_u'@'%'")
        server.admin("CREATE USER 't11_u'@'%' IDENTIFIED BY 't11_pw'")
        server.admin("GRANT SELECT ON scratch.orders TO 't11_u'@'%'")
        change = (
            "SET PASSWORD = 'hacked'"
            if server.key == "mysql"
            else "SET PASSWORD = PASSWORD('hacked')"
        )
    try:
        yield change
    finally:
        if server.key == "postgresql":
            server.admin("REVOKE ALL ON scratch.orders FROM t11_u")
            server.admin("REVOKE ALL ON SCHEMA scratch FROM t11_u")
            server.admin("DROP ROLE IF EXISTS t11_u")
        else:
            server.admin("DROP USER IF EXISTS 't11_u'@'%'")


def test_select_only_account_cannot_change_its_password_through_the_guard(request, server, victim):
    """D1: with the DEFAULT guard, a SELECT-only account loses its password on PostgreSQL (COMMIT inside the statement)."""
    if server.key == "postgresql":
        sql = f"COMMIT; {victim}; COMMIT"
    else:
        sql = victim
    with harness.injected({"u": server.profile("t11_u", password="t11_pw")}) as module:
        outcome = harness.attempt(lambda: module.query("u", sql))
    observe(
        "query-write",
        f"{server.key} guard ON, SELECT-only account changes its own password",
        f"{harness.describe(outcome)}; old works={_login_works(server, 't11_u', 't11_pw')} attacker works={_login_works(server, 't11_u', 'hacked')}",
    )
    assert _login_works(server, "t11_u", "t11_pw") and not _login_works(server, "t11_u", "hacked")


def test_documented_mysql_guard_off_profile_lets_account_ddl_persist(server, victim):
    """MySQL/MariaDB implicit commit: with ``read_only: false`` ``SET PASSWORD`` persists. Keep ``read_only: true`` (default)."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    with harness.injected(
        {"u": server.profile("t11_u", password="t11_pw", read_only=False)}
    ) as module:
        outcome = harness.attempt(lambda: module.query("u", victim))
    observe(
        "query-write",
        f"{server.key} guard OFF, SET PASSWORD",
        f"{harness.describe(outcome)}; attacker works={_login_works(server, 't11_u', 'hacked')}",
    )
    assert outcome[0] == "OK" and _login_works(server, "t11_u", "hacked")


def test_postgres_guard_off_password_change_is_rolled_back(server, victim):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    with harness.injected(
        {"u": server.profile("t11_u", password="t11_pw", read_only=False)}
    ) as module:
        outcome = harness.attempt(lambda: module.query("u", victim))
    assert (
        outcome[0] == "OK"
        and _login_works(server, "t11_u", "t11_pw")
        and not _login_works(server, "t11_u", "hacked")
    )


# ---- dblink: a write channel outside the transaction, and a way to reach other hosts -------------------------------------
@pytest.fixture
def dblink_db(server):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    admin_url = server.url("admin")
    ac = sa.create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with ac.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS t11_db"))
        conn.execute(text("CREATE DATABASE t11_db"))
    ac.dispose()
    inner = sa.create_engine(admin_url.set(database="t11_db"), poolclass=NullPool)
    with inner.begin() as conn:
        conn.execute(text("CREATE EXTENSION dblink"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO rw, ro"))
    inner.dispose()
    server.admin(f"DROP TABLE IF EXISTS {W}")
    server.admin(f"CREATE TABLE {W} (id integer PRIMARY KEY, v text)")
    server.admin(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {W} TO rw")
    server.admin(f"GRANT SELECT ON {W} TO ro")
    try:
        yield
    finally:
        server.admin(f"DROP TABLE IF EXISTS {W}")
        ac = sa.create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        with ac.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS t11_db"))
        ac.dispose()


def test_documented_dblink_when_installed_writes_outside_the_transaction_and_reaches_other_ports(
    server, dblink_db
):
    """The extension is NOT installed in the shared test database; it is installed in a throwaway database here.

    ``dblink_exec`` opens its own autocommit connection, so a ``query`` as ``rw`` persists a row although the module rolls
    back; and ``dblink_connect`` makes the DATABASE server open TCP connections to a host and port the caller names.
    Control: do not install dblink / postgres_fdw for these roles, or ``REVOKE EXECUTE`` on the functions.
    """
    conn = f"dbname={server.cfg['database']} user=rw password=rw_pw host=127.0.0.1 port={server.cfg['port']}"
    with harness.injected(
        {
            "rw": server.profile("rw", database="t11_db", read_only=False),
            "ro": server.profile("ro", database="t11_db"),
        }
    ) as module:
        outcome = harness.attempt(
            lambda: module.query(
                "rw",
                f"SELECT public.dblink_exec('{conn}', 'INSERT INTO scratch.t11_w VALUES (77, ''dl'')')",
            )
        )
        persisted = server.admin(f"SELECT count(*) FROM {W}")[0][0]
        observe(
            "query-write",
            "postgresql rw: dblink_exec INSERT via a second connection",
            f"persisted={persisted} {harness.describe(outcome)}",
        )
        assert outcome[0] == "OK" and persisted == 1
        outcome = harness.attempt(
            lambda: module.query(
                "ro",
                "SELECT public.dblink_connect('dbname=x user=x password=x host=127.0.0.1 port=3306 connect_timeout=2')",
            )
        )
        observe(
            "reach-host",
            "postgresql ro: dblink_connect to 127.0.0.1:3306 (DB server opens the socket)",
            harness.describe(outcome),
        )
        assert outcome[0] == "ERR" and "[E_CONNECT]" in str(outcome[1])
        ro_conn = f"dbname={server.cfg['database']} user=ro password=ro_pw host=127.0.0.1 port={server.cfg['port']}"
        outcome = harness.attempt(
            lambda: module.query(
                "ro",
                f"SELECT public.dblink_exec('{ro_conn}', 'INSERT INTO scratch.t11_w VALUES (78, ''dl'')')",
            )
        )
        assert outcome[0] == "ERR" and "42501" in str(
            outcome[1]
        )  # the remote side still applies ro's privileges
        assert server.admin(f"SELECT count(*) FROM {W}") == [(1,)]


# ---- NUL byte -----------------------------------------------------------------------------------------------------------
def test_nul_byte_in_sql_is_refused_not_silently_truncated(request, server, salt):
    """D9: psycopg/libpq stop reading the statement at NUL, so the server runs a prefix of what was hashed and filtered."""
    outcome = harness.attempt(lambda: salt.query("ro", "SELECT 1 AS a \x00 , 2 AS b"))
    observe("query-write", f"{server.key} NUL byte in SQL", harness.describe(outcome))
    assert outcome[0] == "ERR"
