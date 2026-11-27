# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with,redefined-outer-name,unused-import
"""D17 (owner decision 2026-11-27): ``execute`` / ``psql_query write=True`` work with NO profile, on by default. Attack it.

Two halves:

* no server needed (a counting ``socket.connect`` and the fake PostgreSQL of ``test_call_params_surface``): what a caller
  can never send, which hosts are refused for a write before a secret or a socket, the opt-out and its two interactions
  (``allow_adhoc_connection: false``, ``allow_call_params: []``), the password on every error path and log;
* live servers (PostgreSQL with both drivers, MySQL, MariaDB; skipped with the reason when one is down): one statement
  per call, an ad-hoc ``query`` containing an INSERT writes nothing, an ad-hoc write really commits and a dry run does not.

The refusal tests of the pre-D17 contract are kept as the ``allow_adhoc_execute: false`` mirrors.
"""

import logging

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.security import harness
from tests.security.test_call_params_default import ADHOC
from tests.security.test_call_params_default import METADATA_HOSTS
from tests.security.test_call_params_default import cfg  # fixture
from tests.security.test_call_params_surface import HOST
from tests.security.test_call_params_surface import LITERAL
from tests.security.test_call_params_surface import SDB_REF
from tests.security.test_call_params_surface import SDB_VALUE
from tests.security.test_call_params_surface import SECRETS
from tests.security.test_call_params_surface import connects  # fixture
from tests.security.test_call_params_surface import fake  # fixture

INSERT = "insert into t values (1)"
ERR = (CommandExecutionError, SaltInvocationError)


def write(port=1, **extra):
    return mod.execute(sql=INSERT, **ADHOC, host=HOST, port=port, **extra)


def psql_write(port=1, **extra):
    return mod.psql_query(
        INSERT, user="u", host=HOST, port=port, maintenance_db="d", write=True, **extra
    )


WRITES = {"execute": write, "psql_query": psql_write}


# -- by default an ad-hoc write reaches the connection ----------------------------------------------------------------


@pytest.mark.parametrize("call", sorted(WRITES))
def test_by_default_an_ad_hoc_write_is_not_refused_by_the_extension(cfg, connects, fake, call):
    with pytest.raises(ERR) as exc:
        WRITES[call](password=LITERAL)
    assert not str(exc.value).startswith("[E_EXECUTE_DISABLED]"), str(exc.value)
    assert connects  # it went to the (fake) server


@pytest.mark.parametrize("call", sorted(WRITES))
def test_the_password_goes_only_to_the_server_the_caller_chose_and_nowhere_else(
    cfg, fake, caplog, call
):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ERR) as exc:
        WRITES[call](port=fake.port, password=LITERAL)
    assert LITERAL.encode() in fake.seen()  # the chosen server got it ...
    assert all(s not in str(exc.value) for s in SECRETS)  # ... the caller and the logs did not
    assert LITERAL not in caplog.text and INSERT not in caplog.text


def test_the_profiles_password_does_not_come_into_an_ad_hoc_write(cfg, fake):
    with pytest.raises(ERR):
        mod.execute(sql=INSERT, **ADHOC, host=HOST, port=fake.port)  # no password in the call
    assert b"BASE-PROFILE-PW" not in fake.seen()


# -- what a caller can never send -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_adhoc_execute": True},
        {"allow_adhoc_execute": False},
        {"adhoc_execute_verbs": ["DROP"]},
        {"allow_execute": True},
        {"execute_verbs": ["DROP", "CREATE"]},
        {"error_detail": True},
        {"row_limit": 10**6},
        {"max_bytes": None, "_policy": {"allow_adhoc_execute": True}},
        {"connection": {"allow_adhoc_execute": True}},
        {"connection": {"allow_execute": True}},
        {"connection": {"execute_verbs": ["DROP"]}},
        {"profiles": {"x": {"allow_execute": True}}},
    ],
)
@pytest.mark.parametrize("flag", [None, True, False])
@pytest.mark.parametrize("call", sorted(WRITES))
def test_a_caller_cannot_send_the_switches_of_an_ad_hoc_write(cfg, connects, call, flag, extra):
    if flag is not None:
        cfg["_policy"] = {"allow_adhoc_execute": flag}
    extra = {k: v for k, v in extra.items() if v is not None}
    with pytest.raises(SaltInvocationError) as exc:
        WRITES[call](**extra)
    assert str(exc.value).startswith("[E_OPTION_UNKNOWN]"), str(exc.value)
    assert connects == []


@pytest.mark.parametrize("extra", [{"read_only": False}, {"read_only": "no"}])
@pytest.mark.parametrize("call", sorted(WRITES))
def test_read_only_cannot_be_turned_off_from_a_call_for_an_ad_hoc_write(cfg, connects, call, extra):
    with pytest.raises(SaltInvocationError) as exc:
        WRITES[call](**extra)
    assert str(exc.value).startswith(("[E_POLICY]", "[E_OPTION_UNKNOWN]")), str(exc.value)
    assert connects == []


# -- the host policy applies to a write as to everything else ----------------------------------------------------------


@pytest.mark.parametrize("host", METADATA_HOSTS)
@pytest.mark.parametrize("call", sorted(WRITES))
def test_a_metadata_or_link_local_target_never_gets_an_ad_hoc_write(cfg, connects, call, host):
    def go():
        if call == "execute":
            return mod.execute(sql=INSERT, **ADHOC, host=host, port=1, password=SDB_REF)
        return mod.psql_query(
            INSERT, user="u", host=host, port=1, maintenance_db="d", password=SDB_REF, write=True
        )

    with pytest.raises(ERR) as exc:
        go()
    assert str(exc.value).startswith("[E_POLICY]"), str(exc.value)
    assert connects == []
    assert all(s not in str(exc.value) for s in SECRETS)


def test_allowed_hosts_ports_and_dialects_bound_an_ad_hoc_write(cfg, connects):
    cfg["_policy"] = {
        "allowed_hosts": ["10.9.0.0/16"],
        "allowed_ports": [5432],
        "allowed_dialects": ["postgresql"],
    }
    for kw in (
        dict(host=HOST),
        dict(host="10.9.1.1", port=1),
        dict(host="10.9.1.1", dialect="mysql"),
    ):
        with pytest.raises(SaltInvocationError) as exc:
            mod.execute(sql=INSERT, **{**ADHOC, "port": 5432, **kw})
        assert str(exc.value).startswith("[E_POLICY]")
    assert connects == []


def test_the_caps_of_an_ad_hoc_write_are_the_hard_defaults(cfg, monkeypatch):
    seen = {}

    def spy(profile, password, timeout_ms=None, read_only=None):
        seen.update(profile=profile, timeout_ms=timeout_ms, read_only=read_only)
        raise RuntimeError("stop here")

    monkeypatch.setattr(mod.engine, "connection", spy)
    with pytest.raises(CommandExecutionError):
        mod.execute(sql=INSERT, **ADHOC, host=HOST, timeout_ms=10**9)
    assert seen["timeout_ms"] == 30000 and seen["profile"]["error_detail"] is False
    assert seen["profile"]["row_limit"] == 500 and seen["profile"]["execute_verbs"] == ["*"]
    assert (
        seen["profile"]["read_only"] is True
    )  # the profile of an ad-hoc connection is read-only for the read functions


# -- the opt-out and its interactions (mirrors of the pre-D17 contract) ------------------------------------------------


@pytest.mark.parametrize("call", sorted(WRITES))
def test_allow_adhoc_execute_false_refuses_before_any_socket_or_secret(cfg, connects, call):
    cfg["_policy"] = {"allow_adhoc_execute": False}
    with pytest.raises(CommandExecutionError) as exc:
        WRITES[call](password=SDB_REF)
    assert str(exc.value).startswith("[E_EXECUTE_DISABLED]") and connects == []
    assert all(s not in str(exc.value) for s in SECRETS)


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("call", sorted(WRITES))
def test_allow_adhoc_connection_false_means_no_ad_hoc_write_whatever_the_execute_flag(
    cfg, connects, call, flag
):
    cfg["_policy"] = {"allow_adhoc_connection": False, "allow_adhoc_execute": flag}
    with pytest.raises(SaltInvocationError) as exc:
        WRITES[call]()
    assert str(exc.value).startswith("[E_POLICY]") and "allow_adhoc_connection" in str(exc.value)
    assert connects == []


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("call", sorted(WRITES))
def test_allow_call_params_empty_means_nothing_ad_hoc_can_happen(cfg, connects, call, flag):
    cfg["_policy"] = {"allow_call_params": [], "allow_adhoc_execute": flag}
    with pytest.raises(SaltInvocationError) as exc:
        WRITES[call]()
    assert str(exc.value).startswith(("[E_OPTION_UNKNOWN]", "[E_POLICY]"))
    assert connects == []


@pytest.mark.parametrize("value", ["false", "no", 0, 1, [], {}, "True"])
@pytest.mark.parametrize("call", sorted(WRITES))
def test_a_malformed_flag_is_e_policy_before_any_socket(cfg, connects, call, value):
    cfg["_policy"] = {"allow_adhoc_execute": value}
    with pytest.raises(SaltInvocationError) as exc:
        WRITES[call]()
    assert str(exc.value).startswith("[E_POLICY]") and "allow_adhoc_execute" in str(exc.value)
    assert connects == []


@pytest.mark.parametrize("flag", [True, False])
def test_a_named_profile_is_not_affected_by_the_flag(cfg, connects, flag):
    cfg["_policy"] = {"allow_adhoc_execute": flag}
    with pytest.raises(ERR) as exc:
        mod.execute(profile="base", sql=INSERT, host=HOST)  # base has no allow_execute
    assert str(exc.value).startswith("[E_EXECUTE_DISABLED]") and connects == []
    with pytest.raises(ERR) as exc:
        mod.execute(profile="writer", sql=INSERT, host=HOST)
    assert not str(exc.value).startswith("[E_EXECUTE_DISABLED]") and connects


# -- live servers -----------------------------------------------------------------------------------------------------

T = "scratch.t17_adhoc"
DRIVERS = [("postgresql", None), ("postgresql", "pg8000"), ("postgresql", "psycopg")]


@pytest.fixture(scope="module")
def tables(server):
    server.admin(f"DROP TABLE IF EXISTS {T}")
    server.admin(f"CREATE TABLE {T} (id integer PRIMARY KEY, v integer)")
    if server.key == "postgresql":
        server.admin(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {T} TO rw")
    try:
        yield server
    finally:
        server.admin(f"DROP TABLE IF EXISTS {T}")


@pytest.fixture
def live(tables):
    """``conn(**extra)``: the keywords of an ad-hoc connection as ``rw`` (a literal password), module with no policy."""
    srv = tables
    srv.admin(f"DELETE FROM {T}")
    srv.admin(f"INSERT INTO {T} VALUES (1, 1)")

    def conn(**extra):
        base = dict(
            dialect=srv.dialect,
            host=harness.HOST,
            port=srv.cfg["port"],
            database=srv.cfg["database"],
            username="rw",
            password="rw_pw",
        )
        base.update(extra)
        return base

    with harness.injected({}) as module:
        conn.module, conn.srv = module, srv
        yield conn


def drivers(server):
    return [None, "pg8000", "psycopg"] if server.key == "postgresql" else [None]


def rows(srv):
    return srv.admin(f"SELECT id, v FROM {T} ORDER BY id")


def test_live_an_ad_hoc_write_commits_and_is_visible_to_a_second_connection(live):
    out = live.module.execute(sql=f"INSERT INTO {T} VALUES (2, 2)", **live())
    assert (out["rows_affected"], out["committed"], out["profile"]) == (1, True, "(adhoc)")
    assert rows(live.srv) == [(1, 1), (2, 2)]
    assert "rw_pw" not in repr(out)


def test_live_update_and_delete_commit(live):
    live.module.execute(sql=f"UPDATE {T} SET v = 7 WHERE id = 1", **live())
    assert rows(live.srv) == [(1, 7)]
    live.module.execute(sql=f"DELETE FROM {T} WHERE id = 1", **live())
    assert rows(live.srv) == []


def test_live_dry_run_rolls_back(live):
    out = live.module.execute(sql=f"INSERT INTO {T} VALUES (3, 3)", dry_run=True, **live())
    assert (out["rows_affected"], out["committed"], out["dry_run"]) == (1, False, True)
    assert rows(live.srv) == [(1, 1)]


def test_live_wrong_password_is_the_real_error_without_the_password(live, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CommandExecutionError) as exc:
        live.module.execute(sql=f"INSERT INTO {T} VALUES (4, 4)", **live(password="WRONG-pw-17abc"))
    assert str(exc.value).startswith("[E_AUTH_FAILED]")
    assert "WRONG-pw-17abc" not in str(exc.value) and "WRONG-pw-17abc" not in caplog.text
    assert rows(live.srv) == [(1, 1)]


def admin_conn(srv, **extra):
    base = {
        "dialect": srv.dialect,
        "host": harness.HOST,
        "port": srv.cfg["port"],
        "database": srv.cfg["database"],
        "username": srv.cfg["admin"],
        "password": harness.ADMIN_PW,
    }
    base.update(extra)
    return base


def test_live_the_default_allows_any_single_statement_ddl_included(tables):
    """D17 (any statement by default): CREATE, ALTER, TRUNCATE and DROP run as one statement each, with the admin account."""
    srv = tables
    name = "scratch.t17_ddl"
    srv.admin(f"DROP TABLE IF EXISTS {name}")
    try:
        with harness.injected({}) as module:
            for sql in (
                f"CREATE TABLE {name} (a integer)",
                f"ALTER TABLE {name} ADD COLUMN b integer",
                f"INSERT INTO {name} (a, b) VALUES (1, 2)",
                f"TRUNCATE TABLE {name}",
            ):
                out = module.execute(sql=sql, **admin_conn(srv))
                assert out["committed"] is True and out["profile"] == "(adhoc)", sql
            assert srv.admin(f"SELECT count(*) FROM {name}") == [(0,)]
            module.execute(sql=f"DROP TABLE {name}", **admin_conn(srv))
        assert not srv.admin(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'scratch' AND table_name = 't17_ddl'"
        )
    finally:
        srv.admin(f"DROP TABLE IF EXISTS {name}")


@pytest.mark.parametrize("verbs", [["INSERT", "UPDATE", "DELETE"], ["insert"]])
def test_live_a_narrowed_verb_list_still_refuses_ddl_and_nothing_runs(live, verbs):
    with harness.injected({}, policy={"adhoc_execute_verbs": verbs}) as module:
        for sql in (
            f"DROP TABLE {T}",
            f"TRUNCATE TABLE {T}",
            f"ALTER TABLE {T} ADD COLUMN z integer",
        ):
            with pytest.raises(SaltInvocationError) as exc:
                module.execute(sql=sql, **live())
            assert str(exc.value).startswith("[E_POLICY]") and "not in adhoc_execute_verbs" in str(
                exc.value
            )
        out = module.execute(sql=f"INSERT INTO {T} VALUES (11, 11)", **live())
        assert out["committed"] is True
    assert rows(live.srv) == [(1, 1), (11, 11)]


@pytest.mark.parametrize("variant", ["plain", "comment-trick", "upper-semicolon-end"])
def test_live_multi_statement_is_refused_on_every_dialect_and_pg_driver(live, variant):
    sqls = {
        "plain": f"INSERT INTO {T} VALUES (5, 5); INSERT INTO {T} VALUES (6, 6)",
        "comment-trick": f"INSERT INTO {T} VALUES (5, 5); -- x\nDELETE FROM {T}",
        "upper-semicolon-end": f"INSERT INTO {T} VALUES (5, 5); COMMIT",
    }
    for driver in drivers(live.srv):
        extra = {"driver": driver} if driver else {}
        with pytest.raises(ERR) as exc:
            live.module.execute(sql=sqls[variant], **live(**extra))
        assert str(exc.value).startswith("[E_"), (driver, str(exc.value))
        assert rows(live.srv) == [(1, 1)], (driver, variant, "a second statement ran")


def test_live_psql_query_write_is_one_statement_too(live):
    if live.srv.key != "postgresql":
        pytest.skip("psql_query is PostgreSQL only")
    for driver in drivers(live.srv):
        extra = {"driver": driver} if driver else {}
        with pytest.raises(ERR):
            live.module.psql_query(
                f"INSERT INTO {T} VALUES (5, 5); DELETE FROM {T}",
                user="rw",
                host=harness.HOST,
                port=live.srv.cfg["port"],
                maintenance_db=live.srv.cfg["database"],
                password="rw_pw",
                write=True,
                **extra,
            )
        assert rows(live.srv) == [(1, 1)]


@pytest.mark.parametrize(
    "function", ["query", "check_row_count", "check_query_result", "psql_query"]
)
def test_live_a_read_function_with_an_insert_writes_nothing(live, function):
    sql = f"INSERT INTO {T} VALUES (8, 8)"
    c = live()
    try:
        if function == "query":
            live.module.query(sql=sql, **c)
        elif function == "check_row_count":
            live.module.check_row_count(sql=sql, operator=">=", expected=0, **c)
        elif function == "check_query_result":
            live.module.check_query_result(sql=sql, operator="==", expected=1, **c)
        else:
            if live.srv.key != "postgresql":
                pytest.skip("psql_query is PostgreSQL only")
            live.module.psql_query(
                sql,
                user="rw",
                host=harness.HOST,
                port=live.srv.cfg["port"],
                maintenance_db=live.srv.cfg["database"],
                password="rw_pw",
            )
    except Exception:  # pylint: disable=broad-except
        pass  # refused or an error is fine; what matters is the state
    assert rows(live.srv) == [(1, 1)], f"{function} persisted an INSERT"


def test_live_opt_out_refuses_and_nothing_connects(tables):
    srv = tables
    with harness.injected({}, policy={"allow_adhoc_execute": False}) as module:
        with pytest.raises(CommandExecutionError) as exc:
            module.execute(
                sql=f"INSERT INTO {T} VALUES (9, 9)",
                dialect=srv.dialect,
                host=harness.HOST,
                port=srv.cfg["port"],
                database=srv.cfg["database"],
                username="rw",
                password="rw_pw",
            )
    assert str(exc.value).startswith("[E_EXECUTE_DISABLED]")
    assert (9, 9) not in rows(srv)
