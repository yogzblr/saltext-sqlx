"""Run caller SQL inside a transaction: ``run_query`` (always rolled back) and ``run_execute`` (SPEC sections 4.1, 4.2).

No Salt dunders: the connection, limits and policy are passed in. Caller SQL always goes through
:func:`sqlalchemy.text` with bind parameters (BUILD_PROMPT R11), never ``exec_driver_sql``.

Errors are not translated here. Whatever the driver or SQLAlchemy raises propagates unchanged (after the
transaction is rolled back); the module turns it into ``[E_...]`` with :func:`saltext.sqlx.lib.errors.raise_for`.
The two policy errors raised here, ``[E_POLICY]`` and ``[E_OPTION_INVALID]``, never contain the SQL text or a bound
value.

Two rules are enforced by a source scan (``tests/unit/lib/test_guard.py``): the statement that makes a transaction
permanent appears only inside :func:`run_execute`, and nothing in this module touches a database driver.
"""

import re
import time

from salt.exceptions import SaltInvocationError
from sqlalchemy import text

from saltext.sqlx.lib import serialize

_KEYWORD = re.compile(r"[A-Za-z]\w*", re.ASCII)
_WRITE_WORD = re.compile(r"\b(?:insert|update|delete|merge)\b", re.IGNORECASE | re.ASCII)
_INTO_WORD = re.compile(r"\binto\b", re.IGNORECASE | re.ASCII)

#: Dialects whose driver can hand rows over without buffering the whole result (T11 D3). ``mysql`` is PyMySQL's
#: ``SSCursor`` (also MariaDB), ``postgresql`` is a psycopg server-side cursor.
_STREAMING = {"postgresql", "mysql"}
#: Dialects where an unfinished unbuffered result must be abandoned by closing the connection: PyMySQL would read
#: (and discard) every remaining row on ``close()`` / ``rollback()``.
_ABANDON_UNREAD = {"mysql"}
#: Statements a PostgreSQL ``DECLARE ... CURSOR`` accepts. Anything else (SHOW, EXPLAIN, INSERT ... RETURNING, CALL)
#: is small by nature and is run buffered.
_PG_CURSOR_VERBS = {"select", "values", "table", "with"}


def _elapsed_ms(start):
    return int((time.perf_counter() - start) * 1000)


def _rollback(conn):
    """Roll back, ignoring a failure to do so.

    A broken connection cannot be rolled back, and the server discards the open transaction when the connection
    closes. Raising here would replace the real error that is already on its way to the caller.
    """
    try:
        conn.rollback()
    except Exception:  # pylint: disable=broad-except
        pass


def _dialect_name(conn):
    return getattr(getattr(conn, "dialect", None), "name", None)


def _stream(conn, sql):
    """Whether ``sql`` can be read through a streaming cursor on this connection (T11 D3)."""
    name = _dialect_name(conn)
    if name not in _STREAMING:
        return False
    if name == "mysql":
        return True
    start, problem = _leading(sql)
    if start is None or problem:
        return False
    found = _KEYWORD.match(sql, start)
    verb = found.group(0).lower() if found else ""
    if verb not in _PG_CURSOR_VERBS:
        return False
    # DECLARE ... CURSOR refuses SELECT ... INTO and data-modifying statements in WITH. The words are looked for
    # anywhere in the text: a false positive only means the statement is read buffered, never that it fails.
    if verb == "with" and _WRITE_WORD.search(sql):
        return False
    return not (verb == "select" and _INTO_WORD.search(sql))


def run_query(conn, sql, params, *, limit, max_bytes):
    """Run one statement, read at most ``limit`` rows and ALWAYS roll back; nothing is ever made permanent.

    Fetches ``limit + 1`` rows so that ``truncated`` is known, then hands them to
    :func:`saltext.sqlx.lib.serialize.build_result`, which also applies ``max_bytes``. A statement that returns no
    rows gives ``columns: []`` and ``rows: []``. Returns ``columns``, ``rows``, ``row_count``, ``truncated`` and
    ``elapsed_ms``. The module adds ``profile``.

    On PostgreSQL (plain ``SELECT`` / ``VALUES`` / ``TABLE`` / read-only ``WITH``) and MySQL/MariaDB the rows are read
    through a streaming cursor, so ``fetchmany(limit + 1)`` reads only that many rows from the server and memory stays
    bounded by the caps, not by the size of the result (T11 D3). If rows are left unread on MySQL/MariaDB the
    connection is abandoned instead of drained. One value larger than ``max_bytes`` is still received whole by the
    driver; that is the server's packet limit (MySQL) or a ``LEFT(col, n)`` in the statement (PostgreSQL).

    The rollback is not a read-only control: caller SQL can end its own transaction (SECURITY section 2). The
    database role is the control.
    """
    start = time.perf_counter()
    result = None
    abandoned = False
    try:
        extra = {"execution_options": {"stream_results": True}} if _stream(conn, sql) else {}
        result = conn.execute(text(sql), params or {}, **extra)
        if result.returns_rows:
            columns = list(result.keys())
            raw_rows = result.fetchmany(limit + 1)
        else:
            columns, raw_rows = [], []
        if len(raw_rows) > limit and _dialect_name(conn) in _ABANDON_UNREAD:
            abandoned = True
            conn.invalidate()  # closes the driver connection; the server discards the open transaction
        out = serialize.build_result(columns, raw_rows, limit, max_bytes)
        out["elapsed_ms"] = _elapsed_ms(start)
        return out
    finally:
        if not abandoned:
            if result is not None:
                try:
                    result.close()
                except Exception:  # pylint: disable=broad-except
                    pass
            _rollback(conn)


def _fail(code, message):
    raise SaltInvocationError(f"[{code}] {message}")


def _leading(sql):
    """Locate the first token after whitespace and comments. Returns ``(index, problem)``.

    ``index`` is ``None`` when nothing else is left (empty, whitespace only, comment only, or an unterminated block
    comment). ``problem`` is ``None`` or the reason the position cannot be trusted:

    * ``"executable"``: a ``/*!`` or ``/*M!`` comment, which MySQL and MariaDB run as SQL (T11 D5);
    * ``"nested"``: a block comment that contains ``/*``. PostgreSQL nests block comments, MySQL does not, so the
      two servers disagree about where such a comment ends and the verb starts.

    ``/*+ ... */`` is an ordinary comment at the start of a statement and is skipped.
    """
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            if end < 0:
                return None, None
            i = end + 1
        elif sql.startswith("/*", i):
            if sql[i : i + 4].upper() == "/*M!" or sql.startswith("/*!", i):
                return i, "executable"
            end = sql.find("*/", i + 2)
            if end < 0:
                return None, None
            if sql.find("/*", i + 2, end) >= 0:
                return i, "nested"
            i = end + 2
        else:
            return i, None
    return None, None


def _check_verb(sql, execute_verbs):
    """Best-effort first-keyword filter. NOT a security boundary: a CTE, a ``;`` or a function can still write."""
    start, problem = _leading(sql)
    if start is None:
        _fail("E_OPTION_INVALID", "the statement is empty or contains only comments")
    if problem == "executable":
        _fail(
            "E_POLICY",
            "the statement starts with a /*! or /*M! comment, which MySQL and MariaDB execute; remove it",
        )
    if problem == "nested":
        _fail(
            "E_POLICY",
            "the statement starts with a block comment that contains /*; servers disagree where it ends",
        )
    verbs = [execute_verbs] if isinstance(execute_verbs, str) else list(execute_verbs)
    if isinstance(execute_verbs, (set, frozenset)):
        verbs.sort(key=str)
    allowed = {str(v).lower() for v in verbs}
    listing = ", ".join(str(v).lower() for v in verbs)
    found = _KEYWORD.match(sql, start)
    if found is None:
        _fail(
            "E_POLICY", f"the statement does not start with a SQL keyword; allowed verbs: {listing}"
        )
    verb = found.group(0)
    if verb.lower() not in allowed:
        _fail("E_POLICY", f"statement verb '{verb}' is not in execute_verbs ({listing})")


def run_execute(conn, sql, params, *, dry_run, execute_verbs):
    """Run one data-changing statement and make it permanent only when ``dry_run`` is false.

    This is the ONLY function in ``src/`` that may make a transaction permanent (BUILD_PROMPT R5). It does NOT check
    that the profile allows ``execute``: the caller (``sqlx_mod.execute``) must refuse with ``[E_EXECUTE_DISABLED]``
    unless the profile has ``allow_execute: true`` before it opens a connection or calls this function.

    The first keyword of the statement (after whitespace and ``--`` / ``/* */`` comments; a leading ``/*!``, ``/*M!`` or nested comment is
    ``[E_POLICY]``) must be in
    ``execute_verbs`` (case-insensitive), else ``[E_POLICY]``; an empty or comment-only statement is
    ``[E_OPTION_INVALID]``. The filter is best effort and bypassable; the database role limits what ``execute`` can do.
    ``dry_run`` must be a real ``bool`` (anything else is ``[E_OPTION_INVALID]``, so a malformed flag can never
    commit by accident). ``options.validate`` turns ``dry_run=None`` ("not given") into the default ``False`` before
    this function is reached, so ``None`` at the call level COMMITS (T11 D11).

    With ``dry_run`` true the statement runs, ``rows_affected`` reports what it changed, and the transaction is rolled
    back. On any exception the transaction is rolled back and the original exception is re-raised.

    Returns ``rows_affected``, ``committed``, ``dry_run`` and ``elapsed_ms``.
    """
    if not isinstance(dry_run, bool):
        _fail("E_OPTION_INVALID", "dry_run must be a boolean")
    _check_verb(sql, execute_verbs)
    start = time.perf_counter()
    committed = False
    try:
        result = conn.execute(text(sql), params or {})
        rows_affected = result.rowcount
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            committed = True
    except BaseException:
        _rollback(conn)
        raise
    return {
        "rows_affected": rows_affected,
        "committed": committed,
        "dry_run": dry_run,
        "elapsed_ms": _elapsed_ms(start),
    }
