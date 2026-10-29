"""Build a NullPool SQLAlchemy engine from a validated profile (ARCHITECTURE sections 2 and 3).

One engine per call, ``NullPool``, always disposed (R14). ``connection`` also applies the dialect's session setup
and turns any first-connect failure into a ``CommandExecutionError`` whose message never contains the password, the
URL, or a bare ``TypeError`` (R3). Full SQLSTATE/vendor-code mapping of statement errors is ``lib/errors.py`` (T5);
the connect-time classification here is deliberately small and should be replaced by ``errors.translate`` then.

This module has no Salt dunders. The password is a parameter and lives only in locals.
"""

import contextlib
import re
import time
from urllib.parse import quote
from urllib.parse import quote_plus

from salt.exceptions import CommandExecutionError
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from saltext.sqlx.lib import dialects

__all__ = ["build_engine", "connection", "managed_engine", "ping"]

#: connect-time authentication failures (SPEC section 8): PG SQLSTATE, MySQL, Oracle ORA-, MSSQL error numbers.
_AUTH_CODES = {
    "postgresql": {"28P01", "28000"},
    "mysql": {1045},
    "oracle": {1017},
    "mssql": {18456},
}
#: VERIFIED (PostgreSQL 16, psycopg 3.3.6): a failed connect carries no ``sqlstate``/``diag``; only this text identifies
#: a bad password, so PostgreSQL falls back to it.
_PG_AUTH_TEXT = "password authentication failed"
_URL_RE = re.compile(r"[A-Za-z][\w+.\-]*://\S+")
_MAX_MESSAGE = 300
_ASCII_TYPEERROR = "bytes-like object"


def _effective(profile, timeout_ms):
    """Copy of ``profile`` whose statement timeout is lowered (never raised) to ``timeout_ms``."""
    out = dict(profile)
    if timeout_ms is not None:
        out["statement_timeout_ms"] = min(
            int(profile["statement_timeout_ms"]), dialects.validate_timeout_ms(timeout_ms)
        )
    return out


def build_engine(profile, password, timeout_ms=None):
    """
    Return a ``NullPool`` engine for the validated ``profile``.

    The connect timeout comes from the profile; ``timeout_ms`` can only lower the statement timeout. Nothing
    connects here. ``[E_DRIVER_MISSING]`` if the driver is not installed, ``[E_POLICY]`` for a forbidden
    ``connect_args``/``query`` key. The caller owns ``dispose()``; prefer ``managed_engine`` or ``connection``.
    """
    prof = _effective(profile, timeout_ms)
    adapter = dialects.adapter(prof["dialect"])
    adapter.load_driver()
    return create_engine(
        adapter.url(prof, password),
        poolclass=NullPool,
        connect_args=adapter.connect_args(prof),
        hide_parameters=True,
        echo=False,
    )


@contextlib.contextmanager
def managed_engine(profile, password, timeout_ms=None):
    """Yield an engine and always dispose it (R14)."""
    eng = build_engine(profile, password, timeout_ms)
    try:
        yield eng
    finally:
        eng.dispose()


def _scrub(text, password):
    """Remove the password (raw and URL-quoted forms) and any URL from driver text; single line, bounded."""
    text = str(text)
    if password:
        for form in sorted(
            {password, quote(password, safe=""), quote_plus(password)}, key=len, reverse=True
        ):
            text = text.replace(form, "***")
    text = _URL_RE.sub("<url>", text)
    text = " ".join(text.split())
    return text[:_MAX_MESSAGE]


def _driver_code(orig):
    """Vendor/SQLSTATE code of a driver exception, or None."""
    sqlstate = getattr(orig, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return sqlstate
    first = orig.args[0] if getattr(orig, "args", None) else None
    code = getattr(first, "code", None)  # oracledb: args[0] is an _Error with .code
    if isinstance(code, int):
        return code
    return first if isinstance(first, int) and not isinstance(first, bool) else None


def _driver_message(orig):
    parts = []
    for arg in getattr(orig, "args", ()) or ():
        if isinstance(arg, (bool, int)):
            continue
        if isinstance(arg, bytes):
            arg = arg.decode("utf-8", "replace")
        message = getattr(arg, "message", None)
        parts.append(str(message if message is not None else arg))
    return " ".join(parts) or type(orig).__name__


def _is_auth_failure(dialect, orig):
    if _driver_code(orig) in _AUTH_CODES.get(dialect, ()):
        return True
    return dialect == "postgresql" and _PG_AUTH_TEXT in str(orig)


def _connect_error(exc, dialect, password):
    """Map one exception raised by ``Engine.connect()`` to a ``CommandExecutionError``. No chaining."""
    if isinstance(exc, DBAPIError):
        orig = exc.orig if exc.orig is not None else exc
        kind = "E_AUTH_FAILED" if _is_auth_failure(dialect, orig) else "E_CONNECT"
        # UNVERIFIED(T1): connect-time exception classes and codes for MySQL, Oracle and SQL Server.
        return CommandExecutionError(
            f"[{kind}] {dialect}: {_scrub(_driver_message(orig), password)}"
        )
    if isinstance(exc, OSError):
        return CommandExecutionError(f"[E_CONNECT] {dialect}: {_scrub(exc, password)}")
    if isinstance(exc, TypeError) and dialect == "postgresql" and _ASCII_TYPEERROR in str(exc):
        # psycopg 3 returns bytes for text in a SQL_ASCII database and SQLAlchemy's first-connect
        # initialisation then raises this TypeError (BUILD_PROMPT mistakes table).
        return CommandExecutionError(
            "[E_CONNECT] postgresql: the database encoding must be UTF8; a SQL_ASCII database is not "
            "supported by psycopg 3 (recreate it with ENCODING 'UTF8')"
        )
    return CommandExecutionError(f"[E_INTERNAL] {dialect}: {type(exc).__name__}")


def _open(eng, dialect, password):
    failure = None
    conn = None
    try:
        conn = eng.connect()
    except Exception as exc:  # pylint: disable=broad-except
        failure = _connect_error(exc, dialect, password)
    if failure is not None:
        # Raised outside the except block: no __context__/__cause__ keeps the driver text or password alive.
        raise failure
    return conn


@contextlib.contextmanager
def connection(profile, password, timeout_ms=None, read_only=None):
    """
    Yield a connection with the dialect's session setup applied; always close it and dispose the engine.

    ``timeout_ms`` can only lower the profile's ``statement_timeout_ms``. ``read_only`` defaults to the profile's
    value. The read-only guard is best effort only (SECURITY section 2). Connect failures raise
    ``[E_CONNECT]``, ``[E_AUTH_FAILED]`` or ``[E_INTERNAL]`` without the password or URL.
    """
    prof = _effective(profile, timeout_ms)
    adapter = dialects.adapter(prof["dialect"])
    with managed_engine(prof, password) as eng:
        conn = _open(eng, prof["dialect"], password)
        try:
            adapter.session_setup(
                conn,
                prof,
                prof["statement_timeout_ms"],
                prof["read_only"] if read_only is None else read_only,
            )
            yield conn
        finally:
            conn.close()


def ping(profile, password, timeout_ms=None):
    """Connect, run the dialect's trivial statement and return the SPEC 4.5 result dict."""
    adapter = dialects.adapter(profile["dialect"])
    start = time.perf_counter()
    with connection(profile, password, timeout_ms) as conn:
        conn.exec_driver_sql(adapter.ping_sql)
        server_version = adapter.version(conn)
    return {
        "ok": True,
        "dialect": profile["dialect"],
        "server_version": server_version,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
    }
