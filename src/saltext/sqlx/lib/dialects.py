"""Per-dialect adapter table (ARCHITECTURE section 8).

One small adapter per dialect: URL building, the ``connect_args`` allowlist, session setup (statement timeout and
the best-effort read-only guard), ping statement and server version. ``DIALECT_DRIVER_CHOICES`` is the single source of
truth for the dialect to driver pairing (the first driver of a dialect is its default); ``lib/profiles.py`` imports it.

Rules honoured here:

* R13: URLs are built with ``sqlalchemy.engine.URL.create``; nothing is formatted into a URL string.
* R15: a driver is imported only inside ``Adapter.load_driver`` (lazily). A missing driver raises
  ``[E_DRIVER_MISSING]`` naming only the pip extra, and affects only that dialect.
* R4 / SECURITY section 3: profile ``query`` and ``connect_args`` keys both reach the driver, so both are checked
  against the same per-driver allowlist; anything else is ``[E_POLICY]``. Messages name the key, never the value.
* The module's own SQL is constant text plus at most one integer that went through ``int()`` (ARCHITECTURE
  section 3). Caller SQL never passes through this module.

PostgreSQL has two drivers (decision D15): ``pg8000`` (pure Python) and ``psycopg`` (libpq). A profile without ``driver`` gets psycopg when it is installed (so nothing
changes for an existing installation) and pg8000 otherwise; the profile key ``driver`` always decides. Everything that is specific to pg8000 and differs from psycopg is in ``Pg8000PostgreSQL`` and recorded in
docs/WIRE_SHAPES.md section 16.

PostgreSQL, MySQL and MariaDB behaviour is verified (docs/WIRE_SHAPES.md, T1); Oracle 23.26.2 and SQL Server 2022 were
verified by task T1b (WIRE_SHAPES sections 14-15, end to end through ``tests-e2e/``). Each mechanism sits in its own small
function so that recorded facts can replace it without touching anything else. This module has no Salt dunders.
"""

import datetime
import decimal
import importlib
import importlib.util
import math
import threading
import uuid

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy import event
from sqlalchemy.engine import URL

from saltext.sqlx.lib import options
from saltext.sqlx.lib import tls

#: dialect -> the drivers allowed with it; the first one is the default (SPEC section 7, decision D15). Single source of truth.
DIALECT_DRIVER_CHOICES = {
    "postgresql": ("pg8000", "psycopg"),
    "mysql": ("pymysql",),
    "oracle": ("oracledb",),
    "mssql": ("pymssql",),
}


def _default_postgresql_driver():
    """
    The driver a PostgreSQL profile without ``driver`` gets: ``psycopg`` when it is installed, else ``pg8000``.

    Existing installations (which have psycopg) keep exactly the behaviour they have today, with no configuration change.
    A host that was given only pg8000 (the ``postgres`` extra, the fleet wheelhouse) uses pg8000. Decided once, when the
    library is imported; a profile or call that names ``driver`` always wins.
    """
    try:
        return "psycopg" if importlib.util.find_spec("psycopg") is not None else "pg8000"
    except (ImportError, ValueError):  # pragma: no cover
        return "pg8000"


#: dialect -> its default driver (what a profile without ``driver`` gets)
DIALECT_DRIVERS = {dialect: choices[0] for dialect, choices in DIALECT_DRIVER_CHOICES.items()}
DIALECT_DRIVERS["postgresql"] = _default_postgresql_driver()

_SCALARS = (str, int, float, bool)


def _fail(code, message):
    raise SaltInvocationError(f"[{code}] {message}")


def _positive_int(value, what):
    """Return ``value`` as an ``int >= 1``, or raise ``[E_OPTION_INVALID]``. Booleans and fractions are refused."""
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        _fail("E_OPTION_INVALID", f"{what} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        _fail("E_OPTION_INVALID", f"{what} must be an integer")
    if number < 1:
        _fail("E_OPTION_INVALID", f"{what} must be >= 1")
    return number


def validate_timeout_ms(value):
    """Validate a statement timeout in milliseconds against the hard cap (SPEC section 6)."""
    number = _positive_int(value, "timeout_ms")
    if number > options.HARD_MAX_TIMEOUT_MS:
        _fail("E_POLICY", f"timeout_ms exceeds the hard maximum of {options.HARD_MAX_TIMEOUT_MS}")
    return number


def _flag(value, what):
    if not isinstance(value, bool):
        _fail("E_OPTION_INVALID", f"{what} must be a boolean")
    return value


class Adapter:
    """Base adapter. Subclasses set the class attributes and override only the mechanisms that differ."""

    name = ""
    driver = ""
    module = ""  #: import name of the driver
    pip_extra = ""
    drivername = ""  #: SQLAlchemy ``dialect+driver``
    ping_sql = "SELECT 1"
    allowed_args = frozenset()  #: keys accepted in profile ``query`` and ``connect_args``
    engine_kwargs = {}  #: extra ``create_engine`` keywords (module-owned)

    # -- driver ----------------------------------------------------------------------------------

    def load_driver(self):
        """Import the driver lazily. Raises ``[E_DRIVER_MISSING]`` naming only the pip extra."""
        failed = False
        module = None
        try:
            module = importlib.import_module(self.module)
        except ImportError:
            failed = True
        if failed:
            # Raised outside the except block so no __context__ carries the import error text or a path.
            raise CommandExecutionError(
                f"[E_DRIVER_MISSING] {self.name} driver is not installed; "
                f"install the pip extra 'saltext-sqlx[{self.pip_extra}]'"
            )
        return module

    # -- allowlist -------------------------------------------------------------------------------

    def _check_args(self, mapping, where):
        for key, value in mapping.items():
            if key not in self.allowed_args:
                _fail("E_POLICY", f"{where} key '{key}' is not allowed for {self.name}")
            if value is None or not isinstance(value, _SCALARS):
                _fail("E_POLICY", f"{where} key '{key}' must be a plain string, number or boolean")

    # -- URL and connect args --------------------------------------------------------------------

    def _database(self, profile):  # pylint: disable=unused-argument
        return profile["database"]

    def _extra_query(self, profile):  # pylint: disable=unused-argument
        return {}

    def url(self, profile, password):
        """Build the engine URL with ``URL.create``. ``password`` may contain any character."""
        query = profile.get("query") or {}
        self._check_args(query, "query")
        values = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in query.items()}
        values.update(self._extra_query(profile))
        values.update(tls.query_args(self.name, profile))  # module-produced, not allowlist-checked
        return URL.create(
            self.drivername,
            username=profile["username"],
            password=password,
            host=profile["host"],
            port=profile["port"],
            database=self._database(profile),
            query=values,
        )

    def _timeout_connect_args(self, profile):
        """Connect-time timeouts owned by the module (derived from the profile, not caller-settable)."""
        raise NotImplementedError  # pragma: no cover

    def connect_args(self, profile):
        """Allowlisted driver arguments plus the module-owned connect timeout."""
        extra = profile.get("connect_args") or {}
        self._check_args(extra, "connect_args")
        args = dict(extra)
        args.update(self._timeout_connect_args(profile))
        args.update(tls.connect_args(self.name, profile))  # module-produced, not allowlist-checked
        return args

    def configure_engine(self, eng, profile):  # pylint: disable=unused-argument
        """Hook run once after ``create_engine`` (event listeners). Nothing by default."""

    # -- session ---------------------------------------------------------------------------------

    def _set_timeout(self, conn, ms):
        raise NotImplementedError  # pragma: no cover

    def _set_read_only(self, conn):
        raise NotImplementedError  # pragma: no cover

    def session_setup(
        self, conn, profile, timeout_ms, read_only
    ):  # pylint: disable=unused-argument
        """
        Apply the statement timeout and, when ``read_only`` is true, the best-effort read-only guard.

        The guard is not a security control (SECURITY section 2): the database role is. ``timeout_ms`` must be an
        integer within the hard cap; it is converted with ``int()`` before it reaches any SQL text.
        """
        ms = validate_timeout_ms(timeout_ms)
        read_only = _flag(read_only, "read_only")
        self._set_timeout(conn, ms)
        if read_only:
            self._set_read_only(conn)

    def version(self, conn):
        """Server version as text."""
        value = conn.exec_driver_sql(self._version_sql).scalar()
        return "" if value is None else str(value)

    _version_sql = "SELECT 1"


class PostgreSQLBase(Adapter):  # pylint: disable=abstract-method
    """What both PostgreSQL drivers share: the module-owned SQL for the time cap and the read-only guard."""

    name = "postgresql"
    _version_sql = "SHOW server_version"

    def _set_timeout(self, conn, ms):
        conn.exec_driver_sql(f"SET statement_timeout = {int(ms)}")

    def _set_read_only(self, conn):
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")


class PostgreSQLPsycopg(PostgreSQLBase):
    """VERIFIED on PostgreSQL 16 / psycopg 3.3.6 / SQLAlchemy 2.0.55 (SPEC section 7). Profile ``driver: psycopg``."""

    driver = "psycopg"
    module = "psycopg"
    pip_extra = "postgres-psycopg"
    drivername = "postgresql+psycopg"
    allowed_args = frozenset(
        {
            "application_name",
            "sslmode",
            "target_session_attrs",
            "keepalives",
            "keepalives_idle",
            "keepalives_interval",
            "keepalives_count",
        }
    )

    def _timeout_connect_args(self, profile):
        # ``prepare_threshold=0`` makes psycopg use the extended query protocol for EVERY statement, and PostgreSQL
        # refuses a string with more than one statement there (42601 "cannot insert multiple commands into a prepared
        # statement"). With the default (simple protocol when there are no parameters) ``COMMIT; INSERT ...`` or
        # ``SET statement_timeout = 0; ...`` ran as written, which defeated the read-only guard, the time cap, the
        # ``execute`` verb filter and ``dry_run`` (T11 defect D1). Module-owned: not settable from a profile.
        args = {"connect_timeout": profile["connect_timeout_s"], "prepare_threshold": 0}
        if profile.get("password_dropped"):
            # A call retargeted the connection and the profile's password was withheld: an explicitly empty password
            # stops libpq from using $PGPASSWORD, and an unreadable passfile from using ~/.pgpass (SPEC section 3.2).
            args.update(password="", passfile="/nonexistent/sqlx-no-passfile")
        return args


#: Set once per process when a pg8000 connection has proved that a second statement is refused (see ``_prove``).
_PG8000_PROVEN = False
_PG8000_LOCK = threading.Lock()
_PG8000_CURSOR = (
    None  # the single-statement cursor class, built on first use (it subclasses a pg8000 class)
)


#: Python type -> PostgreSQL type OID that pg8000 should declare for a bound value (anything else is left to the server).
_PG8000_OIDS = {
    float: 701,  # float8
    decimal.Decimal: 1700,  # numeric
    bytes: 17,  # bytea
    bytearray: 17,
    datetime.date: 1082,
    datetime.timedelta: 1186,  # interval
    uuid.UUID: 2950,
}


def _pg8000_oid(value):
    """
    The OID psycopg would declare for ``value``, or 0 (unknown: the server infers it from the statement).

    pg8000 declares nothing, so the server types every bind from its context: ``SELECT :a`` then returns text for 5,
    and ``SELECT :a + :b`` is "operator is not unique: unknown + unknown". Declaring the type of plain scalars gives
    the same answers as psycopg (WIRE_SHAPES section 16). Integers pick the smallest integer type like psycopg, so
    ``repeat('x', :n)`` keeps working for small ``n``. Lists, dicts and strings stay undeclared.
    """
    kind = type(value)
    if kind is bool:
        return 16
    if kind is int:
        for bound, oid in ((15, 21), (31, 23), (63, 20)):
            if -(2**bound) <= value < 2**bound:
                return oid
        return 1700
    if kind is datetime.datetime:
        return 1184 if value.tzinfo else 1114
    if kind is datetime.time:
        return 1266 if value.tzinfo else 1083
    return _PG8000_OIDS.get(kind, 0)


def _pg8000_unsafe(why):
    raise CommandExecutionError(
        "[E_DRIVER_MISSING] postgresql driver pg8000 cannot be used safely here "
        f"({why}); install the pip extra 'saltext-sqlx[postgres]' at the version the extra pins"
    )


def _pg8000_cursor_class():
    """
    Return the cursor class that runs every statement through PostgreSQL's extended query protocol.

    pg8000's own cursor sends a statement without arguments through the SIMPLE protocol, and the simple protocol runs
    ``COMMIT; INSERT ...`` as written: that is the T11 defect D1 (read-only guard, time cap, ``execute`` verb filter
    and ``dry_run`` all bypassed). It also scans the text for ``%`` and fails on a literal one. This class sends
    everything with ``Connection.execute_unnamed`` (Parse/Bind/Execute, one statement per message: PostgreSQL answers
    42601 to a second one) and takes the ``$1`` placeholders SQLAlchemy renders for ``paramstyle="numeric_dollar"``.
    ``execute_unnamed`` is a pg8000 internal, hence ``pg8000`` is pinned to a minor version in ``pyproject.toml`` and
    ``_prove`` refuses a driver that does not behave as expected. WIRE_SHAPES section 16.
    """
    global _PG8000_CURSOR  # pylint: disable=global-statement
    if _PG8000_CURSOR is not None:
        return _PG8000_CURSOR
    dbapi = importlib.import_module("pg8000.dbapi")
    core = importlib.import_module("pg8000.core")
    for owner, attr in (
        (core.CoreConnection, "execute_unnamed"),
        (core.CoreConnection, "execute_simple"),
        (dbapi.Cursor, "execute"),
    ):
        if not hasattr(owner, attr):
            _pg8000_unsafe(f"{owner.__name__}.{attr} is missing")
    failed = core.IN_FAILED_TRANSACTION

    class SingleStatementCursor(
        dbapi.Cursor
    ):  # pylint: disable=too-few-public-methods,attribute-defined-outside-init
        """pg8000 cursor: extended protocol only, ``$n`` placeholders, no ``%`` scanning."""

        def execute(self, operation, args=(), stream=None):  # pylint: disable=unused-argument
            conn = self._c
            try:
                if (
                    not conn._in_transaction
                ):  # the module never switches a connection to its non-transactional mode
                    conn.execute_simple("begin transaction")
                if operation.startswith("CLOSE ") and conn._transaction_status == failed:
                    # SQLAlchemy's server-side cursor closes with CLOSE <name>; in an aborted transaction the cursor is
                    # gone already and the statement could only add a second, misleading error.
                    self._context = None
                    self._row_iter = None
                    return
                values = tuple(args)
                self._context = conn.execute_unnamed(
                    operation, vals=values, oids=tuple(_pg8000_oid(v) for v in values)
                )
                self._row_iter = None if self._context.rows is None else iter(self._context.rows)
                self._input_oids = ()
            except AttributeError as exc:
                if self._c is None:
                    raise dbapi.InterfaceError("Cursor closed") from exc
                if self._c._sock is None:
                    raise dbapi.InterfaceError("connection is closed") from exc
                raise
            self.input_types = []

    _PG8000_CURSOR = SingleStatementCursor
    return _PG8000_CURSOR


def _prove(dbapi_conn):
    """
    Once per process: check on a real connection that a second statement is refused.

    Fails closed. If the installed pg8000 ever runs ``SELECT 1; SELECT 2`` as one call, the single-statement guarantee
    is gone and every call is refused with ``[E_DRIVER_MISSING]`` instead of running unguarded SQL.
    """
    global _PG8000_PROVEN  # pylint: disable=global-statement
    if _PG8000_PROVEN:
        return
    with _PG8000_LOCK:
        if _PG8000_PROVEN:
            return
        refused = False
        cur = dbapi_conn.cursor()
        # pylint: disable=broad-exception-caught
        try:
            cur.execute("SELECT 1; SELECT 2")
        except Exception as exc:
            args = getattr(exc, "args", ())
            refused = bool(args) and isinstance(args[0], dict) and args[0].get("C") == "42601"
        try:
            dbapi_conn.rollback()
        except Exception:
            refused = False
        if not refused:
            try:
                dbapi_conn.close()
            except Exception:  # nosec B110 - closing a connection we refuse anyway
                pass
        # pylint: enable=broad-exception-caught
        if not refused:
            _pg8000_unsafe("it did not refuse a second statement")
        _PG8000_PROVEN = True


class PostgreSQLPg8000(PostgreSQLBase):
    """
    pg8000 (pure Python), the PostgreSQL driver used when psycopg is not installed (decision D15). VERIFIED on PostgreSQL 16 / pg8000 1.31.6.

    Everything that differs from psycopg is in WIRE_SHAPES section 16. The connection arguments of pg8000 are not
    libpq's: there is no ``sslmode`` / ``keepalives`` / ``target_session_attrs``; TLS is an ``ssl_context`` that
    ``lib/tls.py`` builds from ``ssl_certs_dir`` (without it the connection is plain TCP, as with psycopg's default
    ``sslmode=prefer`` on a server that does not offer TLS, but without the opportunistic attempt).
    """

    driver = "pg8000"
    module = "pg8000"
    pip_extra = "postgres"
    drivername = "postgresql+pg8000"
    allowed_args = frozenset({"application_name"})
    #: SQLAlchemy renders ``$1`` placeholders; the cursor above sends them as they are (no ``%`` scanning).
    engine_kwargs = {"paramstyle": "numeric_dollar"}

    #: Seconds added to the statement timeout before the client gives up on a silent server (as for PyMySQL, T11 D2).
    READ_TIMEOUT_MARGIN_S = 1.0

    def load_driver(self):
        module = super().load_driver()
        _pg8000_cursor_class()  # raises [E_DRIVER_MISSING] for a pg8000 without the expected internals
        return module

    def _timeout_connect_args(self, profile):
        # ``timeout`` is the connect timeout AND stays on the socket as the read timeout after the connection is made;
        # ``configure_engine`` replaces it by the statement backstop once connected.
        return {"timeout": profile["connect_timeout_s"]}

    def configure_engine(self, eng, profile):
        backstop = int(profile["statement_timeout_ms"]) / 1000 + self.READ_TIMEOUT_MARGIN_S
        cursor_class = _pg8000_cursor_class()

        def on_connect(dbapi_conn, _record):
            dbapi_conn.cursor = lambda: cursor_class(dbapi_conn)
            _prove(dbapi_conn)
            # The connect timeout would otherwise be the limit for every later statement (observed, section 16).
            dbapi_conn._usock.settimeout(backstop)  # pylint: disable=protected-access

        event.listen(eng, "connect", on_connect, insert=True)

        exceptions = importlib.import_module("pg8000.exceptions")

        def do_connect(dialect, _record, cargs, cparams):
            try:
                return dialect.connect(*cargs, **cparams)
            except AttributeError:
                if cparams.get("password"):
                    raise
                # SCRAM with no password: pg8000 fails with ``'NoneType' object has no attribute 'decode'`` where it
                # raises this text for the other password methods (observed, section 16).
                raise exceptions.InterfaceError(  # pylint: disable=raise-missing-from
                    "server requesting password authentication, but no password was provided"
                )

        event.listen(eng, "do_connect", do_connect)


class MySQL(Adapter):
    """MySQL and MariaDB share this adapter (SPEC section 7); the timeout statement differs by server."""

    name = "mysql"
    driver = "pymysql"
    module = "pymysql"
    pip_extra = "mysql"
    drivername = "mysql+pymysql"
    # Verified (docs/WIRE_SHAPES.md sections 2, 6 and 12): these URL-query / connect arguments reach PyMySQL under
    # these names.
    allowed_args = frozenset(
        {
            "charset",
            "read_timeout",
            "write_timeout",
            "ssl_disabled",
            "ssl_verify_cert",
            "ssl_verify_identity",
        }
    )
    _version_sql = "SELECT VERSION()"

    #: Seconds added to the statement timeout before the client gives up (T11 D2).
    READ_TIMEOUT_MARGIN_S = 1.0

    def _timeout_connect_args(self, profile):
        # ``read_timeout`` is the client-side backstop for the server-side limit (T11 D2): the statement itself can
        # lift ``MAX_EXECUTION_TIME`` (``/*+ MAX_EXECUTION_TIME(n) */``) or ``max_statement_time`` (``SET STATEMENT``),
        # and MySQL's variable does not cover INSERT ... SELECT. The margin lets the server's own error (3024 /
        # 1969, ``E_TIMEOUT`` with the real message) arrive first in the normal case; otherwise the driver raises
        # 2013 "timed out", which ``errors._my`` also maps to ``E_TIMEOUT``. A lower ``read_timeout`` in the profile
        # is honoured; a higher one is not.
        backstop = int(profile["statement_timeout_ms"]) / 1000 + self.READ_TIMEOUT_MARGIN_S
        for source in (profile.get("connect_args"), profile.get("query")):
            given = (source or {}).get("read_timeout")
            if isinstance(given, (int, float)) and not isinstance(given, bool) and given > 0:
                backstop = min(backstop, given)
        return {"connect_timeout": profile["connect_timeout_s"], "read_timeout": backstop}

    def _set_timeout(self, conn, ms):
        if getattr(conn.dialect, "is_mariadb", False):
            # Verified on MariaDB 10.11.14 (WIRE_SHAPES section 2): ``max_statement_time`` takes seconds (decimals
            # allowed) and cancels every statement type, INSERT ... SELECT included (1969). A statement can lift it
            # with ``SET STATEMENT max_statement_time=0 FOR ...``; ``read_timeout`` is the backstop (T11 D2).
            conn.exec_driver_sql(
                f"SET SESSION max_statement_time = {int(ms) // 1000}.{int(ms) % 1000:03d}"
            )
        else:
            # Verified on MySQL 8.0.46 (WIRE_SHAPES section 2): ``MAX_EXECUTION_TIME`` in ms applies to SELECT only
            # (3024; a table-less SELECT SLEEP is cut silently) and not to DML. A hint can override it. The
            # ``read_timeout`` connect argument is the backstop for both (T11 D2).
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {int(ms)}")

    def _set_read_only(self, conn):
        # Verified (WIRE_SHAPES section 3): refuses writes with 1792 in the transaction SQLAlchemy/PyMySQL opens
        # implicitly, but statements in the caller's SQL can undo it. The database role is the control.
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")


class Oracle(Adapter):
    """oracledb thin mode (no Instant Client)."""

    name = "oracle"
    driver = "oracledb"
    module = "oracledb"
    pip_extra = "oracle"
    drivername = "oracle+oracledb"
    ping_sql = "SELECT 1 FROM DUAL"
    # T1b: each name was passed to python-oracledb 26.1.0 by the e2e ``oracle_args`` profile and the connection succeeded;
    # the effect of expire_time / retry_* was not measured, ssl_server_dn_match was not run against a TCPS listener.
    allowed_args = frozenset({"expire_time", "retry_count", "retry_delay", "ssl_server_dn_match"})

    def _database(self, profile):
        # SQLAlchemy treats URL.database as an Oracle SID; the profile ``database`` is the service name instead
        # (``service_name`` query argument). T1b: service ``SAKILA`` (a PDB) connected; an unknown service is DPY-6005.
        return None

    def _extra_query(self, profile):
        return {"service_name": profile["database"]}

    def _timeout_connect_args(self, profile):
        # T1b: ``tcp_connect_timeout`` (seconds); an unreachable host failed with DPY-6005 "timed out" after 3.0 s for 3.
        return {"tcp_connect_timeout": profile["connect_timeout_s"]}

    def _set_timeout(self, conn, ms):
        # T1b: ``call_timeout`` in milliseconds, client-side: DPY-4024 for a query, DPY-4011 for a PL/SQL call (E_TIMEOUT).
        conn.connection.driver_connection.call_timeout = int(ms)

    def _set_read_only(self, conn):
        # T1b: must be the first statement of the transaction; writes then fail with ORA-01456. Bypassable by one PL/SQL
        # block that commits (``begin commit; insert ...; commit; end;``): the database role is the control.
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    def version(self, conn):
        # T1b: ``Connection.version`` is "23.26.2.0.0" (no query, no privilege needed).
        return str(getattr(conn.connection.driver_connection, "version", "") or "")


class MSSQL(Adapter):
    """pymssql (FreeTDS). No in-session read-only exists (SPEC section 7): the database role is the only control."""

    name = "mssql"
    driver = "pymssql"
    module = "pymssql"
    pip_extra = "mssql"
    drivername = "mssql+pymssql"
    # T1b: each name was passed to pymssql 2.4.6 by the e2e ``mssql_args`` profile; ``appname`` arrived (``app_name()``).
    allowed_args = frozenset({"appname", "charset", "tds_version"})
    _version_sql = "SELECT @@VERSION"

    def _timeout_connect_args(self, profile):
        # T1b: ``login_timeout`` and ``timeout`` are whole seconds. A statement over ``timeout`` fails with DB-Lib 20003
        # (E_TIMEOUT) and leaves the connection dead; an unreachable host fails with 20009 after ``login_timeout``.
        # ``timeout`` is a connect argument, so there is no session statement for it.
        return {
            "login_timeout": profile["connect_timeout_s"],
            "timeout": math.ceil(profile["statement_timeout_ms"] / 1000),
        }

    def _set_timeout(self, conn, ms):
        """Nothing to run: the timeout was passed as a connect argument."""

    def _set_read_only(self, conn):
        """No in-session read-only on SQL Server."""


_ADAPTERS = {
    (a.name, a.driver): a
    for a in (PostgreSQLPg8000(), PostgreSQLPsycopg(), MySQL(), Oracle(), MSSQL())
}
assert set(_ADAPTERS) == {  # nosec - import-time consistency check, no user input
    (dialect, driver) for dialect, drivers in DIALECT_DRIVER_CHOICES.items() for driver in drivers
}


def adapter(dialect, driver=None):
    """
    Return the adapter for ``dialect`` and ``driver`` (default: the dialect's default driver).

    ``[E_PROFILE_INVALID]`` if there is none.
    """
    if dialect not in DIALECT_DRIVERS:
        _fail(
            "E_PROFILE_INVALID",
            f"unsupported dialect (expected one of: {', '.join(sorted(DIALECT_DRIVERS))})",
        )
    chosen = DIALECT_DRIVERS[dialect] if driver is None else driver
    try:
        return _ADAPTERS[(dialect, chosen)]
    except (KeyError, TypeError):
        _fail(
            "E_PROFILE_INVALID",
            f"unsupported driver for {dialect} (expected one of: {', '.join(DIALECT_DRIVER_CHOICES[dialect])})",
        )
    return None  # pragma: no cover
