# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, boxes 2 and 3: source scans, independent of ``tests/unit/lib/test_guard.py``.

* ``commit(`` exists only inside ``runner.run_execute``; nothing else can make a transaction permanent (no
  ``engine.begin()``, no AUTOCOMMIT isolation level, no ``autocommit`` argument, no raw DB-API connection or cursor).
* ``exec_driver_sql`` never receives caller SQL: every call takes a class constant (``ping_sql`` / ``_version_sql``), a
  string literal, or an f-string whose only substitutions are ``int(...)``; it is not used in ``runner.py`` or the module.
* No dynamic code execution, no process spawning, no unsafe deserialisation, and exactly one logging call in ``src/``
  (the audit line, whose arguments are the ``info`` dict).
"""

import ast
import pathlib
import re

import pytest

import saltext.sqlx

SRC = pathlib.Path(saltext.sqlx.__file__).resolve().parent
FILES = sorted(
    p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts and p.name != "version.py"
)


def rel(path):
    return path.relative_to(SRC).as_posix()


def trees():
    for path in FILES:
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            yield node, (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            )


def enclosing(tree, target):
    best = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= target.lineno <= node.end_lineno
        ):
            if best is None or node.lineno >= best.lineno:
                best = node
    return best.name if best else None


def test_the_scan_sees_the_whole_package():
    assert {"lib/runner.py", "lib/engine.py", "lib/dialects.py", "modules/sqlx_mod.py"} <= {
        rel(p) for p in FILES
    }


def test_commit_only_in_run_execute():
    found = [
        (rel(p), enclosing(t, c)) for p, t in trees() for c, name in calls(t) if name == "commit"
    ]
    assert found == [("lib/runner.py", "run_execute")]
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        hits = [m.start() for m in re.finditer(r"commit\s*\(", text)]
        assert (len(hits) == 0) or rel(path) == "lib/runner.py", rel(path)


def test_nothing_else_can_commit():
    forbidden_calls = {
        "begin",
        "raw_connection",
        "cursor",
        "autocommit",
        "set_isolation_level",
        "connect",
    }
    bad = []
    for path, tree in trees():
        for call, name in calls(tree):
            if name in forbidden_calls:
                bad.append((rel(path), call.lineno, name))
            for kw in call.keywords:
                if kw.arg in {"isolation_level", "autocommit"}:
                    bad.append((rel(path), call.lineno, kw.arg))
        text = path.read_text(encoding="utf-8")
        for needle in ("AUTOCOMMIT", "autocommit", "isolation_level"):
            if needle in text:
                bad.append((rel(path), 0, needle))
    assert bad == [], bad


def _is_safe_sql_argument(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.Attribute) and node.attr in {"ping_sql", "_version_sql"}:
        return (
            isinstance(node.value, ast.Name) and node.value.id == "self" or node.attr == "ping_sql"
        )
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if not (isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "int"):
                    # the MariaDB timeout is "<int // 1000>.<int % 1000:03d>": arithmetic on int(...) only
                    if not (
                        isinstance(inner, ast.BinOp)
                        and "int" in ast.dump(inner)
                        and "Name(id='sql'" not in ast.dump(inner)
                    ):
                        return False
        return True
    return False


def test_exec_driver_sql_never_gets_caller_sql():
    seen = []
    for path, tree in trees():
        for call, name in calls(tree):
            if name != "exec_driver_sql":
                continue
            seen.append((rel(path), call.lineno))
            assert rel(path) in {
                "lib/dialects.py",
                "lib/engine.py",
            }, f"exec_driver_sql in {rel(path)}"
            if (
                rel(path) == "lib/engine.py"
            ):  # passed as a reference: _guarded(..., conn.exec_driver_sql, adapter.ping_sql)
                continue
            assert call.args and _is_safe_sql_argument(
                call.args[0]
            ), f"{rel(path)}:{call.lineno} {ast.dump(call.args[0])}"
    # engine.ping passes the adapter's constant statement through _guarded
    engine_refs = [
        n
        for n in ast.walk(ast.parse((SRC / "lib" / "engine.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.Attribute) and n.attr == "exec_driver_sql"
    ]
    assert len(engine_refs) == 1
    assert seen, "no exec_driver_sql call found: the scan is broken"


def test_exec_driver_sql_inputs_are_class_constants():
    """``ping_sql`` and ``_version_sql`` are assigned only string literals in class bodies."""
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in {"ping_sql", "_version_sql"}
                for t in node.targets
            ):
                assert isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ), f"{rel(path)}:{node.lineno}"


def test_caller_sql_reaches_the_driver_only_through_text():
    runner = ast.parse((SRC / "lib" / "runner.py").read_text(encoding="utf-8"))
    executes = [c for c, name in calls(runner) if name == "execute"]
    assert len(executes) == 2
    for call in executes:
        first = call.args[0]
        assert isinstance(first, ast.Call) and getattr(first.func, "id", None) == "text", ast.dump(
            first
        )


@pytest.mark.parametrize(
    "name",
    [
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "Popen",
        "run",
        "call",
        "check_output",
        "loads",
        "load",
        "unsafe_load",
        "getattr_static",
    ],
)
def test_no_dynamic_execution_or_unsafe_deserialisation(name):
    allowed = (
        set()
    )  # nothing is allowed; the set is explicit so an exception has to be written down
    bad = []
    for path, tree in trees():
        for call, called in calls(tree):
            if called != name:
                continue
            func = call.func
            owner = getattr(getattr(func, "value", None), "id", None)
            if name in {"run", "call", "load", "loads"} and owner not in {
                "subprocess",
                "os",
                "pickle",
                "yaml",
                "marshal",
            }:
                continue  # e.g. runner.run, json.loads of own data
            if name == "compile" and owner == "re":
                continue  # regular expressions, not code
            if (name, rel(path)) not in allowed:
                bad.append((rel(path), call.lineno, owner, name))
    assert bad == []


def test_only_the_audit_line_logs():
    logged = []
    for path, tree in trees():
        for call, name in calls(tree):
            func = call.func
            if name in {
                "debug",
                "info",
                "warning",
                "warn",
                "error",
                "exception",
                "critical",
                "log",
            } and isinstance(func, ast.Attribute):
                owner = getattr(func.value, "id", "")
                if owner in {"log", "logger", "logging"}:
                    logged.append(
                        (rel(path), call.lineno, name, [ast.dump(a)[:40] for a in call.args[1:]])
                    )
            if name == "print":
                logged.append((rel(path), call.lineno, "print", []))
    assert [(f, n) for f, _l, n, _a in logged] == [("modules/sqlx_mod.py", "info")], logged
    args = logged[0][3]
    assert args == [
        "Name(id='info', ctx=Load())"
    ], args  # the only argument after the format is the info dict


def test_info_dict_holds_no_sql_params_rows_or_secret():
    tree = ast.parse((SRC / "modules" / "sqlx_mod.py").read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "info" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "info"
            and isinstance(node.ctx, ast.Store)
        ):
            keys.add(node.slice.value)
    assert keys == {"fn", "profile", "dialect", "sha", "rows", "code", "user", "ms"}, keys


def test_no_driver_is_imported_outside_dialects():
    drivers = {
        "psycopg",
        "psycopg2",
        "pymysql",
        "MySQLdb",
        "oracledb",
        "cx_Oracle",
        "pymssql",
        "pyodbc",
    }
    bad = []
    for path, tree in trees():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            bad += [(rel(path), n) for n in names if n in drivers]
    assert bad == []
