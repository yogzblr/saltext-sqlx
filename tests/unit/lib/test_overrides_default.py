"""D16: the compat default policy of call-time connection parameters, and the strict config that restores the old contract.

With NO ``sqlx:_policy:allow_call_params`` a caller may send ``dialect``, ``driver``, ``host``, ``port``, ``database``,
``username`` and ``password`` (a literal included) with or without a profile, on every dialect. The operator tightens it
with ``allow_call_params: []`` (strict, the behaviour before D16), a shorter list or the ``allowed_*`` lists. Nothing here
opens a socket: DNS is a fake resolver, except the numeric host forms that need the real ``getaddrinfo`` (offline-safe).
"""

# pylint: disable=unused-argument,protected-access
import copy
import socket

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import options
from saltext.sqlx.lib import overrides
from tests.unit.lib.test_overrides import BASE_PROFILES
from tests.unit.lib.test_overrides import code
from tests.unit.lib.test_overrides import go

LITERAL = "Lit3ral-PW-77qz"
SDB_REF = "sdb://vault/SECRETREF_pg"
DEFAULT_FIELDS = {"dialect", "driver", "host", "port", "database", "username", "password"}
NOT_DEFAULT = {
    "ssl_mode": "verify-ca",
    "ssl_certs_dir": "/etc/salt/sqlx-certs/x",
    "ssl_files": {"ca": "ca.pem"},
    "query": {"application_name": "x"},
    "connect_args": {"application_name": "x"},
    "read_only": True,
    "connect_timeout_s": 3,
    "statement_timeout_ms": 1000,
}


def nopolicy(**policy):
    """A minion config with profiles and, optionally, a ``_policy`` that has no ``allow_call_params``."""
    config = {"profiles": copy.deepcopy(BASE_PROFILES)}
    if policy:
        config["_policy"] = policy
    return config


ADHOC = {"dialect": "postgresql", "host": "10.1.1.1", "database": "d", "username": "u"}


# -- the default object -----------------------------------------------------------------------------------------------


def test_the_default_policy_object_is_the_documented_one():
    d = overrides.DEFAULT_POLICY
    assert d.allow == DEFAULT_FIELDS == set(overrides.DEFAULT_ALLOWED_FIELDS)
    assert d.hosts is None and d.ports == [] and d.dialects is None
    assert d.adhoc is True and d.literal_password is True and d.inherit_password is False
    assert d.default is True
    assert not d.allow & set(
        NOT_DEFAULT
    )  # TLS, query, connect_args, read_only and timeouts stay opt-in


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"profiles": {}},
        {"_policy": None},
        {"_policy": {}},
        {
            "_policy": {"ssl_certs_dir_root": "/etc/salt/sqlx-certs"}
        },  # an existing D12 config does not turn it off
    ],
)
def test_absent_allow_call_params_is_the_default_policy(config):
    assert overrides.parse_policy(config) == overrides.DEFAULT_POLICY


@pytest.mark.parametrize("value", [[], None])
def test_empty_or_null_allow_call_params_is_strict(value):
    policy = overrides.parse_policy({"_policy": {"allow_call_params": value}})
    assert policy == overrides.STRICT_POLICY
    assert not policy.allow and not policy.default


def test_strict_ignores_the_other_keys():
    # as before D16: nothing else is read (even a broken allowed_hosts) when the feature is off
    config = {"_policy": {"allow_call_params": [], "allowed_hosts": "garbage"}}
    assert overrides.parse_policy(config) == overrides.STRICT_POLICY


# -- every default field works on every dialect, with or without a profile ---------------------------------------------


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb", "oracle", "mssql"])
def test_adhoc_connection_with_no_policy_on_every_dialect(dialect):
    kw = dict(ADHOC, dialect=dialect, password=LITERAL)
    prof = go(nopolicy(), kw)
    assert prof["name"] == "(adhoc)" and prof["adhoc"] is True
    assert prof["dialect"] == ("mysql" if dialect == "mariadb" else dialect)
    assert prof["host"] == "10.1.1.1" and prof["database"] == "d" and prof["username"] == "u"
    assert prof["password"] == LITERAL and prof["password_from_call"] is True
    assert prof["call_overrides"] == sorted(kw)
    assert prof["allow_execute"] is False and prof["read_only"] is True
    assert (
        prof["port"]
        == {"postgresql": 5432, "mysql": 3306, "oracle": 1521, "mssql": 1433}[prof["dialect"]]
    )


@pytest.mark.parametrize("field", sorted(DEFAULT_FIELDS))
def test_every_default_field_is_accepted_over_a_profile(field):
    value = {
        "dialect": "mysql",
        "driver": "pymysql",
        "host": "db2.corp.example",
        "port": 3307,
        "database": "other",
        "username": "someone",
        "password": LITERAL,
    }[field]
    kw = (
        {"profile": "my", field: value} if field != "driver" else {"profile": "my", "driver": value}
    )
    prof = go(nopolicy(), kw)
    assert prof["call_overrides"] == [field]
    assert prof["adhoc"] is False


def test_connection_object_works_like_flat_keywords():
    prof = go(nopolicy(), {"connection": dict(ADHOC, password=SDB_REF)})
    assert prof["name"] == "(adhoc)" and prof["password"] == SDB_REF


def test_a_literal_password_is_accepted_by_default_and_marked_as_from_the_call():
    prof = go(nopolicy(), dict(ADHOC, password=LITERAL))
    assert prof["password"] == LITERAL and prof["password_from_call"] is True
    assert (
        overrides.resolve_secrets(prof, lambda uri: pytest.fail("no sdb for a literal"))["password"]
        == LITERAL
    )


def test_an_sdb_reference_still_works_and_is_resolved():
    prof = go(nopolicy(), dict(ADHOC, password=SDB_REF))
    assert overrides.resolve_secrets(prof, lambda uri: "resolved")["password"] == "resolved"


def test_port_defaults_per_dialect_and_may_be_sent():
    assert go(nopolicy(), dict(ADHOC, port=54329))["port"] == 54329
    with pytest.raises(SaltInvocationError) as exc:  # a string is never coerced
        go(nopolicy(), dict(ADHOC, port="5432"))
    assert code(exc) == "[E_PROFILE_INVALID]"


def test_overlay_on_the_default_profile():
    config = nopolicy()
    config["default_profile"] = "pg"
    prof = go(config, {"host": "db2.corp.example", "password": LITERAL})
    assert prof["name"] == "pg" and prof["adhoc"] is False and prof["host"] == "db2.corp.example"


# -- what the default does NOT open ------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(NOT_DEFAULT))
def test_non_default_fields_are_refused_over_a_profile_and_ad_hoc(field):
    for kw in ({"profile": "pg"}, dict(ADHOC)):
        with pytest.raises(SaltInvocationError) as exc:
            go(nopolicy(), dict(kw, **{field: NOT_DEFAULT[field]}))
        assert code(exc) == "[E_POLICY]" and f"'{field}'" in str(exc.value)
        assert "allow_call_params" in str(exc.value)


@pytest.mark.parametrize(
    "key", ["allow_execute", "execute_verbs", "error_detail", "row_limit", "max_bytes", "_policy"]
)
def test_limits_and_safety_switches_are_never_call_keywords(key):
    for kw in ({"profile": "pg"}, dict(ADHOC)):
        if key != "max_bytes":  # flat ``max_bytes`` is the call option that can only lower the cap
            with pytest.raises(SaltInvocationError) as exc:
                go(nopolicy(), dict(kw, **{key: True}))
            assert code(exc) == "[E_OPTION_UNKNOWN]"
        with pytest.raises(SaltInvocationError) as exc:
            go(nopolicy(), dict(kw, connection={key: True}))
        assert code(exc) == "[E_OPTION_UNKNOWN]"


def test_caps_can_still_only_be_lowered():
    opts = options.validate(
        "query",
        {"profile": "pg", "sql": "x", "limit": 10**9, "timeout_ms": 10**9, "max_bytes": 10**12},
    )
    capped = options.apply_caps(opts, go(nopolicy(), {"profile": "pg", "host": "10.0.0.1"}))
    assert (
        capped["limit"] == 500
        and capped["timeout_ms"] == 20000
        and capped["max_bytes"] == 1024 * 1024
    )


def test_read_only_and_timeouts_cannot_be_changed_by_default_but_read_only_stays_on():
    prof = go(nopolicy(), dict(ADHOC))
    assert (
        prof["read_only"] is True
        and prof["allow_execute"] is False
        and prof["error_detail"] is False
    )
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, read_only=False))
    assert code(exc) == "[E_POLICY]"


def test_ad_hoc_and_default_policy_calls_are_marked_unable_to_execute():
    assert go(nopolicy(), dict(ADHOC))["execute_blocked"] is True  # ad-hoc: never
    # a profile with allow_execute plus ANY connection field: blocked under the default (D16) ...
    assert go(nopolicy(), {"profile": "pg_rw", "database": "x"})["execute_blocked"] is True
    # ... but not when the operator listed the fields explicitly (the old opt-in contract), and not without fields
    explicit = nopolicy(allow_call_params=["database"])
    assert go(explicit, {"profile": "pg_rw", "database": "x"})["execute_blocked"] is False
    assert "execute_blocked" not in go(nopolicy(), {"profile": "pg_rw"})


def test_the_profile_password_is_dropped_when_the_target_changes():
    prof = go(nopolicy(), {"profile": "pg", "host": "10.9.9.9"})
    assert "password" not in prof and prof["password_dropped"] is True
    same = go(nopolicy(), {"profile": "pg", "database": "other"})
    assert same["password"] == "sdb://vault/base_pw" and same["password_dropped"] is False
    for kw in ({"port": 5433}, {"username": "x"}, {"dialect": "mysql"}):
        assert "password" not in go(nopolicy(), dict(kw, profile="pg"))
    sent = go(nopolicy(), {"profile": "pg", "host": "10.9.9.9", "password": LITERAL})
    assert sent["password"] == LITERAL and sent["password_dropped"] is False


def test_an_ad_hoc_call_without_a_password_is_marked_so_the_driver_ignores_the_environment():
    assert go(nopolicy(), dict(ADHOC))["password_dropped"] is True


def test_a_profile_can_narrow_the_default():
    ok = go(nopolicy(), {"profile": "narrow", "host": "10.1.1.1", "database": "x"})
    assert ok["host"] == "10.1.1.1"
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), {"profile": "narrow", "port": 1})
    assert code(exc) == "[E_POLICY]"


def test_a_profile_with_an_empty_list_is_closed_even_under_the_default():
    config = nopolicy()
    config["profiles"]["closed"] = dict(BASE_PROFILES["pg"], allow_call_params=[])
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "closed", "host": "10.0.0.1"})
    assert code(exc) == "[E_POLICY]"


# -- incomplete connections -------------------------------------------------------------------------------------------


def test_no_profile_and_an_incomplete_connection_names_what_is_missing_but_no_value():
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), {"host": "10.1.1.1", "password": LITERAL})
    assert code(exc) == "[E_OPTION_INVALID]"
    assert "dialect, database, username" in str(exc.value)
    assert LITERAL not in str(exc.value) and "10.1.1.1" not in str(exc.value)


def test_nothing_sent_and_no_profile_is_the_old_error():
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), {})
    assert code(exc) == "[E_OPTION_INVALID]" and "'profile' is required" in str(exc.value)


# -- hosts under the default: only link-local and metadata are denied -------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["10.0.0.5", "192.168.1.9", "172.16.4.4", "127.0.0.1", "::1", "db2.corp.example", "8.8.8.8"],
)
def test_loopback_private_and_ordinary_hosts_are_allowed_by_default(host):
    assert go(nopolicy(), dict(ADHOC, host=host))["host"] == host


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "169.254.0.1",
        "169.254.170.2",
        "fe80::1",
        "febf::1",
        "::ffff:169.254.169.254",
        "fd00:ec2::254",
        "100.100.100.200",
        "meta.corp.example",  # a name that resolves to the metadata address
        "db3.corp.example",  # one clean and one metadata address
    ],
)
def test_metadata_and_link_local_targets_are_refused_by_default(host):
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, host=host))
    assert code(exc) == "[E_POLICY]" and "allowed_hosts" in str(exc.value)
    assert host not in str(exc.value)


@pytest.mark.parametrize(
    "host", ["2852039166", "0xa9fea9fe", "0251.0376.0251.0376", "169.254.43518"]
)
def test_numeric_spellings_of_the_metadata_address_are_refused_by_the_real_resolver(host):
    """
    The check is on the resolved address, so inet_aton forms cannot slip through (the driver resolves the same way).

    Whether the platform resolver accepts these spellings differs (glibc does; the Windows resolver does not). Where it
    does not, the host cannot be reached either, and the call must fail closed with ``[E_CONNECT]``.
    """
    try:
        socket.getaddrinfo(host, None)
        resolves = True
    except OSError:
        resolves = False
    if resolves:
        with pytest.raises(SaltInvocationError) as exc:
            go(nopolicy(), dict(ADHOC, host=host), resolver=None)
        assert code(exc) == "[E_POLICY]"
    else:
        with pytest.raises(CommandExecutionError) as exc:
            go(nopolicy(), dict(ADHOC, host=host), resolver=None)
        assert str(exc.value).startswith("[E_CONNECT]")


def test_an_unresolvable_name_fails_closed_without_naming_it():
    with pytest.raises(CommandExecutionError) as exc:
        go(nopolicy(), dict(ADHOC, host="gone.example"))
    assert str(exc.value).startswith("[E_CONNECT]") and "gone.example" not in str(exc.value)


@pytest.mark.parametrize("host", ["a@b", "x/y", "h#x", "h h", "x\x00y", "h%eth0", "", "ex ample"])
def test_odd_host_text_is_refused(host):
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, host=host))
    assert code(exc) in ("[E_POLICY]", "[E_OPTION_INVALID]")


def test_listing_the_address_in_allowed_hosts_lifts_the_deny_and_then_restricts_to_the_list():
    config = nopolicy(allowed_hosts=["169.254.169.254"])
    assert go(config, dict(ADHOC, host="169.254.169.254"))["host"] == "169.254.169.254"
    with pytest.raises(
        SaltInvocationError
    ) as exc:  # an allow-list is an allow-list: everything else is outside it
        go(config, dict(ADHOC, host="10.1.1.1"))
    assert code(exc) == "[E_POLICY]" and "allowed_hosts" in str(exc.value)


def test_allowed_hosts_keep_working_exactly_as_before_under_the_default_fields():
    config = nopolicy(allowed_hosts=["*.corp.example", "10.0.0.0/8"])
    assert go(config, dict(ADHOC, host="db2.corp.example"))["host"] == "db2.corp.example"
    assert go(config, dict(ADHOC, host="10.2.3.4"))["host"] == "10.2.3.4"
    for host in (
        "192.168.1.1",
        "outside.example",
        "127.0.0.1",
        "169.254.169.254",
        "meta.corp.example",
    ):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, dict(ADHOC, host=host))
        assert code(exc) == "[E_POLICY]"
    with pytest.raises(SaltInvocationError):  # 0.0.0.0/0 never lifts the denied ranges
        go(nopolicy(allowed_hosts=["0.0.0.0/0"]), dict(ADHOC, host="169.254.169.254"))
    for bad in ("*", "?*"):
        with pytest.raises(SaltInvocationError) as exc:
            go(nopolicy(allowed_hosts=[bad]), dict(ADHOC))
        assert code(exc) == "[E_POLICY]"


def test_allowed_ports_and_dialects_narrow_the_default():
    config = nopolicy(allowed_ports=[5432, "3306-3309"], allowed_dialects=["postgresql", "mariadb"])
    assert go(config, dict(ADHOC, port=5432))["port"] == 5432
    assert go(config, dict(ADHOC, dialect="mysql", port=3308))["port"] == 3308
    for kw in (
        dict(ADHOC, port=5433),
        dict(ADHOC, dialect="oracle"),
        dict(ADHOC, dialect="mssql", port=5432),
    ):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, kw)
        assert code(exc) == "[E_POLICY]"


def test_the_two_default_flags_can_be_switched_off():
    config = nopolicy(allow_adhoc_connection=False)
    with pytest.raises(SaltInvocationError) as exc:
        go(config, dict(ADHOC))
    assert code(exc) == "[E_POLICY]" and "allow_adhoc_connection" in str(exc.value)
    assert go(config, {"profile": "pg", "host": "10.1.1.1"})["host"] == "10.1.1.1"
    config = nopolicy(allow_call_literal_password=False)
    with pytest.raises(SaltInvocationError) as exc:
        go(config, dict(ADHOC, password=LITERAL))
    assert code(exc) == "[E_POLICY]" and LITERAL not in str(exc.value)
    assert go(config, dict(ADHOC, password=SDB_REF))["password"] == SDB_REF


def test_a_bad_value_in_a_default_mode_policy_key_fails_closed():
    for policy in (
        {"allowed_hosts": []},
        {"allowed_ports": "x"},
        {"allowed_dialects": ["nope"]},
        {"allow_adhoc_connection": "yes"},
    ):
        with pytest.raises(SaltInvocationError) as exc:
            go(nopolicy(**policy), dict(ADHOC))
        assert code(exc) == "[E_POLICY]"


# -- an explicit list replaces the default completely ------------------------------------------------------------------


def explicit(allow, **policy):
    config = {
        "profiles": copy.deepcopy(BASE_PROFILES),
        "_policy": dict(policy, allow_call_params=allow),
    }
    return config


def test_a_shorter_explicit_list_narrows_and_keeps_the_old_flag_defaults():
    config = explicit(["host", "database"], allowed_hosts=["10.0.0.0/8"])
    assert go(config, {"profile": "pg", "host": "10.1.1.1"})["host"] == "10.1.1.1"
    for kw in ({"port": 5433}, {"username": "x"}, {"password": SDB_REF}, {"dialect": "mysql"}):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, dict(kw, profile="pg"))
        assert code(exc) == "[E_POLICY]"
    with pytest.raises(SaltInvocationError) as exc:  # the old flag defaults: ad-hoc off ...
        go(config, dict(ADHOC, host="10.1.1.1"))
    assert code(exc) in ("[E_POLICY]",)


def test_an_explicit_list_still_refuses_a_literal_password_unless_the_flag_is_set():
    config = explicit(["password"])
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "password": LITERAL})
    assert code(exc) == "[E_POLICY]" and LITERAL not in str(exc.value)
    config = explicit(["password"], allow_call_literal_password=True)
    assert go(config, {"profile": "pg", "password": LITERAL})["password"] == LITERAL


def test_an_explicit_list_with_host_still_needs_allowed_hosts():
    with pytest.raises(SaltInvocationError) as exc:
        go(explicit(["host"]), {"profile": "pg", "host": "10.1.1.1"})
    assert code(exc) == "[E_POLICY]" and "allowed_hosts" in str(exc.value)


@pytest.mark.parametrize("bad", [["*"], ["host", "nope"], "host", ["allow_execute"], ["host", 1]])
def test_a_star_or_unknown_name_is_still_rejected_in_an_explicit_list(bad):
    with pytest.raises(SaltInvocationError) as exc:
        overrides.parse_policy(explicit(bad, allowed_hosts=["10.0.0.0/8"]))
    assert code(exc) == "[E_POLICY]"


def test_an_explicit_list_has_the_old_denied_ranges_loopback_included():
    config = explicit(
        ["host", "database", "username", "dialect"], allowed_hosts=["10.0.0.0/8", "127.0.0.1"]
    )
    assert go(config, {"profile": "pg", "host": "127.0.0.1"})["host"] == "127.0.0.1"
    with pytest.raises(SaltInvocationError):
        go(
            explicit(["host"], allowed_hosts=["*.corp.example"]),
            {"profile": "pg", "host": "loop.corp.example"},
        )


def test_an_explicit_list_can_not_be_widened_by_the_default_flags():
    # allow_adhoc_connection: true with an explicit list still needs allowed_hosts (old rule)
    with pytest.raises(SaltInvocationError) as exc:
        go(
            explicit(["dialect", "host", "database", "username"], allow_adhoc_connection=True),
            dict(ADHOC),
        )
    assert code(exc) == "[E_POLICY]" and "allowed_hosts" in str(exc.value)


# -- secrets and malformed values -------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [12345, 0, True, ""])
def test_a_non_string_password_is_refused_without_echoing_it(value):
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, password=value))
    assert code(exc) == "[E_OPTION_INVALID]" and "12345" not in str(exc.value)


def test_a_numeric_password_message_tells_how_to_quote_it():
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, password=12345))
    assert "quote" in str(exc.value)


@pytest.mark.parametrize("field", ["password", "database", "username"])
def test_a_nul_character_is_refused(field):
    with pytest.raises(SaltInvocationError) as exc:
        go(nopolicy(), dict(ADHOC, **{field: "a\x00b"}))
    assert code(exc) == "[E_OPTION_INVALID]" and "\x00" not in str(exc.value)


def test_no_refusal_message_contains_a_password_a_reference_or_a_host():
    cases = [
        dict(ADHOC, password=LITERAL, ssl_mode="verify-ca"),
        dict(ADHOC, password=LITERAL, host="169.254.169.254"),
        dict(ADHOC, password=SDB_REF, read_only=False),
        dict(ADHOC, password=LITERAL, allow_execute=True),
        dict(ADHOC, password=LITERAL, port="x"),
    ]
    for kw in cases:
        with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
            go(nopolicy(allowed_ports=[1]) if kw.get("port") == "x" else nopolicy(), kw)
        text = str(exc.value)
        assert LITERAL not in text and SDB_REF not in text and "169.254" not in text


def test_the_audit_helpers_name_fields_only():
    prof = go(nopolicy(), dict(ADHOC, password=LITERAL))
    assert overrides.overridden(prof) == "database,dialect,host,password,username"
    assert LITERAL not in overrides.overridden(prof) and overrides.target(prof) == "10.1.1.1:5432"


def test_resolve_failure_never_names_a_call_supplied_reference():
    prof = go(nopolicy(), dict(ADHOC, password=SDB_REF))

    def boom(uri):
        raise KeyError(uri)

    with pytest.raises(CommandExecutionError) as exc:
        overrides.resolve_secrets(prof, boom)
    assert SDB_REF not in str(exc.value)


# -- strict (mirror): the same calls give exactly the old rejections ---------------------------------------------------


STRICT = {"allow_call_params": []}


@pytest.mark.parametrize(
    "kw",
    [
        dict(ADHOC, password=LITERAL),
        dict(ADHOC),
        {"profile": "pg", "host": "10.1.1.1"},
        {"profile": "pg", "password": SDB_REF},
        {"profile": "pg", "connection": {"host": "10.1.1.1"}},
        {"profile": "pg", "ssl_mode": "verify-ca"},
        {"profile": "pg", "read_only": True},
        {"profile": "pg", "database": "x", "port": 1},
    ],
)
def test_strict_gives_the_old_rejection_for_every_shape_the_default_accepts(kw):
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": STRICT}
    with pytest.raises(SaltInvocationError) as exc:
        go(config, kw)
    assert code(exc) == "[E_OPTION_UNKNOWN]"
    assert LITERAL not in str(exc.value) and SDB_REF not in str(exc.value)


def test_strict_profile_only_calls_are_untouched():
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": STRICT, "default_profile": "pg"}
    assert go(config, {"profile": "pg"})["host"] == "db1.corp.example"
    assert go(config, {})["name"] == "pg"
    assert "call_overrides" not in go(config, {"profile": "pg"})


def test_strict_with_no_profile_is_the_old_error():
    with pytest.raises(SaltInvocationError) as exc:
        go({"profiles": copy.deepcopy(BASE_PROFILES), "_policy": STRICT}, {})
    assert code(exc) == "[E_OPTION_INVALID]" and "'profile' is required" in str(exc.value)


def test_a_broken_policy_does_not_break_profile_only_calls():
    # unchanged: the policy is only parsed when the call carries a connection keyword
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": {"allow_call_params": "oops"}}
    assert go(config, {"profile": "pg"})["host"] == "db1.corp.example"
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "host": "x"})
    assert code(exc) == "[E_POLICY]"
