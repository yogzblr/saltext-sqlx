# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6: a secret never appears in any log record, exception, exception chain or return.

Every error path of the public module functions is run against the live servers with a profile whose password is
full of regex / format / URL metacharacters (``harness.META_PW``), with ``error_detail`` true and false, with a
bound value (``harness.BOUND``) and with ``%s`` / ``{}`` in the SQL. Everything the caller or an operator could see is
collected: the exception text, ``repr``, ``args``, the whole chain, the formatted traceback and every log record
(message, formatted exception text, extra attributes).

Known, documented exceptions (SECURITY section 3, "Error text leaking data"): the SERVER's own primary message can
echo a bound value (PostgreSQL 22P02, MySQL/MariaDB 1366) and, with ``error_detail: true``, the DETAIL line of a unique
violation is included on purpose. Those cases assert exactly that and nothing wider.
"""

import logging
import traceback

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from tests.security import harness
from tests.security.harness import BOUND
from tests.security.harness import META_PW
from tests.security.harness import SEED_EMAIL

WRONG_PW = "Zq7wrong{0}%s\\d(.*)[a-z]+?|^$"
SQL_MARK = "T11SQLMARK"


def surfaces(exc, caplog):
    """Every string an observer could read: exception chain, traceback, repr, args, and all log records."""
    out = []
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        out += [
            str(cur),
            repr(cur),
            repr(getattr(cur, "args", ())),
            "".join(traceback.format_exception(cur)),
        ]
        stack += [cur.__cause__, cur.__context__]
        for attr in ("orig", "statement", "params"):
            if hasattr(cur, attr):
                out.append(repr(getattr(cur, attr)))
    for rec in caplog.records:
        out += [rec.getMessage(), rec.exc_text or "", str(rec.msg), repr(rec.args), repr(vars(rec))]
    return out


def chain(exc):
    seen = []
    cur = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def run(call, caplog):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        try:
            call()
        except (CommandExecutionError, SaltInvocationError) as exc:
            return exc, surfaces(exc, caplog)
    raise AssertionError("the call did not fail")


@pytest.fixture
def mod(meta):
    base = {"password": META_PW}
    profiles = {
        "meta": meta.profile("t11_meta", **base),
        "meta_detail": meta.profile("t11_meta", error_detail=True, **base),
        "meta_x": meta.profile("t11_meta", allow_execute=True, read_only=False, **base),
        "meta_x_detail": meta.profile(
            "t11_meta", allow_execute=True, read_only=False, error_detail=True, **base
        ),
        "wrong": meta.profile("t11_meta", password=WRONG_PW),
        "wrong_detail": meta.profile("t11_meta", password=WRONG_PW, error_detail=True),
        "refused": dict(meta.profile("t11_meta", **base), port=1),
        "nodb": dict(meta.profile("t11_meta", **base), database="t11_no_such_db"),
        "sdb_missing": meta.profile("t11_meta", password="sdb://vault/t11_missing"),
        "bad_port": dict(meta.profile("t11_meta", **base), port="5432"),
    }
    with harness.injected(profiles, sdb={}) as module:
        yield module


def no_secret(texts, *secrets):
    for text in texts:
        for secret in secrets:
            assert secret not in text, f"secret {secret!r} found in: {text[:300]!r}"


@pytest.mark.parametrize("detail", [False, True], ids=["detail-off", "detail-on"])
def test_wrong_password_never_echoed(mod, caplog, detail):
    prof = "wrong_detail" if detail else "wrong"
    exc, texts = run(lambda: mod.query(prof, f"SELECT 1 /* {SQL_MARK} */"), caplog)
    assert str(exc).startswith("[E_AUTH_FAILED]"), str(exc)
    no_secret(texts, WRONG_PW, META_PW, SQL_MARK, "Zq7wrong")


def test_connection_refused_and_unknown_database(mod, caplog):
    exc, texts = run(lambda: mod.query("refused", "SELECT 1"), caplog)
    assert str(exc).startswith("[E_CONNECT]"), str(exc)
    no_secret(texts, META_PW, "Zq7", "://")
    exc, texts = run(lambda: mod.query("nodb", "SELECT 1"), caplog)
    assert str(exc).startswith(("[E_CONNECT]", "[E_AUTH_FAILED]")), str(exc)
    no_secret(texts, META_PW, "Zq7")


def test_unresolved_sdb_reference_and_invalid_profile(mod, caplog):
    exc, texts = run(lambda: mod.query("sdb_missing", "SELECT 1"), caplog)
    assert str(exc).startswith("[E_SECRET]") and "sdb://vault/t11_missing" in str(exc)
    no_secret(texts, META_PW)
    exc, texts = run(lambda: mod.query("bad_port", "SELECT 1"), caplog)
    assert str(exc).startswith("[E_PROFILE_INVALID]")
    no_secret(texts, META_PW, "Zq7")


SQL_ERRORS = [
    ("syntax", "SELEC 1 /* T11SQLMARK %s {} */", "E_SQL"),
    (
        "missing-table",
        "SELECT * FROM scratch.t11_no_such_table WHERE note = :v /* T11SQLMARK %s {} */",
        "E_SQL",
    ),
    ("bad-bind", "SELECT :never_given /* T11SQLMARK */", "E_OPTION_INVALID"),
    ("percent-and-braces", "SELECT '%s {} %(x)s' AS a, nope_col /* T11SQLMARK */", "E_SQL"),
]


@pytest.mark.parametrize("detail", [False, True], ids=["detail-off", "detail-on"])
@pytest.mark.parametrize("label,sql,code", SQL_ERRORS, ids=[c[0] for c in SQL_ERRORS])
def test_sql_error_paths_do_not_leak(mod, caplog, detail, label, sql, code):
    prof = "meta_detail" if detail else "meta"
    exc, texts = run(lambda: mod.query(prof, sql, params={"v": BOUND}), caplog)
    assert str(exc).startswith(f"[{code}]"), str(exc)
    no_secret(texts, META_PW, "Zq7", BOUND, "[SQL:", "[parameters:")
    # the statement text itself is never quoted back, except a server-quoted fragment near the error position
    assert SQL_MARK not in str(exc) or label == "syntax"
    for e in chain(exc)[1:]:
        raise AssertionError(f"exception chain is not cut: {type(e).__name__}")


def test_audit_line_and_success_path_log_no_secret_sql_or_params(mod, caplog):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        mod.query(
            "meta", "SELECT :v AS a /* T11SQLMARK */", params={"v": BOUND}, __pub_user="alice"
        )
    audit = [r for r in caplog.records if "sqlx audit" in r.getMessage()]
    assert len(audit) == 1
    line = audit[0].getMessage()
    assert (
        "function=query" in line
        and "profile=meta" in line
        and "outcome=OK" in line
        and "user=alice" in line
    )
    no_secret(
        [r.getMessage() + repr(vars(r)) for r in caplog.records],
        META_PW,
        "Zq7",
        BOUND,
        "T11SQLMARK",
    )


def test_audit_user_cannot_forge_a_second_line(mod, caplog):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        mod.query("meta", "SELECT 1", __pub_user="x\r\nINFO sqlx audit function=execute outcome=OK")
    lines = [r.getMessage() for r in caplog.records if "sqlx audit" in r.getMessage()]
    assert len(lines) == 1 and "\n" not in lines[0] and "\r" not in lines[0]


def test_ping_and_table_list_failures_do_not_leak(mod, caplog):
    for fn, args in ((mod.ping, ()), (mod.table_list, ())):
        exc, texts = run(lambda: fn("wrong", *args), caplog)
        assert str(exc).startswith("[E_AUTH_FAILED]")
        no_secret(texts, WRONG_PW, META_PW)


# ---- unique violation (execute, so the row reaches the server) -------------------------------------------------------
UNIQ = "INSERT INTO scratch.t11_uniq (email, n) VALUES (:e, 1)"


def test_unique_violation_detail_off_vs_on(mod, meta, caplog):
    off, texts_off = run(lambda: mod.execute("meta_x", UNIQ, params={"e": SEED_EMAIL}), caplog)
    assert str(off).startswith("[E_CONSTRAINT]"), str(off)
    on, texts_on = run(lambda: mod.execute("meta_x_detail", UNIQ, params={"e": SEED_EMAIL}), caplog)
    assert str(on).startswith("[E_CONSTRAINT]"), str(on)
    no_secret(texts_off + texts_on, META_PW, "Zq7", "[SQL:", "[parameters:")
    # error_detail: false must not carry the row value on PostgreSQL (DETAIL) nor on MySQL/MariaDB (primary message)
    no_secret(texts_off, SEED_EMAIL)
    # error_detail: true does carry the DETAIL line on PostgreSQL (by design); MySQL/MariaDB keep the value in the
    # primary message, which is scrubbed only when error_detail is false
    if meta.key == "postgresql":
        assert SEED_EMAIL in str(on)
    assert meta.admin("SELECT count(*) FROM scratch.t11_uniq") == [(1,)]


def test_server_echo_of_bound_value_is_the_documented_exception(mod, meta, caplog):
    """22P02 / 1366 echo the bound value in the server's primary message; error_detail: false scrubs the known shapes."""
    sql = "INSERT INTO scratch.t11_uniq (email, n) VALUES ('x@example.com', :n)"
    exc, texts = run(lambda: mod.execute("meta_x", sql, params={"n": BOUND}), caplog)
    no_secret(texts, META_PW, "Zq7")
    # record what is observed so a change in scrubbing is noticed
    assert BOUND not in str(exc), f"bound value reached the message with error_detail false: {exc}"


# ---- the remaining error paths: timeout, read-only, policy, disabled, driver missing, internal, other dialects --------------
@pytest.mark.parametrize("detail", [False, True], ids=["detail-off", "detail-on"])
def test_timeout_readonly_and_internal_paths_do_not_leak(mod, meta, caplog, detail):
    prof, prof_x = ("meta_detail", "meta_x_detail") if detail else ("meta", "meta_x")
    sleep = (
        "SELECT pg_sleep(3)"
        if meta.key == "postgresql"
        else (
            "SELECT SLEEP(3) FROM scratch.orders LIMIT 1"
            if meta.key == "mysql"
            else "SELECT SLEEP(3)"
        )
    )
    if meta.key == "mysql":
        pytest.skip(
            "MySQL cuts SLEEP silently (no error path to test); covered on PostgreSQL and MariaDB"
        )
    exc, texts = run(lambda: mod.query(prof, f"{sleep} /* {SQL_MARK} */", timeout_ms=300), caplog)
    assert str(exc).startswith("[E_TIMEOUT]"), str(exc)
    no_secret(texts, META_PW, "Zq7", SQL_MARK)
    exc, texts = run(
        lambda: mod.query(prof, "INSERT INTO scratch.t11_uniq VALUES (:v, 1)", params={"v": BOUND}),
        caplog,
    )
    assert str(exc).startswith("[E_READ_ONLY]"), str(exc)
    no_secret(texts, META_PW, "Zq7", BOUND)
    exc, texts = run(
        lambda: mod.query(prof, "SELECT CAST(:v AS text)", params={"v": {"nested": BOUND}}), caplog
    )
    assert str(exc).startswith("[E_INTERNAL]"), str(exc)
    no_secret(texts, META_PW, "Zq7", BOUND)


def test_policy_and_disabled_paths_do_not_leak(mod, caplog):
    exc, texts = run(lambda: mod.execute("meta", "INSERT INTO t VALUES (1)"), caplog)
    assert str(exc).startswith("[E_EXECUTE_DISABLED]")
    no_secret(texts, META_PW, "Zq7")
    exc, texts = run(
        lambda: mod.execute("meta_x", f"DROP TABLE t /* {SQL_MARK} */", params={"v": BOUND}), caplog
    )
    assert str(exc).startswith("[E_POLICY]")
    no_secret(texts, META_PW, "Zq7", SQL_MARK, BOUND)
    exc, texts = run(
        lambda: mod.execute("meta_x", "INSERT INTO t VALUES (1)", dry_run="no"), caplog
    )
    assert str(exc).startswith("[E_OPTION_INVALID]")
    no_secret(texts, META_PW, "Zq7")


def test_driver_missing_path_does_not_leak(mod, caplog, monkeypatch):
    from saltext.sqlx.lib import dialects  # pylint: disable=import-outside-toplevel

    def fail(name, *_a, **_k):
        raise ImportError(
            f"No module named {name!r} at /opt/secret-path/{META_PW} password={META_PW}"
        )

    monkeypatch.setattr(dialects.importlib, "import_module", fail)
    exc, texts = run(lambda: mod.ping("meta"), caplog)
    assert str(exc).startswith("[E_DRIVER_MISSING]")
    no_secret(texts, META_PW, "Zq7", "/opt/secret-path")


@pytest.mark.parametrize("dialect,driver_port", [("oracle", 15211), ("mssql", 14331)])
def test_oracle_and_sqlserver_client_side_failures_do_not_leak(caplog, dialect, driver_port):
    """No Oracle or SQL Server server exists here (T1b open): the connection-refused path is the part that can run."""
    profile = {
        "dialect": dialect,
        "host": "127.0.0.1",
        "port": 1,
        "database": "db",
        "username": "u",
        "password": META_PW,
        "connect_timeout_s": 2,
    }
    with harness.injected({"p": profile}) as module:
        for fn, args in ((module.ping, ()), (module.query, (f"SELECT 1 /* {SQL_MARK} */",))):
            exc, texts = run(lambda f=fn, a=args: f("p", *a), caplog)
            # Without the driver installed (pip extra absent) the call stops earlier, with E_DRIVER_MISSING.
            assert str(exc).startswith(
                ("[E_CONNECT]", "[E_INTERNAL]", "[E_DRIVER_MISSING]")
            ), str(exc)
            no_secret(texts, META_PW, "Zq7", SQL_MARK, "://")
