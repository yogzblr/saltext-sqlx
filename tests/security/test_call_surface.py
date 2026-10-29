# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, box 5: call arguments cannot set host / port / driver / URL / connect_args / credentials.

Every public function is called with every connection-ish keyword, with profile-shaped names, with ``__``-prefixed
keys, and with ``host`` inside ``params`` (a bind value, nothing more). Everything that is not a documented option is
refused with ``[E_OPTION_UNKNOWN]`` before any profile is loaded or any socket is opened (a counting socket proves it).
"""

import socket

import pytest
from salt.exceptions import SaltInvocationError

from tests.security import harness
from tests.security.harness import observe

CONNECTION_KEYWORDS = [
    "host",
    "hostname",
    "port",
    "driver",
    "dialect",
    "url",
    "uri",
    "dsn",
    "connection_string",
    "connect_args",
    "database",
    "db",
    "schema_name",
    "username",
    "user",
    "password",
    "passwd",
    "secret",
    "sslmode",
    "ssl_context",
    "ssl_ca",
    "local_infile",
    "init_command",
    "query",
    "options",
    "unix_socket",
    "read_default_file",
    "creator",
    "engine",
    "isolation_level",
    "autocommit",
    "read_only",
    "allow_execute",
    "execute_verbs",
    "statement_timeout_ms",
    "row_limit",
    "error_detail",
    "profiles",
    "config",
    "opts",
    "__opts__",
    "__salt__",
]

CALLS = {
    "query": lambda m, **kw: m.query("ro", "SELECT 1", **kw),
    "execute": lambda m, **kw: m.execute("ro", "INSERT INTO scratch.t VALUES (1)", **kw),
    "check_query_result": lambda m, **kw: m.check_query_result(
        "ro", "SELECT 1", "==", expected=1, **kw
    ),
    "check_row_count": lambda m, **kw: m.check_row_count("ro", "SELECT 1", "==", expected=1, **kw),
    "ping": lambda m, **kw: m.ping("ro", **kw),
    "table_list": lambda m, **kw: m.table_list("ro", **kw),
}


@pytest.fixture
def forbid_sockets(monkeypatch):
    """Fail the test if anything tries to open a connection."""
    attempts = []
    real = socket.socket.connect

    def spy(self, address):
        attempts.append(address)
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    return attempts


@pytest.mark.parametrize("function", sorted(CALLS))
def test_every_connection_keyword_is_refused_before_any_io(salt, forbid_sockets, function):
    refused = 0
    for keyword in CONNECTION_KEYWORDS:
        if keyword.startswith("__"):
            continue  # dropped by design, covered below
        with pytest.raises(SaltInvocationError) as exc:
            CALLS[function](salt, **{keyword: "evil.example:1"})
        message = str(exc.value)
        assert message.startswith("[E_OPTION_UNKNOWN]") or (
            keyword in {"read_only", "allow_execute"} and message.startswith("[E_OPTION_UNKNOWN]")
        ), (keyword, message)
        assert keyword in message
        refused += 1
    observe(
        "call-surface",
        f"sqlx.{function}",
        f"{refused} connection keywords refused with E_OPTION_UNKNOWN, 0 sockets opened",
    )
    assert forbid_sockets == []


def test_dunder_keys_are_dropped_and_change_nothing(salt, server):
    out = salt.query(
        "ro",
        "SELECT current_user" if server.key == "postgresql" else "SELECT current_user()",
        __pub_user="alice",
        __host="evil.example",
        __connect_args={"host": "evil.example"},
        __kwarg__=True,
        __port=1,
    )
    assert out["rows"][0][0].split("@")[0] == "ro"
    observe(
        "call-surface",
        f"{server.key} __host/__connect_args/__port dunder keys",
        "dropped; query ran against the profile's server as ro",
    )


def test_host_inside_params_is_only_a_bind_value(salt, server, forbid_sockets):
    out = salt.query(
        "ro",
        (
            "SELECT CAST(:host AS CHAR(40)) AS h, CAST(:port AS CHAR(10)) AS p"
            if server.key != "postgresql"
            else "SELECT CAST(:host AS text) AS h, CAST(:port AS text) AS p"
        ),
        params={
            "host": "evil.example",
            "port": "1",
            "database": "private",
            "user": "postgres",
            "password": "x",
        },
    )
    assert out["rows"] == [["evil.example", "1"]]
    assert {a[0] for a in forbid_sockets} <= {harness.HOST}, forbid_sockets
    observe(
        "call-surface",
        f"{server.key} params={{host,port,database,user,password}}",
        "returned as plain strings; all sockets went to the profile host",
    )


def test_profile_argument_is_a_name_not_a_connection_string(salt, forbid_sockets):
    for name in (
        "ro",
        "ro?host=evil",
        "postgresql://evil/db",
        "ro\x00",
        "../ro",
        "RO",
        " ro",
        "ro ",
        "{ro}",
        "%s",
        "ro;host=evil",
    ):
        if name == "ro":
            continue
        with pytest.raises(SaltInvocationError) as exc:
            salt.query(name, "SELECT 1")
        assert str(exc.value) == "[E_PROFILE_UNKNOWN] profile is not configured", name
    assert forbid_sockets == []


def test_unknown_profile_message_does_not_disclose_other_profiles(salt):
    with pytest.raises(SaltInvocationError) as exc:
        salt.query("nope", "SELECT 1")
    for known in ("ro", "rw", "rw_query", "rw_guard"):
        assert f"'{known}'" not in str(exc.value)


def test_sql_cannot_redirect_the_connection(salt, server):
    """A statement can switch the default database on MySQL (``USE``) but not leave what the grants allow."""
    if server.key == "postgresql":
        pytest.skip("PostgreSQL has no USE; a connection is bound to one database")
    kind, value = harness.attempt(lambda: salt.query("ro", "USE private"))
    observe("call-surface", f"{server.key} USE private", harness.describe((kind, value)))
    assert kind == "ERR" and "1044" in str(value)
