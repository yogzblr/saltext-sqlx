"""The verified ``sqlalchemy.text()`` traps (BUILD_PROMPT R11/R12, SPEC section 5), proven with no database.

``TextClause.compile()`` / ``_bindparams`` is enough: the traps are about which names SQLAlchemy reads as binds.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects import postgresql


def binds(sql):
    return sorted(text(sql)._bindparams)


def test_double_colon_cast_hides_the_bind():
    """``:a::int`` is NOT read as a bind named ``a``; the driver would see a literal ``:a``."""
    clause = text("select :a::int")
    assert "a" not in clause._bindparams
    assert clause._bindparams == {}
    assert str(clause) == "select :a::int"


def test_cast_function_yields_the_bind():
    clause = text("select CAST(:a AS int)")
    assert list(clause._bindparams) == ["a"]
    compiled = clause.compile(dialect=postgresql.dialect())
    assert "CAST(%(a)s AS int)" in str(compiled)


def test_double_colon_cast_with_a_space_is_a_bind_but_still_not_portable():
    """Only the exact ``:a::`` form hides the bind; do not rely on spacing, write ``CAST(:a AS int)``."""
    assert binds("select :a ::int") == ["a"]


def test_unescaped_colon_in_json_literal_is_read_as_bind_named_1():
    assert binds("select '{\"a\":1}'") == ["1"]


def test_escaped_colon_in_json_literal_is_not_a_bind():
    clause = text("select '{\"a\"\\:1}'")
    assert clause._bindparams == {}
    compiled = clause.compile(dialect=postgresql.dialect())
    assert "{\"a\":1}" in str(compiled)
    assert "\\" not in str(compiled)


def test_percent_inside_a_literal_is_fine_and_there_is_no_bind():
    clause = text("select * from t where b like '%b%'")
    assert clause._bindparams == {}
    pg = str(clause.compile(dialect=postgresql.dialect()))
    my = str(clause.compile(dialect=mysql.dialect()))
    assert "'%%b%%'" in pg  # doubled for the pyformat paramstyle, which is why text() is safe and exec_driver_sql is not
    assert "'%%b%%'" in my


def test_percent_with_a_bind_is_fine():
    clause = text("select * from t where b like '%b%' and id = :id")
    assert list(clause._bindparams) == ["id"]


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("select :a, :b", ["a", "b"]),
        ("select :a + :a", ["a"]),
        ("select CAST(:n AS INTEGER) + CAST(:m AS NUMERIC(10,2))", ["m", "n"]),
        ("select 1", []),
        ("select 'a\\:b'", []),
    ],
)
def test_bind_names(sql, expected):
    assert binds(sql) == expected
