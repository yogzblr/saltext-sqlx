"""D17 at module level: ``execute`` / ``psql_query write=True`` on a connection with no profile, ON by default.

Same fakes as ``test_sqlx_compat_default`` (the database layer is injected: what is asserted is what the module asks it
to do). The refusal tests of the old contract are the ``allow_adhoc_execute: false`` mirrors at the end and in the
existing files. Real servers: ``tests/functional/test_adhoc_execute_functional.py``.
"""

# pylint: disable=unused-argument,redefined-outer-name,unused-import
import logging

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.unit.modules.test_sqlx_compat_default import CONN
from tests.unit.modules.test_sqlx_compat_default import LITERAL
from tests.unit.modules.test_sqlx_compat_default import REF
from tests.unit.modules.test_sqlx_compat_default import audit
from tests.unit.modules.test_sqlx_compat_default import fake  # fixture
from tests.unit.modules.test_sqlx_module import db_error

DIALECTS = ["postgresql", "mysql", "mariadb", "oracle", "mssql"]
INSERT = "insert into t values (1)"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_execute_with_no_profile_commits_on_every_dialect(fake, dialect):
    out = mod.execute(sql=INSERT, dialect=dialect, password=LITERAL, **CONN)
    assert out["profile"] == "(adhoc)" and out["committed"] is True and out["rows_affected"] == 3
    assert LITERAL not in repr(out)
    (got,) = fake.connections
    assert got["profile"]["dialect"] == ("mysql" if dialect == "mariadb" else dialect)
    assert got["password"] == LITERAL and fake.sdb == []
    assert got["read_only"] is False  # the session read-only guard is not applied to execute
    assert fake.executes == [
        {
            "sql": INSERT,
            "params": None,
            "dry_run": False,
            "execute_verbs": ["INSERT", "UPDATE", "DELETE"],
        }
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_dry_run_is_passed_through_so_it_rolls_back(fake, dialect):
    out = mod.execute(sql=INSERT, dialect=dialect, password=LITERAL, dry_run=True, **CONN)
    assert out["dry_run"] is True and out["committed"] is False
    assert fake.executes[0]["dry_run"] is True


def test_dry_run_none_means_not_given_and_commits_as_for_a_profile(fake):
    assert mod.execute(sql=INSERT, dialect="postgresql", dry_run=None, **CONN)["committed"] is True


def test_the_connection_object_and_an_sdb_reference_work(fake):
    out = mod.execute(sql=INSERT, connection=dict(CONN, dialect="mysql", password=REF))
    assert out["committed"] is True and fake.connections[0]["password"] == "call-resolved-pw"
    assert fake.sdb == [REF]


def test_psql_query_write_with_no_profile_commits_and_returns_rows_affected(fake):
    out = mod.psql_query(
        INSERT, user="rw", host="10.1.1.1", maintenance_db="d", password=LITERAL, write=True
    )
    assert out == [{"rows_affected": "3"}]
    got = fake.connections[0]
    assert got["profile"]["dialect"] == "postgresql" and got["profile"]["name"] == "(adhoc)"
    assert fake.executes[0]["dry_run"] is False and got["password"] == LITERAL


def test_psql_query_write_dry_run_rolls_back(fake):
    mod.psql_query(INSERT, user="rw", host="10.1.1.1", maintenance_db="d", write=True, dry_run=True)
    assert fake.executes[0]["dry_run"] is True


# -- caps and timeouts ------------------------------------------------------------------------------------------------


def test_a_caller_cannot_raise_the_timeout_of_an_ad_hoc_write(fake):
    mod.execute(sql=INSERT, dialect="postgresql", timeout_ms=10**9, **CONN)
    assert fake.connections[0]["timeout_ms"] == 30000
    mod.execute(sql=INSERT, dialect="postgresql", timeout_ms=500, **CONN)
    assert fake.connections[1]["timeout_ms"] == 500


@pytest.mark.parametrize(
    "extra",
    [
        {"statement_timeout_ms": 5},
        {"connect_timeout_s": 5},
        {"read_only": False},
        {"row_limit": 5},
        {"allow_execute": True},
        {"allow_adhoc_execute": True},
        {"execute_verbs": ["DROP"]},
        {"adhoc_execute_verbs": ["DROP"]},
        {"error_detail": True},
        {"connection": {"allow_adhoc_execute": True}},
        {"connection": {"execute_verbs": ["DROP"]}},
        {"_policy": {"allow_adhoc_execute": True}},
    ],
)
def test_a_caller_cannot_widen_anything(fake, extra):
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        mod.execute(sql=INSERT, dialect="postgresql", **CONN, **extra)
    code = str(exc.value).split("]", maxsplit=1)[0]
    assert code in ("[E_OPTION_UNKNOWN", "[E_POLICY"), str(exc.value)
    assert fake.connections == [] and fake.executes == []


# -- verbs ------------------------------------------------------------------------------------------------------------


def test_the_ad_hoc_verbs_default_to_the_profile_default(fake):
    mod.execute(sql="update t set a = 1", dialect="postgresql", **CONN)
    assert fake.executes[0]["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]


def test_the_operator_widens_the_verbs_and_only_the_operator(fake):
    fake.cfg["_policy"] = {"adhoc_execute_verbs": ["create", "drop", "insert"]}
    mod.execute(sql="create table t (a int)", dialect="postgresql", **CONN)
    assert fake.executes[0]["execute_verbs"] == ["CREATE", "DROP", "INSERT"]


def test_a_writing_profile_keeps_its_own_verbs_whatever_the_ad_hoc_list_says(fake):
    fake.cfg["_policy"] = {"adhoc_execute_verbs": ["CREATE"]}
    mod.execute(profile="pg_rw", sql="update t set a = 1", host="10.2.2.2")
    assert fake.executes[0]["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]


# -- the read functions stay read-only ---------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_ad_hoc_query_with_an_insert_goes_to_the_rolled_back_path_only(fake, dialect):
    mod.query(sql=INSERT, dialect=dialect, password=LITERAL, **CONN)
    assert fake.executes == [] and len(fake.queries) == 1
    assert (
        fake.connections[0]["read_only"] is None
    )  # default: the read-only guard of the profile (True) applies
    assert fake.connections[0]["profile"]["read_only"] is True


def test_the_other_read_functions_never_reach_execute(fake):
    mod.check_row_count(sql=INSERT, dialect="postgresql", operator=">=", expected=0, **CONN)
    mod.check_query_result(sql=INSERT, dialect="postgresql", operator="==", expected=1, **CONN)
    mod.ping(dialect="postgresql", **CONN)
    assert fake.executes == []
    assert all(c["profile"]["read_only"] is True for c in fake.connections)


def test_psql_query_without_write_stays_on_the_query_path(fake):
    mod.psql_query(INSERT, user="rw", host="10.1.1.1", maintenance_db="d")
    assert fake.executes == [] and len(fake.queries) == 1


# -- targets ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["169.254.169.254", "metadata.google.internal.meta", "fe80::1"])
def test_metadata_targets_are_refused_for_a_write_before_a_secret_or_a_socket(fake, host):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.execute(sql=INSERT, dialect="postgresql", password=REF, **{**CONN, "host": host})
    assert fake.connections == [] and fake.executes == [] and fake.sdb == []


def test_allowed_hosts_and_ports_still_bound_an_ad_hoc_write(fake):
    fake.cfg["_policy"] = {"allowed_hosts": ["10.0.0.0/8"], "allowed_ports": [5432]}
    mod.execute(sql=INSERT, dialect="postgresql", **CONN)
    for bad in ({"host": "203.0.113.9"}, {"port": 5999}):
        with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
            mod.execute(sql=INSERT, dialect="postgresql", **{**CONN, **bad})
    assert len(fake.executes) == 1


def test_allowed_dialects_still_bound_an_ad_hoc_write(fake):
    fake.cfg["_policy"] = {"allowed_dialects": ["postgresql"]}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.execute(sql=INSERT, dialect="mysql", **CONN)
    assert fake.executes == []


# -- errors, logs, secrets --------------------------------------------------------------------------------------------


def test_the_audit_line_of_an_ad_hoc_write_has_no_password_or_sql(fake, caplog):
    caplog.set_level(logging.DEBUG)
    mod.execute(sql=INSERT, dialect="postgresql", password=LITERAL, **CONN)
    (line,) = audit(caplog)
    assert "function=execute" in line and "profile=(adhoc)" in line and "outcome=OK" in line
    assert LITERAL not in caplog.text and INSERT not in caplog.text


def test_a_failing_ad_hoc_write_returns_the_real_error_without_the_password(fake, caplog):
    fake.execute_error = db_error("23505", "duplicate key value violates unique constraint")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute(sql=INSERT, dialect="postgresql", password=LITERAL, **CONN)
    assert str(exc.value).startswith("[E_CONSTRAINT] postgresql 23505: duplicate key value")
    assert LITERAL not in str(exc.value) and LITERAL not in caplog.text


# -- interactions and the opt-out (mirrors of the pre-D17 contract) -----------------------------------------------------


def test_allow_adhoc_execute_false_restores_the_old_refusal_exactly(fake):
    fake.cfg["_policy"] = {"allow_adhoc_execute": False}
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute(sql=INSERT, dialect="postgresql", password=LITERAL, **CONN)
    assert (
        str(exc.value) == "[E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
    )
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.psql_query(INSERT, user="u", host="10.1.1.1", maintenance_db="d", write=True)
    assert fake.connections == [] and fake.executes == [] and fake.sdb == []
    # reads on the same connection still work
    assert mod.query(sql="select 1", dialect="postgresql", **CONN)["profile"] == "(adhoc)"


def test_the_flag_does_not_touch_named_profiles(fake):
    for flag in (True, False):
        fake.cfg["_policy"] = {"allow_adhoc_execute": flag}
        assert mod.execute(profile="pg_rw", sql=INSERT, host="10.2.2.2")["committed"] is True
        with pytest.raises(
            CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\].*does not set allow_execute"
        ):
            mod.execute(profile="pg_ro", sql=INSERT, host="10.2.2.2")


@pytest.mark.parametrize("flag", [True, False])
def test_allow_adhoc_connection_false_means_no_ad_hoc_write_at_all(fake, flag):
    fake.cfg["_policy"] = {"allow_adhoc_connection": False, "allow_adhoc_execute": flag}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_adhoc_connection"):
        mod.execute(sql=INSERT, dialect="postgresql", **CONN)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_adhoc_connection"):
        mod.psql_query(INSERT, user="u", host="10.1.1.1", maintenance_db="d", write=True)
    assert fake.connections == []


@pytest.mark.parametrize("flag", [True, False])
def test_strict_allow_call_params_means_nothing_ad_hoc_can_happen(fake, flag):
    fake.cfg["_policy"] = {"allow_call_params": [], "allow_adhoc_execute": flag}
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.execute(sql=INSERT, dialect="postgresql", **CONN)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.psql_query(INSERT, user="u", host="10.1.1.1", maintenance_db="d", write=True)
    assert fake.connections == [] and fake.executes == []


def test_a_malformed_flag_is_e_policy_before_anything_connects(fake):
    fake.cfg["_policy"] = {"allow_adhoc_execute": "false"}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_adhoc_execute"):
        mod.execute(sql=INSERT, dialect="postgresql", **CONN)
    assert fake.connections == []


def test_with_a_default_profile_a_connection_in_the_call_is_an_overlay_and_the_profiles_rule_applies(
    fake,
):
    fake.cfg["default_profile"] = "pg_ro"
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\].*'pg_ro'"):
        mod.execute(sql=INSERT, dialect="postgresql", **CONN)
