"""Unit tests for saltext.sqlx.lib.errors (TEST_PLAN section 3, SPEC section 8).

The mapping table is driven by the observations in ``docs/wire_shapes/*.json`` (task T1, rule R18): each recorded
error is rebuilt as a stand-in driver exception with the recorded class name, SQLSTATE / vendor code and message
fields, wrapped in the real SQLAlchemy exception class that was observed, and then translated.
"""

import ast
import json
import re
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy.exc as sa_exc
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import errors

REPO_ROOT = Path(__file__).resolve().parents[3]
SHAPES = REPO_ROOT / "docs" / "wire_shapes"

SQL_TEXT = "SELECT * FROM users WHERE api_token = 'zz-sql-text-marker'"
SECRET = "hunter2-bound-secret"
PARAMS = {"pw": SECRET}

SPEC_CODES = {
    "E_PROFILE_UNKNOWN",
    "E_PROFILE_INVALID",
    "E_OPTION_UNKNOWN",
    "E_OPTION_INVALID",
    "E_SECRET",
    "E_POLICY",
    "E_EXECUTE_DISABLED",
    "E_DRIVER_MISSING",
    "E_CONNECT",
    "E_AUTH_FAILED",
    "E_SQL",
    "E_SQL_DATA",
    "E_CONSTRAINT",
    "E_READ_ONLY",
    "E_TIMEOUT",
    "E_INTERNAL",
}


# --------------------------------------------------------------------------------------------------
# Rebuilding recorded exceptions
# --------------------------------------------------------------------------------------------------
def _driver_class(qualified):
    return type(qualified.rsplit(".", 1)[-1], (Exception,), {})


def _build_orig(dialect, rec):
    """Rebuild the driver exception from a recorded observation."""
    cls = _driver_class(rec["driver_exc"])
    if dialect == "postgresql":
        diag_fields = rec.get("diag_fields") or {}
        orig = cls(rec["message"])
        orig.sqlstate = rec.get("sqlstate")
        orig.diag = SimpleNamespace(
            message_primary=diag_fields.get("message_primary"),
            message_hint=diag_fields.get("message_hint"),
            message_detail=diag_fields.get("message_detail"),
            context=diag_fields.get("context"),
            sqlstate=diag_fields.get("sqlstate"),
        )
        return orig
    if dialect in ("mysql", "mariadb"):
        code, message = ast.literal_eval(rec["message"])
        orig = cls(code, message)
        orig.sqlstate = rec.get("sqlstate")
        return orig
    if dialect == "oracle":
        fields = rec["args0_fields"]
        return cls(SimpleNamespace(full_code=fields["full_code"], message=fields["message"]))
    if dialect == "mssql":
        return cls(ast.literal_eval(rec["message"]))
    raise AssertionError(dialect)


def _build_exc(dialect, rec):
    """Rebuild the exception that reached the caller (SQLAlchemy wrapper around the driver exception)."""
    if rec.get("sa_exc") is None:
        # raw exception, not wrapped by SQLAlchemy (TypeError, RuntimeError, ValueError, ...)
        name = rec["driver_exc"].rsplit(".", 1)[-1]
        return {
            "TypeError": TypeError,
            "RuntimeError": RuntimeError,
            "ValueError": ValueError,
            "AttributeError": AttributeError,
        }[name](rec["message"])
    wrapper = getattr(sa_exc, rec["sa_exc"].rsplit(".", 1)[-1])
    if rec["sa_exc"].endswith("StatementError"):
        inner = sa_exc.InvalidRequestError(rec["message"])
        return sa_exc.StatementError("(secret detail) " + SQL_TEXT, SQL_TEXT, PARAMS, inner)
    return wrapper(SQL_TEXT, PARAMS, _build_orig(dialect, rec))


def _load(name):
    return json.loads((SHAPES / name).read_text(encoding="utf-8"))["results"]


# Expected code per observed case. Evidence: docs/wire_shapes/<dialect>__sa2.0.55.json, docs/WIRE_SHAPES.md.
PG_EXPECTED = {
    "wrong_password": "E_AUTH_FAILED",
    "unreachable_port": "E_CONNECT",
    "unknown_database": "E_CONNECT",
    "connect_timeout_blackhole": "E_CONNECT",
    "syntax_error": "E_SQL",
    "missing_table": "E_SQL",
    "missing_column": "E_SQL",
    "division_by_zero": "E_SQL_DATA",
    "division_by_zero_dml": "E_SQL_DATA",
    "unique_violation": "E_CONSTRAINT",
    "permission_denied_insert": "E_SQL",
    "permission_denied_select": "E_SQL",
    "ro_update": "E_SQL",
    "ro_delete": "E_SQL",
    "ro_ddl": "E_SQL",
    "bad_cast_bound_value": "E_SQL_DATA",
    "statement_timeout": "E_TIMEOUT",
    "text_trap_colon_cast": "E_SQL",
    "text_trap_json_colon_literal": "E_OPTION_INVALID",
    "driver_sql_percent_no_params": "E_OPTION_INVALID",
}
MYSQL_EXPECTED = {
    "wrong_password": "E_AUTH_FAILED",
    "unreachable_port": "E_CONNECT",
    "unknown_database": "E_AUTH_FAILED",  # 1044, SQLSTATE 42000: an access error, not E_SQL
    "connect_timeout_blackhole": "E_CONNECT",
    "syntax_error": "E_SQL",
    "missing_table": "E_SQL",
    "missing_column": "E_SQL",  # 1054 arrives as OperationalError
    "division_by_zero_dml": "E_SQL_DATA",  # 1365 arrives as OperationalError
    "unique_violation": "E_CONSTRAINT",
    "permission_denied_insert": "E_SQL",  # 1142 arrives as OperationalError
    "permission_denied_select": "E_SQL",
    "ro_update": "E_SQL",
    "ro_delete": "E_SQL",
    "ro_ddl": "E_SQL",
    "bad_cast_bound_value": "E_SQL_DATA",  # 1366: SQLSTATE HY000 on MySQL, 22007 on MariaDB
    "max_execution_time_select_from_table": "E_TIMEOUT",  # 3024
    "max_statement_time_select": "E_TIMEOUT",  # 1969
    "max_statement_time_select_from_table": "E_TIMEOUT",
    "max_statement_time_dml": "E_TIMEOUT",
    "wrong_variable_max_statement_time": "E_INTERNAL",  # 1193
    "wrong_variable_max_execution_time": "E_INTERNAL",
    "client_read_timeout_1s": "E_TIMEOUT",  # 2013, message says "timed out" (decision recorded in errors.py)
    "text_trap_colon_cast": "E_SQL",
    "text_trap_json_colon_literal": "E_OPTION_INVALID",
    "driver_sql_percent_no_params": "E_OPTION_INVALID",
}
CLIENT_SIDE_EXPECTED = {
    "unreachable_port": "E_CONNECT",
    "connect_timeout_blackhole": "E_CONNECT",
}
# Guard/bypass rows are many; they are keyed on the recorded native code instead of the case name.
BY_NATIVE = {
    "25006": "E_READ_ONLY",
    "1792": "E_READ_ONLY",
    "1193": "E_INTERNAL",
    "1568": "E_INTERNAL",
}
# Text that must still be present (the server's own words) in the message of the case, when the whole recorded
# message is not expected verbatim (scrubbed or reshaped cases).
FRAGMENTS = {
    ("mysql", "unique_violation"): "Duplicate entry",
    ("mariadb", "unique_violation"): "Duplicate entry",
    ("mysql", "bad_cast_bound_value"): "Incorrect integer value",
    ("mariadb", "bad_cast_bound_value"): "Incorrect integer value",
    ("postgresql", "bad_cast_bound_value"): "invalid input syntax for type integer",
    ("mssql", "unreachable_port"): "Unable to connect",
    ("mssql", "connect_timeout_blackhole"): "Unable to connect",
    ("oracle", "unreachable_port"): "cannot connect to database",
    ("oracle", "connect_timeout_blackhole"): "cannot connect to database",
}
# Values that appear in the recorded server text and must NOT survive when error_detail is false.
LEAKS = {
    ("mysql", "unique_violation"): "a@example.com",
    ("mariadb", "unique_violation"): "a@example.com",
    ("mysql", "bad_cast_bound_value"): "zz_secret_bound_value",
    ("mariadb", "bad_cast_bound_value"): "zz_secret_bound_value",
    ("postgresql", "bad_cast_bound_value"): "zz_secret_bound_value",
}
# Distinct, human wording of the hints required by the task.
HINT_OPTION = {
    "text_trap_json_colon_literal": ("\\:", "CAST"),
    "driver_sql_percent_no_params": ("exec_driver_sql",),
}


def _collect():
    rows = []
    for dialect, files in (
        ("postgresql", ["postgresql__sa2.0.55.json"]),
        ("mysql", ["mysql__sa2.0.55.json"]),
        ("mariadb", ["mariadb__sa2.0.55.json"]),
        ("oracle", ["oracle__sa2.0.55.json"]),
        ("mssql", ["mssql__sa2.0.55.json"]),
    ):
        for fname in files:
            for rec in _load(fname):
                if "driver_exc" not in rec:
                    continue
                case = rec["case"]
                native = rec.get("sqlstate") or rec.get("vendor_code")
                if dialect in ("mysql", "mariadb") and rec.get("vendor_code") is not None:
                    native = rec["vendor_code"]
                table = {
                    "postgresql": PG_EXPECTED,
                    "mysql": MYSQL_EXPECTED,
                    "mariadb": MYSQL_EXPECTED,
                }.get(dialect, CLIENT_SIDE_EXPECTED)
                expected = table.get(case) or BY_NATIVE.get(str(native))
                rows.append(pytest.param(dialect, rec, expected, id=f"{dialect}-{case}"))
    return rows


OBSERVED = _collect()


def test_every_observed_error_row_has_an_expectation():
    missing = [p.id for p in OBSERVED if p.values[2] is None]
    assert not missing, f"add an expectation (evidence-based) for: {missing}"


def test_observation_files_are_present():
    assert len(OBSERVED) > 80


@pytest.mark.parametrize(("dialect", "rec", "expected"), OBSERVED)
def test_observed_rows(dialect, rec, expected):
    exc = _build_exc(dialect, rec)
    code, message = errors.translate(exc, dialect)
    assert code == expected
    case = rec["case"]
    assert message
    if expected == "E_INTERNAL":
        assert message == type(getattr(exc, "orig", exc)).__name__  # class name only
        return
    assert SQL_TEXT not in message
    assert SECRET not in message
    assert "[SQL:" not in message
    assert "[parameters:" not in message
    fragment = FRAGMENTS.get((dialect, case))
    if fragment:
        assert fragment in message
    elif dialect == "postgresql" and rec.get("diag_fields", {}).get("message_primary"):
        assert rec["diag_fields"]["message_primary"] in message
    elif dialect in ("mysql", "mariadb") and rec.get("sa_exc"):
        if rec["sa_exc"].endswith("StatementError"):
            assert "\\:" in message
        else:
            assert ast.literal_eval(rec["message"])[1] in message
    leak = LEAKS.get((dialect, case))
    if leak:
        assert leak not in message
    for needle in HINT_OPTION.get(case, ()):
        assert needle in message


@pytest.mark.parametrize(("dialect", "rec", "expected"), OBSERVED)
def test_observed_rows_raise_for_format(dialect, rec, expected):
    exc = _build_exc(dialect, rec)
    with pytest.raises(CommandExecutionError) as info:
        errors.raise_for(exc, dialect)
    text = str(info.value)
    assert text.startswith(f"[{expected}] {dialect}")
    assert SQL_TEXT not in text
    assert SECRET not in text
    leak = LEAKS.get((dialect, rec["case"]))
    if leak:
        assert leak not in text


@pytest.mark.parametrize(
    ("dialect", "rec", "expected"),
    [p for p in OBSERVED if (p.values[0], p.values[1]["case"]) in LEAKS],
)
def test_leaks_survive_when_error_detail_is_true(dialect, rec, expected):
    leak = LEAKS[(dialect, rec["case"])]
    code, message = errors.translate(_build_exc(dialect, rec), dialect, error_detail=True)
    assert code == expected
    assert leak in message


RAW_CASES = [
    ("postgresql_sqlascii.json", "postgresql", "E_CONNECT", "UTF8"),
    ("mysql_auth__without-cryptography.json", "mysql", "E_DRIVER_MISSING", "cryptography"),
    ("mysql_auth__with-cryptography.json", "mysql", "E_CONNECT", "retry"),
]


@pytest.mark.parametrize(("fname", "dialect", "expected", "hint"), RAW_CASES)
def test_observed_raw_exceptions(fname, dialect, expected, hint):
    records = [r for r in _load(fname) if r.get("status") == "error" and "driver_exc" in r]
    assert records, fname
    for rec in records:
        exc = _build_exc(dialect, {**rec, "sa_exc": None})
        code, message = errors.translate(exc, dialect)
        assert code == expected
        assert hint in message
        assert rec["message"] not in message  # reworded, never the raw exception text


# --------------------------------------------------------------------------------------------------
# Hand-built cases not in the observation tables
# --------------------------------------------------------------------------------------------------
def _pg(sqlstate=None, primary="boom", hint=None, detail=None, cls_name="DatabaseError", args=None):
    cls = type(cls_name, (Exception,), {})
    orig = cls(*(args if args is not None else (primary,)))
    orig.sqlstate = sqlstate
    orig.diag = SimpleNamespace(
        message_primary=primary, message_hint=hint, message_detail=detail, sqlstate=sqlstate
    )
    return orig


def _wrap(orig, cls=sa_exc.DBAPIError):
    return cls(SQL_TEXT, PARAMS, orig)


def test_str_of_sqlalchemy_exception_would_leak():
    """Guard the premise: the SQLAlchemy ``str()`` really embeds SQL and values."""
    text = str(_wrap(_pg("42601", 'syntax error at or near "selec"')))
    assert SQL_TEXT in text
    assert SECRET in text


def test_postgres_detail_only_when_error_detail():
    orig = _pg("23505", "duplicate key value", detail="Key (email)=(a@example.com) already exists.")
    exc = _wrap(orig, sa_exc.IntegrityError)
    assert errors.translate(exc, "postgresql") == ("E_CONSTRAINT", "duplicate key value")
    code, message = errors.translate(exc, "postgresql", error_detail=True)
    assert code == "E_CONSTRAINT"
    assert "Key (email)=(a@example.com) already exists." in message
    assert message.startswith("duplicate key value")


def test_postgres_hint_is_kept():
    orig = _pg(
        "42703",
        'column "note" does not exist',
        hint='Perhaps you meant to reference the column "orders.note".',
    )
    _, message = errors.translate(_wrap(orig, sa_exc.ProgrammingError), "postgresql")
    assert 'column "note" does not exist' in message
    assert 'Perhaps you meant to reference the column "orders.note".' in message


def test_postgres_context_is_never_included():
    orig = _pg("22P02", 'invalid input syntax for type integer: "zz"')
    orig.diag.context = f"unnamed portal parameter $1 = '{SECRET}'"
    _, message = errors.translate(_wrap(orig, sa_exc.DataError), "postgresql", error_detail=True)
    assert SECRET not in message


def test_postgres_sqlstate_without_primary_message_uses_first_arg_line_only():
    orig = _pg("42601", primary=None, args=("syntax error\nDETAIL:  secret row",))
    _, message = errors.translate(_wrap(orig, sa_exc.ProgrammingError), "postgresql")
    assert message == "syntax error"


def test_postgres_sqlstate_taken_from_diag_when_attribute_missing():
    orig = _pg(None, "canceling statement due to statement timeout")
    del orig.sqlstate
    orig.diag.sqlstate = "57014"
    assert errors.translate(_wrap(orig), "postgresql")[0] == "E_TIMEOUT"


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("42601", "E_SQL"),
        ("42P01", "E_SQL"),
        ("42501", "E_SQL"),
        ("22012", "E_SQL_DATA"),
        ("22P02", "E_SQL_DATA"),
        ("23505", "E_CONSTRAINT"),
        ("23503", "E_CONSTRAINT"),
        ("25006", "E_READ_ONLY"),
        ("57014", "E_TIMEOUT"),
        ("28P01", "E_AUTH_FAILED"),  # UNVERIFIED(T1): not observable on PostgreSQL 16
        ("08006", "E_CONNECT"),  # UNVERIFIED(T1)
        ("XX000", "E_INTERNAL"),
        ("25001", "E_INTERNAL"),  # class 25 other than 25006 is not a read-only error
    ],
)
def test_postgres_sqlstate_table(sqlstate, expected):
    assert errors.translate(_wrap(_pg(sqlstate, "msg")), "postgresql")[0] == expected


def test_postgres_class_does_not_decide_when_sqlstate_says_otherwise():
    """The SQLSTATE wins over the SQLAlchemy class (R2)."""
    exc = _wrap(_pg("23505", "dup"), sa_exc.ProgrammingError)
    assert errors.translate(exc, "postgresql")[0] == "E_CONSTRAINT"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'connection failed: ... FATAL:  password authentication failed for user "x"',
            "E_AUTH_FAILED",
        ),
        ("connection failed: ... failed: Connection refused\n\tIs the server running", "E_CONNECT"),
        ("could not connect to server: timeout", "E_CONNECT"),
        ("... server closed the connection unexpectedly\n\tThis probably means", "E_CONNECT"),
        ('connection failed: ... FATAL:  database "nope" does not exist', "E_CONNECT"),
        ("only '%s', '%b', '%t' are allowed as placeholders, got '%''", "E_OPTION_INVALID"),
        ("something entirely different", "E_INTERNAL"),
    ],
)
def test_postgres_no_sqlstate_text_rules(text, expected):
    orig = type("OperationalError", (Exception,), {})(text)
    orig.sqlstate = None
    orig.diag = SimpleNamespace(
        message_primary=None, message_hint=None, message_detail=None, sqlstate=None
    )
    code, message = errors.translate(_wrap(orig, sa_exc.OperationalError), "postgresql")
    assert code == expected
    if expected == "E_INTERNAL":
        assert message == "OperationalError"


def test_postgres_unknown_database_keeps_that_text():
    orig = type("OperationalError", (Exception,), {})(
        'connection failed: x failed: FATAL:  database "nope" does not exist'
    )
    code, message = errors.translate(_wrap(orig, sa_exc.OperationalError), "postgresql")
    assert code == "E_CONNECT"
    assert 'database "nope" does not exist' in message


def test_postgres_error_without_diag_or_sqlstate_attribute():
    orig = type("OperationalError", (Exception,), {})("Connection refused")
    assert errors.translate(_wrap(orig, sa_exc.OperationalError), "postgresql")[0] == "E_CONNECT"


def _my(code, message, sqlstate=None, cls_name="OperationalError"):
    orig = type(cls_name, (Exception,), {})(code, message)
    orig.sqlstate = sqlstate
    return orig


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize(
    ("code", "sqlstate", "expected"),
    [
        (1045, "28000", "E_AUTH_FAILED"),
        (1044, "42000", "E_AUTH_FAILED"),
        (1142, "42000", "E_SQL"),
        (1054, "42S22", "E_SQL"),
        (1064, "42000", "E_SQL"),
        (1146, "42S02", "E_SQL"),
        (1365, "22012", "E_SQL_DATA"),
        (1366, "HY000", "E_SQL_DATA"),
        (1366, "22007", "E_SQL_DATA"),
        (1062, "23000", "E_CONSTRAINT"),
        (1792, "25006", "E_READ_ONLY"),
        (3024, "HY000", "E_TIMEOUT"),
        (1969, "70100", "E_TIMEOUT"),
        (2003, None, "E_CONNECT"),
        (2013, None, "E_CONNECT"),
        (1193, "HY000", "E_INTERNAL"),
        (1568, "25001", "E_INTERNAL"),
        # SQLSTATE-class fallback for vendor codes T1 did not observe (UNVERIFIED(T1))
        (1451, "23000", "E_CONSTRAINT"),
        (1265, "22001", "E_SQL_DATA"),
        (1100, "42000", "E_SQL"),
        (1698, "28000", "E_AUTH_FAILED"),
        (9999, None, "E_INTERNAL"),
    ],
)
def test_mysql_family_vendor_code_first(dialect, code, sqlstate, expected):
    # The SQLAlchemy class is deliberately wrong for most rows: only the vendor code may decide.
    exc = _wrap(_my(code, "msg", sqlstate), sa_exc.InterfaceError)
    assert errors.translate(exc, dialect)[0] == expected


def test_mysql_1366_sqlstate_differs_but_code_is_the_same():
    a = errors.translate(
        _wrap(_my(1366, "Incorrect integer value: 'x' for column 'id' at row 1", "HY000")), "mysql"
    )
    b = errors.translate(
        _wrap(_my(1366, "Incorrect integer value: 'x' for column 'id' at row 1", "22007")),
        "mariadb",
    )
    assert a[0] == b[0] == "E_SQL_DATA"


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        (
            "Duplicate entry 'a@example.com' for key 'uniq.PRIMARY'",
            "a@example.com",
            "for key 'uniq.PRIMARY'",
        ),
        ("Duplicate entry 'it's, 'quoted' ' for key 'PRIMARY'", "quoted", "Duplicate entry"),
        (
            "Incorrect integer value: 'zz' for column `s`.`t`.`id` at row 1",
            "zz'",
            "for column `s`.`t`.`id` at row 1",
        ),
        (
            "Incorrect datetime value: 'not-a-date' for column 'c' at row 1",
            "not-a-date",
            "Incorrect datetime value",
        ),
        ("Incorrect string value: '\\xF0\\x9F' for column 'c' at row 1", "xF0", "for column 'c'"),
        ("Truncated incorrect DOUBLE value: 'abc'", "abc", "Truncated incorrect DOUBLE value"),
        (
            "Data truncated for column 'amount' at row 1",
            "zzzz",
            "Data truncated for column 'amount' at row 1",
        ),
    ],
)
def test_mysql_scrub_values_unless_error_detail(raw, must_not_contain, must_contain):
    exc = _wrap(_my(1062, raw, "23000", "IntegrityError"), sa_exc.IntegrityError)
    _, scrubbed = errors.translate(exc, "mysql")
    assert must_not_contain not in scrubbed
    assert must_contain in scrubbed
    _, full = errors.translate(exc, "mysql", error_detail=True)
    assert full == raw


def test_mysql_nonstring_message_and_missing_args():
    odd = type("OperationalError", (Exception,), {})(1045)  # no message element
    assert errors.translate(_wrap(odd, sa_exc.OperationalError), "mysql") == (
        "E_AUTH_FAILED",
        "OperationalError",
    )
    odd2 = type("OperationalError", (Exception,), {})("not-a-code", "text")
    assert errors.translate(_wrap(odd2, sa_exc.OperationalError), "mysql") == (
        "E_INTERNAL",
        "OperationalError",
    )
    odd3 = type("OperationalError", (Exception,), {})(1045, 12345)
    assert errors.translate(_wrap(odd3, sa_exc.OperationalError), "mysql")[0] == "E_AUTH_FAILED"


def test_mysql_2013_read_timeout_is_a_timeout_when_message_says_so():
    msg = "Lost connection to MySQL server during query (The read operation timed out)"
    assert errors.translate(_wrap(_my(2013, msg)), "mysql")[0] == "E_TIMEOUT"
    assert (
        errors.translate(_wrap(_my(2013, "Lost connection to MySQL server during query")), "mysql")[
            0
        ]
        == "E_CONNECT"
    )


def test_oracle_error_object_and_unverified_codes():
    err = SimpleNamespace(
        full_code="DPY-6005",
        message="DPY-6005: cannot connect to database.\n[Errno 111] Connection refused",
    )
    orig = type("OperationalError", (Exception,), {})(err)
    code, message = errors.translate(_wrap(orig, sa_exc.OperationalError), "oracle")
    assert code == "E_CONNECT"
    assert "\n" not in message
    # ORA-01017 etc. were not observed (UNVERIFIED(T1)): class name only, never guessed
    ora = SimpleNamespace(
        full_code="ORA-01017", message="ORA-01017: invalid username/password; logon denied"
    )
    orig = type("DatabaseError", (Exception,), {})(ora)
    assert errors.translate(_wrap(orig), "oracle") == ("E_INTERNAL", "DatabaseError")
    assert errors.translate(_wrap(type("Odd", (Exception,), {})()), "oracle") == (
        "E_INTERNAL",
        "Odd",
    )


def test_mssql_bytes_message_is_decoded_and_deduplicated():
    text = b"DB-Lib error message 20009, severity 9:\nUnable to connect: TDS server is unavailable (h)\nNet-Lib error during Connection refused (111)\nDB-Lib error message 20009, severity 9:\nUnable to connect: TDS server is unavailable (h)\nNet-Lib error during Connection refused (111)\n"
    orig = type("OperationalError", (Exception,), {})((20009, text))
    code, message = errors.translate(_wrap(orig, sa_exc.OperationalError), "mssql")
    assert code == "E_CONNECT"
    assert message.count("Unable to connect") == 1
    assert "b'" not in message
    # SQL Server error 18456 etc. were not observed (UNVERIFIED(T1))
    orig = type("OperationalError", (Exception,), {})((18456, b"Login failed for user"))
    assert errors.translate(_wrap(orig, sa_exc.OperationalError), "mssql") == (
        "E_INTERNAL",
        "OperationalError",
    )
    plain = type("OperationalError", (Exception,), {})(20009, "text message")
    assert errors.translate(_wrap(plain, sa_exc.OperationalError), "mssql")[0] == "E_CONNECT"
    weird = type("OperationalError", (Exception,), {})(("x",))
    assert errors.translate(_wrap(weird, sa_exc.OperationalError), "mssql")[0] == "E_INTERNAL"
    empty = type("OperationalError", (Exception,), {})()
    assert errors.translate(_wrap(empty, sa_exc.OperationalError), "mssql")[0] == "E_INTERNAL"


def test_unbound_parameter_name_in_hint():
    inner = sa_exc.InvalidRequestError("A value is required for bind parameter 'x'")
    exc = sa_exc.StatementError("msg " + SQL_TEXT, SQL_TEXT, PARAMS, inner)
    code, message = errors.translate(exc, "postgresql")
    assert code == "E_OPTION_INVALID"
    assert "'x'" in message
    assert "\\:" in message
    assert "CAST(:x AS type)" in message or "CAST(" in message
    bare = sa_exc.InvalidRequestError("A value is required for bind parameter 'y'")
    assert errors.translate(bare, "mysql")[0] == "E_OPTION_INVALID"
    nameless = sa_exc.InvalidRequestError("A value is required for bind parameter")
    assert errors.translate(nameless, "mysql")[0] == "E_OPTION_INVALID"


def test_other_statement_and_request_errors_are_internal():
    other = sa_exc.InvalidRequestError("Something else with " + SECRET)
    assert errors.translate(other, "postgresql") == ("E_INTERNAL", "InvalidRequestError")
    stmt = sa_exc.StatementError("msg " + SQL_TEXT, SQL_TEXT, PARAMS, ValueError(SECRET))
    assert errors.translate(stmt, "postgresql") == ("E_INTERNAL", "StatementError")


def test_wrong_dialect_shape_never_guesses():
    assert errors.translate(_wrap(_pg("42601")), "sqlite") == ("E_INTERNAL", "DatabaseError")
    assert errors.translate(_wrap(_pg("42601")), None) == ("E_INTERNAL", "DatabaseError")


def test_dbapi_error_without_orig():
    exc = sa_exc.DBAPIError(SQL_TEXT, PARAMS, None)
    assert errors.translate(exc, "postgresql") == ("E_INTERNAL", "DBAPIError")


# --------------------------------------------------------------------------------------------------
# Non-DBAPI exceptions
# --------------------------------------------------------------------------------------------------
def test_typeerror_sql_ascii_only_for_postgresql():
    exc = TypeError("cannot use a string pattern on a bytes-like object")
    code, message = errors.translate(exc, "postgresql")
    assert code == "E_CONNECT"
    assert "UTF8" in message
    assert errors.translate(exc, "mysql") == ("E_INTERNAL", "TypeError")
    assert errors.translate(TypeError("unrelated " + SECRET), "postgresql") == (
        "E_INTERNAL",
        "TypeError",
    )


def test_pymysql_auth_exceptions_only_for_mysql_family():
    rt = RuntimeError(
        "'cryptography' package is required for sha256_password or caching_sha2_password auth methods"
    )
    assert errors.translate(rt, "mariadb")[0] == "E_DRIVER_MISSING"
    assert errors.translate(rt, "postgresql") == ("E_INTERNAL", "RuntimeError")
    assert errors.translate(RuntimeError("other"), "mysql") == ("E_INTERNAL", "RuntimeError")
    at = AttributeError("'NoneType' object has no attribute 'is_auth_switch_request'")
    assert errors.translate(at, "mysql")[0] == "E_CONNECT"
    assert errors.translate(at, "oracle") == ("E_INTERNAL", "AttributeError")
    assert errors.translate(AttributeError("x"), "mysql") == ("E_INTERNAL", "AttributeError")


def test_valueerror_percent_hint():
    exc = ValueError("unsupported format character 'b' (0x62) at index 20")
    code, message = errors.translate(exc, "mysql")
    assert code == "E_OPTION_INVALID"
    assert "exec_driver_sql" in message
    assert errors.translate(ValueError("nope " + SECRET), "mysql") == ("E_INTERNAL", "ValueError")


@pytest.mark.parametrize(
    "exc",
    [
        KeyError(SECRET),
        ValueError(SECRET),
        Exception(f"{SQL_TEXT} {SECRET}"),
        ZeroDivisionError(),
        sa_exc.NoSuchModuleError(f"Can't load plugin: sqlalchemy.dialects:{SECRET}"),
        sa_exc.ArgumentError(f"Could not parse URL postgresql://u:{SECRET}@h/db"),
        CommandExecutionError(SECRET),
        SaltInvocationError(f"no prefix {SECRET}"),
    ],
)
def test_unknown_exceptions_are_internal_with_class_name_only(exc):
    code, message = errors.translate(exc, "postgresql")
    assert code == "E_INTERNAL"
    assert message == type(exc).__name__
    with pytest.raises(CommandExecutionError) as info:
        errors.raise_for(exc, "postgresql")
    assert str(info.value) == f"[E_INTERNAL] postgresql: {type(exc).__name__}"


def test_internal_for_known_driver_error_with_unmapped_code_keeps_code_not_text():
    exc = _wrap(_pg("XX000", f"internal error mentioning {SECRET}", cls_name="InternalError"))
    with pytest.raises(CommandExecutionError) as info:
        errors.raise_for(exc, "postgresql")
    assert str(info.value) == "[E_INTERNAL] postgresql XX000: InternalError"


# --------------------------------------------------------------------------------------------------
# Pass-through of other modules' prefixed errors
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(SPEC_CODES))
def test_prefixed_salt_errors_pass_through_unchanged(code):
    for cls in (SaltInvocationError, CommandExecutionError):
        exc = cls(f"[{code}] something specific")
        assert errors.translate(exc, "postgresql") == (code, "something specific")
        with pytest.raises(cls) as info:
            errors.raise_for(exc, "postgresql")
        assert str(info.value) == f"[{code}] something specific"


def test_prefix_with_unknown_code_is_not_trusted():
    exc = CommandExecutionError("[E_MADE_UP] hello")
    assert errors.translate(exc, "postgresql") == ("E_INTERNAL", "CommandExecutionError")


# --------------------------------------------------------------------------------------------------
# Chaining
# --------------------------------------------------------------------------------------------------
def _raise_inside_handler(exc, dialect="postgresql"):
    try:
        raise exc
    except Exception as caught:  # pylint: disable=broad-except
        errors.raise_for(caught, dialect)


def test_chaining_is_cleared_when_raised_inside_except():
    orig = _pg("23505", "duplicate key")
    exc = _wrap(orig, sa_exc.IntegrityError)
    with pytest.raises(CommandExecutionError) as info:
        _raise_inside_handler(exc)
    out = info.value
    assert out.__cause__ is None
    assert out.__context__ is None
    assert out.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(out))
    assert SQL_TEXT not in rendered
    assert SECRET not in rendered
    assert "IntegrityError" not in rendered


def test_chaining_is_cleared_for_raw_and_passthrough_exceptions():
    for exc in (ValueError(SECRET), CommandExecutionError("[E_SQL] x")):
        with pytest.raises(Exception) as info:
            _raise_inside_handler(exc)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None
        assert SECRET not in "".join(traceback.format_exception(info.value))


def test_raise_for_outside_except_has_no_chain():
    with pytest.raises(CommandExecutionError) as info:
        errors.raise_for(ValueError(SECRET), "mysql")
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


# --------------------------------------------------------------------------------------------------
# Message hygiene and spec coverage
# --------------------------------------------------------------------------------------------------
def test_message_whitespace_collapsed_and_length_capped():
    orig = _pg("42601", "line one\n\tline two " + "x" * 5000)
    _, message = errors.translate(_wrap(orig), "postgresql")
    assert "\n" not in message
    assert "\t" not in message
    assert len(message) <= 1000
    assert message.startswith("line one line two")


def test_codes_constant_matches_spec_section_8():
    assert set(errors.CODES) == SPEC_CODES
    assert len(errors.CODES) == len(SPEC_CODES) == 16


def test_every_spec_code_is_produced_or_documented():
    """Section 8 lists 16 codes. translate() produces the database/driver ones; the rest come from validation."""
    produced = {p.values[2] for p in OBSERVED if p.values[2]}
    produced |= {"E_OPTION_INVALID", "E_DRIVER_MISSING"}  # raw-exception tests above
    produced |= (
        SPEC_CODES  # passthrough of other modules' prefixed errors (test_prefixed_salt_errors_...)
    )
    assert SPEC_CODES <= produced
    # Codes that translate() produces from a database or driver exception on its own, with a test above:
    from_driver = {
        "E_CONNECT",
        "E_AUTH_FAILED",
        "E_SQL",
        "E_SQL_DATA",
        "E_CONSTRAINT",
        "E_READ_ONLY",
        "E_TIMEOUT",
        "E_INTERNAL",
        "E_OPTION_INVALID",
        "E_DRIVER_MISSING",
    }
    assert from_driver == set(errors.DRIVER_CODES)
    # Not producible from a driver exception: raised by validation/policy/secret code and passed through unchanged.
    assert set(errors.CODES) - set(errors.DRIVER_CODES) == {
        "E_PROFILE_UNKNOWN",
        "E_PROFILE_INVALID",
        "E_OPTION_UNKNOWN",
        "E_SECRET",
        "E_POLICY",
        "E_EXECUTE_DISABLED",
    }


def test_no_driver_or_sqlalchemy_names_leak_into_module_imports():
    source = (REPO_ROOT / "src" / "saltext" / "sqlx" / "lib" / "errors.py").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"^\s*(import|from)\s+(psycopg|pymysql|oracledb|pymssql)", source, re.M)
    assert "str(exc)" not in source  # the SQLAlchemy str() embeds SQL and values (R3)


# --------------------------------------------------------------------------------------------------
# T11 D7: values the driver cannot represent are data errors, not E_INTERNAL
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "timestamp too large (after year 10K): 'infinity'",
        "date too large (after year 10K): '5874897-01-01'",
        "timestamp too small (before year 1): '-infinity'",
    ],
)
def test_postgres_driver_data_error_is_sql_data_without_the_value(text):
    orig = _pg(None, text, cls_name="DataError")
    orig.diag = None  # psycopg raised it client-side: no server diagnostics
    code, message = errors.translate(_wrap(orig, sa_exc.DataError), "postgresql")
    assert code == "E_SQL_DATA"
    assert "too " in message and "'" not in message and "infinity" not in message


def test_postgres_driver_data_error_keeps_the_value_with_error_detail():
    orig = _pg(None, "timestamp too large (after year 10K): 'infinity'", cls_name="DataError")
    orig.diag = None
    code, message = errors.translate(_wrap(orig, sa_exc.DataError), "postgresql", error_detail=True)
    assert code == "E_SQL_DATA" and "'infinity'" in message


def test_overflow_error_is_a_data_error():
    code, message = errors.translate(OverflowError("date value out of range"), "postgresql")
    assert code == "E_SQL_DATA" and "out of range" in message


# -- PostgreSQL connect failures without SQLSTATE (docs/WIRE_SHAPES.md section 13, observed 2026-10-30) -----------------

_PG_CONN = 'connection failed: connection to server at "127.0.0.1", port 54329 failed: '
_PG_CONNECT_ROWS = [
    # (case, driver class name, driver message, expected code)
    ("no_password", "OperationalError", _PG_CONN + "fe_sendauth: no password supplied", "E_AUTH_FAILED"),
    (
        "wrong_password",
        "OperationalError",
        _PG_CONN + 'FATAL:  password authentication failed for user "ro"',
        "E_AUTH_FAILED",
    ),
    (
        "role_cannot_login",
        "OperationalError",
        _PG_CONN + 'FATAL:  role "pgx_nologin" is not permitted to log in',
        "E_AUTH_FAILED",
    ),
    ("connect_timeout", "ConnectionTimeout", "connection timeout expired", "E_CONNECT"),
    (
        "port_closed",
        "OperationalError",
        'connection failed: connection to server at "127.0.0.1", port 1 failed: Connection refused\n'
        "\tIs the server running on that host and accepting TCP/IP connections?",
        "E_CONNECT",
    ),
    (
        "unknown_host",
        "OperationalError",
        "failed to resolve host 'no-such-host.invalid': [Errno -2] Name or service not known",
        "E_CONNECT",
    ),
    (
        "server_closed",
        "OperationalError",
        _PG_CONN + "server closed the connection unexpectedly\n\tThis probably means the server terminated "
        "abnormally\n\tbefore or while processing the request.",
        "E_CONNECT",
    ),
    (
        "pg_hba_reject",
        "OperationalError",
        _PG_CONN + 'FATAL:  pg_hba.conf rejects connection for host "127.0.0.1", user "pgx_rej", '
        'database "sqlx_test", no encryption',
        "E_CONNECT",
    ),
    (
        "pg_hba_no_entry",
        "OperationalError",
        _PG_CONN + 'FATAL:  no pg_hba.conf entry for host "127.0.0.1", user "rw", database "sqlx_test", '
        "no encryption",
        "E_CONNECT",
    ),
    (
        "ssl_required",
        "OperationalError",
        _PG_CONN + "server does not support SSL, but SSL was required",
        "E_CONNECT",
    ),
    (
        "role_connection_limit",
        "OperationalError",
        _PG_CONN + 'FATAL:  too many connections for role "pgx_lim"',
        "E_CONNECT",
    ),
    (
        "database_refuses",
        "OperationalError",
        _PG_CONN + 'FATAL:  database "sqlx_test" is not currently accepting connections',
        "E_CONNECT",
    ),
    (
        "unknown_database",
        "OperationalError",
        _PG_CONN + 'FATAL:  database "nodb" does not exist',
        "E_CONNECT",
    ),
]


def _pg_connect_exc(cls_name, text):
    orig = type(cls_name, (Exception,), {})(text)
    orig.sqlstate = None
    orig.diag = SimpleNamespace(
        message_primary=None, message_hint=None, message_detail=None, sqlstate=None
    )
    return _wrap(orig, sa_exc.OperationalError)


@pytest.mark.parametrize(
    ("case", "cls_name", "text", "expected"), _PG_CONNECT_ROWS, ids=[r[0] for r in _PG_CONNECT_ROWS]
)
def test_postgres_connect_failures_keep_the_real_libpq_message(case, cls_name, text, expected):
    exc = _pg_connect_exc(cls_name, text)
    result = errors.describe(exc, "postgresql")
    assert result.code == expected, case
    assert result.native is None
    assert result.message == errors._clean(text)  # the real message, whitespace collapsed, nothing else
    with pytest.raises(CommandExecutionError) as err:
        errors.raise_for(exc, "postgresql")
    assert str(err.value) == f"[{expected}] postgresql: {errors._clean(text)}"


def test_postgres_connect_failure_rows_are_in_wire_shapes_section_13():
    doc = (REPO_ROOT / "docs" / "WIRE_SHAPES.md").read_text(encoding="utf-8")
    section = doc.split("## 13. Connect failures with no SQLSTATE", 1)[1]
    for case, _cls, text, _code in _PG_CONNECT_ROWS:
        marker = text.rsplit("failed: ", 1)[-1].split("\n", 1)[0]
        assert marker in section, f"{case}: {marker!r} is not recorded in WIRE_SHAPES section 13"


def test_postgres_connect_timeout_is_not_e_timeout():
    # SPEC section 8: E_TIMEOUT is a statement timeout / cancel; a connect timeout is E_CONNECT.
    exc = _pg_connect_exc("ConnectionTimeout", "connection timeout expired")
    assert errors.translate(exc, "postgresql")[0] == "E_CONNECT"


def test_postgres_unrecognised_connect_failure_is_still_a_connect_failure():
    # psycopg wraps every libpq connect failure as "connection failed: ..."; an unlisted libpq text is not E_INTERNAL.
    exc = _pg_connect_exc("OperationalError", _PG_CONN + "some future libpq wording")
    code, message = errors.translate(exc, "postgresql")
    assert code == "E_CONNECT" and "some future libpq wording" in message


def test_postgres_auth_text_wins_over_the_generic_connect_prefix():
    exc = _pg_connect_exc("OperationalError", _PG_CONN + "fe_sendauth: no password supplied")
    assert errors.translate(exc, "postgresql")[0] == "E_AUTH_FAILED"


@pytest.mark.parametrize("text", ["something about a timeout", "connection is bad", "no route"])
def test_postgres_unrelated_text_is_still_internal(text):
    code, message = errors.translate(_pg_connect_exc("OperationalError", text), "postgresql")
    assert (code, message) == ("E_INTERNAL", "OperationalError")


def test_postgres_bare_timeout_expired_is_a_connect_failure():
    # older psycopg wording of the same connect timeout (no "connection" prefix)
    exc = _pg_connect_exc("ConnectionTimeout", "timeout expired")
    assert errors.translate(exc, "postgresql") == ("E_CONNECT", "timeout expired")


def test_mysql_family_connect_failures_are_not_internal():
    # docs/WIRE_SHAPES.md section 13 (MySQL table): no change was needed.
    for vendor, text, expected in (
        (1045, "Access denied for user 'ro'@'localhost' (using password: NO)", "E_AUTH_FAILED"),
        (2003, "Can't connect to MySQL server on '10.255.255.1' (timed out)", "E_CONNECT"),
        (
            2003,
            "Can't connect to MySQL server on 'no-such-host.invalid' ([Errno -2] Name or service not known)",
            "E_CONNECT",
        ),
    ):
        exc = _wrap(_my(vendor, text, "28000" if vendor == 1045 else None), sa_exc.OperationalError)
        assert errors.describe(exc, "mysql").code == expected
