"""Same-host minion: the minion runs on the database server itself and connects to 127.0.0.1 / localhost.

Started by ``tests-e2e/up.sh local``: one minion per database container, sharing its network namespace
(``network_mode: service:<db>``), with the profiles of ``salt/minion-local.d/<db>.conf``. Skipped for a database whose
same-host minion does not answer.
"""

import contextlib

import pytest
from conftest import DBS
from conftest import SECRETS
from conftest import assert_error
from conftest import assert_ok
from conftest import compose
from dbs import sql

DIALECT = {
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "mssql": "mssql",
}
AUTH_TEXT = {
    "pg": "password authentication failed",
    "mysql": "1045",
    "mariadb": "1045",
    "oracle": "ORA-01017",
    "mssql": "18456",
}
NOPW_TEXT = {
    "pg": "no password supplied",
    "mysql": "1045",
    "mariadb": "1045",
    "oracle": "DPY-4001",
    "mssql": "18456",
}


@pytest.fixture(params=DBS)
def local(request, salt):
    db = request.param
    minion = f"minion-local-{db}"
    res = salt.cli("test.ping", minion=minion, timeout=20)
    if res.data is not True:
        pytest.skip(f"{minion} is not running (tests-e2e/up.sh local)")
    return db, minion


@pytest.mark.parametrize("profile", ["local_ro", "local_name"])
def test_same_host_ping_and_query(salt, local, profile):
    db, minion = local
    data = assert_ok(salt.cli("sqlx.ping", minion=minion, profile=profile))
    assert data["ok"] is True and data["dialect"] == DIALECT[db]
    data = assert_ok(
        salt.api("sqlx.query", minion=minion, profile=profile, sql=sql(db, "count_film"))
    )
    assert data["rows"] == [[1000]]


def test_same_host_execute_dry_run_and_errors(salt, local):
    db, minion = local
    data = assert_ok(
        salt.cli(
            "sqlx.execute",
            minion=minion,
            profile="local_rw",
            sql=sql(db, "insert_note"),
            params={"i": 7100, "n": "local"},
            dry_run=True,
        )
    )
    assert data["rows_affected"] == 1 and data["committed"] is False
    res = salt.cli(
        "sqlx.query",
        minion=minion,
        profile="local_rw",
        sql=sql(db, "count_note"),
        params={"i": 7100},
    )
    assert assert_ok(res)["rows"] == [[0]]
    assert_error(
        salt.cli("sqlx.query", minion=minion, profile="local_ro", sql=sql(db, "missing_table")),
        "E_SQL",
    )
    assert_error(
        salt.api("sqlx.execute", minion=minion, profile="local_ro", sql=sql(db, "ro_write")),
        "E_EXECUTE_DISABLED",
    )


def test_same_host_authentication_is_enforced(salt, local):
    # a local connection must still authenticate (no trust / peer rule lets a wrong or missing password in)
    db, minion = local
    if db == "pg":
        pytest.skip(
            "PostgreSQL loopback is trust in the image: see test_same_host_postgresql_trust_on_loopback_*"
        )
    res = salt.cli("sqlx.ping", minion=minion, profile="local_badpw")
    assert_error(res, "E_AUTH_FAILED", AUTH_TEXT[db])
    assert_error(
        salt.cli("sqlx.ping", minion=minion, profile="local_nopw"), "E_AUTH_FAILED", NOPW_TEXT[db]
    )
    assert_error(salt.cli("sqlx.ping", minion=minion, profile="local_closed"), "E_CONNECT")
    for secret in SECRETS.values():
        assert secret not in res.raw


HBA = "$(psql -U sakila -d sakila -tAc 'show hba_file')"


@contextlib.contextmanager
def _pg_loopback_auth(method):
    """Set the PostgreSQL loopback rules (127.0.0.1, ::1) to ``method``, reload, and restore the file afterwards."""
    compose("exec", "-T", "pg", "sh", "-c", f'cp "{HBA}" /tmp/pg_hba.orig', check=True)
    try:
        sed = rf"s#^(host\s+all\s+all\s+(127\.0\.0\.1/32|::1/128)\s+)\S+#\1{method}#"
        # rewrite in place (cat >): sed -i would replace the file with a root-owned copy the server cannot read
        compose(
            "exec", "-T", "pg", "sh", "-c", f'sed -E "{sed}" /tmp/pg_hba.orig > "{HBA}"', check=True
        )
        compose(
            "exec",
            "-T",
            "pg",
            "psql",
            "-U",
            "sakila",
            "-d",
            "sakila",
            "-tAc",
            "select pg_reload_conf()",
            check=True,
        )
        yield
    finally:
        compose("exec", "-T", "pg", "sh", "-c", f'cat /tmp/pg_hba.orig > "{HBA}"', check=True)
        compose(
            "exec",
            "-T",
            "pg",
            "psql",
            "-U",
            "sakila",
            "-d",
            "sakila",
            "-tAc",
            "select pg_reload_conf()",
            check=True,
        )


def test_same_host_postgresql_trust_on_loopback_ignores_the_password(salt):
    """Observed: the postgres images keep initdb's ``host all all 127.0.0.1/32 trust``. A minion on the database
    host then logs in as the profile's user with ANY password (or none): the profile password protects nothing.
    With ``scram-sha-256`` on the loopback lines the same profile is refused. Operators must set that (USER_GUIDE).
    """
    minion = "minion-local-pg"
    if salt.cli("test.ping", minion=minion, timeout=20).data is not True:
        pytest.skip(f"{minion} is not running (tests-e2e/up.sh local)")
    with _pg_loopback_auth("trust"):
        assert assert_ok(salt.cli("sqlx.ping", minion=minion, profile="local_badpw"))["ok"] is True
        assert assert_ok(salt.cli("sqlx.ping", minion=minion, profile="local_nopw"))["ok"] is True
    with _pg_loopback_auth("scram-sha-256"):
        assert_error(
            salt.cli("sqlx.ping", minion=minion, profile="local_badpw"),
            "E_AUTH_FAILED",
            AUTH_TEXT["pg"],
        )
        assert_error(
            salt.cli("sqlx.ping", minion=minion, profile="local_nopw"),
            "E_AUTH_FAILED",
            NOPW_TEXT["pg"],
        )
        assert assert_ok(salt.cli("sqlx.ping", minion=minion, profile="local_ro"))["ok"] is True
