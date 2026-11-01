"""D16 at module level: with NO ``sqlx:_policy`` the connection can be sent in the call, for every function and dialect.

Same fakes as ``test_sqlx_call_params``; the database layer is injected, so what is asserted is what the module asks it to
do. Mirror tests for the strict config (``allow_call_params: []``) sit at the end.
"""

# pylint: disable=unused-argument,redefined-outer-name
import logging

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.unit.modules.test_sqlx_module import CONFIG
from tests.unit.modules.test_sqlx_module import ROWS
from tests.unit.modules.test_sqlx_module import Fake

LITERAL = "Lit3ral-call-PW-91xz"
REF = "sdb://vault/CALLREF_x"
CONN = {"host": "10.1.1.1", "port": 5432, "database": "d", "username": "u"}
CALLS = {
    "query": lambda **kw: mod.query(sql="select 1", **kw),
    "check_query_result": lambda **kw: mod.check_query_result(
        sql="select 1", operator="==", expected=1, **kw
    ),
    "check_row_count": lambda **kw: mod.check_row_count(
        sql="select 1", operator=">=", expected=1, **kw
    ),
    "ping": mod.ping,
}


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    f.cfg = {"profiles": dict(CONFIG["profiles"])}  # no sqlx:_policy at all
    f.sdb_map = {"sdb://vault/pg_ro": "base-pw", REF: "call-resolved-pw"}

    def get(uri):
        f.sdb.append(uri)
        return f.sdb_map[uri]

    salt = {
        "config.get": lambda key, default=None: f.cfg if key == "sqlx" else default,
        "sdb.get": get,
    }
    monkeypatch.setattr(mod, "__salt__", salt, raising=False)
    monkeypatch.setattr(mod, "__opts__", {"id": "minion1"}, raising=False)
    monkeypatch.setattr(mod.engine, "connection", f.connection)
    monkeypatch.setattr(mod.engine, "ping", f.ping)
    monkeypatch.setattr(mod.runner, "run_query", f.run_query)
    monkeypatch.setattr(mod.runner, "run_execute", f.run_execute)
    monkeypatch.setattr(
        mod.overrides,
        "_lookup",
        lambda resolver, host, port, listed=True: (
            ["169.254.169.254"] if "meta" in host else ["10.1.1.1"]
        ),
    )
    return f


def audit(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == mod.log.name and r.levelno == logging.INFO
    ]


def used(fake):
    return fake.connections[0] if fake.connections else fake.pings[0]


# -- every function, ad-hoc, no policy --------------------------------------------------------------------------------


@pytest.mark.parametrize("function", sorted(CALLS))
@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb", "oracle", "mssql"])
def test_every_function_takes_the_connection_in_the_call_on_every_dialect(fake, function, dialect):
    out = CALLS[function](dialect=dialect, password=LITERAL, **CONN)
    assert out["profile"] == "(adhoc)"
    assert LITERAL not in repr(out)
    got = used(fake)
    assert got["profile"]["dialect"] == ("mysql" if dialect == "mariadb" else dialect)
    assert got["profile"]["host"] == "10.1.1.1" and got["profile"]["username"] == "u"
    assert got["password"] == LITERAL  # a literal reaches the driver as it is
    assert fake.sdb == []  # a literal is not looked up


def test_connection_object_and_sdb_reference_work_too(fake):
    out = mod.query(sql="select 1", connection=dict(CONN, dialect="mysql", password=REF))
    assert out["profile"] == "(adhoc)"
    assert fake.connections[0]["password"] == "call-resolved-pw" and fake.sdb == [REF]


def test_with_a_profile_the_call_fields_overlay_it(fake):
    mod.query(profile="pg_ro", sql="select 1", database="other", password=LITERAL)
    assert fake.connections[0]["profile"]["database"] == "other"
    assert fake.connections[0]["profile"]["host"] == "db1"
    assert fake.connections[0]["password"] == LITERAL


def test_retargeting_a_profile_never_sends_its_password_elsewhere(fake):
    mod.query(profile="pg_ro", sql="select 1", host="10.9.9.9")
    assert fake.sdb == [] and fake.connections[0]["password"] is None
    assert fake.connections[0]["profile"]["password_dropped"] is True


def test_the_audit_line_names_fields_and_target_but_never_the_password(fake, caplog):
    caplog.set_level(logging.DEBUG)
    mod.query(sql="select 1", dialect="postgresql", password=LITERAL, **CONN)
    (line,) = audit(caplog)
    assert "overrides=database,dialect,host,password,port,username" in line
    assert "target=10.1.1.1:5432" in line and "profile=(adhoc)" in line
    assert all(LITERAL not in r.getMessage() for r in caplog.records)
    assert LITERAL not in caplog.text


def test_a_failure_with_a_literal_password_logs_and_raises_without_it(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.connect_error = RuntimeError(f"connection refused for password={LITERAL}")
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(sql="select 1", dialect="postgresql", password=LITERAL, **CONN)
    assert LITERAL not in str(exc.value) and LITERAL not in caplog.text


def test_the_module_never_logs_the_call_keywords(fake, caplog):
    caplog.set_level(logging.DEBUG)
    mod.psql_query(
        "select 1", user="u", host="10.1.1.1", port=5432, maintenance_db="d", password=LITERAL
    )
    mod.query(sql="select 1", dialect="oracle", password=LITERAL, **CONN)
    assert LITERAL not in caplog.text and all(
        LITERAL not in str(r.__dict__) for r in caplog.records
    )


# -- psql_query with Salt's own argument names ------------------------------------------------------------------------


def test_psql_query_with_salts_argument_names_and_no_policy(fake):
    out = mod.psql_query(
        "select 1 as one;",
        user="ro",
        host="127.0.0.1",
        port=54329,
        maintenance_db="sqlx_test",
        password="ro_pw",
    )
    assert out == [{"id": "1", "total": "12.50"}, {"id": "2", "total": None}]
    got = fake.connections[0]
    assert got["profile"]["dialect"] == "postgresql" and got["profile"]["name"] == "(adhoc)"
    assert (got["profile"]["username"], got["profile"]["database"], got["profile"]["port"]) == (
        "ro",
        "sqlx_test",
        54329,
    )
    assert got["password"] == "ro_pw" and fake.queries[0]["sql"] == "select 1 as one"


def test_psql_query_incomplete_connection_says_what_is_missing(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\].*database, username"):
        mod.psql_query("select 1", host="127.0.0.1")
    assert fake.connections == []


def test_psql_query_ad_hoc_is_postgresql_only(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\].*postgresql"):
        mod.psql_query("select 1", user="u", host="10.1.1.1", maintenance_db="d", dialect="mysql")
    assert fake.connections == []


def test_psql_query_overlay_on_the_default_profile_does_not_add_a_dialect(fake):
    fake.cfg["default_profile"] = "pg_ro"
    mod.psql_query("select 1", host="10.1.1.1", password=LITERAL)
    assert fake.connections[0]["profile"]["call_overrides"] == ["host", "password"]


def test_psql_query_write_with_an_ad_hoc_connection_is_refused_before_connecting(fake):
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.psql_query(
            "insert into t values (1)",
            user="rw",
            host="10.1.1.1",
            maintenance_db="d",
            password="rw_pw",
            write=True,
        )
    assert fake.connections == [] and fake.executes == []


# -- execute --------------------------------------------------------------------------------------------------------


def test_execute_with_an_ad_hoc_connection_is_refused(fake):
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(sql="update t set a = 1", dialect="postgresql", password=LITERAL, **CONN)
    assert fake.connections == [] and fake.executes == [] and fake.sdb == []


@pytest.mark.parametrize(
    "kw", [{"database": "x"}, {"host": "10.2.2.2"}, {"password": LITERAL}, {"username": "x"}]
)
def test_execute_on_a_writing_profile_works_with_connection_fields(fake, kw):
    """Not restricted: Salt's own psql_query writes whatever connection the call names; ``allow_execute`` is the profile's."""
    assert mod.execute(profile="pg_rw", sql="update t set a = 1", **kw)["rows_affected"] == 3
    assert len(fake.connections) == 1


def test_execute_on_a_writing_profile_without_fields_still_works(fake):
    assert mod.execute(profile="pg_rw", sql="update t set a = 1")["rows_affected"] == 3


def test_execute_with_connection_fields_works_only_when_the_operator_lists_them(fake):
    fake.cfg["_policy"] = {"allow_call_params": ["database"]}
    assert (
        mod.execute(profile="pg_rw", sql="update t set a = 1", database="x")["rows_affected"] == 3
    )
    assert fake.connections[0]["profile"]["database"] == "x"


def test_a_read_only_profile_cannot_execute_whatever_the_call_sends(fake):
    with pytest.raises(
        CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\].*does not set allow_execute"
    ):
        mod.execute(profile="pg_ro", sql="update t set a = 1", database="x")


# -- targets and caps ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["169.254.169.254", "metadata.google.internal.meta", "fe80::1"])
def test_metadata_targets_are_refused_before_a_secret_or_a_socket(fake, host):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.query(sql="select 1", dialect="postgresql", password=REF, **{**CONN, "host": host})
    assert fake.connections == [] and fake.sdb == []


def test_loopback_and_private_addresses_are_fine_by_default(fake):
    for host in ("127.0.0.1", "10.4.4.4", "192.168.0.7"):
        mod.ping(dialect="postgresql", **{**CONN, "host": host})
    assert len(fake.pings) == 3


@pytest.mark.parametrize(
    "kw",
    [
        {"read_only": False},
        {"ssl_mode": "verify-ca"},
        {"query": {"application_name": "x"}},
        {"connect_args": {"application_name": "x"}},
        {"statement_timeout_ms": 5},
        {"connect_timeout_s": 5},
    ],
)
def test_opt_in_fields_are_refused_by_default(fake, kw):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.query(sql="select 1", dialect="postgresql", **CONN, **kw)
    assert fake.connections == []


@pytest.mark.parametrize(
    "kw",
    [
        {"allow_execute": True},
        {"error_detail": True},
        {"row_limit": 99999},
        {"execute_verbs": ["DROP"]},
    ],
)
def test_safety_switches_are_unknown_options_by_default(fake, kw):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.query(sql="select 1", dialect="postgresql", **CONN, **kw)
    assert fake.connections == []


def test_an_ad_hoc_connection_has_the_hard_defaults_and_callers_cannot_raise_them(fake):
    mod.query(
        sql="select 1",
        dialect="postgresql",
        limit=10**9,
        timeout_ms=10**9,
        max_bytes=10**12,
        **CONN,
    )
    got = fake.connections[0]
    assert got["profile"]["read_only"] is True and got["profile"]["error_detail"] is False
    assert got["timeout_ms"] == 30000
    assert fake.queries[0]["limit"] == 500 and fake.queries[0]["max_bytes"] == 1024 * 1024


# -- the operator tightens it ---------------------------------------------------------------------------------------


def test_allowed_hosts_in_minion_config_restrict_the_default(fake):
    fake.cfg["_policy"] = {"allowed_hosts": ["10.1.0.0/16"]}
    mod.ping(dialect="postgresql", **CONN)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allowed_hosts"):
        mod.ping(dialect="postgresql", **{**CONN, "host": "10.2.0.1"})
    assert len(fake.pings) == 1


@pytest.mark.parametrize("function", sorted(CALLS))
def test_strict_config_refuses_every_connection_keyword_exactly_as_before(fake, function):
    fake.cfg["_policy"] = {"allow_call_params": []}
    for kw in (
        {"dialect": "postgresql", "password": LITERAL, **CONN},
        {"connection": dict(CONN)},
        {"host": "x"},
    ):
        with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
            CALLS[function](**kw)
    assert fake.connections == [] and fake.pings == [] and fake.sdb == []


def test_strict_config_still_serves_profile_calls(fake):
    fake.cfg["_policy"] = {"allow_call_params": []}
    assert mod.query(profile="pg_ro", sql="select 1")["rows"] == ROWS["rows"]


def test_strict_config_psql_query_is_e_policy_and_adds_no_dialect(fake):
    fake.cfg["_policy"] = {"allow_call_params": []}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_call_params"):
        mod.psql_query("select 1", user="u", host="10.1.1.1", maintenance_db="d", password=LITERAL)
    assert fake.connections == []


def test_strict_config_execute_is_unchanged(fake):
    fake.cfg["_policy"] = {"allow_call_params": []}
    assert mod.execute(profile="pg_rw", sql="update t set a = 1")["rows_affected"] == 3
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.execute(profile="pg_rw", sql="update t set a = 1", host="10.1.1.1")
