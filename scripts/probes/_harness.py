"""Shared probe harness for T1 (docs/WIRE_SHAPES.md).

Every probe_<dialect>.py builds a config dict and calls ``main(cfg)``. The harness runs one fixed list of cases per
dialect, records what was OBSERVED (exception classes, codes, message fields, password leakage, guard/bypass results,
timeouts, text() bind traps, type round-trips) and writes JSON to ``docs/wire_shapes/<dialect>__sa<version>.json``.

Rules: never state a database behaviour that was not observed (BUILD_PROMPT R18). If the server is not reachable the
case is recorded as ``NOT RUN`` with the reason; nothing is filled in from documentation.

Run it with the repo root as cwd and ``python -I`` (R20: never from src/saltext).
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import os
import socket
import sys
import traceback
import uuid

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy import text
from sqlalchemy.pool import NullPool

SIMPLE = (str, int, float, bool, type(None))
WRONG_PW = (
    "S3cret" + "Pw_wrong"
)  # built from pieces so the literal never appears in a traceback source line


def _short(s, n=400):
    s = str(s)
    return s if len(s) <= n else s[:n] + "...[+%d]" % (len(s) - n)


def _qual(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


def _simple_attrs(obj):
    """Public, non-callable attributes with simple values: shows which message/code fields exist on an exception."""
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
        except Exception:  # pylint: disable=broad-except
            continue
        if callable(val):
            continue
        if isinstance(val, bytes):
            val = val.decode("utf-8", "replace")
        if isinstance(val, SIMPLE):
            out[name] = val
    return out


def classify(e, secrets=(), sql_texts=(), param_values=()):
    """Describe an exception in terms the error mapper needs."""
    orig = getattr(e, "orig", None)
    is_dbapi = isinstance(e, exc.DBAPIError)
    drv = orig if orig is not None else e
    rec = {
        "sa_exc": _qual(type(e)) if isinstance(e, exc.SQLAlchemyError) else None,
        "dbapi_error": is_dbapi,
        "driver_exc": _qual(type(drv)),
        "driver_mro": [_qual(c) for c in type(drv).__mro__ if c.__module__ not in ("builtins",)],
        "args_types": [type(a).__name__ for a in getattr(drv, "args", ())],
    }
    if not isinstance(e, exc.SQLAlchemyError):
        rec["note"] = "NOT a SQLAlchemy exception: escaped the driver/SQLAlchemy raw"
    attrs = _simple_attrs(drv)
    # psycopg: diag object; oracledb: args[0] is an _Error object
    diag = getattr(drv, "diag", None)
    if diag is not None:
        rec["diag_fields"] = {
            k: _short(v, 200) for k, v in _simple_attrs(diag).items() if v not in (None, "")
        }
    a0 = drv.args[0] if getattr(drv, "args", None) else None
    if (
        a0 is not None
        and not isinstance(a0, SIMPLE + (bytes,))
        and hasattr(a0, "__dict__")
        or (a0 is not None and type(a0).__module__.startswith("oracledb"))
    ):
        rec["args0_fields"] = {
            k: _short(v, 200) for k, v in _simple_attrs(a0).items() if v not in (None, "")
        }
        rec["args0_class"] = _qual(type(a0))
    rec["driver_attrs"] = {
        k: v for k, v in attrs.items() if k not in ("args",) and v not in (None, "")
    }
    # codes
    sqlstate = attrs.get("sqlstate") or attrs.get("pgcode")
    vendor = None
    if drv.args:
        if isinstance(drv.args[0], int):
            vendor = drv.args[0]
        elif (
            isinstance(drv.args[0], (tuple, list))
            and drv.args[0]
            and isinstance(drv.args[0][0], int)
        ):
            vendor = drv.args[0][0]  # pymssql: args == ((code, b"message"),)
        elif a0 is not None and hasattr(a0, "full_code"):
            vendor = getattr(a0, "full_code", None) or getattr(a0, "code", None)
    rec["sqlstate"] = sqlstate
    rec["vendor_code"] = vendor
    rec["message"] = _short(
        diag.message_primary if diag is not None and diag.message_primary else str(drv).strip()
    )
    if diag is not None:
        rec["message_str_form"] = _short(str(drv).strip())
    # leakage surfaces
    tb = "".join(traceback.format_exception(e))
    surfaces = {
        "str(exc)": str(e),
        "repr(exc)": repr(e),
        "repr(orig)": repr(drv),
        "repr(orig.args)": repr(getattr(drv, "args", "")),
        "traceback": tb,
    }
    rec["password_in"] = {k: any(p and p in v for p in secrets) for k, v in surfaces.items()}
    # SQLAlchemy appends "[SQL: ...]" and "[parameters: ...]" blocks to str(exc); the bound value can also be echoed by the
    # server inside the driver's own message.
    rec["sql_text_in_str"] = "[SQL:" in str(e)
    rec["parameters_block_in_str"] = "[parameters:" in str(e)
    rec["bound_value_in_str"] = any(v and v in str(e) for v in param_values)
    rec["bound_value_in_driver_message"] = any(v and v in str(drv) for v in param_values)
    rec["sa_has_statement_attr"] = bool(getattr(e, "statement", None))
    rec["str_exc_first_line"] = _short(str(e).splitlines()[0] if str(e) else "", 300)
    return rec


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg
        self.results = []
        self.reason = None  # set when server is unreachable

    # --- engines -------------------------------------------------------------------------------
    def url(self, acct):
        return self.cfg["urls"][acct]

    def pw(self, acct):
        return sa.engine.make_url(self.cfg["urls"][acct]).password

    def engine(self, acct="rw", url=None, **kw):
        return sa.create_engine(url or self.url(acct), poolclass=NullPool, **kw)

    # --- recording -----------------------------------------------------------------------------
    def add(self, case, **fields):
        r = {"dialect": self.cfg["name"], "case": case}
        r.update(fields)
        self.results.append(r)
        return r

    def not_run(self, case, why=None, **extra):
        return self.add(case, status="NOT RUN", reason=why or self.reason, **extra)

    def case(self, case, fn, secrets=(), sql=(), values=(), keep=False, **extra):
        """Run fn(); record the exception (error) or the return value (ok)."""
        if self.reason and not extra.pop("client_side", False):
            return self.not_run(case, **extra)
        extra.pop("client_side", None)
        try:
            val = fn()
        except Exception as e:  # pylint: disable=broad-except
            return self.add(case, status="error", **classify(e, secrets, sql, values), **extra)
        if keep:
            extra["data"] = val
        return self.add(case, status="ok", value=_short(repr(val), 300), **extra)

    # --- helpers -------------------------------------------------------------------------------
    def run_sql(
        self, acct, sql, params=None, pre=(), commit=False, engine_kw=None, driver_sql=False
    ):
        eng = self.engine(acct, **(engine_kw or {}))
        try:
            with eng.connect() as c:
                for s in pre:
                    c.exec_driver_sql(s)
                if driver_sql:
                    res = c.exec_driver_sql(sql)
                else:
                    res = c.execute(text(sql), params or {})
                rows = res.fetchall() if res.returns_rows else f"rowcount={res.rowcount}"
                if commit:
                    c.commit()
                return rows
        finally:
            eng.dispose()

    def scalar(self, acct, sql, params=None):
        rows = self.run_sql(acct, sql, params)
        return rows[0][0]

    def reset_probe_table(self):
        self.run_sql("rw", self.cfg["sql"]["delete_probe"], commit=True)


# =================================================================================================
# case groups
# =================================================================================================
def group_connect(h):
    cfg, S = h.cfg, h.cfg["sql"]
    bad = sa.engine.make_url(cfg["urls"]["rw"]).set(password=WRONG_PW)

    # ping + versions
    def ping():
        eng = h.engine("rw")
        try:
            with eng.connect() as c:
                d = eng.dialect
                return {
                    "select_ping": c.exec_driver_sql(S["ping"]).scalar(),
                    "version": c.exec_driver_sql(S["version"]).scalar(),
                    "sa_dialect": d.name,
                    "sa_driver": d.driver,
                    "is_mariadb": getattr(d, "is_mariadb", None),
                    "server_version_info": list(d.server_version_info or ()),
                    "default_isolation": (
                        d.default_isolation_level if hasattr(d, "default_isolation_level") else None
                    ),
                }
        finally:
            eng.dispose()

    h.case("ping", ping, keep=True)
    h.case(
        "wrong_password",
        lambda: _conn(h, bad.render_as_string(hide_password=False)),
        secrets=[WRONG_PW],
    )
    unreach = sa.engine.make_url(cfg["urls"]["rw"]).set(port=1)
    h.case(
        "unreachable_port",
        lambda: _conn(h, unreach.render_as_string(hide_password=False)),
        secrets=[h.pw("rw")],
        client_side=True,
    )
    nodb = sa.engine.make_url(cfg["urls"]["rw"]).set(**cfg["unknown_db_set"])
    h.case(
        "unknown_database",
        lambda: _conn(h, nodb.render_as_string(hide_password=False)),
        secrets=[h.pw("rw")],
    )
    black = sa.engine.make_url(cfg["urls"]["rw"]).set(host="192.0.2.1")
    h.case(
        "connect_timeout_blackhole",
        lambda: _conn(
            h, black.render_as_string(hide_password=False), **cfg["connect_timeout_args"]
        ),
        secrets=[h.pw("rw")],
        client_side=True,
        note="192.0.2.1 (TEST-NET-1), connect timeout 2 s; result depends on the sandbox network",
    )


def _conn(h, url, **connect_args):
    eng = sa.create_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        with eng.connect() as c:
            return c.exec_driver_sql(h.cfg["sql"]["ping"]).scalar()
    finally:
        eng.dispose()


def group_errors(h):
    S = h.cfg["sql"]

    def q(acct, key, commit=False):
        return lambda: h.run_sql(acct, S[key], commit=commit)

    for case, acct, key in [
        ("syntax_error", "ro", "syntax"),
        ("missing_table", "ro", "missing_table"),
        ("missing_column", "ro", "missing_column"),
        ("division_by_zero", "ro", "div0"),
    ]:
        h.case(case, q(acct, key), sql=[S[key]])
    if "div0_dml" in S:
        h.case("division_by_zero_dml", q("rw", "div0_dml"), sql=[S["div0_dml"]])
    h.case("unique_violation", q("rw", "unique", commit=True), sql=[S["unique"]])
    h.case("permission_denied_insert", q("ro", "perm_insert", commit=True), sql=[S["perm_insert"]])
    h.case("permission_denied_select", q("ro", "perm_select"), sql=[S["perm_select"]])
    for case, key in [("ro_update", "ro_update"), ("ro_delete", "ro_delete"), ("ro_ddl", "ro_ddl")]:
        h.case(case, q("ro", key, commit=True), sql=[S[key]])
    # bound value that causes a data error: does the SQLAlchemy exception text include the bound value?
    secret = "zz_secret_bound_value"
    h.case(
        "bad_cast_bound_value",
        lambda: h.run_sql(S.get("bad_cast_acct", "ro"), S["bad_cast"], {"x": secret}),
        sql=[S["bad_cast"]],
        values=[secret],
    )

    # rw through a query-style transaction (rolled back): write must not persist
    def rolled_back():
        h.reset_probe_table()
        eng = h.engine("rw")
        try:
            with eng.connect() as c:
                c.execute(text(S["write_probe"]), {"i": 7001})
                c.rollback()
        finally:
            eng.dispose()
        return {"rows_after_rollback": h.scalar("rw", S["count_probe"])}

    h.case("rw_write_then_rollback", rolled_back)


def group_timeout(h):
    T = h.cfg["timeout"]
    for case, spec in T["cases"].items():
        info = {}

        def fn(spec=spec, info=info):
            eng = h.engine("rw", **spec.get("engine_kw", {}))
            try:
                with eng.connect() as c:
                    for s in spec.get("pre", []):
                        c.exec_driver_sql(s)
                    if spec.get("hook"):
                        spec["hook"](c)
                    for s in spec.get("check", []):
                        info.setdefault("checks", {})[s] = [
                            list(map(str, r)) for r in c.exec_driver_sql(s).fetchall()
                        ]
                    t0 = datetime.datetime.now()
                    try:
                        res = c.execute(text(spec["sql"]))
                        rows = res.fetchall() if res.returns_rows else f"rowcount={res.rowcount}"
                    finally:
                        info["elapsed_s"] = round((datetime.datetime.now() - t0).total_seconds(), 2)
                    c.rollback()
                    return rows
            finally:
                eng.dispose()

        r = h.case(
            case, fn, sql=[spec["sql"]], mechanism=spec.get("mechanism"), statement=spec["sql"]
        )
        if r.get("status") != "NOT RUN":
            r.update(info)


def group_guard(h):
    """Session read-only guard + every bypass in SECURITY.md section 2, with persistence checked on a second connection."""
    G = h.cfg["guard"]
    S = h.cfg["sql"]
    marker = [8000]

    def attempt(case, pre, engine_kw=None, mid_kind=None):
        if h.reason:
            return h.not_run(case)
        h.reset_probe_table()
        marker[0] += 1
        mid = marker[0]
        step = ["connect"]

        def fn():
            eng = h.engine("rw", **(engine_kw or {}))
            try:
                with eng.connect() as c:
                    for s in pre:
                        step[0] = "pre: " + s
                        c.exec_driver_sql(s)
                    step[0] = "write"
                    c.execute(text(S["write_probe"]), {"i": mid})
                    step[0] = "commit"
                    c.commit()
                return "write statement and commit accepted"
            finally:
                eng.dispose()

        r = h.case(
            case,
            fn,
            pre_statements=pre,
            engine_kw={k: str(v) for k, v in (engine_kw or {}).items()},
        )
        if r.get("status") != "NOT RUN":
            r["failed_at_step"] = step[0] if r["status"] == "error" else None
            r["persisted_row_count"] = h.scalar(
                "rw", S["count_probe"] + " where id = :i", {"i": mid}
            )
            h.reset_probe_table()
        return r

    attempt("guard_control_no_guard", [])
    for gname, gstmts in G["guards"].items():
        attempt(f"guard[{gname}]_write", gstmts)
        for fname, fstmts in G["flips"].items():
            attempt(f"bypass[{gname} -> {fname}]", gstmts + fstmts)
        attempt(f"bypass[{gname} -> COMMIT then write]", gstmts + ["COMMIT"])
    # connection-level default read-only, then flip
    for cname, (ckw, flips) in G.get("connection_level", {}).items():
        attempt(f"connlevel[{cname}]_write", [], engine_kw=ckw)
        for fname, fstmts in flips.items():
            attempt(f"bypass[connlevel {cname} -> {fname}]", fstmts, engine_kw=ckw)
    # and the guard under the ro account for completeness is covered by permission_denied_*


def group_types(h):
    S = h.cfg["sql"]

    def fn():
        eng = h.engine("ro")
        try:
            with eng.connect() as c:
                res = c.execute(text(S["types"]))
                cols = list(res.keys())
                out = {}
                for row in res.fetchall():
                    out[str(row[0])] = {
                        k: [type(v).__name__, _jsonable(v)] for k, v in zip(cols, row, strict=True)
                    }
                return out
        finally:
            eng.dispose()

    h.case("types_roundtrip", fn, keep=True)


def _jsonable(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "bytes:" + bytes(v).hex()
    if isinstance(
        v, (decimal.Decimal, uuid.UUID, datetime.date, datetime.time, datetime.timedelta)
    ):
        return str(v)
    if isinstance(v, (dict, list, str, int, float, bool, type(None))):
        return v
    return repr(v)


def group_traps(h):
    """text() bind traps (BUILD_PROMPT) on this dialect, plus a client-side compile check that needs no server."""
    S = h.cfg["sql"]
    dialect = sa.create_engine(h.cfg["urls"]["rw"], poolclass=NullPool).dialect
    comp = {}
    for label, stmt in {
        "colon_cast  select :a::int": "select :a::int",
        "json literal select '{\"a\":1}'": "select '{\"a\":1}'",
        "escaped     select '{\"a\"\\:1}'": "select '{\"a\"\\:1}'",
        "percent     like '%b%'": "select 'abc' like '%b%'",
        "CAST(:a AS ..)": "select CAST(:a AS " + h.cfg["cast_type"] + ")",
    }.items():
        try:
            c = text(stmt).compile(dialect=dialect)
            comp[label] = {"bind_names": sorted(c.params), "compiled": str(c)}
        except Exception as e:  # pylint: disable=broad-except
            comp[label] = {"error": type(e).__name__ + ": " + str(e).splitlines()[0][:120]}
    h.add(
        "text_trap_compile_client_side", status="ok", paramstyle=dialect.paramstyle, compiled=comp
    )

    def run(sql, params=None, driver=False):
        return lambda: h.run_sql("rw", sql, params, driver_sql=driver)

    live = {
        "text_trap_colon_cast": (S.get("colon_cast"), {"a": 1}, False),
        "text_trap_json_colon_literal": (S["json_literal"], None, False),
        "text_trap_json_colon_escaped": (S["json_literal_escaped"], None, False),
        "text_percent_in_text": (S["percent"], None, False),
        "text_cast_bind": (S["cast_bind"], {"a": 5}, False),
        "driver_sql_percent_no_params": (S["percent"], None, True),
    }
    for case, (sql, params, drv) in live.items():
        if sql is None:
            h.add(case, status="n/a", reason="syntax does not exist in this dialect")
            continue
        h.case(case, run(sql, params, drv), sql=[sql])


def host_port_open(url):
    u = sa.engine.make_url(url)
    try:
        with socket.create_connection((u.host, u.port), timeout=2):
            return True
    except OSError as e:
        return str(e)


def main(cfg):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/wire_shapes")
    ap.add_argument(
        "--only", default="", help="comma list of groups: connect,errors,timeout,guard,types,traps"
    )
    ap.add_argument("--not-run-reason", default=cfg.get("not_run_reason", ""))
    args = ap.parse_args()

    h = Harness(cfg)
    status = host_port_open(cfg["urls"]["rw"])
    if status is not True:
        h.reason = args.not_run_reason or f"server not reachable: {status}"
    groups = [
        ("connect", group_connect),
        ("errors", group_errors),
        ("timeout", group_timeout),
        ("guard", group_guard),
        ("types", group_types),
        ("traps", group_traps),
    ]
    only = set(filter(None, args.only.split(",")))
    for name, fn in groups:
        if only and name not in only:
            continue
        try:
            fn(h)
        except Exception:  # pylint: disable=broad-except
            h.add(
                f"_group_{name}_crashed",
                status="harness error",
                traceback=_short(traceback.format_exc(), 1500),
            )
    meta = {
        "dialect": cfg["name"],
        "python": sys.version.split()[0],
        "sqlalchemy": sa.__version__,
        "drivers": cfg.get("driver_versions", lambda: {})(),
        "server_reachable": status is True,
        "not_run_reason": h.reason,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{cfg['name']}__sa{sa.__version__}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"meta": meta, "results": h.results}, f, indent=1, ensure_ascii=False, default=repr
        )
    print(
        path,
        len(h.results),
        "records;",
        sum(r.get("status") == "NOT RUN" for r in h.results),
        "NOT RUN",
    )
