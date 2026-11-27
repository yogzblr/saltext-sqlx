"""D17: ``sqlx:_policy:allow_adhoc_execute`` (absent = true) and ``adhoc_execute_verbs`` in ``lib/overrides.py``.

Policy parsing, the profile an ad-hoc call gets, and the interactions with ``allow_adhoc_connection: false`` and
``allow_call_params: []``. Nothing opens a socket (DNS is the fake resolver of ``test_overrides``).
"""

# pylint: disable=unused-argument
import copy

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import overrides
from saltext.sqlx.lib import profiles
from tests.unit.lib.test_overrides import BASE_PROFILES
from tests.unit.lib.test_overrides import code
from tests.unit.lib.test_overrides import go

ADHOC = {"dialect": "postgresql", "host": "10.1.1.1", "database": "d", "username": "u"}
WORDS = ("INSERT", "UPDATE", "DELETE")


def cfg(**policy):
    config = {"profiles": copy.deepcopy(BASE_PROFILES)}
    if policy:
        config["_policy"] = policy
    return config


# -- the default ------------------------------------------------------------------------------------------------------


def test_the_default_policy_allows_adhoc_execute_with_the_profile_default_verbs():
    d = overrides.DEFAULT_POLICY
    assert d.adhoc_execute is True
    assert tuple(d.adhoc_verbs) == profiles.DEFAULT_EXECUTE_VERBS == WORDS


@pytest.mark.parametrize(
    "config", [{}, {"_policy": None}, {"_policy": {}}, {"_policy": {"allowed_ports": [5432]}}]
)
def test_absent_allow_adhoc_execute_is_true(config):
    policy = overrides.parse_policy(config)
    assert policy.adhoc_execute is True and tuple(policy.adhoc_verbs) == WORDS


@pytest.mark.parametrize("value", [True, False])
def test_the_flag_is_read(value):
    assert overrides.parse_policy(cfg(allow_adhoc_execute=value)).adhoc_execute is value


@pytest.mark.parametrize("value", ["false", "no", 0, 1, None, [], {}, "True"])
def test_a_malformed_flag_is_e_policy_and_names_the_key(value):
    with pytest.raises(SaltInvocationError) as exc:
        overrides.parse_policy(cfg(allow_adhoc_execute=value))
    assert code(exc) == "[E_POLICY]" and "allow_adhoc_execute" in str(exc.value)


def test_a_malformed_flag_also_fails_closed_with_an_explicit_list():
    with pytest.raises(SaltInvocationError) as exc:
        overrides.parse_policy(
            cfg(allow_call_params=["host"], allowed_hosts=["a.b"], allow_adhoc_execute="x")
        )
    assert code(exc) == "[E_POLICY]"


def test_the_flag_defaults_to_true_with_an_explicit_list_too():
    """Owner decision D17: ABSENT = true everywhere. Whether ad-hoc is reachable at all is allow_adhoc_connection's job."""
    policy = overrides.parse_policy(
        cfg(allow_call_params=["dialect", "host", "database", "username"], allowed_hosts=["10.*"])
    )
    assert (
        policy.adhoc_execute is True and policy.adhoc is False
    )  # no ad-hoc connection, so nothing to execute on


def test_strict_is_strict_and_reads_nothing_else():
    policy = overrides.parse_policy(
        {"_policy": {"allow_call_params": [], "allow_adhoc_execute": "garbage"}}
    )
    assert policy == overrides.STRICT_POLICY
    assert policy.adhoc_execute is False and not policy.allow


# -- adhoc_execute_verbs ----------------------------------------------------------------------------------------------


def test_adhoc_execute_verbs_are_upper_cased():
    policy = overrides.parse_policy(cfg(adhoc_execute_verbs=["insert", "Create", "DROP"]))
    assert list(policy.adhoc_verbs) == ["INSERT", "CREATE", "DROP"]


def test_an_empty_verb_list_is_accepted_and_means_no_ad_hoc_write_passes():
    assert not overrides.parse_policy(cfg(adhoc_execute_verbs=[])).adhoc_verbs


@pytest.mark.parametrize(
    "value", ["INSERT", ["INSERT UPDATE"], ["drop;"], [1], [""], None, {"a": 1}, ["*"]]
)
def test_a_malformed_verb_list_is_e_policy(value):
    with pytest.raises(SaltInvocationError) as exc:
        overrides.parse_policy(cfg(adhoc_execute_verbs=value))
    assert code(exc) == "[E_POLICY]" and "adhoc_execute_verbs" in str(exc.value)


def test_the_verbs_are_validated_even_when_the_flag_is_off():
    with pytest.raises(SaltInvocationError) as exc:
        overrides.parse_policy(cfg(allow_adhoc_execute=False, adhoc_execute_verbs="x"))
    assert code(exc) == "[E_POLICY]"


# -- the profile an ad-hoc call gets ----------------------------------------------------------------------------------


def test_an_adhoc_profile_may_execute_by_default_with_the_default_verbs_and_stays_read_only_for_reads():
    prof = go(cfg(), dict(ADHOC, password="pw-literal-1"))
    assert prof["adhoc"] is True and prof["allow_execute"] is True
    assert prof["execute_verbs"] == list(WORDS)
    assert (
        prof["read_only"] is True
    )  # the read functions keep their guard; ``execute`` skips it itself
    assert (
        prof["row_limit"] == 500
        and prof["statement_timeout_ms"] == 30000
        and prof["error_detail"] is False
    )


def test_the_flag_off_gives_exactly_the_old_profile():
    prof = go(cfg(allow_adhoc_execute=False), dict(ADHOC))
    assert prof["allow_execute"] is False and prof["adhoc"] is True


def test_operator_verbs_reach_the_adhoc_profile():
    prof = go(cfg(adhoc_execute_verbs=["insert", "create"]), dict(ADHOC))
    assert prof["execute_verbs"] == ["INSERT", "CREATE"]


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb", "oracle", "mssql"])
def test_every_dialect_gets_it(dialect):
    prof = go(cfg(), dict(ADHOC, dialect=dialect))
    assert prof["allow_execute"] is True and prof["execute_verbs"] == list(WORDS)


def test_a_profile_is_never_changed_by_the_flag():
    """The flag is about ad-hoc connections only: a named profile keeps its own allow_execute and verbs."""
    for flag in (True, False):
        config = cfg(allow_adhoc_execute=flag)
        assert go(config, {"profile": "pg"})["allow_execute"] is False
        assert go(config, {"profile": "pg", "host": "10.1.1.1"})["allow_execute"] is False
        rw = go(config, {"profile": "pg_rw", "host": "10.1.1.1"})
        assert (
            rw["allow_execute"] is True
            and rw["execute_verbs"] == list(WORDS)
            and rw["adhoc"] is False
        )


def test_a_default_profile_means_the_call_is_an_overlay_not_ad_hoc():
    config = cfg(adhoc_execute_verbs=["CREATE"])
    config["default_profile"] = "pg"
    prof = go(config, dict(ADHOC))
    assert prof["adhoc"] is False and prof["name"] == "pg"
    assert prof["allow_execute"] is False and prof["execute_verbs"] == list(WORDS)


# -- a call can never set it ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_adhoc_execute": True},
        {"adhoc_execute_verbs": ["DROP"]},
        {"allow_execute": True},
        {"execute_verbs": ["DROP"]},
        {"connection": {"allow_adhoc_execute": True}},
        {"connection": {"allow_execute": True}},
        {"connection": {"execute_verbs": ["DROP"]}},
        {"_policy": {"allow_adhoc_execute": True}},
    ],
)
@pytest.mark.parametrize("flag", [True, False])
def test_a_call_cannot_send_the_flag_or_the_verbs(extra, flag):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow_adhoc_execute=flag), dict(ADHOC, **extra), function="execute")
    assert code(exc) == "[E_OPTION_UNKNOWN]"


# -- interactions -----------------------------------------------------------------------------------------------------


def test_allow_adhoc_connection_false_means_no_ad_hoc_profile_at_all_whatever_the_execute_flag():
    for flag in (True, False):
        with pytest.raises(SaltInvocationError) as exc:
            go(
                cfg(allow_adhoc_connection=False, allow_adhoc_execute=flag),
                dict(ADHOC),
                function="execute",
            )
        assert code(exc) == "[E_POLICY]" and "allow_adhoc_connection" in str(exc.value)


def test_strict_means_no_connection_fields_so_nothing_ad_hoc_can_happen():
    for flag in (True, False):
        config = cfg(allow_call_params=[], allow_adhoc_execute=flag)
        with pytest.raises(SaltInvocationError) as exc:
            go(config, dict(ADHOC), function="execute")
        assert code(exc) == "[E_OPTION_UNKNOWN]"


def test_an_explicit_list_with_adhoc_connection_on_gets_adhoc_execute_unless_the_flag_is_false():
    base = {
        "allow_call_params": ["dialect", "host", "database", "username"],
        "allowed_hosts": ["10.0.0.0/8"],
        "allow_adhoc_connection": True,
    }
    assert go(cfg(**base), dict(ADHOC), function="execute")["allow_execute"] is True
    assert (
        go(cfg(allow_adhoc_execute=False, **base), dict(ADHOC), function="execute")["allow_execute"]
        is False
    )


def test_the_host_policy_still_applies_to_an_ad_hoc_write():
    config = cfg(allowed_hosts=["10.0.0.0/8"])
    with pytest.raises(SaltInvocationError) as exc:
        go(config, dict(ADHOC, host="203.0.113.9"), function="execute")
    assert code(exc) == "[E_POLICY]"
    assert go(config, dict(ADHOC), function="execute")["allow_execute"] is True


@pytest.mark.parametrize("host", ["169.254.169.254", "fe80::1", "100.100.100.200", "fd00:ec2::254"])
def test_metadata_targets_are_refused_for_an_ad_hoc_write(host):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), dict(ADHOC, host=host), function="execute")
    assert code(exc) == "[E_POLICY]"


def test_ports_and_dialects_policy_still_apply_to_an_ad_hoc_write():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allowed_ports=[5432]), dict(ADHOC, port=3306), function="execute")
    assert code(exc) == "[E_POLICY]"
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allowed_dialects=["mysql"]), dict(ADHOC), function="execute")
    assert code(exc) == "[E_POLICY]"
