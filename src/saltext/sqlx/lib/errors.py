"""The single error translation path (rule R1, SPEC section 8, ARCHITECTURE section 5).

``translate(exc, dialect)`` returns ``(code, message)``; ``raise_for`` raises
``CommandExecutionError("[E_CODE] <dialect> <native>: <message>")``.

Rules enforced here:

* R2: map on the vendor code / SQLSTATE first and the exception class second. Unknown -> ``E_INTERNAL`` with the
  exception class name only.
* R3: the message is built from the driver exception (``exc.orig``) only, never from ``str()`` of a SQLAlchemy
  exception, which embeds the SQL text and the bound values. Row values echoed by MySQL/MariaDB/PostgreSQL in the
  primary message are scrubbed unless ``error_detail`` is true.
* ``raise_for`` clears ``__cause__`` / ``__context__`` so that the original exception text cannot leak via chaining.

Every mapping carries the evidence in ``docs/wire_shapes/`` (observed by task T1 on PostgreSQL 16.15, MySQL 8.0.46 and
MariaDB 10.11.14). Oracle and SQL Server were never run server-side: only what ``WIRE_SHAPES.md`` shows is mapped, the
rest is marked ``UNVERIFIED(T1)`` and falls to ``E_INTERNAL`` (class name only). No Salt dunders, no driver imports.
"""

import re
import ssl
from collections import namedtuple

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

#: The sixteen codes of SPEC section 8.
CODES = (
    "E_PROFILE_UNKNOWN",
    "E_PROFILE_INVALID",
    "E_OPTION_UNKNOWN",
    "E_OPTION_INVALID",
    "E_SECRET",
    "E_POLICY",
    "E_EXECUTE_DISABLED",
    "E_DRIVER_MISSING",
    "E_CONNECT",
    "E_AUTH_FAILED",
    "E_SQL",
    "E_SQL_DATA",
    "E_CONSTRAINT",
    "E_READ_ONLY",
    "E_TIMEOUT",
    "E_INTERNAL",
)

#: Codes this module can produce from a database or driver exception. The others are raised by validation, policy
#: and secret code (already prefixed) and are passed through unchanged.
DRIVER_CODES = (
    "E_OPTION_INVALID",
    "E_DRIVER_MISSING",
    "E_CONNECT",
    "E_AUTH_FAILED",
    "E_SQL",
    "E_SQL_DATA",
    "E_CONSTRAINT",
    "E_READ_ONLY",
    "E_TIMEOUT",
    "E_INTERNAL",
)

MAX_MESSAGE = 1000

Translation = namedtuple("Translation", "code native message")

_PREFIXED = re.compile(r"^\[(E_[A-Z_]+)\] (.*)$", re.DOTALL)
_WS = re.compile(r"\s*[\r\n\t]+\s*|\s{2,}")

_MYSQL_FAMILY = ("mysql", "mariadb")

_HINT_PERCENT = (
    "a literal '%' reached the driver unescaped; caller SQL must go through text() with bound parameters, "
    "never exec_driver_sql"
)
_HINT_BIND = (
    "a ':' in the statement was read as a bind parameter{name}; write literal colons as \\: and casts as "
    "CAST(:x AS type), never :x::type"
)
_MSG_SQL_ASCII = (
    "the PostgreSQL database cannot be read by the driver; the database must be created with UTF8 encoding "
    "(SQL_ASCII is not supported)"
)
_MSG_CRYPTOGRAPHY = (
    "PyMySQL needs the 'cryptography' package for sha256_password / caching_sha2_password authentication; "
    "install it or use mysql_native_password"
)
_MSG_TLS_FILE = "a TLS file given to the driver (CA, client certificate or key) could not be opened: {reason}"
_MSG_AUTH_SWITCH = "the MySQL authentication handshake failed on first connect (driver received no packet); retry the call"


def _mro_names(exc):
    return {c.__name__ for c in type(exc).__mro__ if c.__module__.startswith("sqlalchemy")}


def _plain_text(obj):
    return str(obj)


def _clean(text):
    text = _WS.sub(" ", str(text)).strip()
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 3] + "..."
    return text


def _first_line(text):
    return str(text).split("\n", 1)[0]


# --------------------------------------------------------------------------------------------------
# PostgreSQL (psycopg 3). Evidence: docs/wire_shapes/postgresql__sa2.0.55.json
# --------------------------------------------------------------------------------------------------
# Exact SQLSTATE -> code. 57014 (statement_timeout) and 25006 (read-only transaction) observed.
_PG_EXACT = {
    "57014": "E_TIMEOUT",
    "25006": "E_READ_ONLY",
}
# SQLSTATE class -> code. 22, 23, 42 observed (42601, 42P01, 42703, 42501; 22012, 22P02; 23505).
_PG_CLASS = {
    "22": "E_SQL_DATA",
    "23": "E_CONSTRAINT",
    "42": "E_SQL",
    "28": "E_AUTH_FAILED",  # UNVERIFIED(T1): 28P01 is not observable, wrong password has no SQLSTATE
    "08": "E_CONNECT",  # UNVERIFIED(T1): connect-phase errors carried no SQLSTATE
}
# No SQLSTATE: connect-phase errors, classified on message text (observed: cases wrong_password,
# unreachable_port, unknown_database, connect_timeout_blackhole, driver_sql_percent_no_params).
_PG_TEXT = (
    ("password authentication failed", "E_AUTH_FAILED"),
    ("Connection refused", "E_CONNECT"),
    ("could not connect", "E_CONNECT"),
    ("server closed the connection", "E_CONNECT"),
    ("are allowed as placeholders", "E_OPTION_INVALID"),
    # TLS (docs/WIRE_SHAPES.md section 11; libpq 18.0.6 against PostgreSQL 16.15). Paths in these are removed by
    # ``tls.scrub`` in ``engine`` before the caller sees them.
    ("connection requires a valid client certificate", "E_AUTH_FAILED"),
    ("certificate authentication failed", "E_AUTH_FAILED"),
    ("SSL error:", "E_CONNECT"),  # unknown CA, expired certificate, alert unknown ca / bad certificate
    ("does not match host name", "E_CONNECT"),
    ("no encryption", "E_CONNECT"),  # pg_hba.conf hostnossl ... reject
    ("server does not support SSL", "E_CONNECT"),
    ("private key file", "E_CONNECT"),  # could not load / has group or world access
    ("root certificate file", "E_CONNECT"),
)
_PG_DB_MISSING = re.compile(r'database ".*" does not exist')
_PG_INVALID_INPUT = re.compile(r'(invalid input (?:syntax|value) for [^:]*): ".*"', re.DOTALL)


def _pg(orig, error_detail):
    diag = getattr(orig, "diag", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(diag, "sqlstate", None)
    if sqlstate:
        sqlstate = str(sqlstate)
        code = _PG_EXACT.get(sqlstate) or _PG_CLASS.get(sqlstate[:2])
        primary = getattr(diag, "message_primary", None)
        if not primary:
            primary = _first_line(orig.args[0]) if orig.args else ""
        if not error_detail:
            primary = _PG_INVALID_INPUT.sub(r'\1: "..."', primary)  # 22P02 echoes the bound value
        parts = [primary]
        hint = getattr(diag, "message_hint", None)
        if hint:
            parts.append(f"HINT: {hint}")
        detail = getattr(diag, "message_detail", None)
        if error_detail and detail:
            parts.append(f"DETAIL: {detail}")
        return code, sqlstate, " ".join(parts)
    text = str(orig.args[0]) if orig.args else ""
    for needle, code in _PG_TEXT:
        if needle in text:
            return code, None, _HINT_PERCENT if code == "E_OPTION_INVALID" else text
    if _PG_DB_MISSING.search(text):
        return "E_CONNECT", None, text
    return None, None, None


# --------------------------------------------------------------------------------------------------
# MySQL / MariaDB (PyMySQL). Evidence: docs/wire_shapes/{mysql,mariadb}__sa2.0.55.json
# The vendor number is the key: SQLSTATE differs between servers (1366: HY000 / 22007) and the SQLAlchemy class
# does not follow SQLSTATE classes (1054, 1142 and 1365 are OperationalError).
# --------------------------------------------------------------------------------------------------
_MY_CODES = {
    1045: "E_AUTH_FAILED",  # wrong password, SQLSTATE 28000
    1044: "E_AUTH_FAILED",  # access denied to database, SQLSTATE 42000: NOT E_SQL (SPEC lists it under auth)
    2003: "E_CONNECT",  # no SQLSTATE
    2013: "E_CONNECT",  # lost connection; "timed out" in the text -> E_TIMEOUT (decision, see _my)
    3024: "E_TIMEOUT",  # MySQL MAX_EXECUTION_TIME
    1969: "E_TIMEOUT",  # MariaDB max_statement_time
    1792: "E_READ_ONLY",
    3159: "E_CONNECT",  # MySQL require_secure_transport=ON and the client did not use TLS (observed)
    1062: "E_CONSTRAINT",
    1064: "E_SQL",
    1146: "E_SQL",
    1054: "E_SQL",
    1142: "E_SQL",
    1365: "E_SQL_DATA",
    1366: "E_SQL_DATA",
}
# Fallback for vendor codes T1 did not observe: the SQLSTATE class, which was consistent for every observed code
# except 1044 (handled above). UNVERIFIED(T1) for the unobserved codes.
_MY_SQLSTATE_CLASS = {
    "22": "E_SQL_DATA",
    "23": "E_CONSTRAINT",
    "42": "E_SQL",
    "28": "E_AUTH_FAILED",
}
_MY_DUP = re.compile(r"(Duplicate entry )'.*'( for key)", re.DOTALL)
_MY_VALUE = re.compile(
    r"((?:Truncated )?[Ii]ncorrect [^:']*? value: )'.*'(?= for column|\s*$)", re.DOTALL
)


def _my(orig, error_detail):
    args = orig.args
    if not args or isinstance(args[0], bool) or not isinstance(args[0], int):
        return None, None, None
    vendor = args[0]
    message = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
    sqlstate = getattr(orig, "sqlstate", None)
    code = _MY_CODES.get(vendor)
    if code is None and sqlstate:
        code = _MY_SQLSTATE_CLASS.get(str(sqlstate)[:2])
    if vendor == 2013 and "timed out" in message:
        code = "E_TIMEOUT"  # decision: SPEC left 2013 undecided; both servers say "timed out" for read_timeout
    if not error_detail:
        message = _MY_DUP.sub(r"\1'...'\2", message)
        message = _MY_VALUE.sub(r"\1'...'", message)
    return code, str(vendor), message


# --------------------------------------------------------------------------------------------------
# Oracle (oracledb). Only the client-side row is observed: DPY-6005 (docs/wire_shapes/oracle__*.json).
# UNVERIFIED(T1): ORA-01017 (auth), ORA-00942 (missing table), ORA-01013 (timeout) and every other ORA code, and the
# exception classes for SQL errors: not mapped, they fall to E_INTERNAL with the class name only.
# --------------------------------------------------------------------------------------------------
_ORA_CODES = {"DPY-6005": "E_CONNECT"}


def _oracle(orig, _error_detail):
    err = orig.args[0] if orig.args else None
    full_code = getattr(err, "full_code", None)
    if not full_code:
        return None, None, None
    return _ORA_CODES.get(str(full_code)), str(full_code), str(getattr(err, "message", "") or "")


# --------------------------------------------------------------------------------------------------
# SQL Server (pymssql). Only the client-side row is observed: 20009, args == ((20009, b"..."),).
# UNVERIFIED(T1): 18456 (login failed), 208/102 (SQL errors), timeouts and every other code: E_INTERNAL, class only.
# --------------------------------------------------------------------------------------------------
_MSSQL_CODES = {20009: "E_CONNECT"}


def _mssql(orig, _error_detail):
    args = orig.args
    pair = args[0] if args and isinstance(args[0], tuple) else args
    if len(pair) < 2 or not isinstance(pair[0], int):
        return None, None, None
    raw = pair[1]
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("DB-Lib error message") and line not in lines:
            lines.append(line)
    return _MSSQL_CODES.get(pair[0]), str(pair[0]), "; ".join(lines)


_DRIVER_PATH = {
    "postgresql": _pg,
    "mysql": _my,
    "mariadb": _my,
    "oracle": _oracle,
    "mssql": _mssql,
}


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------
def _internal(exc, native=None):
    return Translation("E_INTERNAL", native, type(exc).__name__)


def _raw(exc, dialect):
    """Exceptions that are not wrapped by SQLAlchemy (WS 5-7) or are SQLAlchemy errors without a driver error."""
    names = _mro_names(exc)
    inner = getattr(exc, "orig", None) if "StatementError" in names else None
    # Only inspected, never copied into a message; a StatementError's own text embeds the SQL and the values.
    text = _plain_text(inner if inner is not None else exc)
    if "InvalidRequestError" in names or (
        inner is not None and "InvalidRequestError" in _mro_names(inner)
    ):
        if "A value is required for bind parameter" in text:
            found = re.search(r"bind parameter '([^']*)'", text)
            name = f" '{found.group(1)}'" if found else ""
            return Translation("E_OPTION_INVALID", None, _HINT_BIND.format(name=name))
        return None
    if isinstance(exc, TypeError) and dialect == "postgresql":
        if (
            "string pattern on a bytes-like object" in text
        ):  # SQL_ASCII database, first connect (WS 7)
            return Translation("E_CONNECT", None, _MSG_SQL_ASCII)
    elif isinstance(exc, RuntimeError) and dialect in _MYSQL_FAMILY:
        if "'cryptography' package is required" in text:  # PyMySQL authentication (WS 6)
            return Translation("E_DRIVER_MISSING", None, _MSG_CRYPTOGRAPHY)
    elif isinstance(exc, ssl.SSLError) and dialect in _MYSQL_FAMILY:
        # PyMySQL loads the client certificate and key itself and lets the ssl error escape (observed: garbage key,
        # certificate/key mismatch). args == (code, "[SSL] PEM lib (_ssl.c:...)"): no path in it.
        detail = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], str) else "TLS error"
        return Translation("E_CONNECT", None, detail)
    elif isinstance(exc, (FileNotFoundError, PermissionError)) and dialect in _MYSQL_FAMILY:
        # FileNotFoundError / PermissionError from the same step; the file name in the exception is deliberately dropped
        return Translation("E_CONNECT", None, _MSG_TLS_FILE.format(reason=exc.strerror))
    elif isinstance(exc, AttributeError) and dialect in _MYSQL_FAMILY:
        if "is_auth_switch_request" in text:  # first connect after FLUSH PRIVILEGES (WS 6)
            return Translation("E_CONNECT", None, _MSG_AUTH_SWITCH)
    elif isinstance(exc, ValueError) and "unsupported format character" in text:  # WS 5
        return Translation("E_OPTION_INVALID", None, _HINT_PERCENT)
    return None


def describe(exc, dialect, error_detail=False):
    """Classify ``exc`` and return ``Translation(code, native, message)``.

    ``native`` is the SQLSTATE or vendor code when there is one, else ``None``. The message never contains the SQL
    text, bound values or the URL.
    """
    if isinstance(exc, (SaltInvocationError, CommandExecutionError)):
        found = _PREFIXED.match(_plain_text(exc))
        if found and found.group(1) in CODES:
            return Translation(found.group(1), None, found.group(2))
        return _internal(exc)
    if "DBAPIError" in _mro_names(exc):
        orig = exc.orig
        path = _DRIVER_PATH.get(dialect) if orig is not None else None
        if path is None:
            return _internal(orig if orig is not None else exc)
        code, native, message = path(orig, error_detail)
        if code is None:
            return _internal(orig, native)
        return Translation(code, native, _clean(message) or type(orig).__name__)
    hit = _raw(exc, dialect)
    return hit if hit is not None else _internal(exc)


def translate(exc, dialect, *, error_detail=False):
    """Return ``(code, message)`` for ``exc``; ``code`` is one of :data:`CODES`."""
    result = describe(exc, dialect, error_detail)
    return result.code, result.message


def raise_for(exc, dialect, error_detail=False):
    """Raise the translated error with no exception chaining.

    ``[E_CODE] <dialect> <native>: <message>`` as ``CommandExecutionError``. Errors that other modules already raised
    with a ``[E_...]`` prefix are re-raised with the same class and text.
    """
    result = describe(exc, dialect, error_detail)
    if (
        isinstance(exc, (SaltInvocationError, CommandExecutionError))
        and _PREFIXED.match(_plain_text(exc))
        and (
            result.code in CODES and result.native is None and result.message != type(exc).__name__
        )
    ):
        out = type(exc)(f"[{result.code}] {result.message}")
    else:
        head = f"{dialect} {result.native}" if result.native else f"{dialect}"
        out = CommandExecutionError(f"[{result.code}] {head}: {result.message}")
    # Raised from inside a handler so that the interpreter's implicit chaining cannot attach the caller's active
    # exception (whose text can hold the SQL and the bound values); cleared afterwards.
    try:
        raise out
    except type(out) as err:
        err.__cause__ = None
        err.__context__ = None
        err.__suppress_context__ = True
        raise
