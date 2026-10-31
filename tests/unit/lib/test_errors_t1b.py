"""Oracle and SQL Server error mapping, driven by the T1b observations (docs/wire_shapes/t1b/*.json).

Each recorded error is rebuilt as a stand-in driver exception with the recorded class name and fields, wrapped in the
SQLAlchemy class that was observed, and translated. Observed on Oracle AI Database 26ai Free 23.26.2 (python-oracledb
26.1.0 thin) and SQL Server 2022 CU25 16.0.4255.1 (pymssql 2.4.6), SQLAlchemy 2.0.55, 2026-10-31.
"""

import ast
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy.exc as sa_exc

from saltext.sqlx.lib import errors

SHAPES = Path(__file__).resolve().parents[3] / "docs" / "wire_shapes" / "t1b"
SQL_TEXT = "SELECT secret_marker_sql FROM dual"

ORACLE = {
    "syntax": "E_SQL",
    "syntax_from": "E_SQL",
    "missing_table": "E_SQL",
    "missing_column": "E_SQL",
    "division_by_zero": "E_SQL_DATA",
    "unique_violation": "E_CONSTRAINT",
    "unique_violation_2nd": "E_CONSTRAINT",
    "fk_violation": "E_CONSTRAINT",
    "not_null_violation": "E_CONSTRAINT",
    "permission_denied_write": "E_SQL",
    "permission_denied_unseen": "E_SQL",
    "read_only_txn_insert": "E_READ_ONLY",
    "set_read_write_after_ro": "E_SQL",
    "multi_statement": "E_SQL",
    "multi_statement_dml": "E_SQL",
    "call_timeout_500ms": "E_TIMEOUT",
    "bad_cast": "E_SQL_DATA",
    "value_too_large": "E_SQL_DATA",
    "wrong_password": "E_AUTH_FAILED",
    "no_password": "E_AUTH_FAILED",
    "unknown_user": "E_AUTH_FAILED",
    "unknown_service": "E_CONNECT",
    "closed_port": "E_CONNECT",
    "unknown_host": "E_CONNECT",
    "unreachable_host": "E_CONNECT",
    "t1_div0_dml": "E_SQL_DATA",
    "t1_ro_select_other_schema": "E_SQL",
    "t1_ro_update": "E_SQL",
    "t1_ro_delete": "E_SQL",
    "t1_ro_ddl": "E_SQL",
    "t1_bad_bound_value": "E_SQL_DATA",
    "t1_text_trap_colon_cast": "E_OPTION_INVALID",
    "t1_text_trap_json_colon_literal": "E_OPTION_INVALID",
    "t1_timeout_sleep": "E_TIMEOUT",
    "t1_guard_set_transaction_read_only_write": "E_READ_ONLY",
    "t1_bypass_set_transaction_read_write": "E_SQL",
}
MSSQL = {
    "syntax": "E_SQL",
    "syntax_from": "E_SQL",
    "missing_table": "E_SQL",
    "missing_column": "E_SQL",
    "division_by_zero": "E_SQL_DATA",
    "unique_violation": "E_CONSTRAINT",
    "fk_violation": "E_CONSTRAINT",
    "not_null_violation": "E_CONSTRAINT",
    "permission_denied_write": "E_SQL",
    "permission_denied_scratch": "E_SQL",
    "nul_byte_literal": None,  # refused before the driver since T1b (options.validate); the raw shape is not mapped
    "nul_byte_bind": None,
    "timeout_1s": "E_TIMEOUT",
    "timeout_1s_select": "E_TIMEOUT",
    "bad_cast": "E_SQL_DATA",
    "value_too_large": "E_SQL_DATA",
    # in one process, after wrong_password: the number is a stale 18456, the text decides
    "wrong_password": "E_AUTH_FAILED",
    "no_password": "E_AUTH_FAILED",
    "unknown_database": "E_AUTH_FAILED",
    "closed_port": "E_CONNECT",
    "unknown_host": "E_CONNECT",
    "unreachable_host": "E_CONNECT",
    "t1_div0_dml": "E_SQL_DATA",
    "t1_ro_select_other_schema": "E_SQL",
    "t1_ro_update": "E_SQL",
    "t1_ro_delete": "E_SQL",
    "t1_ro_ddl": "E_SQL",
    "t1_bad_bound_value": "E_SQL_DATA",
    "t1_text_trap_colon_cast": "E_SQL",
    "t1_text_trap_json_colon_literal": "E_OPTION_INVALID",
    "t1_timeout_sleep": "E_TIMEOUT",
}
#: values the server echoed that must not survive without error_detail
LEAKS = {
    ("oracle", "unique_violation_2nd"): "(ID:900)",
    ("oracle", "bad_cast"): "abc",
    ("mssql", "unique_violation"): "value is (1)",
    ("mssql", "value_too_large"): "xxxxxxxxxx",
    ("mssql", "bad_cast"): "'abc'",
    ("oracle", "t1_bad_bound_value"): "zz_secret_bound_value",
    ("mssql", "t1_bad_bound_value"): "zz_secret_bound_value",
}


def _load(dialect):
    data = json.loads((SHAPES / f"{dialect}.json").read_text(encoding="utf-8"))
    return {
        **data["cases"],
        **{f"fresh:{k}": v for k, v in data.get("cases_fresh_process", {}).items()},
    }


def _cls(qualified):
    return type(qualified.rsplit(".", 1)[-1], (Exception,), {})


def _rebuild(dialect, rec):
    if (
        rec["driver_class"] == "sqlalchemy.exc.InvalidRequestError"
    ):  # text() bind trap, raised before the driver
        return sa_exc.StatementError(
            "(detail) " + SQL_TEXT,
            SQL_TEXT,
            {},
            sa_exc.InvalidRequestError(*ast.literal_eval(rec["args"])),
        )
    if rec["sa_class"] is None:
        assert rec["driver_class"] == "socket.gaierror"
        return socket.gaierror(*ast.literal_eval(rec["args"]))
    if dialect == "oracle":
        err = SimpleNamespace(full_code=rec["full_code"], code=rec["code"], message=rec["message"])
        orig = _cls(rec["driver_class"])(err)
    else:
        orig = _cls(rec["driver_class"])(*ast.literal_eval(rec["args"]))
    return getattr(sa_exc, rec["sa_class"].rsplit(".", 1)[-1])(
        SQL_TEXT, {"p": "bound_marker"}, orig
    )


def _rows(dialect, table):
    rows = []
    for name, rec in _load(dialect).items():
        if rec["outcome"] != "error":
            continue
        case = name.split(":", 1)[-1]
        rows.append(pytest.param(dialect, case, rec, table[case], id=f"{dialect}-{name}"))
    return rows


@pytest.mark.parametrize(
    "dialect,case,rec,expected", _rows("oracle", ORACLE) + _rows("mssql", MSSQL)
)
def test_observed_error_maps_to_the_code(dialect, case, rec, expected):
    if expected is None:
        pytest.skip("refused before the driver")
    found = errors.describe(_rebuild(dialect, rec), dialect)
    assert found.code == expected, (case, found)
    text = found.message
    assert "Help: http" not in text and "DB-Lib error message" not in text and "\n" not in text
    assert "secret_marker_sql" not in text and "bound_marker" not in text
    assert "General SQL Server error" not in text
    leak = LEAKS.get((dialect, case))
    if leak:
        assert leak not in text, text
        assert leak in errors.describe(_rebuild(dialect, rec), dialect, error_detail=True).message
    if dialect == "oracle" and found.native:
        assert found.native == rec.get("full_code") and not text.startswith(found.native)


def test_every_observed_error_case_has_an_expectation():
    for dialect, table in (("oracle", ORACLE), ("mssql", MSSQL)):
        cases = {n.split(":", 1)[-1] for n, r in _load(dialect).items() if r["outcome"] == "error"}
        assert cases <= set(table), (dialect, cases - set(table))


def test_mssql_stale_number_does_not_decide_a_connect_failure():
    text = b"DB-Lib error message 20009, severity 9:\nUnable to connect: TDS server is unavailable or does not exist (h)\n"
    orig = type("OperationalError", (Exception,), {})((18456, text))
    found = errors.describe(sa_exc.OperationalError(SQL_TEXT, {}, orig), "mssql")
    assert found.code == "E_CONNECT" and found.native == "20009"
    assert found.message == "Unable to connect: TDS server is unavailable or does not exist (h)"


def test_mssql_message_formats():
    orig = type("ProgrammingError", (Exception,), {})(
        208,
        b"Invalid object name 'nope'.DB-Lib error message 20018, severity 16:\nGeneral SQL Server error: Check messages from the SQL Server\n",
    )
    found = errors.describe(sa_exc.ProgrammingError(SQL_TEXT, {}, orig), "mssql")
    assert found == errors.Translation("E_SQL", "208", "Invalid object name 'nope'.")


def test_oracle_message_format_and_unobserved_code():
    err = SimpleNamespace(
        full_code="ORA-00942",
        message='ORA-00942: table or view "RO"."NOPE" does not exist\nHelp: https://docs.oracle.com/error-help/db/ora-00942/',
    )
    orig = type("DatabaseError", (Exception,), {})(err)
    found = errors.describe(sa_exc.DatabaseError(SQL_TEXT, {}, orig), "oracle")
    assert found == errors.Translation(
        "E_SQL", "ORA-00942", 'table or view "RO"."NOPE" does not exist'
    )
    with pytest.raises(Exception) as caught:
        errors.raise_for(sa_exc.DatabaseError(SQL_TEXT, {}, orig), "oracle")
    assert str(caught.value) == '[E_SQL] oracle ORA-00942: table or view "RO"."NOPE" does not exist'
    unknown = type("DatabaseError", (Exception,), {})(
        SimpleNamespace(full_code="ORA-99999", message="ORA-99999: x")
    )
    assert errors.translate(sa_exc.DatabaseError(SQL_TEXT, {}, unknown), "oracle") == (
        "E_INTERNAL",
        "DatabaseError",
    )
    integrity = type("IntegrityError", (Exception,), {})(
        SimpleNamespace(full_code="ORA-02290", message="ORA-02290: check constraint violated")
    )
    assert (
        errors.translate(sa_exc.IntegrityError(SQL_TEXT, {}, integrity), "oracle")[0]
        == "E_CONSTRAINT"
    )


def test_bare_gaierror_is_a_connect_failure():
    assert errors.translate(socket.gaierror(-2, "Name or service not known"), "oracle") == (
        "E_CONNECT",
        "cannot resolve the database host name: Name or service not known",
    )
