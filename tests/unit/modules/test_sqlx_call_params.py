"""Module-level tests for call-time connection parameters and ``sqlx.psql_query`` (SPEC sections 3.2 and 4.7).

Same fakes as ``test_sqlx_module``: the database layer is injected, so what is asserted is what the module asks it to do.
"""

# pylint: disable=unused-argument,redefined-outer-name
import inspect
import logging

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.unit.modules.test_sqlx_module import CONFIG
from tests.unit.modules.test_sqlx_module import PG_PASSWORD
from tests.unit.modules.test_sqlx_module import ROWS
from tests.unit.modules.test_sqlx_module import Fake
from tests.unit.modules.test_sqlx_module import db_error

LITERAL = "Lit3ral-call-PW-91xz"
REF = "sdb://vault/CALLREF_x"


def policy(**kw):
    base = {
        "allow_call_params": ["host", "port", "database", "username", "password", "read_only"],
        "allowed_hosts": ["*.corp.example", "10.0.0.0/8"],
    }
    base.update(kw)
    return base


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    f.cfg = dict(CONFIG, _policy=policy())
    f.cfg["profiles"] = dict(CONFIG["profiles"])
    f.sdb_map = {"sdb://vault/pg_ro": PG_PASSWORD, REF: "call-resolved-pw"}

    def get(uri):
        f.sdb.append(uri)
        return f.sdb_map[uri]

    f.sdb_get = get
    salt = {
        "config.get": lambda key, default=None: f.cfg if key == "sqlx" else default,
        "sdb.get": lambda u: f.sdb_get(u),  # pylint: disable=unnecessary-lambda
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
        lambda resolver, host, port: ["203.0.113.9"] if "evil" in host else ["10.1.1.1"],
    )
    return f


def audit(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == mod.log.name and r.levelno == logging.INFO
    ]


# -- feature off ------------------------------------------------------------------------------------------------------


def test_feature_off_connection_keywords_are_unknown_options_and_nothing_connects(fake):
    fake.cfg["_policy"] = {}
    for kw in ({"host": "db2.corp.example"}, {"connection": {"host": "x"}}, {"password": LITERAL}):
        with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
            mod.query(profile="pg_ro", sql="select 1", **kw)
    assert fake.connections == [] and fake.sdb == []


def test_calls_without_overrides_are_unchanged_when_the_feature_is_on(fake):
    out = mod.query(profile="pg_ro", sql="select 1")
    assert out["profile"] == "pg_ro"
    assert fake.connections[0]["profile"]["host"] == "db1"


# -- every connecting function takes the keywords ---------------------------------------------------------------------

CALLS = {
    "query": lambda **kw: mod.query(profile="pg_ro", sql="select 1", **kw),
    "execute": lambda **kw: mod.execute(profile="pg_rw", sql="update t set a = 1", **kw),
    "check_query_result": lambda **kw: mod.check_query_result(
        profile="pg_ro", sql="select 1", operator="==", expected=1, **kw
    ),
    "check_row_count": lambda **kw: mod.check_row_count(
        profile="pg_ro", sql="select 1", operator=">=", expected=1, **kw
    ),
    "ping": lambda **kw: mod.ping(profile="pg_ro", **kw),
}


@pytest.mark.parametrize("function", sorted(CALLS))
@pytest.mark.parametrize("style", ["flat", "connection"])
def test_overrides_reach_the_connection(fake, function, style):
    given = {"host": "db2.corp.example", "port": 6543, "database": "other", "username": "svc"}
    CALLS[function](**(given if style == "flat" else {"connection": given}))
    used = (fake.connections or fake.pings)[0]["profile"]
    assert (used["host"], used["port"], used["database"], used["username"]) == (
        "db2.corp.example",
        6543,
        "other",
        "svc",
    )


def test_table_list_takes_overrides(fake, monkeypatch):
    monkeypatch.setattr(
        mod.sqlalchemy,
        "inspect",
        lambda conn: type(
            "I",
            (),
            {
                "default_schema_name": "s",
                "get_table_names": lambda s, schema=None: ["t"],
                "get_view_names": lambda s, schema=None: [],
            },
        )(),
    )
    out = mod.table_list(profile="pg_ro", host="db2.corp.example")
    assert out["tables"] == ["t"]
    assert fake.connections[0]["profile"]["host"] == "db2.corp.example"


def test_policy_violations_connect_to_nothing(fake):
    for kw in (
        {"host": "evil.example"},
        {"username": "x", "connection": {"username": "y"}},
        {"read_only": False},
    ):
        with pytest.raises(SaltInvocationError):
            mod.query(profile="pg_ro", sql="select 1", **kw)
    fake.cfg["_policy"] = policy(allow_call_params=["database"])
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.query(profile="pg_ro", sql="select 1", host="db2.corp.example")
    assert fake.connections == [] and fake.sdb == []


# -- hard rules through the module -----------------------------------------------------------------------------------


def test_call_cannot_enable_execute(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.execute(profile="pg_ro", sql="update t set a = 1", allow_execute=True)
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.execute(profile="pg_ro", sql="update t set a = 1", connection={"allow_execute": True})
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(profile="pg_ro", sql="update t set a = 1", host="db2.corp.example")
    assert fake.executes == []


def test_adhoc_connection_can_never_execute(fake):
    fake.cfg["_policy"] = policy(
        allow_call_params=["dialect", "host", "database", "username"], allow_adhoc_connection=True
    )
    adhoc = {"dialect": "postgresql", "host": "db2.corp.example", "database": "d", "username": "u"}
    assert mod.query(sql="select 1", **adhoc)["profile"] == "(adhoc)"
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(sql="update t set a = 1", **adhoc)


def test_read_only_cannot_be_turned_off_and_timeouts_only_lowered(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.query(profile="pg_ro", sql="select 1", read_only=False)
    fake.cfg["_policy"] = policy(allow_call_params=["statement_timeout_ms", "read_only"])
    mod.query(profile="pg_ro", sql="select 1", statement_timeout_ms=10**9, read_only=True)
    used = fake.connections[0]["profile"]
    assert used["statement_timeout_ms"] == 30000 and used["read_only"] is True


# -- secrets ----------------------------------------------------------------------------------------------------------


def test_call_sdb_password_is_resolved_and_the_reference_not_logged(fake, caplog):
    caplog.set_level(logging.DEBUG)
    mod.query(profile="pg_ro", sql="select 1", host="db2.corp.example", password=REF)
    assert fake.connections[0]["password"] == "call-resolved-pw"
    assert fake.sdb == [REF]
    assert "CALLREF" not in caplog.text and "call-resolved-pw" not in caplog.text


def test_unresolvable_call_reference_is_e_secret_without_the_reference(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.sdb_get = lambda uri: (_ for _ in ()).throw(KeyError(uri))
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(profile="pg_ro", sql="select 1", password=REF)
    assert str(exc.value).startswith("[E_SECRET]")
    assert "CALLREF" not in str(exc.value) + caplog.text


def test_literal_call_password_is_refused_and_never_logged(fake, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]") as exc:
        mod.query(profile="pg_ro", sql="select 1", password=LITERAL)
    assert LITERAL not in str(exc.value) + caplog.text
    assert fake.connections == []


def test_literal_call_password_when_allowed_is_used_but_masked_in_logs(fake, caplog):
    fake.cfg["_policy"] = policy(allow_call_literal_password=True)
    caplog.set_level(logging.DEBUG)
    mod.query(profile="pg_ro", sql="select 1", password=LITERAL, host="db2.corp.example")
    assert fake.connections[0]["password"] == LITERAL
    assert LITERAL not in caplog.text


def test_retargeted_connection_does_not_receive_the_profile_password(fake):
    mod.query(profile="pg_ro", sql="select 1", host="db2.corp.example")
    assert fake.connections[0]["password"] is None
    assert fake.sdb == []


# -- audit ------------------------------------------------------------------------------------------------------------


def test_audit_line_names_override_keys_and_the_target_only(fake, caplog):
    fake.cfg["_policy"] = policy(allow_call_literal_password=True)
    caplog.set_level(logging.INFO)
    mod.query(
        profile="pg_ro",
        sql="select 1",
        host="db2.corp.example",
        port=6543,
        password=LITERAL,
        __pub_user="alice",
    )
    (line,) = audit(caplog)
    assert "overrides=host,password,port" in line
    assert "target=db2.corp.example:6543" in line
    assert LITERAL not in line


def test_audit_line_without_overrides_still_has_the_target(fake, caplog):
    caplog.set_level(logging.INFO)
    mod.query(profile="pg_ro", sql="select 1")
    (line,) = audit(caplog)
    assert "overrides=-" in line and "target=db1:5432" in line


def test_audit_line_of_a_refused_call_has_no_target(fake, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(SaltInvocationError):
        mod.query(profile="pg_ro", sql="select 1", host="evil.example")
    (line,) = audit(caplog)
    assert "target=-" in line and "evil.example" not in line and "outcome=E_POLICY" in line


# -- default_profile and psql_query ----------------------------------------------------------------------------------


def test_default_profile_is_used_when_no_profile_is_given(fake):
    fake.cfg["default_profile"] = "pg_ro"
    assert mod.query(sql="select 1")["profile"] == "pg_ro"
    assert mod.ping()["ok"] is True


def test_psql_query_signature_matches_salt():
    sig = inspect.signature(mod.psql_query)
    names = list(sig.parameters)
    assert names[:8] == [
        "query",
        "user",
        "host",
        "port",
        "maintenance_db",
        "password",
        "runas",
        "write",
    ]
    assert all(sig.parameters[n].default is None for n in names[1:7])
    assert sig.parameters["write"].default is False


def test_psql_query_returns_salts_list_of_dicts(fake):
    fake.cfg["default_profile"] = "pg_ro"
    out = mod.psql_query("select id, total from orders;")
    assert out == [{"id": "1", "total": "12.50"}, {"id": "2", "total": None}]
    assert fake.queries[0]["sql"] == "select id, total from orders"


def test_psql_query_with_a_profile_and_call_params(fake):
    out = mod.psql_query(
        "select 1",
        user="svc",
        host="db2.corp.example",
        port=6543,
        maintenance_db="other",
        password=REF,
        profile="pg_ro",
    )
    assert out[0]["id"] == "1"
    used = fake.connections[0]["profile"]
    assert (used["username"], used["host"], used["port"], used["database"]) == (
        "svc",
        "db2.corp.example",
        6543,
        "other",
    )
    assert fake.connections[0]["password"] == "call-resolved-pw"


def test_psql_query_without_default_profile_or_profile_is_e_option_invalid(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        mod.psql_query("select 1")


def test_psql_query_runas_is_refused(fake):
    fake.cfg["default_profile"] = "pg_ro"
    with pytest.raises(SaltInvocationError) as exc:
        mod.psql_query("select 1", runas="postgres")
    assert (
        str(exc.value)
        == "[E_OPTION_INVALID] runas is not supported; sqlx does not use psql or an OS user"
    )
    assert fake.connections == []


def test_psql_query_connection_arguments_with_the_feature_off_are_e_policy(fake):
    fake.cfg["default_profile"] = "pg_ro"
    fake.cfg["_policy"] = {}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_call_params"):
        mod.psql_query("select 1", user="svc")
    assert mod.psql_query("select 1")  # no override: runs on the default profile
    assert len(fake.connections) == 1


def test_psql_query_only_for_postgresql_profiles(fake):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\].*postgresql"):
        mod.psql_query("select 1", profile="my_ro")
    assert fake.connections == []


def test_psql_query_failure_raises_the_real_error_not_an_empty_list(fake):
    fake.cfg["default_profile"] = "pg_ro"
    fake.query_error = db_error("42P01", 'relation "x" does not exist')
    with pytest.raises(
        CommandExecutionError, match=r'^\[E_SQL\] postgresql 42P01: relation "x" does not exist'
    ):
        mod.psql_query("select * from x")


def test_psql_query_write_goes_through_execute_and_its_gates(fake):
    fake.cfg["default_profile"] = "pg_ro"
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.psql_query("update t set a = 1", write=True)
    assert fake.executes == []
    out = mod.psql_query("update t set a = 1;", write=True, profile="pg_rw")
    assert out == [{"rows_affected": "3"}]
    assert fake.executes[0]["sql"] == "update t set a = 1"
    assert fake.executes[0]["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]
    assert fake.executes[0]["dry_run"] is False


def test_psql_query_write_still_applies_the_verb_filter_in_the_runner(fake):
    # the runner (not the module) owns the verb filter; the module passes the profile's verbs unchanged
    mod.psql_query("update t set a = 1", write=True, profile="pg_rw")
    assert fake.executes[0]["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]


def test_psql_query_truncation_is_logged(fake, caplog, monkeypatch):
    fake.cfg["default_profile"] = "pg_ro"
    fake.query_result = dict(ROWS, truncated=True)
    own = logging.getLogger(
        "sqlx_test_truncation"
    )  # not propagated: keeps Salt's deferred root handler out of it
    own.propagate = False
    own.addHandler(caplog.handler)
    monkeypatch.setattr(mod, "log", own)
    assert len(mod.psql_query("select 1")) == 2
    assert "truncated" in caplog.text
