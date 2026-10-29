"""
Run SQL on PostgreSQL, MySQL/MariaDB, Oracle and SQL Server and return the real database error.

A thin shell over ``saltext.sqlx.lib`` (SPEC section 4): validate, load the profile, resolve its secret, apply the
caps, run, and turn ANY failure into ``[E_...]`` with ``errors.raise_for``. The Salt dunders are used only here.
All functions take their options as keywords (``options.validate`` checks them and drops Salt's ``__pub_*``).
"""

import hashlib
import logging
import re
import time

from salt.exceptions import CommandExecutionError

try:
    import sqlalchemy

    from saltext.sqlx.lib import checks
    from saltext.sqlx.lib import engine
    from saltext.sqlx.lib import errors
    from saltext.sqlx.lib import options
    from saltext.sqlx.lib import profiles
    from saltext.sqlx.lib import runner
    from saltext.sqlx.lib import secrets

    HAS_SQLALCHEMY = True
except ImportError:  # pragma: no cover - tested by loading this file with SQLAlchemy hidden
    HAS_SQLALCHEMY = False

__virtualname__ = "sqlx"

log = logging.getLogger(__name__)
if HAS_SQLALCHEMY:
    _MASK = secrets.MaskingFilter()
    log.addFilter(_MASK)


def __virtual__():
    if not HAS_SQLALCHEMY:
        return (False, "sqlx: SQLAlchemy is not installed (pip install saltext-sqlx)")
    return __virtualname__


def _run(function, work, kwargs):
    """The one call path: validate, profile, secret, caps, ``work``, audit. Any failure goes to ``raise_for``."""
    started = time.perf_counter()
    user = kwargs.get("__pub_user")
    info = {"fn": function, "profile": "-", "dialect": "-", "sha": "-", "rows": "-", "code": "OK"}
    info["user"] = re.sub(r"\W", "_", str(user)) if user else "-"
    profile = {"dialect": "unknown", "error_detail": False}
    try:
        opts = options.validate(function, kwargs)
        profile = profiles.load_profile(opts["profile"], __salt__["config.get"]("sqlx", {}))
        info.update(profile=profile["name"], dialect=profile["dialect"])
        if "sql" in opts:
            info["sha"] = hashlib.sha256(opts["sql"].encode("utf-8")).hexdigest()[:12]
        if function == "execute" and not profile["allow_execute"]:
            raise CommandExecutionError(
                f"[E_EXECUTE_DISABLED] profile '{profile['name']}' does not set allow_execute: true"
            )
        resolved = secrets.resolve_profile_secrets(profile, __salt__["sdb.get"])
        _MASK.add_secret(resolved.get("password"))
        result = work(options.apply_caps(opts, profile), resolved)
        result["profile"] = profile["name"]
        info["rows"] = result.get("row_count", result.get("rows_affected", "-"))
        return result
    except Exception as exc:  # pylint: disable=broad-except
        detail = profile["error_detail"]
        info["code"] = errors.translate(exc, profile["dialect"], error_detail=detail)[0]
        errors.raise_for(exc, profile["dialect"], error_detail=detail)
    finally:
        info["ms"] = int((time.perf_counter() - started) * 1000)
        log.info(  # SECURITY section 5: never the SQL, the params, the rows or a secret
            "sqlx audit function=%(fn)s profile=%(profile)s dialect=%(dialect)s sql_sha256=%(sha)s "
            "rows=%(rows)s elapsed_ms=%(ms)s outcome=%(code)s user=%(user)s",
            info,
        )


def _query(opts, resolved):
    with engine.connection(resolved, resolved.get("password"), opts["timeout_ms"]) as conn:
        return runner.run_query(
            conn, opts["sql"], opts["params"], limit=opts["limit"], max_bytes=opts["max_bytes"]
        )


def query(profile=None, sql=None, **kwargs):
    """
    Run one statement in a transaction that is always rolled back.

    Keywords: ``params`` (object of bind values), ``limit``, ``timeout_ms``, ``max_bytes`` (each can only lower
    the profile's cap). Returns ``profile``, ``columns``, ``rows``, ``row_count``, ``truncated``, ``elapsed_ms``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.query profile=orders_pg sql="select id, total from orders where status = :s" params='{"s": "open"}'
    """
    return _run("query", _query, dict(kwargs, profile=profile, sql=sql))


def execute(profile=None, sql=None, **kwargs):
    """
    Run one data-changing statement. Refused with ``[E_EXECUTE_DISABLED]`` unless the profile has ``allow_execute``.

    Keywords: ``params``, ``dry_run`` (default false; true rolls back), ``timeout_ms``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.execute profile=orders_rw sql="update orders set status = 'x' where id = 1" dry_run=True
    """

    def work(opts, resolved):
        # An execute-enabled profile must be able to write, so the best-effort read-only guard is not applied.
        with engine.connection(
            resolved, resolved.get("password"), opts["timeout_ms"], False
        ) as conn:
            return runner.run_execute(
                conn,
                opts["sql"],
                opts["params"],
                dry_run=opts["dry_run"],
                execute_verbs=resolved["execute_verbs"],
            )

    return _run("execute", work, dict(kwargs, profile=profile, sql=sql))


def check_query_result(profile=None, sql=None, operator=None, **kwargs):
    """
    Compare ``rows[row][column]`` with ``expected``. A failed comparison is ``ok: false``, not an error.

    Keywords: ``expected`` (not needed for ``is_null`` / ``is_not_null``), ``params``, ``row``, ``column``,
    ``retry_timeout_s``, ``retry_pause_s``. Operators: ``== != < <= > >= contains not_contains is_null is_not_null``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.check_query_result profile=orders_pg sql="select count(*) from orders" operator=">" expected=0
    """

    def work(opts, resolved):
        return checks.check_query_result(
            lambda: _query(opts, resolved),
            opts["operator"],
            opts["expected"],
            opts["row"],
            opts["column"],
            opts["retry_timeout_s"],
            opts["retry_pause_s"],
        )

    return _run(
        "check_query_result", work, dict(kwargs, profile=profile, sql=sql, operator=operator)
    )


def check_row_count(profile=None, sql=None, operator=None, **kwargs):
    """
    Compare the number of returned rows with ``expected``. A failed comparison is ``ok: false``, not an error.

    Keywords: ``expected``, ``params``, ``retry_timeout_s``, ``retry_pause_s``. Operators: ``== != < <= > >=``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.check_row_count profile=orders_pg sql="select 1 from orders" operator=">=" expected=1
    """

    def work(opts, resolved):
        return checks.check_row_count(
            lambda: _query(opts, resolved),
            opts["operator"],
            opts["expected"],
            opts["retry_timeout_s"],
            opts["retry_pause_s"],
        )

    return _run("check_row_count", work, dict(kwargs, profile=profile, sql=sql, operator=operator))


def ping(profile=None, **kwargs):
    """
    Connect and run the dialect's trivial statement. Returns ``ok``, ``dialect``, ``server_version``, ``elapsed_ms``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.ping profile=orders_pg
    """
    return _run(
        "ping",
        lambda _o, resolved: engine.ping(resolved, resolved.get("password")),
        dict(kwargs, profile=profile),
    )


def _like(pattern):
    """A SQL ``LIKE`` pattern (``%`` and ``_``) as a regular expression."""
    parts = {"%": ".*", "_": "."}
    return re.compile("".join(parts.get(c) or re.escape(c) for c in pattern), re.DOTALL)


def table_list(profile=None, **kwargs):
    """
    List tables (and views with ``include_views=True``) with ``sqlalchemy.inspect``.

    Keywords: ``schema`` (default: the connection's default schema), ``pattern`` (SQL ``LIKE`` syntax),
    ``include_views``. Returns ``profile``, ``schema``, ``tables``, ``views``.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.table_list profile=orders_pg schema=public pattern='ord%' include_views=True
    """

    def work(opts, resolved):
        match = _like(opts["pattern"]).fullmatch if opts["pattern"] else lambda _name: True
        with engine.connection(resolved, resolved.get("password")) as conn:
            inspector = sqlalchemy.inspect(conn)
            schema = opts["schema"] or inspector.default_schema_name
            tables = [n for n in inspector.get_table_names(schema=schema) if match(n)]
            views = inspector.get_view_names(schema=schema) if opts["include_views"] else []
        return {
            "schema": schema,
            "tables": sorted(tables),
            "views": sorted(v for v in views if match(v)),
        }

    return _run("table_list", work, dict(kwargs, profile=profile))
