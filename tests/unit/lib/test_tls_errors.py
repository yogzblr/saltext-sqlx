"""TLS failure mapping in lib/errors.py. Every message below was observed on a live TLS server (docs/WIRE_SHAPES.md
section 11) with PostgreSQL 16.15 (libpq 18.0.6, psycopg 3.3.6), MySQL 8.0.46, MariaDB 10.11.14 and PyMySQL 1.2.3.
"""

import ssl

import pytest
import sqlalchemy.exc as sa_exc

from saltext.sqlx.lib import errors

PG_PREFIX = 'connection failed: connection to server at "127.0.0.1", port 54339 failed: '


def pg(text):
    return sa_exc.OperationalError("SELECT 1", {}, Exception(PG_PREFIX + text))


def my(code, text):
    return sa_exc.OperationalError("SELECT 1", {}, _MyError(code, text))


class _MyError(Exception):
    pass


PG_CASES = [
    (
        "SSL error: certificate verify failed",
        "E_CONNECT",
    ),  # unknown CA / expired server certificate
    (
        'server certificate for "localhost" (and 1 other name) does not match host name "127.0.0.2"',
        "E_CONNECT",
    ),
    ("SSL error: ssl/tls alert certificate expired", "E_CONNECT"),  # expired client certificate
    ("SSL error: tlsv1 alert unknown ca", "E_CONNECT"),  # client certificate from another CA
    ("SSL error: tlsv1 alert bad certificate", "E_CONNECT"),
    ("FATAL:  connection requires a valid client certificate", "E_AUTH_FAILED"),
    ('FATAL:  certificate authentication failed for user "tls_cert"', "E_AUTH_FAILED"),
    (
        'FATAL:  pg_hba.conf rejects connection for host "127.0.0.1", user "tls_pw", database "tlsdb", no encryption',
        "E_CONNECT",
    ),
    ("server does not support SSL, but SSL was required", "E_CONNECT"),
    ('could not load private key file "/x/client.key": key values mismatch', "E_CONNECT"),
    ('could not load private key file "/x/client.key": unsupported', "E_CONNECT"),
    ('certificate present, but not private key file "/x/client.key"', "E_CONNECT"),
    (
        'private key file "/x/client.key" has group or world access; file must have permissions u=rw',
        "E_CONNECT",
    ),
    ('root certificate file "/x/ca.pem" does not exist\nEither provide the file', "E_CONNECT"),
]


@pytest.mark.parametrize("text,code", PG_CASES)
def test_postgresql_tls_failures(text, code):
    result = errors.describe(pg(text), "postgresql")
    assert result.code == code, result
    assert (
        "SSL" in result.message
        or "certificate" in result.message
        or "key file" in result.message
        or "encryption" in result.message
    )


MY_CASES = [
    (
        2003,
        "Can't connect to MySQL server on 'localhost' ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain (_ssl.c:1032))",
        "E_CONNECT",
    ),
    (
        2003,
        "Can't connect to MySQL server on '127.0.0.2' ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: IP address mismatch, certificate is not valid for '127.0.0.2'. (_ssl.c:1032))",
        "E_CONNECT",
    ),
    (
        2013,
        "Lost connection to MySQL server during query ([SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] sslv3 alert certificate expired (_ssl.c:2660))",
        "E_CONNECT",
    ),
    (
        2013,
        "Lost connection to MySQL server during query ([SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca (_ssl.c:2660))",
        "E_CONNECT",
    ),
    (1045, "Access denied for user 'tls_cert'@'localhost' (using password: YES)", "E_AUTH_FAILED"),
    (
        3159,
        "Connections using insecure transport are prohibited while --require_secure_transport=ON.",
        "E_CONNECT",
    ),
]


@pytest.mark.parametrize("vendor,text,code", MY_CASES)
@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_tls_failures(vendor, text, code, dialect):
    result = errors.describe(my(vendor, text), dialect)
    assert result.code == code and result.native == str(vendor)
    assert result.message.startswith(text[:20])


def _dbapi(vendor, text):
    exc = _MyError(vendor, text)
    return sa_exc.OperationalError("SELECT 1", {}, exc)


def test_mysql_family_raw_ssl_errors_are_connect_errors():
    """PyMySQL raises ssl.SSLError itself when the client certificate or key cannot be loaded (observed)."""
    for args in (
        (524297, "[SSL] PEM lib (_ssl.c:4129)"),
        (116, "[X509: KEY_VALUES_MISMATCH] key values mismatch (_ssl.c:4129)"),
    ):
        for dialect in ("mysql", "mariadb"):
            result = errors.describe(ssl.SSLError(*args), dialect)
            assert (
                result.code == "E_CONNECT" and args[1].split(" (", maxsplit=1)[0] in result.message
            )


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", "/secret/dir/client.key"),
        PermissionError(13, "Permission denied", "/secret/dir/client.key"),
    ],
)
def test_mysql_family_unreadable_tls_file_names_no_path(exc):
    result = errors.describe(exc, "mysql")
    assert result.code == "E_CONNECT"
    assert "/secret" not in result.message and "client.key" not in result.message
    assert exc.strerror in result.message


def test_other_dialects_do_not_get_the_tls_file_rule():
    assert errors.describe(FileNotFoundError(2, "No such file"), "postgresql").code == "E_INTERNAL"
    assert errors.describe(ssl.SSLError(1, "x"), "oracle").code == "E_INTERNAL"


def test_raise_for_keeps_the_real_driver_message():
    with pytest.raises(Exception) as exc:
        errors.raise_for(pg("SSL error: certificate verify failed"), "postgresql")
    assert str(exc.value).startswith("[E_CONNECT] postgresql: connection failed:")
    assert "certificate verify failed" in str(exc.value)
