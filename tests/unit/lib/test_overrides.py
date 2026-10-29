"""Unit tests for saltext.sqlx.lib.overrides (call-time connection parameters, SPEC section 3.2).

No database and no network: DNS is an injected fake resolver. Every policy gate of the owner decision is covered:
feature off by default, field allowlist, host/CIDR/resolved-IP checks, ad-hoc connections, secrets, caps, read-only,
TLS tightening and the hard rules.
"""

# pylint: disable=unused-argument,protected-access
import copy

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import options
from saltext.sqlx.lib import overrides

LITERAL = "Lit3ral-PW-77qz"
SDB_REF = "sdb://vault/SECRETREF_pg"

DNS = {
    "db2.corp.example": ["10.1.2.3"],
    "db3.corp.example": ["10.1.2.4", "169.254.169.254"],
    "meta.corp.example": ["169.254.169.254"],
    "loop.corp.example": ["127.0.0.1"],
    "mixed.corp.example": ["10.1.2.3", "192.168.9.9"],
    "2130706433": ["127.0.0.1"],
    "v6.corp.example": ["::ffff:127.0.0.1"],
    "plain.other.example": ["10.9.9.9"],
}


def fake_resolver(host, port):
    if host.startswith("gone."):
        raise OSError("no such host")
    return DNS.get(host, ["203.0.113.9"])


BASE_PROFILES = {
    "pg": {
        "dialect": "postgresql",
        "host": "db1.corp.example",
        "port": 5432,
        "database": "orders",
        "username": "ro",
        "password": "sdb://vault/base_pw",
        "statement_timeout_ms": 20000,
        "connect_timeout_s": 5,
    },
    "pg_rw": {
        "dialect": "postgresql",
        "host": "db1.corp.example",
        "port": 5432,
        "database": "orders",
        "username": "rw",
        "password": "rw-literal-in-config",
        "allow_execute": True,
        "read_only": False,
    },
    "narrow": {
        "dialect": "postgresql",
        "host": "db1.corp.example",
        "port": 5432,
        "database": "orders",
        "username": "ro",
        "allow_call_params": ["database", "host"],
    },
    "my": {
        "dialect": "mysql",
        "host": "db1.corp.example",
        "port": 3306,
        "database": "scratch",
        "username": "ro",
    },
    "pgq": {
        "dialect": "postgresql",
        "host": "db1.corp.example",
        "port": 5432,
        "database": "orders",
        "username": "ro",
        "query": {"application_name": "base"},
    },
}


def cfg(allow=("host", "port", "database", "username", "password"), **policy):
    policy = dict(policy)
    if allow is not None:
        policy["allow_call_params"] = list(allow)
    policy.setdefault("allowed_hosts", ["*.corp.example", "10.0.0.0/8"])
    return {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": policy}


def go(config, kwargs, function="query", resolver=fake_resolver):
    """prepare + validate + build_profile, as the module does."""
    kwargs = dict(kwargs)
    kwargs.setdefault("sql", "select 1")
    call = overrides.prepare(function, kwargs, config)
    opts = options.validate(function, call.kwargs)
    return overrides.build_profile(call, opts["profile"], config, resolver=resolver)


def code(exc):
    return str(exc.value).split("]", maxsplit=1)[0] + "]"


# -- feature off by default -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy", [None, {}, {"allow_call_params": []}, {"allow_call_params": None}]
)
def test_feature_is_off_by_default(policy):
    config = {"profiles": copy.deepcopy(BASE_PROFILES)}
    if policy is not None:
        config["_policy"] = policy
    for kw in ({"host": "x"}, {"connection": {"host": "x"}}, {"password": LITERAL}):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, dict(kw, profile="pg"))
        assert code(exc) == "[E_OPTION_UNKNOWN]"
        assert LITERAL not in str(exc.value)


def test_feature_off_without_overrides_is_todays_behaviour():
    config = {"profiles": copy.deepcopy(BASE_PROFILES)}
    prof = go(config, {"profile": "pg"})
    assert prof["host"] == "db1.corp.example"
    assert "call_overrides" not in prof


def test_missing_profile_is_still_required_when_nothing_is_configured():
    with pytest.raises(SaltInvocationError) as exc:
        go({"profiles": copy.deepcopy(BASE_PROFILES)}, {})
    assert code(exc) == "[E_OPTION_INVALID]"


def test_compat_wording_when_feature_off():
    config = {"profiles": copy.deepcopy(BASE_PROFILES)}
    with pytest.raises(SaltInvocationError) as exc:
        overrides.require_enabled(config, ["username", "host"])
    assert code(exc) == "[E_POLICY]"
    assert "allow_call_params" in str(exc.value)
    overrides.require_enabled(cfg(), [])  # nothing supplied: fine
    overrides.require_enabled(cfg(), ["host"])


# -- policy parsing ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"allow_call_params": ["*"]},
        {"allow_call_params": ["host", "nope"]},
        {"allow_call_params": "host"},
        {"allow_call_params": ["host", 1]},
        {"allow_call_params": ["allow_execute"]},
        {"allow_call_params": ["host"], "allowed_hosts": "db"},
        {"allow_call_params": ["host"], "allowed_hosts": ["*"]},
        {"allow_call_params": ["host"], "allowed_hosts": ["??"]},
        {"allow_call_params": ["host"], "allowed_hosts": ["10.0.0.0/99"]},
        {"allow_call_params": ["host"], "allowed_hosts": [""]},
        {"allow_call_params": ["host"], "allowed_hosts": [5]},
        {"allow_call_params": ["host"], "allowed_hosts": []},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_ports": "5432"},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_ports": [0]},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_ports": ["9-1"]},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_ports": ["x"]},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_ports": [True]},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_dialects": ["db2"]},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allowed_dialects": "mysql"},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allow_adhoc_connection": "yes"},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "allow_call_literal_password": 1},
        {"allow_call_params": ["host"], "allowed_hosts": ["a"], "inherit_password": "no"},
    ],
)
def test_invalid_policy_fails_closed_with_e_policy(bad):
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": bad}
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "host": "db2.corp.example"})
    assert code(exc) == "[E_POLICY]"
    assert "_policy" in str(exc.value)


def test_policy_not_a_mapping_is_e_profile_invalid_like_tls():
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": ["x"]}
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "host": "db2.corp.example"})
    assert code(exc) == "[E_PROFILE_INVALID]"


def test_invalid_policy_is_ignored_when_the_call_has_no_overrides():
    config = {"profiles": copy.deepcopy(BASE_PROFILES), "_policy": {"allow_call_params": ["*"]}}
    assert go(config, {"profile": "pg"})["host"] == "db1.corp.example"


def test_host_overridable_requires_allowed_hosts():
    config = cfg(allow=["host"])
    del config["_policy"]["allowed_hosts"]
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "host": "db2.corp.example"})
    assert code(exc) == "[E_POLICY]"
    assert "allowed_hosts" in str(exc.value)


def test_non_host_fields_do_not_need_allowed_hosts():
    config = cfg(allow=["database"])
    del config["_policy"]["allowed_hosts"]
    assert go(config, {"profile": "pg", "database": "other"})["database"] == "other"


# -- merging ----------------------------------------------------------------------------------------------------------


def test_flat_and_connection_are_merged():
    prof = go(
        cfg(),
        {
            "profile": "pg",
            "host": "db2.corp.example",
            "connection": {"database": "d2", "port": 5433},
        },
    )
    assert (prof["host"], prof["database"], prof["port"]) == ("db2.corp.example", "d2", 5433)
    assert prof["call_overrides"] == ["database", "host", "port"]


def test_key_in_both_flat_and_connection_is_e_option_invalid():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), {"profile": "pg", "database": "a", "connection": {"database": "b"}})
    assert code(exc) == "[E_OPTION_INVALID]"
    assert "database" in str(exc.value)
    assert "'a'" not in str(exc.value) and "'b'" not in str(exc.value)


@pytest.mark.parametrize("bad", ["x", ["host"], 5, True])
def test_connection_must_be_an_object(bad):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), {"profile": "pg", "connection": bad})
    assert code(exc) == "[E_OPTION_INVALID]"


def test_connection_with_unknown_or_forbidden_keys_is_e_option_unknown():
    for key in (
        "allow_execute",
        "execute_verbs",
        "error_detail",
        "_policy",
        "row_limit",
        "max_bytes",
        "typo",
    ):
        with pytest.raises(SaltInvocationError) as exc:
            go(cfg(), {"profile": "pg", "connection": {key: True}})
        assert code(exc) == "[E_OPTION_UNKNOWN]"
        assert key in str(exc.value)


@pytest.mark.parametrize(
    "key", ["allow_execute", "execute_verbs", "error_detail", "_policy", "row_limit", "name", "tls"]
)
def test_forbidden_flat_keys_stay_unknown_even_with_the_feature_on(key):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), {"profile": "pg", key: True})
    assert code(exc) == "[E_OPTION_UNKNOWN]"


def test_none_values_mean_not_given():
    prof = go(cfg(), {"profile": "pg", "host": None, "connection": {"database": None}})
    assert prof["host"] == "db1.corp.example"
    assert "call_overrides" not in prof


def test_dunder_keys_are_ignored():
    prof = go(cfg(), {"profile": "pg", "__pub_user": "x", "database": "z", "__kwarg__": True})
    assert prof["database"] == "z"


def test_overrides_do_not_mutate_the_config():
    config = cfg()
    before = copy.deepcopy(config)
    go(config, {"profile": "pg", "host": "db2.corp.example", "database": "z"})
    assert config == before


# -- field allowlist --------------------------------------------------------------------------------------------------


def test_field_not_in_allow_call_params_is_e_policy_naming_the_key_only():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=["database"]), {"profile": "pg", "username": "root"})
    assert code(exc) == "[E_POLICY]"
    assert "username" in str(exc.value)
    assert "root" not in str(exc.value)


def test_profile_allow_call_params_can_only_narrow():
    config = cfg(allow=["database", "host", "port"])
    # 'narrow' allows database and host; username is not in its list; port is global-only but not in the profile list
    assert go(config, {"profile": "narrow", "database": "z"})["database"] == "z"
    for field, value in (("port", 5433), ("username", "u")):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, {"profile": "narrow", field: value})
        assert code(exc) == "[E_POLICY]"
    # the profile list names 'host' but the global list does not: cannot widen
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=["database"]), {"profile": "narrow", "host": "db2.corp.example"})
    assert code(exc) == "[E_POLICY]"


def test_profile_allow_call_params_validated():
    config = cfg()
    config["profiles"]["bad"] = dict(BASE_PROFILES["pg"], allow_call_params=["*"])
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "bad"})
    assert code(exc) == "[E_PROFILE_INVALID]"
    config["profiles"]["bad"] = dict(BASE_PROFILES["pg"], allow_call_params="host")
    with pytest.raises(SaltInvocationError):
        go(config, {"profile": "bad"})


def test_unknown_profile_still_e_profile_unknown_and_discloses_nothing():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), {"profile": "nope", "host": "db2.corp.example"})
    assert code(exc) == "[E_PROFILE_UNKNOWN]"
    assert "pg_rw" not in str(exc.value)


# -- host policy ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, hosts",
    [
        ("db2.corp.example", ["db2.corp.example"]),  # exact
        ("DB2.Corp.Example", ["db2.corp.example"]),  # case-insensitive
        ("db2.corp.example.", ["db2.corp.example"]),  # trailing dot
        ("db2.corp.example", ["*.corp.example"]),  # glob
        ("db2.corp.example", ["10.0.0.0/8"]),  # name allowed by resolved IP in a CIDR
        ("10.7.7.7", ["10.0.0.0/8"]),  # literal in CIDR
        ("10.7.7.7", ["10.7.7.7"]),  # literal exact
        ("10.7.7.7", ["10.7.*"]),  # glob on the literal text
        ("fd00::5", ["fd00::/8"]),  # IPv6
    ],
)
def test_allowed_hosts(host, hosts):
    prof = go(cfg(allowed_hosts=hosts), {"profile": "pg", "host": host})
    assert prof["host"] == host


@pytest.mark.parametrize(
    "host, hosts",
    [
        ("evil.example", ["*.corp.example"]),
        ("db2.corp.example.evil.example", ["*.corp.example"]),
        ("10.7.7.7", ["192.168.0.0/16"]),
        ("11.0.0.1", ["10.0.0.0/8"]),
        ("fd00::5", ["10.0.0.0/8"]),
        ("mixed.corp.example", ["10.0.0.0/8"]),  # one resolved IP outside the CIDR
        ("plain.other.example", ["db2.corp.example", "192.168.0.0/16"]),
    ],
)
def test_host_not_in_allowed_hosts_is_e_policy_without_echo(host, hosts):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allowed_hosts=hosts), {"profile": "pg", "host": host})
    assert code(exc) == "[E_POLICY]"
    assert "allowed_hosts" in str(exc.value)
    assert host not in str(exc.value)


@pytest.mark.parametrize(
    "host, hosts",
    [
        ("meta.corp.example", ["*.corp.example"]),  # name allowed, resolves to the metadata address
        ("db3.corp.example", ["*.corp.example"]),  # one of two resolved IPs is the metadata address
        ("loop.corp.example", ["*.corp.example"]),  # resolves to loopback
        ("v6.corp.example", ["*.corp.example"]),  # IPv4-mapped IPv6 loopback
        (
            "2130706433",
            ["2130706433"],
        ),  # decimal spelling of 127.0.0.1 allowed by (wrong) exact name
        ("169.254.169.254", ["169.254.*"]),  # a glob never unblocks the denied ranges
        ("127.0.0.1", ["127.*"]),
        ("127.0.0.1", ["localhost"]),
        ("::1", ["::*"]),
        ("fe80::1", ["fe80:*"]),
        ("0.0.0.0", ["0.0.0.*"]),
        ("169.254.169.254", ["10.0.0.0/8"]),
        ("fd00:ec2::254", ["fd00::/8"]),  # IPv6 metadata address
    ],
)
def test_metadata_link_local_and_loopback_are_refused_unless_an_ip_entry_allows_them(host, hosts):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allowed_hosts=hosts), {"profile": "pg", "host": host})
    assert code(exc) == "[E_POLICY]"


@pytest.mark.parametrize(
    "host, hosts",
    [
        ("127.0.0.1", ["127.0.0.1"]),
        ("127.0.0.1", ["127.0.0.0/8"]),
        ("::1", ["::1"]),
        ("loop.corp.example", ["*.corp.example", "127.0.0.1"]),
        ("169.254.169.254", ["169.254.0.0/16"]),
        ("meta.corp.example", ["*.corp.example", "169.254.169.254/32"]),
    ],
)
def test_explicit_ip_entries_allow_denied_ranges(host, hosts):
    assert go(cfg(allowed_hosts=hosts), {"profile": "pg", "host": host})["host"] == host


@pytest.mark.parametrize(
    "host",
    [
        "a,b",
        "/var/run/postgresql",
        "a b",
        "a@b",
        "a?x=1",
        "a#b",
        "",
        "h\n",
        "a/b",
        "[::1]",
        "%s",
        "a;b",
        "é.example",
        "x" * 300,
    ],
)
def test_host_syntax_is_validated(host):
    with pytest.raises(SaltInvocationError) as exc:
        go(
            cfg(allowed_hosts=["*"][:0] or ["*.corp.example", "10.0.0.0/8"]),
            {"profile": "pg", "host": host},
        )
    assert code(exc) in ("[E_POLICY]", "[E_OPTION_INVALID]", "[E_PROFILE_INVALID]")
    assert len(host) < 4 or host.strip() not in str(exc.value)


def test_unresolvable_name_is_e_connect_before_any_connection():
    with pytest.raises(CommandExecutionError) as exc:
        go(cfg(allowed_hosts=["*.corp.example"]), {"profile": "pg", "host": "gone.corp.example"})
    assert str(exc.value).startswith("[E_CONNECT]")


def test_unresolvable_unlisted_name_is_e_policy_whatever_dns_says():
    """With a CIDR in the list an unlisted name is looked up; if it does not resolve it is simply outside the list."""
    with pytest.raises(SaltInvocationError) as exc:
        go(
            cfg(allowed_hosts=["10.0.0.0/8"]),
            {"profile": "pg", "host": "gone.example"},
            resolver=lambda h, p: [],
        )
    assert str(exc.value).startswith("[E_POLICY]")


def test_real_resolver_is_used_by_default(monkeypatch):
    calls = []

    def spy(host, port, *a, **k):
        calls.append(host)
        return [(2, 1, 6, "", ("10.1.1.1", port))]

    monkeypatch.setattr(overrides.socket, "getaddrinfo", spy)
    call = overrides.prepare("ping", {"profile": "pg", "host": "x.corp.example"}, cfg())
    prof = overrides.build_profile(call, "pg", cfg())
    assert prof["host"] == "x.corp.example"
    assert calls == ["x.corp.example"]


def test_host_check_runs_only_when_host_is_supplied():
    # the base profile's own host is operator configuration and is never checked
    config = cfg(allowed_hosts=["10.0.0.0/8"])
    assert go(config, {"profile": "pg", "database": "z"})["host"] == "db1.corp.example"


def test_resolver_is_not_called_for_an_ip_literal():
    def boom(host, port):
        raise AssertionError("resolved a literal")

    assert go(cfg(), {"profile": "pg", "host": "10.1.1.1"}, resolver=boom)["host"] == "10.1.1.1"


# -- ports and dialects -----------------------------------------------------------------------------------------------


def test_allowed_ports():
    config = cfg(allowed_ports=[5432, "6000-6010"])
    assert go(config, {"profile": "pg", "port": 6005})["port"] == 6005
    for port in (5433, 6011):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, {"profile": "pg", "port": port})
        assert code(exc) == "[E_POLICY]"
    # a host override is checked against the effective (profile) port too
    with pytest.raises(SaltInvocationError):
        go(cfg(allowed_ports=[6000]), {"profile": "pg", "host": "db2.corp.example"})


def test_port_without_allowed_ports_only_needs_to_be_valid():
    assert go(cfg(), {"profile": "pg", "port": 1})["port"] == 1
    for bad in (0, 70000, "5432", True, 5432.0):
        with pytest.raises(SaltInvocationError) as exc:
            go(cfg(), {"profile": "pg", "port": bad})
        assert code(exc) in ("[E_PROFILE_INVALID]", "[E_OPTION_INVALID]")


def test_allowed_dialects_and_mariadb_alias():
    config = cfg(
        allow=["dialect", "host", "port", "database", "username"], allowed_dialects=["mysql"]
    )
    prof = go(config, {"profile": "my", "dialect": "mariadb"})
    assert prof["dialect"] == "mysql" and prof["driver"] == "pymysql"
    prof = go(config, {"profile": "pg", "dialect": "mysql", "port": 3306})
    assert (prof["dialect"], prof["driver"]) == ("mysql", "pymysql")
    config = cfg(
        allow=["dialect", "host", "port", "database", "username"], allowed_dialects=["postgresql"]
    )
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "dialect": "oracle", "port": 1521})
    assert code(exc) == "[E_POLICY]"


def test_allowed_dialects_accepts_mariadb_spelling():
    config = cfg(allow=["dialect"], allowed_dialects=["mariadb"])
    assert go(config, {"profile": "pg", "dialect": "mysql"})["dialect"] == "mysql"


def test_dialect_override_drops_base_driver_but_explicit_driver_must_match():
    config = cfg(allow=["dialect", "driver"])
    config["profiles"]["pg"]["driver"] = "psycopg"
    assert go(config, {"profile": "pg", "dialect": "mysql"})["driver"] == "pymysql"
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "dialect": "mysql", "driver": "psycopg"})
    assert code(exc) == "[E_PROFILE_INVALID]"


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb", "oracle", "mssql"])
def test_every_dialect_can_be_targeted(dialect):
    config = cfg(allow=["dialect", "host", "port", "database", "username"])
    prof = go(config, {"profile": "pg", "dialect": dialect, "port": 1234})
    assert prof["dialect"] == ("mysql" if dialect == "mariadb" else dialect)


def test_bad_dialect_value():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=["dialect"]), {"profile": "pg", "dialect": "db2"})
    assert code(exc) == "[E_PROFILE_INVALID]"
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=["dialect"]), {"profile": "pg", "dialect": 5})
    assert code(exc) == "[E_OPTION_INVALID]"


# -- ad-hoc connections -----------------------------------------------------------------------------------------------

ADHOC = {"dialect": "postgresql", "host": "db2.corp.example", "database": "d", "username": "u"}
ADHOC_ALLOW = ("dialect", "host", "port", "database", "username", "password")


def test_adhoc_is_off_by_default():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=ADHOC_ALLOW), dict(ADHOC))
    assert code(exc) == "[E_POLICY]"
    assert "allow_adhoc_connection" in str(exc.value)


def test_adhoc_connection_builds_a_profile_with_the_default_port():
    prof = go(cfg(allow=ADHOC_ALLOW, allow_adhoc_connection=True), dict(ADHOC))
    assert prof["name"] == overrides.ADHOC_NAME
    assert (prof["host"], prof["port"], prof["database"], prof["username"]) == (
        "db2.corp.example",
        5432,
        "d",
        "u",
    )
    assert prof["allow_execute"] is False and prof["read_only"] is True
    assert prof["error_detail"] is False
    assert "password" not in prof
    assert prof["call_overrides"] == ["database", "dialect", "host", "username"]


@pytest.mark.parametrize(
    "dialect, port",
    [("postgresql", 5432), ("mysql", 3306), ("mariadb", 3306), ("oracle", 1521), ("mssql", 1433)],
)
def test_adhoc_default_ports(dialect, port):
    prof = go(cfg(allow=ADHOC_ALLOW, allow_adhoc_connection=True), dict(ADHOC, dialect=dialect))
    assert prof["port"] == port


def test_adhoc_applies_every_gate():
    config = cfg(
        allow=ADHOC_ALLOW,
        allow_adhoc_connection=True,
        allowed_ports=[5432],
        allowed_dialects=["postgresql"],
    )
    go(config, dict(ADHOC))
    with pytest.raises(SaltInvocationError):  # host
        go(config, dict(ADHOC, host="evil.example"))
    with pytest.raises(SaltInvocationError):  # port
        go(config, dict(ADHOC, port=9999))
    with pytest.raises(SaltInvocationError):  # dialect
        go(config, dict(ADHOC, dialect="mysql"))
    with pytest.raises(
        SaltInvocationError
    ):  # every supplied field must also be in allow_call_params
        go(cfg(allow=("dialect", "host", "database"), allow_adhoc_connection=True), dict(ADHOC))


@pytest.mark.parametrize("missing", ["dialect", "host", "database", "username"])
def test_incomplete_adhoc_connection_is_e_option_invalid(missing):
    kw = dict(ADHOC)
    del kw[missing]
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=ADHOC_ALLOW, allow_adhoc_connection=True), kw)
    assert code(exc) == "[E_OPTION_INVALID]"
    assert "profile" in str(exc.value)


def test_adhoc_with_a_named_profile_is_an_overlay_not_adhoc():
    prof = go(cfg(allow=ADHOC_ALLOW, allow_adhoc_connection=True), dict(ADHOC, profile="pg"))
    assert prof["name"] == "pg"


def test_adhoc_cannot_execute_or_turn_off_read_only():
    prof = go(
        cfg(allow=ADHOC_ALLOW + ("read_only",), allow_adhoc_connection=True),
        dict(ADHOC, read_only=True),
    )
    assert prof["allow_execute"] is False
    with pytest.raises(SaltInvocationError) as exc:
        go(
            cfg(allow=ADHOC_ALLOW + ("read_only",), allow_adhoc_connection=True),
            dict(ADHOC, read_only=False),
        )
    assert code(exc) == "[E_POLICY]"


def test_default_profile_is_the_base_when_no_profile_is_given():
    config = cfg()
    config["default_profile"] = "pg_rw"
    assert go(config, {})["name"] == "pg_rw"
    assert go(config, {"database": "z"})["database"] == "z"
    assert go(config, {"profile": "pg"})["name"] == "pg"
    config["default_profile"] = "nope"
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {})
    assert code(exc) == "[E_PROFILE_UNKNOWN]"
    config["default_profile"] = 5
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {})
    assert code(exc) == "[E_OPTION_INVALID]"


def test_default_profile_beats_adhoc():
    config = cfg(allow=ADHOC_ALLOW, allow_adhoc_connection=True)
    config["default_profile"] = "pg"
    assert go(config, dict(ADHOC))["name"] == "pg"


# -- passwords --------------------------------------------------------------------------------------------------------


def test_literal_password_from_a_call_is_e_policy_naming_the_key_only():
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(), {"profile": "pg", "password": LITERAL})
    assert code(exc) == "[E_POLICY]"
    assert "password" in str(exc.value)
    assert LITERAL not in str(exc.value)


def test_sdb_password_from_a_call_is_accepted_and_marked():
    prof = go(cfg(), {"profile": "pg", "password": SDB_REF})
    assert prof["password"] == SDB_REF
    assert prof["password_from_call"] is True
    assert prof["call_overrides"] == ["password"]


def test_literal_password_accepted_only_with_the_policy_flag():
    prof = go(cfg(allow_call_literal_password=True), {"profile": "pg", "password": LITERAL})
    assert prof["password"] == LITERAL and prof["password_from_call"] is True


@pytest.mark.parametrize("bad", ["", "sdb://", 5, ["x"], True])
def test_bad_password_values(bad):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow_call_literal_password=True), {"profile": "pg", "password": bad})
    assert code(exc) in ("[E_OPTION_INVALID]", "[E_PROFILE_INVALID]")


def test_profile_password_is_not_sent_to_a_retargeted_connection():
    for field, value in (("host", "db2.corp.example"), ("port", 5433), ("username", "other")):
        prof = go(cfg(), {"profile": "pg", field: value})
        assert "password" not in prof, field
        assert prof["password_from_call"] is False


def test_profile_password_survives_changes_that_do_not_retarget():
    assert go(cfg(), {"profile": "pg", "database": "z"})["password"] == "sdb://vault/base_pw"
    # the same host/port/user as the profile is not a retarget
    prof = go(cfg(), {"profile": "pg", "host": "db1.corp.example", "port": 5432, "username": "ro"})
    assert prof["password"] == "sdb://vault/base_pw"


def test_inherit_password_policy_keeps_it():
    prof = go(cfg(inherit_password=True), {"profile": "pg", "host": "db2.corp.example"})
    assert prof["password"] == "sdb://vault/base_pw"


def test_retarget_with_its_own_password_uses_that_one():
    prof = go(cfg(), {"profile": "pg", "host": "db2.corp.example", "password": SDB_REF})
    assert prof["password"] == SDB_REF


# -- caps and read-only -----------------------------------------------------------------------------------------------

CAP_ALLOW = ("statement_timeout_ms", "connect_timeout_s", "read_only")


def test_timeouts_can_only_be_lowered():
    config = cfg(allow=CAP_ALLOW)
    prof = go(config, {"profile": "pg", "statement_timeout_ms": 5000, "connect_timeout_s": 2})
    assert (prof["statement_timeout_ms"], prof["connect_timeout_s"]) == (5000, 2)
    prof = go(config, {"profile": "pg", "statement_timeout_ms": 10**9, "connect_timeout_s": 10**6})
    assert (prof["statement_timeout_ms"], prof["connect_timeout_s"]) == (20000, 5)
    # a profile without values uses the defaults as the ceiling
    prof = go(config, {"profile": "pg_rw", "statement_timeout_ms": 10**9, "connect_timeout_s": 999})
    assert (prof["statement_timeout_ms"], prof["connect_timeout_s"]) == (30000, 10)


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5])
def test_timeout_values_are_validated(bad):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=CAP_ALLOW), {"profile": "pg", "statement_timeout_ms": bad})
    assert code(exc) == "[E_OPTION_INVALID]"


def test_read_only_can_be_turned_on_never_off():
    config = cfg(allow=CAP_ALLOW)
    assert go(config, {"profile": "pg_rw", "read_only": True})["read_only"] is True
    for profile in ("pg", "pg_rw"):
        with pytest.raises(SaltInvocationError) as exc:
            go(config, {"profile": profile, "read_only": False})
        assert code(exc) == "[E_POLICY]"
        assert "read_only" in str(exc.value)
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "read_only": "no"})
    assert code(exc) == "[E_OPTION_INVALID]"


def test_allow_execute_cannot_come_from_a_call_and_stays_the_profiles():
    assert go(cfg(), {"profile": "pg"})["allow_execute"] is False
    assert go(cfg(), {"profile": "pg_rw", "database": "z"})["allow_execute"] is True
    for where in ({"allow_execute": True}, {"connection": {"allow_execute": True}}):
        with pytest.raises(SaltInvocationError) as exc:
            go(cfg(), dict(where, profile="pg"))
        assert code(exc) == "[E_OPTION_UNKNOWN]"


# -- query / connect_args / ssl ---------------------------------------------------------------------------------------


def test_query_and_connect_args_are_merged_over_the_profile_never_replacing_it():
    config = cfg(allow=["query", "connect_args"])
    prof = go(
        config,
        {"profile": "pgq", "query": {"keepalives": 1}, "connect_args": {"keepalives_idle": 5}},
    )
    assert prof["query"] == {"application_name": "base", "keepalives": 1}
    assert prof["connect_args"] == {"keepalives_idle": 5}
    prof = go(config, {"profile": "pgq", "query": {"application_name": "mine"}})
    assert prof["query"] == {"application_name": "mine"}


@pytest.mark.parametrize(
    "dialect, section, key",
    [
        ("postgresql", "query", "sslmode"),
        ("postgresql", "query", "sslrootcert"),
        ("postgresql", "connect_args", "sslkey"),
        ("mysql", "connect_args", "ssl_disabled"),
        ("mysql", "connect_args", "ssl_verify_cert"),
        ("mysql", "query", "ssl_ca"),
        ("oracle", "connect_args", "ssl_server_dn_match"),
        ("oracle", "connect_args", "wallet_location"),
        ("mysql", "connect_args", "ssl_verify_identity"),
    ],
)
def test_tls_owned_keys_from_a_call_are_refused(dialect, section, key):
    config = cfg(allow=["query", "connect_args", "dialect", "port"])
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "dialect": dialect, "port": 1234, section: {key: "zzz-value"}})
    assert code(exc) == "[E_POLICY]"
    assert key in str(exc.value)
    assert "zzz-value" not in str(exc.value)


def test_non_allowlisted_driver_args_from_a_call_are_refused_by_the_adapter():
    prof = go(
        cfg(allow=["query", "connect_args"]), {"profile": "pg", "query": {"options": "-c x=1"}}
    )
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter("postgresql").url(prof, "pw")
    assert code(exc) == "[E_POLICY]"
    prof = go(
        cfg(allow=["query", "connect_args"]),
        {"profile": "pg", "connect_args": {"ssl_context": "x"}},
    )
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter("postgresql").connect_args(prof)
    assert code(exc) == "[E_POLICY]"


@pytest.mark.parametrize(
    "field, bad", [("query", "x"), ("connect_args", ["a"]), ("ssl_files", "x")]
)
def test_mapping_fields_must_be_objects(field, bad):
    with pytest.raises(SaltInvocationError) as exc:
        go(cfg(allow=[field]), {"profile": "pg", field: bad})
    assert code(exc) == "[E_OPTION_INVALID]"


@pytest.fixture
def pki(tmp_path):
    root = tmp_path / "certs"
    for name in ("good", "good2"):
        d = root / name
        d.mkdir(parents=True)
        (d / "ca.pem").write_text("not read")
    return root


def tls_cfg(root, **kw):
    config = cfg(allow=["ssl_certs_dir", "ssl_mode", "ssl_files", "query", "connect_args"], **kw)
    config["_policy"]["ssl_certs_dir_root"] = str(root)
    config["profiles"]["tls_full"] = dict(BASE_PROFILES["pg"], ssl_certs_dir=str(root / "good"))
    config["profiles"]["tls_ca"] = dict(
        BASE_PROFILES["pg"], ssl_certs_dir=str(root / "good"), ssl_mode="verify-ca"
    )
    return config


def test_call_can_add_tls_to_a_plain_profile(pki):
    prof = go(tls_cfg(pki), {"profile": "pg", "ssl_certs_dir": str(pki / "good")})
    assert prof["tls"]["mode"] == "verify-full"
    prof = go(
        tls_cfg(pki), {"profile": "pg", "ssl_certs_dir": str(pki / "good"), "ssl_mode": "verify-ca"}
    )
    assert prof["tls"]["mode"] == "verify-ca"


def test_ssl_mode_may_only_tighten(pki):
    config = tls_cfg(pki)
    assert (
        go(config, {"profile": "tls_ca", "ssl_mode": "verify-full"})["tls"]["mode"] == "verify-full"
    )
    assert go(config, {"profile": "tls_ca", "ssl_mode": "verify-ca"})["tls"]["mode"] == "verify-ca"
    assert (
        go(config, {"profile": "tls_full", "ssl_mode": "verify-full"})["tls"]["mode"]
        == "verify-full"
    )
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "tls_full", "ssl_mode": "verify-ca"})
    assert code(exc) == "[E_POLICY]"
    assert "ssl_mode" in str(exc.value)


@pytest.mark.parametrize("mode", ["disable", "prefer", "allow", "require", "off", 5])
def test_weak_ssl_modes_are_refused(pki, mode):
    with pytest.raises(SaltInvocationError) as exc:
        go(tls_cfg(pki), {"profile": "tls_ca", "ssl_mode": mode})
    assert code(exc) in ("[E_POLICY]", "[E_PROFILE_INVALID]", "[E_OPTION_INVALID]")


def test_ssl_mode_without_a_directory_is_refused(pki):
    with pytest.raises(SaltInvocationError) as exc:
        go(tls_cfg(pki), {"profile": "pg", "ssl_mode": "verify-full"})
    assert code(exc) == "[E_PROFILE_INVALID]"


def test_ssl_certs_dir_from_a_call_is_checked_by_the_tls_root_policy(pki, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ca.pem").write_text("x")
    with pytest.raises(SaltInvocationError) as exc:
        go(tls_cfg(pki), {"profile": "pg", "ssl_certs_dir": str(outside)})
    assert code(exc) == "[E_POLICY]"
    with pytest.raises(SaltInvocationError) as exc:
        go(
            tls_cfg(pki),
            {"profile": "pg", "ssl_certs_dir": str(pki / "good" / ".." / ".." / "outside")},
        )
    assert code(exc) == "[E_PROFILE_INVALID]"
    config = tls_cfg(pki)
    del config["_policy"]["ssl_certs_dir_root"]
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "ssl_certs_dir": str(pki / "good")})
    assert code(exc) == "[E_POLICY]"


def test_tls_profile_with_query_sslmode_conflict_stays_refused(pki):
    config = tls_cfg(pki)
    config["profiles"]["pg"]["query"] = {"sslmode": "verify-full"}
    with pytest.raises(SaltInvocationError) as exc:
        go(config, {"profile": "pg", "ssl_certs_dir": str(pki / "good")})
    assert code(exc) == "[E_PROFILE_INVALID]"


def test_ssl_files_are_merged_and_validated(pki):
    (pki / "good" / "other.pem").write_text("x")
    prof = go(
        tls_cfg(pki),
        {"profile": "pg", "ssl_certs_dir": str(pki / "good"), "ssl_files": {"ca": "other.pem"}},
    )
    assert prof["tls"]["ca"].endswith("other.pem")
    with pytest.raises(SaltInvocationError) as exc:
        go(
            tls_cfg(pki),
            {"profile": "pg", "ssl_certs_dir": str(pki / "good"), "ssl_files": {"ca": "../x"}},
        )
    assert code(exc) == "[E_PROFILE_INVALID]"


# -- secrets ----------------------------------------------------------------------------------------------------------


def test_resolve_secrets_for_a_profile_password_delegates_to_secrets():
    prof = go(cfg(), {"profile": "pg"})
    out = overrides.resolve_secrets(prof, lambda ref: "resolved-pw")
    assert out["password"] == "resolved-pw"


def test_resolve_secrets_call_password_success():
    prof = go(cfg(), {"profile": "pg", "password": SDB_REF})
    out = overrides.resolve_secrets(prof, {SDB_REF: "resolved-pw"}.__getitem__)
    assert out["password"] == "resolved-pw"


def test_resolve_secrets_call_password_never_shows_the_reference():
    prof = go(cfg(), {"profile": "pg", "password": SDB_REF})

    def boom(ref):
        raise KeyError(ref)

    for getter in (boom, lambda ref: None, lambda ref: 5, lambda ref: ref):
        with pytest.raises(CommandExecutionError) as exc:
            overrides.resolve_secrets(prof, getter)
        text = str(exc.value)
        assert text.startswith("[E_SECRET]")
        assert "SECRETREF" not in text and "sdb://" not in text.replace("sdb://<call-supplied>", "")


def test_resolve_secrets_literal_call_password_passes_through():
    prof = go(cfg(allow_call_literal_password=True), {"profile": "pg", "password": LITERAL})
    assert overrides.resolve_secrets(prof, lambda ref: 1 / 0)["password"] == LITERAL


def test_resolve_secrets_without_password():
    prof = go(cfg(), {"profile": "pg", "host": "db2.corp.example"})
    assert "password" not in overrides.resolve_secrets(prof, lambda ref: 1 / 0)


# -- nothing secret in any message -------------------------------------------------------------------------------------

LEAK_CASES = [
    # (policy overrides, call kwargs): every one raises; none may echo the password, the sdb reference or host values
    ({}, {"profile": "pg", "password": LITERAL}),
    ({"allow": ["password"]}, {"profile": "pg", "password": LITERAL, "host": "evil.example"}),
    (
        {"allow": ["host", "password"]},
        {"profile": "pg", "password": SDB_REF, "host": "evil.example"},
    ),
    (
        {"allow": ["host", "password"]},
        {"profile": "pg", "password": SDB_REF, "host": "meta.corp.example"},
    ),
    (
        {"allow": ["host", "password"]},
        {"profile": "pg", "password": SDB_REF, "host": "gone.corp.example"},
    ),
    ({"allow": ["host", "password"]}, {"profile": "pg", "password": SDB_REF, "host": "a,b"}),
    (
        {"allow": ["password", "port"], "allowed_ports": [1]},
        {"profile": "pg", "password": SDB_REF, "port": 2},
    ),
    ({"allow": ["password", "port"]}, {"profile": "pg", "password": SDB_REF, "port": 99999}),
    (
        {"allow": ["password", "read_only"]},
        {"profile": "pg", "password": SDB_REF, "read_only": False},
    ),
    (
        {"allow": ["password", "database"]},
        {"profile": "pg", "password": SDB_REF, "connection": {"database": "x"}, "database": "y"},
    ),
    (
        {"allow": ["password"]},
        {"profile": "pg", "password": SDB_REF, "connection": {"password": SDB_REF}},
    ),
    ({"allow": ["password"]}, {"profile": "pg", "password": SDB_REF, "connection": {"typo": 1}}),
    ({"allow": ["password"]}, {"profile": "pg", "password": SDB_REF, "typo": 1}),
    ({"allow": ["database"]}, {"profile": "pg", "password": SDB_REF}),
    ({"allow": ["password"]}, {"profile": "nope", "password": SDB_REF}),
    ({"allow": ["password"]}, {"profile": "pg", "password": SDB_REF, "connection": "x"}),
    (
        {"allow": ["password", "dialect"], "allowed_dialects": ["mysql"]},
        {"profile": "pg", "password": SDB_REF, "dialect": "oracle"},
    ),
    (
        {"allow": ["password", "query"]},
        {"profile": "pg", "password": SDB_REF, "query": {"sslmode": "disable"}},
    ),
    ({"allow": ["password"], "allow_adhoc_connection": False}, dict(ADHOC, password=SDB_REF)),
    (
        {"allow": ["password", "host"]},
        {"profile": "pg", "password": SDB_REF, "host": LITERAL + ".evil.example"},
    ),
]


@pytest.mark.parametrize("policy, kwargs", LEAK_CASES)
def test_no_secret_or_host_value_in_any_error(policy, kwargs):
    allow = policy.pop("allow", None)
    config = cfg(allow=allow if allow is not None else ("host", "password"), **policy)
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        go(config, kwargs)
    text = str(exc.value)
    assert text.startswith("[E_")
    for forbidden in (
        LITERAL,
        "SECRETREF",
        "sdb://vault",
        "evil.example",
        "meta.corp.example",
        "gone.corp.example",
    ):
        assert forbidden not in text, (forbidden, text)
    # also not through the chain
    assert exc.value.__cause__ is None


def test_profile_extras_never_contain_the_password_value():
    prof = go(
        cfg(allow_call_literal_password=True),
        {"profile": "pg", "password": LITERAL, "database": "z"},
    )
    assert LITERAL not in repr(prof["call_overrides"])
    assert prof["call_overrides"] == ["database", "password"]


# -- target for the audit line ----------------------------------------------------------------------------------------


def test_target_is_host_and_port():
    prof = go(cfg(), {"profile": "pg", "host": "db2.corp.example", "port": 6000})
    assert overrides.target(prof) == "db2.corp.example:6000"
    assert overrides.target({"host": "::1", "port": 5}) == "[::1]:5"
    assert overrides.target({"host": "h\n evil", "port": 5}) == "h__evil:5"


def test_overridden_names_helper():
    assert overrides.overridden({"call_overrides": ["a", "b"]}) == "a,b"
    assert overrides.overridden({}) == "-"
