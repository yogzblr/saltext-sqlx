"""Build a NullPool SQLAlchemy engine from a validated profile (ARCHITECTURE sections 2 and 3).

One engine per call, ``NullPool``, always disposed (R14). ``connection`` also applies the dialect's session setup
and turns any failure of the connect, the session setup and the ``ping`` statements into the error of
``errors.raise_for`` (the single error path, R1), with the password and any URL removed from the text (R3).

This module has no Salt dunders. The password is a parameter and lives only in locals.
"""

import contextlib
import re
import time

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import errors
from saltext.sqlx.lib import secrets

__all__ = ["build_engine", "connection", "managed_engine", "ping"]

_URL_RE = re.compile(r"[A-Za-z][\w+.\-]*://\S+")
_MAX_MESSAGE = 1000


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


def _fail(exc, profile, password):
    """
    Raise the translated error for ``exc`` through :func:`errors.raise_for`, the one error path (R1).

    The result is a ``CommandExecutionError`` (or the ``SaltInvocationError`` it was already). As a second line of
    defence the password (raw and URL-quoted) and any URL are removed from the final text. Called from outside
    the ``except`` block that caught ``exc`` so no chaining keeps the driver text alive.
    """
    failure = None
    try:
        errors.raise_for(exc, profile["dialect"], error_detail=profile.get("error_detail", False))
    except (CommandExecutionError, SaltInvocationError) as err:
        text = _URL_RE.sub("<url>", secrets.mask(str(err), [password]))
        failure = type(err)(text[:_MAX_MESSAGE])
    raise failure


def _guarded(profile, password, step, *args):
    """Run ``step(*args)``; any exception is translated by :func:`_fail`. Returns the step's result."""
    failure = None
    result = None
    try:
        result = step(*args)
    except Exception as exc:  # pylint: disable=broad-except
        failure = exc
    if failure is not None:
        _fail(failure, profile, password)
    return result


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
        conn = _guarded(prof, password, eng.connect)
        try:
            _guarded(
                prof,
                password,
                adapter.session_setup,
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
        _guarded(profile, password, conn.exec_driver_sql, adapter.ping_sql)
        server_version = _guarded(profile, password, adapter.version, conn)
    return {
        "ok": True,
        "dialect": profile["dialect"],
        "server_version": server_version,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
    }
