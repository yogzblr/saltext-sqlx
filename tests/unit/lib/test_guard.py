"""Source-scan guards (TEST_PLAN section 3, BUILD_PROMPT R5, R11, R15)."""

import ast
import pathlib

import pytest

import saltext.sqlx

SRC = pathlib.Path(saltext.sqlx.__file__).resolve().parent
FILES = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)

DRIVER_MODULES = {
    "psycopg",
    "psycopg2",
    "psycopg_binary",
    "pg8000",
    "asyncpg",
    "pymysql",
    "MySQLdb",
    "mysql",  # mysql.connector (not sqlalchemy.dialects.mysql: that is a dotted name starting with sqlalchemy)
    "mysqlclient",
    "oracledb",
    "cx_Oracle",
    "pymssql",
    "pyodbc",
    "aioodbc",
    "aiomysql",
}

#: Attributes of the dialect adapter that hold constant statements (set in the class body, never from a caller).
CONSTANT_SQL_ATTRIBUTES = {"ping_sql", "_version_sql"}


def rel(path):
    return path.relative_to(SRC).as_posix()


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def calls_named(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                yield node


def enclosing_function(tree, target):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= target.lineno <= node.end_lineno:
                if best is None or node.lineno >= best.lineno:
                    best = node
    return best.name if best else None


def test_source_files_were_found():
    names = {rel(p) for p in FILES}
    assert (
        "lib/runner.py" in names and "lib/dialects.py" in names and "modules/sqlx_mod.py" in names
    )


def test_commit_is_called_only_inside_run_execute():
    found = []
    for path in FILES:
        tree = parse(path)
        for call in calls_named(tree, "commit"):
            found.append((rel(path), enclosing_function(tree, call)))
    assert found == [("lib/runner.py", "run_execute")]


def test_no_text_occurrence_of_commit_call_outside_run_execute():
    """Catches ``commit(`` hidden from the AST scan, for example in a string passed to ``eval``."""
    runner = SRC / "lib" / "runner.py"
    tree = parse(runner)
    func = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_execute"
    )
    allowed = range(func.lineno, func.end_lineno + 1)
    offenders = []
    for path in FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "commit(" in line and not (path == runner and number in allowed):
                offenders.append(f"{rel(path)}:{number}")
    assert not offenders


def test_no_autocommit_isolation_level():
    offenders = [
        rel(p)
        for p in FILES
        if "AUTOCOMMIT" in p.read_text(encoding="utf-8").upper().replace("-", "_")
    ]
    assert not offenders


def _is_int_expression(node):
    """``int(x)``, an integer constant, or arithmetic on those (``int(ms) // 1000``)."""
    if isinstance(node, ast.Call):
        return getattr(node.func, "id", None) == "int"
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int)
    if isinstance(node, ast.BinOp):
        return _is_int_expression(node.left) and _is_int_expression(node.right)
    return False


def _is_constant_sql(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        # f"SET x = {int(ms)}": every interpolation must be an int() expression
        return all(
            isinstance(part, ast.Constant)
            or (isinstance(part, ast.FormattedValue) and _is_int_expression(part.value))
            for part in node.values
        )
    if isinstance(node, ast.Attribute):
        return node.attr in CONSTANT_SQL_ATTRIBUTES
    return False


def test_exec_driver_sql_only_with_constant_statements_in_dialects_and_engine():
    problems = []
    for path in FILES:
        tree = parse(path)
        for call in calls_named(tree, "exec_driver_sql"):
            where = f"{rel(path)}:{call.lineno}"
            if rel(path) not in {"lib/dialects.py", "lib/engine.py"}:
                problems.append(f"{where} exec_driver_sql outside dialects.py/engine.py")
            elif not call.args or not _is_constant_sql(call.args[0]):
                problems.append(f"{where} exec_driver_sql with a non-constant statement")
    assert not problems


def test_runner_uses_text_for_caller_sql():
    runner = SRC / "lib" / "runner.py"
    tree = parse(runner)
    assert not list(calls_named(tree, "exec_driver_sql"))
    assert list(calls_named(tree, "text"))
    assert not list(calls_named(tree, "execute_string"))


def _imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node, node.module
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called in {"import_module", "__import__"} and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    yield node, node.args[0].value


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_no_driver_import_outside_dialects(path):
    if rel(path) == "lib/dialects.py":
        pytest.skip("dialects.py is the one module allowed to import drivers (R15)")
    offenders = [
        f"{rel(path)}:{node.lineno} imports {name}"
        for node, name in _imported_modules(parse(path))
        if name.split(".")[0] in DRIVER_MODULES
    ]
    assert not offenders


def test_guard_scan_would_catch_a_violation():
    """The scanners themselves: feed them code that breaks each rule."""
    bad = ast.parse(
        "import psycopg\n"
        "from pymysql import connect\n"
        "importlib.import_module('oracledb')\n"
        "def f(conn, sql):\n"
        "    conn.commit()\n"
        "    conn.exec_driver_sql(sql)\n"
    )
    assert [n for _, n in _imported_modules(bad)] == ["psycopg", "pymysql", "oracledb"]
    assert [enclosing_function(bad, c) for c in calls_named(bad, "commit")] == ["f"]
    (call,) = list(calls_named(bad, "exec_driver_sql"))
    assert not _is_constant_sql(call.args[0])
    assert _is_constant_sql(ast.parse("x('select 1')").body[0].value.args[0])
    assert _is_constant_sql(ast.parse("x(f'set a = {int(b)}')").body[0].value.args[0])
    assert _is_constant_sql(
        ast.parse("x(f'set a = {int(m) // 1000}.{int(m) % 1000:03d}')").body[0].value.args[0]
    )
    assert not _is_constant_sql(ast.parse("x(f'set a = {b}')").body[0].value.args[0])
    assert not _is_constant_sql(ast.parse("x(f'set a = {b + 1}')").body[0].value.args[0])
