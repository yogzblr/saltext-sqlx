"""pg8000 error shapes (docs/WIRE_SHAPES.md section 16) through ``lib/errors.py``.

pg8000 raises ``DatabaseError`` / ``ProgrammingError`` / ``InterfaceError`` whose ``args[0]`` is the server's
ErrorResponse as a dict (``S`` severity, ``V`` severity, ``C`` SQLSTATE, ``M`` message, ``D`` detail, ``H`` hint, plus
``P`` position and ``F``/``L``/``R`` source position). The dictionaries below were captured from PostgreSQL 16.15 with
pg8000 1.31.6 (the source position keys are kept because the code must ignore them). The mapping has to give the same
code and the same ``postgresql <SQLSTATE>: <message>`` text as the psycopg shape of the same error.
"""

import socket
import ssl
from types import SimpleNamespace

import pytest
import sqlalchemy.exc as sa_exc

from saltext.sqlx.lib import errors

SQL_TEXT = "SELECT * FROM users WHERE api_token = 'zz-sql-text-marker'"
SECRET = "hunter2-bound-secret"


class DatabaseError(Exception):
    """Stand-in for pg8000.exceptions.DatabaseError (the code never imports the driver)."""


class InterfaceError(Exception):
    """Stand-in for pg8000.exceptions.InterfaceError."""


class ProgrammingError(DatabaseError):
    """Stand-in for pg8000.dbapi.ProgrammingError."""


def fields(code, message, severity="ERROR", **more):
    out = {"S": severity, "V": severity, "C": code, "M": message, "F": "x.c", "L": "1", "R": "fn"}
    out.update(more)
    return out


def wrap(orig):
    return sa_exc.DBAPIError("SELECT 1", {"pw": SECRET}, orig)


def describe(orig, **kw):
    return errors.describe(wrap(orig), "postgresql", **kw)


# (SQLSTATE, message, severity, expected code), real server texts
SERVER = [
    ("42P01", 'relation "nope" does not exist', "ERROR", "E_SQL"),
    ("42601", 'syntax error at or near "selec"', "ERROR", "E_SQL"),
    ("42703", 'column "x" does not exist', "ERROR", "E_SQL"),
    ("42501", "permission denied for table t", "ERROR", "E_SQL"),
    ("22012", "division by zero", "ERROR", "E_SQL_DATA"),
    ("22021", 'invalid byte sequence for encoding "UTF8": 0x00', "ERROR", "E_SQL_DATA"),
    (
        "23505",
        'duplicate key value violates unique constraint "orders_pkey"',
        "ERROR",
        "E_CONSTRAINT",
    ),
    ("23502", 'null value in column "id" violates not-null constraint', "ERROR", "E_CONSTRAINT"),
    (
        "25006",
        "cannot execute CREATE TABLE in a read-only transaction",
        "ERROR",
        "E_READ_ONLY",
    ),
    ("57014", "canceling statement due to statement timeout", "ERROR", "E_TIMEOUT"),
    # connect phase: pg8000 reports the SQLSTATE, psycopg did not
    ("28P01", 'password authentication failed for user "rw"', "FATAL", "E_AUTH_FAILED"),
    ("3D000", 'database "nodb" does not exist', "FATAL", "E_CONNECT"),
    ("57P03", "the database system is starting up", "FATAL", "E_CONNECT"),
    ("57P01", "terminating connection due to administrator command", "FATAL", "E_CONNECT"),
    ("53300", "sorry, too many clients already", "FATAL", "E_CONNECT"),
    (
        "28000",
        'no pg_hba.conf entry for host "10.0.0.5", user "u", database "d", no encryption',
        "FATAL",
        "E_CONNECT",
    ),
    ("28000", "connection requires a valid client certificate", "FATAL", "E_AUTH_FAILED"),
    (
        "28000",
        'certificate authentication failed for user "tls_mismatch"',
        "FATAL",
        "E_AUTH_FAILED",
    ),
    ("28000", 'role "x" is not permitted to log in', "FATAL", "E_AUTH_FAILED"),
]


@pytest.mark.parametrize("sqlstate,message,severity,code", SERVER)
@pytest.mark.parametrize("cls", [DatabaseError, ProgrammingError, InterfaceError])
def test_server_error_dictionary_maps_on_the_sqlstate(sqlstate, message, severity, code, cls):
    result = describe(cls(fields(sqlstate, message, severity)))
    assert result.code == code
    assert result.native == sqlstate
    assert result.message == message


@pytest.mark.parametrize("sqlstate,message,severity,code", SERVER)
def test_same_code_and_text_as_the_psycopg_shape(sqlstate, message, severity, code):
    """psycopg: attributes ``sqlstate`` and ``diag``; pg8000: the dictionary. Same result for the same error."""
    psycopg_style = DatabaseError(message)
    psycopg_style.sqlstate = sqlstate
    psycopg_style.diag = SimpleNamespace(
        message_primary=message, message_hint=None, message_detail=None, sqlstate=sqlstate
    )
    ours = describe(DatabaseError(fields(sqlstate, message, severity)))
    theirs = describe(psycopg_style)
    assert ours == theirs or (ours.code, ours.native) == (theirs.code, theirs.native)


def test_raise_for_text_keeps_the_documented_format():
    with pytest.raises(Exception) as exc:
        errors.raise_for(
            wrap(ProgrammingError(fields("42P01", 'relation "x" does not exist'))), "postgresql"
        )
    assert str(exc.value) == '[E_SQL] postgresql 42P01: relation "x" does not exist'


def test_message_never_contains_the_sql_text_or_bound_values():
    exc = wrap(ProgrammingError(fields("42P01", 'relation "x" does not exist', P="15")))
    text = str(errors.describe(exc, "postgresql"))
    assert SQL_TEXT not in text and SECRET not in text and "parse_relation" not in text
    assert "'P'" not in text and "{" not in text


def test_hint_is_kept_and_detail_only_with_error_detail():
    orig = ProgrammingError(
        fields(
            "23505",
            'duplicate key value violates unique constraint "k"',
            D="Key (id)=(1) already exists.",
            H="try another id",
        )
    )
    plain = describe(orig).message
    assert "HINT: try another id" in plain and "Key (id)" not in plain
    detailed = describe(orig, error_detail=True).message
    assert "DETAIL: Key (id)=(1) already exists." in detailed


def test_invalid_input_value_is_scrubbed_unless_error_detail():
    orig = DatabaseError(fields("22P02", 'invalid input syntax for type integer: "s3cr3t-value"'))
    assert "s3cr3t-value" not in describe(orig).message
    assert "invalid input syntax for type integer" in describe(orig).message
    assert "s3cr3t-value" in describe(orig, error_detail=True).message


def test_dictionary_without_sqlstate_falls_back_to_text_then_internal():
    result = describe(InterfaceError({"S": "FATAL", "M": "password authentication failed"}))
    assert result.code == "E_AUTH_FAILED"
    assert describe(InterfaceError("something new")).code == "E_INTERNAL"


# -- socket-level failures: one InterfaceError, the cause tells what happened ----------------------------------------------
def connect_failure(cause, host="10.0.0.5", port=5432):
    orig = InterfaceError(
        f"Can't create a connection to host {host} and port {port} (timeout is 2 and source_address is None)."
    )
    orig.__cause__ = cause
    return orig


@pytest.mark.parametrize(
    "cause,reason",
    [
        (ConnectionRefusedError(111, "Connection refused"), "Connection refused"),
        (TimeoutError("timed out"), "timeout expired"),
        (
            socket.gaierror(-2, "Name or service not known"),
            "could not translate host name: Name or service not known",
        ),
        (OSError(113, "No route to host"), "No route to host"),
    ],
)
def test_connect_failures_are_e_connect_with_the_real_reason(cause, reason):
    result = describe(connect_failure(cause))
    assert result.code == "E_CONNECT"
    assert result.message == f'connection to server at "10.0.0.5", port 5432 failed: {reason}'


def test_connect_timeout_is_e_connect_not_e_timeout():
    assert describe(connect_failure(TimeoutError("timed out"))).code == "E_CONNECT"


def test_no_password_for_scram_is_an_auth_failure():
    result = describe(
        InterfaceError("server requesting password authentication, but no password was provided")
    )
    assert result.code == "E_AUTH_FAILED"
    assert "no password was provided" in result.message


def network_error(*chain):
    orig = InterfaceError("network error")
    cur = orig
    for exc in chain:
        cur.__cause__ = exc
        cur = exc
    return orig


def test_network_error_carries_the_tls_alert_from_the_cause_chain():
    zero = ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed (EOF) (_ssl.c:2556)")
    alert = ssl.SSLError(
        1, "[SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] ssl/tls alert certificate expired (_ssl.c:2713)"
    )
    zero.__context__ = alert
    result = describe(network_error(zero))
    assert result.code == "E_CONNECT"
    assert (
        result.message == "[SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] ssl/tls alert certificate expired"
    )


def test_network_error_without_alert_says_the_connection_closed():
    zero = ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed (EOF) (_ssl.c:2556)")
    assert describe(network_error(zero)).message == "TLS/SSL connection has been closed (EOF)"


def test_network_error_from_the_os_names_the_os_error():
    result = describe(network_error(BrokenPipeError(32, "Broken pipe")))
    assert (result.code, result.message) == ("E_CONNECT", "network error: Broken pipe")
    assert describe(network_error()).message == "network error"


def test_ssl_module_errors_escape_pg8000_unwrapped_and_are_connect_errors():
    exc = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain (_ssl.c:1082)",
    )
    result = errors.describe(exc, "postgresql")
    assert result.code == "E_CONNECT"
    assert "_ssl.c" not in result.message and "certificate verify failed" in result.message


def test_unreadable_tls_file_is_a_connect_error_without_the_path():
    exc = FileNotFoundError(2, "No such file or directory", "/secret/dir/client.key")
    result = errors.describe(exc, "postgresql")
    assert result.code == "E_CONNECT"
    assert "/secret" not in result.message and "client.key" not in result.message
