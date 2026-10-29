# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with,redefined-outer-name
"""SECURITY section 3 (call-time parameters): SSRF, credential relay, and secrets on every error path.

The feature is policy-gated (``sqlx:_policy:allow_call_params``). These tests run it ENABLED and attack it:

* SSRF: a host the policy does not list, a name that resolves to a forbidden address, the cloud metadata address, an
  IPv6 form and a decimal/hex spelling of loopback never get a TCP connection (a counting listener and a patched
  ``socket.connect`` prove it).
* Credential relay: a fake PostgreSQL server the test owns demands a cleartext password. The profile's own password
  must never reach it; a password from the call (an ``sdb://`` reference) reaches it only because the caller asked.
* Secrets: a literal password, an ``sdb://`` reference and the resolved value never appear in any exception text,
  chain, traceback or log record, on every error path of the new surface.
"""

import logging
import socket
import struct
import threading

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.security.test_secret_leak import surfaces

HOST = "127.0.0.1"
BASE_PW = "BASE-PROFILE-PW-7c1d"  # the profile's own password: must not follow a retargeted call
SDB_REF = "sdb://vault/callpw"
SDB_VALUE = "SDB-RESOLVED-PW-93ab"
LITERAL = "LITERAL-CALL-PW-55e0"
SECRETS = (BASE_PW, SDB_REF, SDB_VALUE, LITERAL)


@pytest.fixture
def cfg(monkeypatch):
    config = {
        "profiles": {
            "base": {
                "dialect": "postgresql",
                "host": HOST,
                "port": 1,
                "database": "d",
                "username": "u",
                "password": BASE_PW,
                "connect_timeout_s": 3,
            }
        },
        "default_profile": "base",
        "_policy": {
            "allow_call_params": ["host", "port", "password", "username", "database", "read_only"],
            "allowed_hosts": ["127.0.0.1", "db*.corp.example"],
        },
    }
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": {SDB_REF: SDB_VALUE}.__getitem__,
        },
        raising=False,
    )
    return config


@pytest.fixture
def connects(monkeypatch):
    """Every address a socket is asked to connect to."""
    seen = []
    real = socket.socket.connect

    def spy(self, address):
        seen.append(address)
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    return seen


class FakePostgres:
    """Accepts, declines TLS, asks for a cleartext password and records everything the client sends."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind((HOST, 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.accepted = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _recv(self, conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.settimeout(3)
            try:
                length, code = struct.unpack("!II", self._recv(conn, 8))
                self.received.append(code.to_bytes(4, "big"))
                if code == 80877103:  # SSLRequest: decline, expect the real startup next
                    conn.sendall(b"N")
                    length, code = struct.unpack("!II", self._recv(conn, 8))
                self.received.append(self._recv(conn, length - 8))
                conn.sendall(b"R" + struct.pack("!II", 8, 3))  # AuthenticationCleartextPassword
                kind = self._recv(conn, 1)
                size = struct.unpack("!I", self._recv(conn, 4))[0]
                self.received.append(kind + self._recv(conn, size - 4))
                conn.sendall(b"E" + struct.pack("!I", 4 + 8) + b"SFATAL\x00\x00")
            except (OSError, struct.error):
                pass
            finally:
                conn.close()

    def seen(self):
        return b"".join(self.received)

    def close(self):
        self.sock.close()
        self.thread.join(timeout=2)


@pytest.fixture
def fake():
    server = FakePostgres()
    try:
        yield server
    finally:
        server.close()


# -- SSRF -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "192.0.2.1",
        "169.254.169.254",
        "169.254.169.254.",
        "[::1]",
        "::1",
        "0x7f000001",
        "2130706433",
        "0177.0.0.1",
        "127.1",
        "localhost",
        "db1.corp.example.evil.example",
        "db1.corp.example\x00.evil",
        "127.0.0.1@evil.example",
        "evil.example#127.0.0.1",
        " 127.0.0.1",
        "",
    ],
)
@pytest.mark.parametrize("function", ["ping", "query", "table_list"])
def test_a_host_outside_the_policy_never_gets_a_connection(cfg, connects, function, host):
    cfg["_policy"]["allowed_hosts"] = ["db*.corp.example", "10.0.0.0/8"]  # no loopback entry
    call = {"ping": {}, "query": {"sql": "select 1"}, "table_list": {}}[function]
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        getattr(mod, function)(profile="base", host=host, **call)
    assert str(exc.value).startswith(("[E_POLICY]", "[E_OPTION_INVALID]")), str(exc.value)
    assert connects == []
    assert all(s not in str(exc.value) for s in SECRETS)


def test_a_name_that_resolves_to_a_forbidden_address_never_gets_a_connection(
    cfg, connects, monkeypatch
):
    """DNS rebinding shape: an allowed name that resolves to the metadata address (or a mix) is refused."""
    real = socket.getaddrinfo

    def fake_resolver(name, *args, **kwargs):
        if name == "db1.corp.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 5432))]
        return real(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(profile="base", host="db1.corp.example")
    assert connects == []

    def mixed(name, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5432)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 5432)),
        ]

    cfg["_policy"]["allowed_hosts"] = ["db1.corp.example"]
    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(profile="base", host="db1.corp.example")
    assert connects == []


def test_an_unresolvable_allowed_name_is_a_policy_error_not_a_connection(
    cfg, connects, monkeypatch
):
    def boom(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    cfg["_policy"]["allowed_hosts"] = ["db1.corp.example"]
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        mod.ping(profile="base", host="db1.corp.example")
    assert connects == []
    assert all(s not in str(exc.value) for s in SECRETS)


def test_the_feature_is_off_by_default_even_with_a_hosts_list(cfg, connects):
    cfg["_policy"].pop("allow_call_params")
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.ping(profile="base", host=HOST)
    assert connects == []


# -- credential relay -------------------------------------------------------------------------------------------------


def test_the_profiles_password_does_not_follow_a_retargeted_call(cfg, fake):
    """The fake server asks for a cleartext password; the profile's password must not be offered to it."""
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port)
    assert fake.accepted >= 1
    assert BASE_PW.encode() not in fake.seen()


def test_retargeting_only_the_database_keeps_the_profiles_password(cfg, fake):
    """Control: the target (host, port, user) is unchanged, so the profile's own credentials are used as designed."""
    cfg["profiles"]["base"]["port"] = fake.port
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", database="other")
    assert BASE_PW.encode() in fake.seen()


def test_inherit_password_policy_is_the_only_way_the_profiles_password_follows(cfg, fake):
    cfg["_policy"]["inherit_password"] = True
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port)
    assert BASE_PW.encode() in fake.seen()  # the operator opted in; documented as dangerous


def test_a_password_the_caller_names_reaches_the_server_the_caller_chose(cfg, fake):
    """Control: an sdb reference is resolved on the minion and used for the target the caller chose."""
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port, password=SDB_REF)
    assert SDB_VALUE.encode() in fake.seen()
    assert BASE_PW.encode() not in fake.seen()


def test_a_relay_attempt_with_a_listed_host_but_other_user_does_not_carry_the_password(cfg, fake):
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", username="postgres", host=HOST, port=fake.port)
    assert BASE_PW.encode() not in fake.seen()


# -- secrets on every error path --------------------------------------------------------------------------------------

ERROR_PATHS = {
    "literal-password-refused": dict(password=LITERAL),
    "sdb-unresolvable": dict(password="sdb://vault/missing", host=HOST, port=1),
    "host-not-allowed": dict(password=SDB_REF, host="evil.example"),
    "metadata-host": dict(password=SDB_REF, host="169.254.169.254"),
    "field-not-allowed": dict(password=SDB_REF, ssl_mode="verify-ca"),
    "read-only-off": dict(password=SDB_REF, read_only=False),
    "read-only-string": dict(password=SDB_REF, read_only="false"),
    "both-flat-and-connection": dict(password=SDB_REF, connection={"password": LITERAL}),
    "bad-connection-type": dict(password=SDB_REF, connection="host=evil"),
    "unknown-connection-key": dict(connection={"password": SDB_REF, "allow_execute": True}),
    "bad-port": dict(password=SDB_REF, port="5432"),
    "connect-refused": dict(password=SDB_REF, host=HOST, port=1),
    "literal-in-connection": dict(connection={"password": LITERAL}),
}


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
@pytest.mark.parametrize("function", ["ping", "query", "execute"])
def test_no_secret_on_any_error_path(cfg, caplog, function, path):
    extra = {"sql": "select 1"} if function in ("query", "execute") else {}
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
            getattr(mod, function)(profile="base", **extra, **ERROR_PATHS[path])
    for text in surfaces(exc.value, caplog):
        for secret in SECRETS:
            assert secret not in text, (path, function, secret)


def test_the_audit_line_names_override_keys_and_the_target_only(cfg, caplog, fake):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises((CommandExecutionError, SaltInvocationError)):
            mod.ping(profile="base", host=HOST, port=fake.port, password=SDB_REF)
    text = "\n".join(r.getMessage() + repr(vars(r)) for r in caplog.records)
    for secret in SECRETS:
        assert secret not in text, secret


def test_the_minions_own_libpq_environment_is_not_offered_to_a_retargeted_target(
    cfg, fake, monkeypatch, tmp_path
):
    """$PGPASSWORD and ~/.pgpass would otherwise answer the password request of a server the caller chose."""
    passfile = tmp_path / "pgpass"
    passfile.write_text(f"{HOST}:*:*:*:PGPASSFILE-PW-8d2f\n")
    passfile.chmod(0o600)
    monkeypatch.setenv("PGPASSWORD", "PGPASSWORD-ENV-PW-61ce")
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port)
    assert fake.accepted >= 1
    for leaked in (b"PGPASSWORD-ENV-PW-61ce", b"PGPASSFILE-PW-8d2f", BASE_PW.encode()):
        assert leaked not in fake.seen()


def test_control_the_environment_is_used_when_the_target_is_not_retargeted(
    cfg, fake, monkeypatch
):
    """The guard is limited to retargeted calls: a profile without a password keeps libpq's normal behaviour."""
    cfg["profiles"]["base"].pop("password")
    cfg["profiles"]["base"]["port"] = fake.port
    monkeypatch.setenv("PGPASSWORD", "PGPASSWORD-ENV-PW-61ce")
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", database="other")
    assert b"PGPASSWORD-ENV-PW-61ce" in fake.seen()
