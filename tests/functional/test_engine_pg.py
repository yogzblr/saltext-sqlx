"""Functional tests for lib/engine against a real PostgreSQL (TEST_PLAN section 4: ping, encoding).

Set ``SQLX_TEST_PG_URL`` (for example ``postgresql+psycopg://user:pw@127.0.0.1:5432/db``); the database must be UTF8.
Optional: ``SQLX_TEST_PG_ASCII_DB`` names an existing SQL_ASCII database on the same server for the encoding
regression. Without the URL every test here is skipped.
"""

import os

import pytest
from salt.exceptions import CommandExecutionError
from sqlalchemy.engine import make_url

from saltext.sqlx.lib import engine
from saltext.sqlx.lib import profiles

PG_URL = os.environ.get("SQLX_TEST_PG_URL")
ASCII_DB = os.environ.get("SQLX_TEST_PG_ASCII_DB")

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not PG_URL, reason="SQLX_TEST_PG_URL is not set"),
]


def _profile(**overrides):
    url = make_url(PG_URL)
    raw = {
        "dialect": "postgresql",
        "host": url.host,
        "port": url.port or 5432,
        "database": url.database,
        "username": url.username,
        "connect_timeout_s": 5,
    }
    raw.update(overrides)
    return profiles.validate_profile("pg_func", raw), url.password


def test_ping_returns_version():
    prof, password = _profile()
    out = engine.ping(prof, password)
    assert out["ok"] is True
    assert out["dialect"] == "postgresql"
    assert out["server_version"].startswith("16"), out
    assert isinstance(out["elapsed_ms"], int)


def test_connection_applies_timeout_and_read_only():
    prof, password = _profile(statement_timeout_ms=1234)
    with engine.connection(prof, password) as conn:
        assert conn.exec_driver_sql("SHOW statement_timeout").scalar() == "1234ms"
        assert conn.exec_driver_sql("SHOW transaction_read_only").scalar() == "on"
        conn.rollback()


def test_call_timeout_can_only_lower_profile_timeout():
    prof, password = _profile(statement_timeout_ms=5000)
    with engine.connection(prof, password, timeout_ms=900) as conn:
        assert conn.exec_driver_sql("SHOW statement_timeout").scalar() == "900ms"
    with engine.connection(prof, password, timeout_ms=60_000) as conn:
        assert conn.exec_driver_sql("SHOW statement_timeout").scalar() == "5s"


def test_not_read_only_profile():
    prof, password = _profile(read_only=False)
    with engine.connection(prof, password) as conn:
        assert conn.exec_driver_sql("SHOW transaction_read_only").scalar() == "off"


def test_nullpool_connection_is_really_closed():
    prof, password = _profile()
    with engine.managed_engine(prof, password) as eng:
        with eng.connect() as conn:
            pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        with eng.connect() as conn:
            assert conn.exec_driver_sql("SELECT pg_backend_pid()").scalar() != pid


def test_wrong_password_is_auth_failed_without_secrets():
    prof, _ = _profile()
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(prof, "definitely-wrong-Pw@:/%?")
    msg = str(exc.value)
    if not msg.startswith("[E_AUTH_FAILED]"):
        # trust-authenticated servers accept any password; any other outcome is a real failure
        assert False, msg
    assert "definitely-wrong" not in msg
    assert "://" not in msg


def test_wrong_port_is_e_connect():
    prof, password = _profile(port=1)
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(prof, password)
    assert str(exc.value).startswith("[E_CONNECT] postgresql")
    assert password not in str(exc.value)


def test_unknown_database_is_e_connect():
    prof, password = _profile(database="no_such_db_t4")
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(prof, password)
    assert str(exc.value).startswith("[E_CONNECT] postgresql")
    assert "no_such_db_t4" in str(exc.value)


@pytest.mark.skipif(not ASCII_DB, reason="SQLX_TEST_PG_ASCII_DB is not set")
def test_sql_ascii_database_gives_clear_utf8_error():
    prof, password = _profile(database=ASCII_DB)
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(prof, password)
    msg = str(exc.value)
    assert msg.startswith("[E_CONNECT] postgresql")
    assert "UTF8" in msg
    assert "TypeError" not in msg
    assert password not in msg
