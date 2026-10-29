"""Unit tests for saltext.sqlx.lib.psql_compat (argument translation and Salt result shape)."""

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import psql_compat


def test_translate_maps_the_salt_arguments():
    function, kw = psql_compat.translate(
        "select 1;  ",
        None,
        False,
        user="u",
        host="h",
        port=5433,
        maintenance_db="d",
        password="sdb://v/p",
        extra={"limit": 5},
    )
    assert function == "query"
    assert kw == {
        "sql": "select 1",
        "username": "u",
        "host": "h",
        "port": 5433,
        "database": "d",
        "password": "sdb://v/p",
        "limit": 5,
    }


def test_translate_skips_none_and_routes_write_to_execute():
    function, kw = psql_compat.translate("insert into t values (1)", None, True)
    assert function == "execute"
    assert kw == {"sql": "insert into t values (1)"}


def test_runas_is_refused_with_the_documented_message():
    with pytest.raises(SaltInvocationError) as exc:
        psql_compat.translate("select 1", "postgres", False)
    assert (
        str(exc.value)
        == "[E_OPTION_INVALID] runas is not supported; sqlx does not use psql or an OS user"
    )
    with pytest.raises(SaltInvocationError):
        psql_compat.translate("select 1", "", False)  # any non-None value, even empty


@pytest.mark.parametrize("write", [1, "yes", 0, "false"])
def test_write_must_be_a_bool(write):
    with pytest.raises(SaltInvocationError) as exc:
        psql_compat.translate("select 1", None, write)
    assert "[E_OPTION_INVALID]" in str(exc.value)


def test_query_must_be_text():
    with pytest.raises(SaltInvocationError):
        psql_compat.translate(5, None, False)


def test_conflict_with_the_sqlx_spelling():
    with pytest.raises(SaltInvocationError) as exc:
        psql_compat.translate("select 1", None, False, user="a", extra={"username": "b"})
    assert "[E_OPTION_INVALID]" in str(exc.value)
    assert "'a'" not in str(exc.value)


def test_supplied_names():
    assert psql_compat.supplied_names(
        user="u", host=None, port=0, maintenance_db=None, password="x"
    ) == ["password", "port", "user"]
    assert psql_compat.supplied_names() == []


def test_shape_is_a_list_of_dicts_of_text_or_none():
    result = {
        "columns": ["id", "name", "ok", "doc", "n"],
        "rows": [[1, "a", True, {"b": [1]}, None], [2, None, False, [1, 2], 1.5]],
    }
    assert psql_compat.shape(result, False) == [
        {"id": "1", "name": "a", "ok": "t", "doc": '{"b":[1]}', "n": None},
        {"id": "2", "name": None, "ok": "f", "doc": "[1,2]", "n": "1.5"},
    ]
    assert psql_compat.shape({"columns": ["x"], "rows": []}, False) == []


def test_shape_write():
    assert psql_compat.shape({"rows_affected": 3, "committed": True}, True) == [
        {"rows_affected": "3"}
    ]
