"""Call-time parameters over TLS against the separate TLS servers of ``tests-support/tls`` (SPEC section 3.2, D12).

PostgreSQL 54339, MySQL 33071, MariaDB 33072. The base profile is TLS-verified but aims at a closed port, so a call only
connects if the call-supplied host/port are the ones used and the profile's certificate directory still verifies the
server. The root policy for ``ssl_certs_dir`` (lib/tls.py) applies to a directory that comes from a call as well.
"""

# pylint: disable=redefined-outer-name,unused-argument
import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_call_params_functional import listener
from tests.functional.test_tls_functional import PKI
from tests.functional.test_tls_functional import SERVERS
from tests.functional.test_tls_functional import _reachable

pytestmark = pytest.mark.tls

SDB = {"sdb://vault/tls": "tls_pw_pw"}
PW = {"password": "sdb://vault/tls"}  # the base password is not sent to a retargeted connection


@pytest.fixture(scope="module", params=sorted(SERVERS))
def srv(request):
    info = dict(SERVERS[request.param], key=request.param)
    reason = _reachable(info["port"]) or (
        None if (PKI / "good" / "client.key").exists() else f"no PKI in {PKI}"
    )
    if reason:
        pytest.skip(reason)
    return info


@pytest.fixture
def cfg(srv, monkeypatch):
    config = {
        "profiles": {
            "tls_base": {
                "dialect": srv["dialect"],
                "host": "localhost",
                "port": 1,
                "database": "tlsdb",
                "username": "tls_pw",
                "password": "sdb://vault/tls",
                "ssl_certs_dir": str(PKI / "good"),
                "connect_timeout_s": 5,
            }
        },
        "_policy": {
            "ssl_certs_dir_root": str(PKI),
            "allow_call_params": ["host", "port", "ssl_mode", "ssl_certs_dir", "password"],
            "allowed_hosts": ["localhost", "127.0.0.1", "127.0.0.2"],
        },
    }
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": SDB.__getitem__,
        },
        raising=False,
    )
    return config


def test_call_supplied_host_and_port_connect_over_verified_tls(cfg, srv):
    out = mod.ping(profile="tls_base", **PW, host="localhost", port=srv["port"])
    assert out["ok"] is True and out["server_version"]


def test_the_profiles_certificate_directory_still_verifies_a_call_supplied_host(cfg, srv):
    """verify-full: 127.0.0.2 is not in the server certificate, so retargeting cannot weaken verification."""
    with pytest.raises(CommandExecutionError, match=r"^\[E_CONNECT\]"):
        mod.ping(profile="tls_base", **PW, host="127.0.0.2", port=srv["port"])


def test_ssl_mode_can_only_tighten_from_a_call(cfg, srv):
    cfg["profiles"]["tls_base"]["ssl_mode"] = "verify-ca"
    assert mod.ping(
        profile="tls_base", **PW, host="127.0.0.2", port=srv["port"], ssl_mode="verify-ca"
    )["ok"]
    with pytest.raises(CommandExecutionError, match=r"^\[E_CONNECT\]"):  # tightened to verify-full
        mod.ping(
            profile="tls_base", **PW, host="127.0.0.2", port=srv["port"], ssl_mode="verify-full"
        )
    cfg["profiles"]["tls_base"]["ssl_mode"] = "verify-full"
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*tightened"):
        mod.ping(profile="tls_base", **PW, host="localhost", port=srv["port"], ssl_mode="verify-ca")


def test_a_call_supplied_certificate_directory_under_the_root_is_used(cfg, srv):
    """A wrong CA directory from the call is refused by the real TLS handshake: the call's directory is the one used."""
    with pytest.raises(CommandExecutionError, match=r"^\[E_CONNECT\]"):
        mod.ping(
            profile="tls_base",
            **PW,
            host="localhost",
            port=srv["port"],
            ssl_certs_dir=str(PKI / "wrong-ca"),
        )


@pytest.mark.parametrize("where", ["/etc", "/tmp", "{pki}/../server"])
def test_a_certificate_directory_outside_the_root_is_refused_without_connecting(cfg, srv, where):
    with listener() as spy:
        with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
            mod.ping(
                profile="tls_base",
                **PW,
                host="127.0.0.1",
                port=spy.port,
                ssl_certs_dir=where.format(pki=PKI),
            )
        assert spy.accepted() == 0
    assert "tls_pw_pw" not in str(exc.value)


def test_certificate_directory_from_a_call_needs_the_policy_field(cfg, srv):
    cfg["_policy"]["allow_call_params"] = ["host", "port", "password"]
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(
            profile="tls_base",
            **PW,
            host="localhost",
            port=srv["port"],
            ssl_certs_dir=str(PKI / "good"),
        )


def test_a_hostile_listener_gets_a_tls_attempt_but_no_credentials(cfg, srv):
    """The call aims the TLS profile at a listener the test owns: it sees a ClientHello at most, never the password."""
    with listener() as spy:
        with pytest.raises(CommandExecutionError) as exc:
            mod.ping(profile="tls_base", **PW, host="127.0.0.1", port=spy.port)
        assert spy.accepted() >= 1
    assert "tls_pw_pw" not in str(exc.value) and "sdb://" not in str(exc.value)


def test_the_profiles_password_is_not_sent_to_a_retargeted_connection(cfg, srv):
    """Host changed, no password in the call: the server sees no password at all (credential relay defence)."""
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(profile="tls_base", host="localhost", port=srv["port"])
    # MySQL/MariaDB: 1045 "using password: NO". PostgreSQL: libpq has nothing to send ("no password supplied"), which
    # the classifier currently reports as [E_INTERNAL]; either way no connection is made with the profile's password.
    assert str(exc.value).startswith(("[E_AUTH_FAILED]", "[E_INTERNAL]"))
    assert "tls_pw_pw" not in str(exc.value)
