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
MariaDB 10.11.14; by task T1b on Oracle 23.26.2 Free and SQL Server 2022 CU25, ``docs/wire_shapes/t1b/``). A code that
was not observed falls to ``E_INTERNAL`` (class name only). No Salt dunders, no driver imports.
"""

import re
import socket
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
#: Dialects whose client loads the TLS files in Python (PyMySQL, and pg8000 through ``tls.pg8000_context``).
_TLS_FILE_DIALECTS = _MYSQL_FAMILY + ("postgresql",)

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
_MSG_TLS_FILE = (
    "a TLS file given to the driver (CA, client certificate or key) could not be opened: {reason}"
)
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
# PostgreSQL (psycopg 3 and pg8000). Evidence: docs/wire_shapes/postgresql__sa2.0.55.json and WIRE_SHAPES section 16
# --------------------------------------------------------------------------------------------------
# Exact SQLSTATE -> code. 57014 (statement_timeout) and 25006 (read-only transaction) observed.
_PG_EXACT = {
    "57014": "E_TIMEOUT",
    "25006": "E_READ_ONLY",
    # Connect-phase and server-going-away states. pg8000 reports the server's own SQLSTATE for these (psycopg
    # reported none for connect failures); observed 3D000 and 28P01 (WIRE_SHAPES section 16), the others are the
    # documented states of the same kind (UNVERIFIED).
    "3D000": "E_CONNECT",  # invalid_catalog_name: database "x" does not exist
    "53300": "E_CONNECT",  # too_many_connections
    "57P01": "E_CONNECT",  # admin_shutdown
    "57P02": "E_CONNECT",  # crash_shutdown
    "57P03": "E_CONNECT",  # cannot_connect_now (starting up / shutting down)
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
    # Authentication first: these libpq texts also arrive inside the generic "connection failed: ..." wrapper.
    ("password authentication failed", "E_AUTH_FAILED"),
    (
        "fe_sendauth",
        "E_AUTH_FAILED",
    ),  # "fe_sendauth: no password supplied" (WIRE_SHAPES section 13)
    (
        "no password was provided",
        "E_AUTH_FAILED",
    ),  # pg8000: "server requesting password authentication, but no password was provided"
    ("is not permitted to log in", "E_AUTH_FAILED"),  # role without LOGIN
    ("Connection refused", "E_CONNECT"),
    ("could not connect", "E_CONNECT"),
    ("server closed the connection", "E_CONNECT"),
    # Section 13: connect-phase failures that carry no SQLSTATE and were E_INTERNAL. A connect timeout is a connect
    # failure, not E_TIMEOUT (SPEC section 8: E_TIMEOUT is a statement timeout / cancel).
    ("timeout expired", "E_CONNECT"),  # psycopg ConnectionTimeout: "connection timeout expired"
    (
        "failed to resolve host",
        "E_CONNECT",
    ),  # psycopg's own wording for a name that does not resolve
    ("could not translate host name", "E_CONNECT"),  # libpq wording of the same failure
    ("no pg_hba.conf entry", "E_CONNECT"),
    ("pg_hba.conf rejects", "E_CONNECT"),
    ("too many connections", "E_CONNECT"),
    ("not currently accepting connections", "E_CONNECT"),
    ("the database system is", "E_CONNECT"),  # starting up / shutting down / in recovery mode
    ("are allowed as placeholders", "E_OPTION_INVALID"),
    # TLS (docs/WIRE_SHAPES.md section 12; libpq 18.0.6 against PostgreSQL 16.15). Paths in these are removed by
    # ``tls.scrub`` in ``engine`` before the caller sees them.
    ("connection requires a valid client certificate", "E_AUTH_FAILED"),
    ("certificate authentication failed", "E_AUTH_FAILED"),
    (
        "SSL error:",
        "E_CONNECT",
    ),  # unknown CA, expired certificate, alert unknown ca / bad certificate
    ("does not match host name", "E_CONNECT"),
    ("no encryption", "E_CONNECT"),  # pg_hba.conf hostnossl ... reject
    ("server does not support SSL", "E_CONNECT"),
    ("private key file", "E_CONNECT"),  # could not load / has group or world access
    ("root certificate file", "E_CONNECT"),
    # Last resort: psycopg words every libpq connect failure "connection failed: ...", so an unlisted libpq text is
    # still a failed connect (the real text is kept), never E_INTERNAL.
    ("connection failed:", "E_CONNECT"),
)
_PG_VALUE_TAIL = re.compile(r":\s*'.*'\s*$", re.DOTALL)
_PG_DB_MISSING = re.compile(r'database ".*" does not exist')
_PG_INVALID_INPUT = re.compile(r'(invalid input (?:syntax|value) for [^:]*): ".*"', re.DOTALL)


#: Keys of the ErrorResponse dictionary that pg8000 puts in ``exc.args[0]`` (protocol message fields):
#: S severity, V severity (non-localised), C SQLSTATE, M primary message, D detail, H hint.
_PG8000_CONNECT = re.compile(
    r"Can't create a connection to host (?P<host>.*) and port (?P<port>\d+) \(timeout is"
)


def _pg_fields(orig):
    """``(sqlstate, primary, hint, detail, fatal)`` from a psycopg exception or a pg8000 ErrorResponse dictionary."""
    fields = orig.args[0] if orig.args and isinstance(orig.args[0], dict) else None
    if fields is not None:
        sqlstate = fields.get("C")
        return (
            str(sqlstate) if sqlstate else None,
            fields.get("M"),
            fields.get("H"),
            fields.get("D"),
            fields.get("V", fields.get("S")) in ("FATAL", "PANIC"),
        )
    diag = getattr(orig, "diag", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(diag, "sqlstate", None)
    return (
        str(sqlstate) if sqlstate else None,
        getattr(diag, "message_primary", None),
        getattr(diag, "message_hint", None),
        getattr(diag, "message_detail", None),
        False,
    )


def _pg_text_code(text):
    for needle, code in _PG_TEXT:
        if needle in text:
            return code
    return None


def _pg8000_connect(orig):
    """pg8000 wraps every socket failure in one InterfaceError; the cause says what happened."""
    found = _PG8000_CONNECT.match(str(orig.args[0])) if orig.args else None
    if found is None:
        return None
    cause = orig.__cause__ or orig.__context__
    if isinstance(cause, socket.gaierror):
        reason = "could not translate host name: " + (
            cause.args[1] if len(cause.args) > 1 else "name resolution failed"
        )
    elif isinstance(cause, TimeoutError):  # socket.timeout is the same class since Python 3.10
        reason = "timeout expired"  # psycopg: "connection timeout expired"
    elif isinstance(cause, OSError):
        reason = cause.strerror or type(cause).__name__
    else:
        reason = "connection failed"
    return f'connection to server at "{found.group("host")}", port {found.group("port")} failed: {reason}'


_PG_LOGIN_CLASSES = (
    "28",
    "08",
)  # login and connection exceptions: the text says more than the class
_SSL_SUFFIX = re.compile(r"\s*\(_ssl\.c:\d+\)")


def _pg8000_network(orig):
    """
    pg8000 words a failed read or write "network error"; the ssl alert or the OS error is in the cause chain.

    An alert from the server after the handshake (expired or unknown client certificate) arrives that way
    (observed, WIRE_SHAPES section 16).
    """
    if not orig.args or orig.args[0] != "network error":
        return None
    chain, cur = [], orig.__cause__ or orig.__context__
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    for exc in chain:
        if (
            isinstance(exc, ssl.SSLError)
            and not isinstance(exc, ssl.SSLZeroReturnError)
            and len(exc.args) > 1
            and isinstance(exc.args[1], str)
        ):
            return _SSL_SUFFIX.sub("", exc.args[1])
    for exc in chain:
        if isinstance(exc, ssl.SSLError):
            return "TLS/SSL connection has been closed (EOF)"
        if isinstance(exc, OSError):
            return "network error: " + (exc.strerror or type(exc).__name__)
    return "network error"


def _pg(orig, error_detail):
    sqlstate, primary, hint, detail, fatal = _pg_fields(orig)
    if sqlstate:
        text_code = (
            _pg_text_code(primary or "") if fatal or sqlstate[:2] in _PG_LOGIN_CLASSES else None
        )
        # A FATAL error is a refused or ended session (login, pg_hba.conf, shutdown): the message says more than the
        # class does (``28000`` is both a bad pg_hba.conf entry and a bad role), and psycopg classed those by text.
        code = text_code or _PG_EXACT.get(sqlstate) or _PG_CLASS.get(sqlstate[:2])
        if not primary:
            primary = _first_line(orig.args[0]) if orig.args else ""
        if not error_detail:
            primary = _PG_INVALID_INPUT.sub(r'\1: "..."', primary)  # 22P02 echoes the bound value
        parts = [primary]
        if hint:
            parts.append(f"HINT: {hint}")
        if error_detail and detail:
            parts.append(f"DETAIL: {detail}")
        return code, sqlstate, " ".join(parts)
    connect_text = _pg8000_connect(orig) or _pg8000_network(orig)
    text = connect_text or (str(orig.args[0]) if orig.args else "")
    if connect_text is not None:
        return "E_CONNECT", None, connect_text
    code = _pg_text_code(text)
    if code is not None:
        return code, None, _HINT_PERCENT if code == "E_OPTION_INVALID" else text
    if any(c.__name__ == "DataError" for c in type(orig).__mro__):
        # Raised by psycopg itself, not the server, so there is no SQLSTATE: a value Python cannot hold, such as
        # 'infinity' or a year after 9999 ("timestamp too large (after year 10K): 'infinity'"). Observed on 16.15.
        # The value after the colon is data from the table and is dropped unless ``error_detail`` is set.
        return "E_SQL_DATA", None, text if error_detail else _PG_VALUE_TAIL.sub("", text)
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
    1048: "E_CONSTRAINT",  # column cannot be null (explicit NULL), SQLSTATE 23000 (T1b)
    1364: "E_CONSTRAINT",  # NOT NULL column without a default left out (strict mode), SQLSTATE HY000 (T1b)
    1452: "E_CONSTRAINT",  # foreign key: cannot add or update a child row, SQLSTATE 23000 (T1b)
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
# Oracle (python-oracledb thin). Evidence: docs/wire_shapes/t1b/oracle.json, Oracle AI Database 26ai Free 23.26.2,
# oracledb 26.1.0 (task T1b, 2026-10-31). ``exc.orig.args[0]`` is an error object with ``full_code`` ("ORA-00942",
# "DPY-6005") and ``message`` ("ORA-00942: table or view ... does not exist\nHelp: https://docs.oracle.com/...").
# The SQLAlchemy class is DatabaseError for almost everything (IntegrityError for constraints, OperationalError for
# DPY-6005), so the code is the key. Only observed codes are listed; any other code is E_INTERNAL (class name).
# --------------------------------------------------------------------------------------------------
_ORA_CODES = {
    "ORA-00900": "E_SQL",  # invalid SQL statement
    "ORA-00904": "E_SQL",  # invalid identifier (unknown column)
    "ORA-00923": "E_SQL",  # FROM keyword not found where expected (syntax)
    "ORA-00942": "E_SQL",  # table or view does not exist (also: not granted)
    "ORA-01031": "E_SQL",  # insufficient privileges (CREATE TABLE as a SELECT-only account)
    "ORA-01453": "E_SQL",  # SET TRANSACTION must be first statement of transaction
    "ORA-03405": "E_SQL",  # text after the end of the statement: a second statement in the string
    "ORA-41900": "E_SQL",  # missing <verb> privilege on <object> (23ai wording of insufficient privileges)
    "ORA-01476": "E_SQL_DATA",  # divisor is equal to zero
    "ORA-01722": "E_SQL_DATA",  # unable to convert string value to a number
    "ORA-12899": "E_SQL_DATA",  # value too large for column
    "ORA-00001": "E_CONSTRAINT",  # unique constraint violated
    "ORA-01400": "E_CONSTRAINT",  # cannot insert NULL
    "ORA-02291": "E_CONSTRAINT",  # integrity constraint violated - parent key not found
    "ORA-01456": "E_READ_ONLY",  # may not perform insert, delete, update operation inside a READ ONLY transaction
    "DPY-4024": "E_TIMEOUT",  # call timeout of <n> ms exceeded (Connection.call_timeout)
    "DPY-4011": "E_CONNECT",  # the database or network closed the connection; "timed out" in the text: E_TIMEOUT
    "DPY-4010": "E_OPTION_INVALID",  # no value for a bind placeholder (`:a::int` reaches the driver as written)
    "ORA-01017": "E_AUTH_FAILED",  # invalid credential or not authorized (wrong password and unknown user alike)
    "DPY-4001": "E_AUTH_FAILED",  # no credentials specified (profile without a password)
    "DPY-6005": "E_CONNECT",  # cannot connect: refused, timed out, service not registered (DPY-6001)
}
#: Driver class fallback for codes not listed above: every observed IntegrityError was a constraint violation.
_ORA_CLASS = {"IntegrityError": "E_CONSTRAINT"}
_ORA_HELP = re.compile(r"^Help: https?://\S+$")
#: "ORA-03301: (ORA-00001 details) row with column values (ID:900)", "ORA-03302: (ORA-01722 details) invalid string
#: value: abc": the server echoes row or bound values; kept only with ``error_detail``.
_ORA_DETAIL = re.compile(r"^ORA-033\d\d: \(ORA-\d+ details\)")
_ORA_CONN_ID = re.compile(r"\s*\(CONNECTION_ID=[^)]*\)")
_ORA_VALUE = re.compile(r"(string value containing )'.*?'")


def _oracle(orig, error_detail):
    err = orig.args[0] if orig.args else None
    full_code = getattr(err, "full_code", None)
    if not full_code:
        return None, None, None
    full_code = str(full_code)
    lines = []
    for line in str(getattr(err, "message", "") or "").splitlines():
        line = _ORA_CONN_ID.sub("", line).strip()
        if not line or _ORA_HELP.match(line) or (_ORA_DETAIL.match(line) and not error_detail):
            continue
        if not lines and line.startswith(f"{full_code}: "):
            line = line[len(full_code) + 2 :]  # the code is already the native part of the message
        lines.append(line)
    message = "; ".join(lines)
    if not error_detail:
        message = _ORA_VALUE.sub(r"\1'...'", message)
    code = _ORA_CODES.get(full_code)
    if full_code == "DPY-4011" and "timed out" in message:
        # call_timeout expired during a PL/SQL call and the driver could not recover the connection in time
        # (observed: "socket timed out while recovering from previous socket timeout")
        code = "E_TIMEOUT"
    if code is None:
        names = {c.__name__ for c in type(orig).__mro__}
        code = next((c for name, c in _ORA_CLASS.items() if name in names), None)
    return code, full_code, message


# --------------------------------------------------------------------------------------------------
# SQL Server (pymssql 2.4.6 / FreeTDS). Evidence: docs/wire_shapes/t1b/mssql.json, SQL Server 2022 CU25
# (16.0.4255.1), task T1b 2026-10-31. Statement errors: ``args == (code, b"<server text>DB-Lib error message 20018,
# ...")``; connect errors: ``args == ((code, b"..."),)``. The server's own text comes first and the DB-Lib lines are
# glued to it without a newline. The number is NOT reliable for connect failures: pymssql keeps the last server
# message number of the process, so a closed port right after a failed login reported 18456 (observed). Connect
# failures are therefore decided on the DB-Lib text first (20009 unable to connect, 20003 timed out).
# --------------------------------------------------------------------------------------------------
_MSSQL_CODES = {
    102: "E_SQL",  # incorrect syntax near ...
    207: "E_SQL",  # invalid column name
    208: "E_SQL",  # invalid object name
    229: "E_SQL",  # the <verb> permission was denied on the object
    262: "E_SQL",  # CREATE TABLE permission denied in database
    2812: "E_SQL",  # could not find stored procedure (a statement that starts with an unknown word)
    245: "E_SQL_DATA",  # conversion failed
    2628: "E_SQL_DATA",  # string or binary data would be truncated
    8134: "E_SQL_DATA",  # divide by zero
    515: "E_CONSTRAINT",  # cannot insert the value NULL
    547: "E_CONSTRAINT",  # conflicted with the FOREIGN KEY constraint
    2627: "E_CONSTRAINT",  # violation of PRIMARY KEY / UNIQUE constraint
    18456: "E_AUTH_FAILED",  # login failed (wrong password, no password, unknown database at login)
    20009: "E_CONNECT",  # DB-Lib: unable to connect (refused, timed out, unknown host)
}
_MSSQL_DBLIB = re.compile(r"DB-Lib error message (\d+), severity \d+:")
_MSSQL_NOISE = "General SQL Server error: Check messages from the SQL Server"
#: Values the server echoes: "The duplicate key value is (1).", "Truncated value: 'xxx'.", "the varchar value 'abc'"
_MSSQL_VALUES = (
    (re.compile(r"(The duplicate key value is )\(.*?\)(?=\.)", re.DOTALL), r"\1(...)"),
    (re.compile(r"(Truncated value: )'.*?'(?=\.)", re.DOTALL), r"\1'...'"),
    (re.compile(r"(converting the \w+ value )'.*?'", re.DOTALL), r"\1'...'"),
)


def _mssql(orig, error_detail):
    args = orig.args
    pair = args[0] if args and isinstance(args[0], tuple) else args
    if len(pair) < 2 or not isinstance(pair[0], int):
        return None, None, None
    raw = pair[1]
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    first = _MSSQL_DBLIB.search(text)
    server = (text[: first.start()] if first else text).strip()
    dblib = set(_MSSQL_DBLIB.findall(text))
    lines = [server] if server else []
    for line in _MSSQL_DBLIB.sub("\n", text[first.start() :] if first else "").splitlines():
        line = line.strip()
        if line and line != _MSSQL_NOISE and line not in lines:
            lines.append(line)
    message = "; ".join(lines)
    if not error_detail:
        for pattern, repl in _MSSQL_VALUES:
            message = pattern.sub(repl, message)
    if "20003" in dblib:  # "TDS server connection timed out": the client-side query timeout expired
        return "E_TIMEOUT", "20003", message
    if "20009" in dblib:  # "Unable to connect": whatever (possibly stale) number pymssql reported
        return "E_CONNECT", "20009", message
    return _MSSQL_CODES.get(pair[0]), str(pair[0]), message


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
    elif isinstance(exc, ssl.SSLError) and dialect in _TLS_FILE_DIALECTS:
        # PyMySQL loads the client certificate and key itself and lets the ssl error escape (observed: garbage key,
        # certificate/key mismatch). args == (code, "[SSL] PEM lib (_ssl.c:...)"): no path in it.
        detail = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], str) else "TLS error"
        if (
            dialect == "postgresql"
        ):  # pg8000 lets the ssl module's error escape unwrapped; drop its source position
            detail = _SSL_SUFFIX.sub("", detail)
        return Translation("E_CONNECT", None, detail)
    elif isinstance(exc, (FileNotFoundError, PermissionError)) and dialect in _TLS_FILE_DIALECTS:
        # FileNotFoundError / PermissionError from the same step; the file name in the exception is deliberately dropped
        return Translation("E_CONNECT", None, _MSG_TLS_FILE.format(reason=exc.strerror))
    elif isinstance(exc, AttributeError) and dialect in _MYSQL_FAMILY:
        if "is_auth_switch_request" in text:  # first connect after FLUSH PRIVILEGES (WS 6)
            return Translation("E_CONNECT", None, _MSG_AUTH_SWITCH)
    elif isinstance(exc, OverflowError):
        # A number or date the driver cannot represent (for example a year or an interval beyond Python's range).
        return Translation(
            "E_SQL_DATA", None, "a value in the result is out of range for the driver"
        )
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
    if isinstance(exc, socket.gaierror):
        # python-oracledb thin resolves the host itself and lets the error escape unwrapped (observed, T1b:
        # socket.gaierror(-2, 'Name or service not known')); the other drivers wrap it. The host is not in args.
        reason = (
            exc.args[1]
            if len(exc.args) > 1 and isinstance(exc.args[1], str)
            else "name resolution failed"
        )
        return Translation("E_CONNECT", None, f"cannot resolve the database host name: {reason}")
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
