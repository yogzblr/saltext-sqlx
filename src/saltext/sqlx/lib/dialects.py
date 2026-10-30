"""Per-dialect adapter table (ARCHITECTURE section 8).

One small adapter per dialect: URL building, the ``connect_args`` allowlist, session setup (statement timeout and
the best-effort read-only guard), ping statement and server version. ``DIALECT_DRIVERS`` is the single source of
truth for the dialect to driver pairing; ``lib/profiles.py`` imports it.

Rules honoured here:

* R13: URLs are built with ``sqlalchemy.engine.URL.create``; nothing is formatted into a URL string.
* R15: a driver is imported only inside ``Adapter.load_driver`` (lazily). A missing driver raises
  ``[E_DRIVER_MISSING]`` naming only the pip extra, and affects only that dialect.
* R4 / SECURITY section 3: profile ``query`` and ``connect_args`` keys both reach the driver, so both are checked
  against the same per-driver allowlist; anything else is ``[E_POLICY]``. Messages name the key, never the value.
* The module's own SQL is constant text plus at most one integer that went through ``int()`` (ARCHITECTURE
  section 3). Caller SQL never passes through this module.

PostgreSQL, MySQL and MariaDB behaviour is verified (docs/WIRE_SHAPES.md). Every statement or mechanism for Oracle
and SQL Server comes from documentation (SPEC section 7) and is marked ``UNVERIFIED(T1)``; each mechanism sits in
its own small function so that recorded facts can replace it without touching anything else. This module has no Salt dunders.
"""

import importlib
import math

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy.engine import URL

from saltext.sqlx.lib import options
from saltext.sqlx.lib import tls

#: dialect -> the only driver allowed with it (SPEC section 7). Single source of truth.
DIALECT_DRIVERS = {
    "postgresql": "psycopg",
    "mysql": "pymysql",
    "oracle": "oracledb",
    "mssql": "pymssql",
}

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


class PostgreSQL(Adapter):
    """VERIFIED on PostgreSQL 16 / psycopg 3.3.6 / SQLAlchemy 2.0.55 (SPEC section 7)."""

    name = "postgresql"
    driver = "psycopg"
    module = "psycopg"
    pip_extra = "postgres"
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
    _version_sql = "SHOW server_version"

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

    def _set_timeout(self, conn, ms):
        conn.exec_driver_sql(f"SET statement_timeout = {int(ms)}")

    def _set_read_only(self, conn):
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")


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
    # UNVERIFIED(T1): argument names are from the oracledb documentation.
    allowed_args = frozenset({"expire_time", "retry_count", "retry_delay", "ssl_server_dn_match"})

    def _database(self, profile):
        # UNVERIFIED(T1): SQLAlchemy treats URL.database as an Oracle SID; the profile ``database`` is used as the
        # service name instead (``service_name`` query argument), which is what current Oracle servers expose.
        return None

    def _extra_query(self, profile):
        return {"service_name": profile["database"]}

    def _timeout_connect_args(self, profile):
        # UNVERIFIED(T1): oracledb ``tcp_connect_timeout`` (seconds) from documentation.
        return {"tcp_connect_timeout": profile["connect_timeout_s"]}

    def _set_timeout(self, conn, ms):
        # UNVERIFIED(T1): ``call_timeout`` is documented in milliseconds on the oracledb connection object.
        conn.connection.driver_connection.call_timeout = int(ms)

    def _set_read_only(self, conn):
        # UNVERIFIED(T1): must be the first statement of the transaction (documentation).
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    def version(self, conn):
        # UNVERIFIED(T1): oracledb exposes the server version as ``Connection.version`` (no query, no privilege).
        return str(getattr(conn.connection.driver_connection, "version", "") or "")


class MSSQL(Adapter):
    """pymssql (FreeTDS). No in-session read-only exists (SPEC section 7): the database role is the only control."""

    name = "mssql"
    driver = "pymssql"
    module = "pymssql"
    pip_extra = "mssql"
    drivername = "mssql+pymssql"
    # UNVERIFIED(T1): argument names are from the pymssql documentation.
    allowed_args = frozenset({"appname", "charset", "tds_version"})
    _version_sql = "SELECT @@VERSION"

    def _timeout_connect_args(self, profile):
        # UNVERIFIED(T1): pymssql ``login_timeout`` and ``timeout`` (query timeout) are seconds, per documentation.
        # ``timeout`` is a connect argument, so there is no session statement for it.
        return {
            "login_timeout": profile["connect_timeout_s"],
            "timeout": math.ceil(profile["statement_timeout_ms"] / 1000),
        }

    def _set_timeout(self, conn, ms):
        """Nothing to run: the timeout was passed as a connect argument."""

    def _set_read_only(self, conn):
        """No in-session read-only on SQL Server."""


_ADAPTERS = {a.name: a for a in (PostgreSQL(), MySQL(), Oracle(), MSSQL())}
assert set(_ADAPTERS) == set(
    DIALECT_DRIVERS
)  # nosec - import-time consistency check, no user input


def adapter(dialect):
    """Return the adapter for ``dialect``; ``[E_PROFILE_INVALID]`` if there is none."""
    try:
        return _ADAPTERS[dialect]
    except (KeyError, TypeError):
        _fail(
            "E_PROFILE_INVALID",
            f"unsupported dialect (expected one of: {', '.join(sorted(DIALECT_DRIVERS))})",
        )
    return None  # pragma: no cover
