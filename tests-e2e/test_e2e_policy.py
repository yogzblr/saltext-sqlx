"""Policy, eauth and the psql_query look-alike, through the CLI and salt-api."""

import json
import time

import pytest
from conftest import EAUTH
from conftest import assert_error
from conftest import assert_ok


def test_policy_off_refuses_call_time_connection_fields(salt):
    for minion_kw in (
        {"host": "pg"},
        {"port": 5432},
        {"username": "rw"},
        {"connection": {"host": "pg"}},
    ):
        res = salt.cli("sqlx.query", profile="pg_ro", sql="select 1", **minion_kw)
        assert_error(res, "E_OPTION_UNKNOWN")
    assert_error(salt.api("sqlx.ping", profile="pg_ro", host="mysql"), "E_OPTION_UNKNOWN")
    assert_error(salt.cli("sqlx.psql_query", "select 1", profile="pg_ro", host="pg"), "E_POLICY")


def test_policy_on_allowed_host_works(salt):
    # same dialect, different listed host: the profile password is dropped (D14), so the call names its own sdb://
    res = salt.cli(
        "sqlx.query",
        minion="minion-policy",
        profile="mysql_ro",
        host="mariadb",
        password="sdb://e2e_sdb/ro_pw",
        sql="select version() as v",
    )
    assert "MariaDB" in assert_ok(res)["rows"][0][0]
    res = salt.api("sqlx.ping", minion="minion-policy", profile="mysql_ro", host="mariadb")
    assert_error(res, "E_AUTH_FAILED", "1045")  # retargeted without a password: not sent
    res = salt.api(
        "sqlx.query",
        minion="minion-policy",
        profile="pg_ro",
        database="sakila",
        username="rw",
        password="sdb://e2e_sdb/rw_pw",
        sql="select current_user as u",
    )
    assert assert_ok(res)["rows"] == [["rw"]]


@pytest.mark.parametrize(
    "host", ["evil.example", "10.254.254.254", "169.254.169.254", "localhost", "pg.evil.example"]
)
def test_policy_on_disallowed_host_refused_without_connecting(salt, host):
    start = time.monotonic()
    res = salt.cli(
        "sqlx.ping",
        minion="minion-policy",
        profile="pg_ro",
        host=host,
        password="sdb://e2e_sdb/ro_pw",
    )
    assert_error(res, "E_POLICY")
    assert host not in res.error
    assert time.monotonic() - start < 10


def test_policy_on_port_and_hard_rules(salt):
    assert_error(
        salt.cli("sqlx.ping", minion="minion-policy", profile="pg_ro", port=6543), "E_POLICY"
    )
    assert_error(
        salt.cli("sqlx.ping", minion="minion-policy", profile="pg_ro", read_only=False), "E_POLICY"
    )
    for key, value in (
        ("row_limit", 9999),
        ("allow_execute", True),
        ("error_detail", True),
        ("_policy", {}),
    ):
        assert_error(
            salt.api(
                "sqlx.query",
                minion="minion-policy",
                profile="pg_ro",
                sql="select 1",
                **{key: value},
            ),
            "E_OPTION_UNKNOWN",
        )


def test_literal_password_refused(salt):
    res = salt.cli(
        "sqlx.ping", minion="minion-policy", profile="pg_ro", password="LiteralPw_e2e_not_a_secret"
    )
    assert_error(res, "E_POLICY", "password")
    assert "LiteralPw_e2e_not_a_secret" not in res.error


def test_sdb_resolved(salt):
    data = assert_ok(
        salt.cli(
            "sqlx.ping", minion="minion-policy", profile="pg_ro", password="sdb://e2e_sdb/ro_pw"
        )
    )
    assert data["ok"] is True
    assert_error(
        salt.cli(
            "sqlx.ping", minion="minion-policy", profile="pg_ro", password="sdb://e2e_sdb/nope_key"
        ),
        "E_SECRET",
    )


def _api_low(user, fun, kwarg):
    return {
        "client": "local",
        "tgt": "minion",
        "fun": fun,
        "kwarg": kwarg,
        "eauth": "file",
        "username": user,
        "password": EAUTH[user],
    }


@pytest.mark.parametrize(
    "user,fun,kwarg,allowed",
    [
        ("sre_ops", "sqlx.query", {"profile": "pg_ro", "sql": "select 1"}, True),
        ("sre_ops", "sqlx.ping", {"profile": "mssql_ro"}, True),
        ("sre_ops", "sqlx.query", {"profile": "pg_rw", "sql": "select 1"}, False),
        ("sre_ops", "sqlx.query", {"profile": "pg_ro_x", "sql": "select 1"}, False),
        ("sre_ops", "sqlx.query", {"profile": "xpg_ro", "sql": "select 1"}, False),
        ("sre_ops", "sqlx.execute", {"profile": "pg_ro", "sql": "delete from film"}, False),
        ("sre_ops", "sqlx.table_list", {"profile": "pg_ro"}, False),
        ("sre_ops", "cmd.run", {"cmd": "id"}, False),
        (
            "dba_writers",
            "sqlx.execute",
            {"profile": "pg_rw", "sql": "update scratch.e2e_notes set note = note where id = 1"},
            True,
        ),
        (
            "dba_writers",
            "sqlx.execute",
            {"profile": "pg_ro_exec", "sql": "delete from film where film_id = -1"},
            False,
        ),
        ("dba_writers", "sqlx.query", {"profile": "pg_rw", "sql": "select 1"}, False),
    ],
)
def test_eauth_regex_through_salt_api(salt, user, fun, kwarg, allowed):
    status, text = salt.api_raw(_api_low(user, fun, kwarg))
    if allowed:
        assert status == 200, text
        ret = json.loads(text)["return"][0]["minion"]
        assert not (isinstance(ret, str) and ret.startswith("ERROR")), ret
    else:
        assert status == 401, (status, text[:300])


def test_eauth_wrong_password(salt):
    low = _api_low("sre_ops", "sqlx.ping", {"profile": "pg_ro"})
    low["password"] = "not-the-password"
    assert salt.api_raw(low)[0] == 401


def test_psql_query_shape_and_errors(salt):
    rows = assert_ok(
        salt.cli(
            "sqlx.psql_query",
            "select film_id, title, rental_rate, last_update from film order by film_id limit 2",
            profile="pg_ro",
        )
    )
    assert rows[0] == {
        "film_id": "1",
        "title": "ACADEMY DINOSAUR",
        "rental_rate": "0.99",
        "last_update": rows[0]["last_update"],
    }
    assert isinstance(rows[0]["last_update"], str) and len(rows) == 2
    rows = assert_ok(
        salt.api(
            "sqlx.psql_query", "select t_null, t_json from type_probe where id = 1", profile="pg_ro"
        )
    )
    assert rows == [{"t_null": None, "t_json": '{"a":1,"b":[true,null]}'}]
    assert_error(
        salt.cli("sqlx.psql_query", "select * from nope_e2e", profile="pg_ro"), "E_SQL", "42P01"
    )
    assert_error(salt.cli("sqlx.psql_query", "select 1", profile="mysql_ro"), "E_OPTION_INVALID")
    assert_error(
        salt.cli("sqlx.psql_query", "select 1", profile="pg_ro", runas="postgres"),
        "E_OPTION_INVALID",
        "runas",
    )
    rows = assert_ok(
        salt.cli(
            "sqlx.psql_query",
            "select 1 as one",
            minion="minion-policy",
            host="pg",
            port=5432,
            user="ro",
            maintenance_db="sakila",
            password="sdb://e2e_sdb/ro_pw",
        )
    )
    assert rows == [{"one": "1"}]
    rows = assert_ok(
        salt.cli("sqlx.psql_query", "select 2 as two;", minion="minion-policy")
    )  # default_profile pg_ro
    assert rows == [{"two": "2"}]


@pytest.mark.parametrize(
    "db,sql",
    [
        ("oracle", "select lower(user) as u from dual"),
        ("mssql", "select suser_name() as u"),
        ("mysql", "select substring_index(current_user(), '@', 1) as u"),
    ],
)
def test_policy_on_username_override_on_other_dialects(salt, db, sql):
    res = salt.api(
        "sqlx.query",
        minion="minion-policy",
        profile=f"{db}_ro",
        username="rw",
        password="sdb://e2e_sdb/rw_pw",
        sql=sql,
    )
    assert assert_ok(res)["rows"] == [["rw"]]
    # retargeted without its own password: the profile's password is not sent (D14)
    assert_error(
        salt.api("sqlx.ping", minion="minion-policy", profile=f"{db}_ro", username="rw"),
        "E_AUTH_FAILED",
    )
    assert_error(
        salt.cli("sqlx.ping", minion="minion-policy", profile=f"{db}_ro", host="evil.example"),
        "E_POLICY",
    )
