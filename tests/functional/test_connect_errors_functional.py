"""Connect failures through the module (``sqlx.ping``) against REAL servers (docs/WIRE_SHAPES.md section 13).

Every failure that happens before a session exists must carry the driver's real message and the right code, never
``[E_INTERNAL] OperationalError``, and never the password. PostgreSQL 54329, MySQL 33061, MariaDB 33062
(``tests-support/env.sh``). ``__salt__`` is injected as in the other functional module tests. The ``pg_hba.conf``
cases are not run here (they need a change of the server configuration); they are unit tests built from the messages
recorded in WIRE_SHAPES section 13.
"""

# pylint: disable=redefined-outer-name,unused-argument
import contextlib
import socket
import threading

import pytest
from salt.exceptions import CommandExecutionError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_runner_functional import DIALECTS
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = [pytest.mark.pg, pytest.mark.mysql, pytest.mark.mariadb]

PASSWORD = "Zq-never-in-a-message-91"  # a distinctive wrong password: it must not appear anywhere
REF = "sdb://vault/ro"


@pytest.fixture(scope="module", params=DIALECTS)
def server(request):
    srv, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    return srv


@pytest.fixture
def cfg(server, monkeypatch):
    base = {
        "dialect": server.dialect,
        "host": HOST,
        "port": server.cfg["port"],
        "database": server.cfg["database"],
        "username": "ro",
        "connect_timeout_s": 1,
    }
    config = {
        "profiles": {
            "nopw": dict(base),  # no password key at all: the server wants one
            "wrongpw": dict(base, password=PASSWORD),
            "good": dict(base, password="ro_pw"),
            "dead": dict(base, password=PASSWORD, port=1),
            # a profile host is not resolved by the call-parameter policy, so the name reaches the driver
            "nohost": dict(base, password=PASSWORD, host="no-such-host.invalid"),
        },
        "_policy": {
            "allow_call_params": ["host", "port", "username", "password"],
            "allowed_hosts": ["127.0.0.1", "10.255.255.1", "no-such-host.invalid"],
        },
    }
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": {REF: "ro_pw"}.__getitem__,
        },
        raising=False,
    )
    return config


def fail(**kw):
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(**kw)
    text = str(exc.value)
    assert PASSWORD not in text and "ro_pw" not in text and REF not in text, text
    assert "E_INTERNAL" not in text, text
    return text


def test_baseline_good_profile_connects(cfg, server):
    assert mod.ping(profile="good")["ok"] is True


def test_no_password_to_a_server_that_requires_one(cfg, server):
    text = fail(profile="nopw")
    assert text.startswith("[E_AUTH_FAILED]")
    assert ("no password supplied" if server.key == "postgresql" else "using password: NO") in text


def test_password_withheld_after_the_call_retargets(cfg, server):
    # The profile's password is not sent to a different port (SPEC 3.2); the real "no password" error comes back.
    cfg["profiles"]["moved"] = dict(cfg["profiles"]["good"], port=1)
    text = fail(profile="moved", port=server.cfg["port"])
    assert text.startswith("[E_AUTH_FAILED]")
    assert ("no password supplied" if server.key == "postgresql" else "using password: NO") in text


def test_wrong_password(cfg, server):
    text = fail(profile="wrongpw")
    assert text.startswith("[E_AUTH_FAILED]")
    assert ("authentication failed" if server.key == "postgresql" else "Access denied") in text


def test_connect_timeout_is_a_connect_error(cfg, server):
    text = fail(profile="wrongpw", host="10.255.255.1")
    assert text.startswith("[E_CONNECT]"), text
    assert ("timeout expired" if server.key == "postgresql" else "timed out") in text


def test_port_closed(cfg, server):
    text = fail(profile="dead")
    assert text.startswith("[E_CONNECT]")
    assert "Connection refused" in text


def test_unknown_host_name(cfg, server):
    text = fail(profile="nohost")
    assert text.startswith("[E_CONNECT]")
    assert "Name or service not known" in text or "resolve" in text or "getaddrinfo" in text


@contextlib.contextmanager
def hangs_up():
    sock = socket.socket()
    sock.bind((HOST, 0))
    sock.listen(5)

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()
        thread.join(timeout=2)


def test_server_closes_the_connection(cfg, server):
    with hangs_up() as port:
        text = fail(profile="wrongpw", port=port)
    assert text.startswith("[E_CONNECT]")
    assert "closed the connection" in text or "Lost connection" in text or "Connection reset" in text
