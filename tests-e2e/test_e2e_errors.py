"""Error matrix: the real database message and the right [E_...] code on every path (CLI, salt-call, salt-api)."""

import pytest
from conftest import DBS
from conftest import PATHS
from conftest import SECRETS
from conftest import assert_error
from conftest import assert_ok
from dbs import TEXT
from dbs import sql

QUERY_ERRORS = [
    ("syntax", "E_SQL"),
    ("missing_table", "E_SQL"),
    ("missing_column", "E_SQL"),
    ("multi", None),  # E_SQL from the server, E_POLICY where the module refuses it (SQL Server)
]


def _multi_code(db):
    return "E_POLICY" if db == "mssql" else "E_SQL"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("case,code", QUERY_ERRORS)
def test_query_errors(salt, db, path, case, code):
    res = salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, case))
    assert_error(res, code or _multi_code(db), TEXT[case][db])
    assert res.retcode in (1, 0) if path == "cli" else res.retcode == 1, res


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_division_by_zero(salt, db, path):
    res = salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, "div0"))
    if db in ("mysql", "mariadb"):
        assert assert_ok(res)["rows"] == [[None]]  # MySQL family returns NULL for SELECT 1/0
    else:
        assert_error(res, "E_SQL_DATA", TEXT["div0"][db])


WRITE_ERRORS = [("unique", "E_CONSTRAINT"), ("fk", "E_CONSTRAINT"), ("not_null", "E_CONSTRAINT")]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("case,code", WRITE_ERRORS)
def test_constraint_errors(salt, db, path, case, code):
    res = salt.run(path, "sqlx.execute", profile=f"{db}_rw", sql=sql(db, case))
    assert_error(res, code, TEXT[case][db])


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_permission_denied_role_is_the_control(salt, db, path):
    # an execute-enabled profile on the read-only account: the database role refuses the write
    res = salt.run(path, "sqlx.execute", profile=f"{db}_ro_exec", sql=sql(db, "ro_write"))
    assert_error(res, "E_SQL", TEXT["permission"][db])


@pytest.mark.parametrize("path", ("cli", "api"))
@pytest.mark.parametrize("db", DBS)
def test_read_only_guard_on_query(salt, db, path):
    # sqlx.query on the write account: the in-session guard refuses the write (best effort, not the control)
    res = salt.run(
        path,
        "sqlx.query",
        profile=f"{db}_rw",
        sql=sql(db, "unique").replace("(1, 'dup')", "(9301, 'ro')"),
    )
    if db == "mssql":
        # no in-session read-only on SQL Server: the insert runs and is rolled back
        assert assert_ok(res)["rows"] == []
        assert assert_ok(
            salt.cli(
                "sqlx.query", profile=f"{db}_rw", sql=sql(db, "count_note"), params={"i": 9301}
            )
        )["rows"] == [[0]]
    else:
        assert_error(res, "E_READ_ONLY", TEXT["read_only"][db])


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_statement_timeout(salt, db, path):
    res = salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, "sleep"), timeout_ms=1000)
    assert_error(res, "E_TIMEOUT", TEXT["timeout"][db])
    assert res.elapsed < 15, res.elapsed


@pytest.mark.parametrize("db", ("mysql", "mariadb"))
def test_timeout_backstop_when_the_statement_lifts_the_server_limit(salt, db):
    res = salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "sleep_hint"), timeout_ms=1000)
    assert_error(res, "E_TIMEOUT", "2013")
    assert res.elapsed < 10


CONNECT_ERRORS = [
    ("badpw", "E_AUTH_FAILED", "auth"),
    ("nopw", "E_AUTH_FAILED", "nopw"),
    ("closed", "E_CONNECT", "closed"),
    ("unreach", "E_CONNECT", "unreach"),
    ("nohost", "E_CONNECT", "nohost"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("suffix,code,text", CONNECT_ERRORS)
def test_connect_errors(salt, db, path, suffix, code, text):
    res = salt.run(path, "sqlx.ping", profile=f"{db}_{suffix}")
    assert_error(res, code, TEXT[text][db])
    for secret in SECRETS.values():
        assert secret not in res.raw


TLS_CASES = [
    ("tls", None, None),
    ("tls_cert", None, None),
    (
        "tls_wrongca",
        "E_CONNECT",
        {
            "pg": "certificate verify failed",
            "mysql": "certificate verify failed",
            "mariadb": "certificate verify failed",
        },
    ),
    (
        "tls_badname",
        "E_CONNECT",
        {"pg": "does not match host name", "mysql": "match", "mariadb": "match"},
    ),
    ("tls_plain", None, None),
    ("tls_nocert", "E_AUTH_FAILED", {"pg": "certificate", "mysql": "1045", "mariadb": "1045"}),
    ("tls_otherclient", None, None),
]


@pytest.mark.parametrize("db", ("pg", "mysql", "mariadb"))
@pytest.mark.parametrize("case,code,texts", TLS_CASES)
def test_tls(salt, db, case, code, texts):
    res = salt.cli("sqlx.ping", profile=f"{db}_{case}")
    if case in ("tls", "tls_cert"):
        assert assert_ok(res)["ok"] is True
    elif case == "tls_plain":
        expected = {
            "pg": ("E_CONNECT", "no encryption"),
            "mysql": ("E_CONNECT", "3159"),
            "mariadb": ("E_AUTH_FAILED", "1045"),
        }[db]
        assert_error(res, *expected)
    elif case == "tls_otherclient":
        assert not res.ok and res.code in ("E_CONNECT", "E_AUTH_FAILED"), res
    else:
        assert_error(res, code, texts[db])
    assert "/etc/salt/sqlx-certs" not in (res.error or "")


def test_tls_mssql_refused_and_oracle_wallet_required(salt):
    assert_error(
        salt.cli("sqlx.ping", profile="mssql_tls"), "E_PROFILE_INVALID", "not supported for mssql"
    )
    assert_error(salt.cli("sqlx.ping", profile="oracle_tls"), "E_PROFILE_INVALID")


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_multi_statement_execute(salt, db, path):
    res = salt.run(path, "sqlx.execute", profile=f"{db}_rw", sql=sql(db, "multi_dml"))
    assert_error(res, _multi_code(db), TEXT["multi"][db])
    assert assert_ok(
        salt.cli("sqlx.query", profile=f"{db}_rw", sql=sql(db, "count_note"), params={"i": 9101})
    )["rows"] == [[0]]


@pytest.mark.parametrize("db", ("mysql", "mariadb"))
def test_executable_comment_runs_in_query_on_mysql_family(salt, db):
    # Documented behaviour, not a defect: the server executes /*! ... */ content; execute refuses it (verb filter).
    data = assert_ok(
        salt.cli("sqlx.query", profile=f"{db}_ro", sql="select 1 /*!40000 + 1 */ as v")
    )
    assert data["rows"] == [[2]]


@pytest.mark.parametrize("db", DBS)
def test_nul_byte_refused_before_the_driver(salt, db):
    # Only salt-api (JSON) can carry a NUL; argv cannot. The driver would truncate the statement at the NUL.
    assert_error(
        salt.api("sqlx.query", profile=f"{db}_ro", sql=sql(db, "nul")), "E_OPTION_INVALID", "NUL"
    )
    assert_error(
        salt.api("sqlx.execute", profile=f"{db}_rw", sql=sql(db, "nul_delete")),
        "E_OPTION_INVALID",
        "NUL",
    )
    assert assert_ok(
        salt.cli("sqlx.query", profile=f"{db}_rw", sql=sql(db, "count_note"), params={"i": 1})
    )["rows"] == [[1]]


def test_nul_byte_in_a_bound_value_on_mssql(salt):
    res = salt.api("sqlx.query", profile="mssql_ro", sql="select :x as v", params={"x": "a\x00b"})
    assert_error(res, "E_OPTION_INVALID", "NUL")


def test_sdb_missing_key(salt):
    assert_error(salt.cli("sqlx.ping", profile="pg_sdb_missing"), "E_SECRET")
