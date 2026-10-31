"""Happy paths of every function on every database (Sakila data), through the remote-minion CLI and salt-api."""

import base64

import pytest
from conftest import DBS
from conftest import assert_error
from conftest import assert_ok
from dbs import SCHEMA
from dbs import TABLES
from dbs import VIEWS
from dbs import sql

DIALECT = {
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "mssql": "mssql",
}

# Values from the PostgreSQL run of the same Sakila data (identical on all five servers, asserted below)
ACTOR1_FILMS = 19
CUSTOMER1_RENTALS = 32


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("path", ("cli", "api"))
def test_ping(salt, db, path):
    data = assert_ok(salt.run(path, "sqlx.ping", profile=f"{db}_ro"))
    assert data["ok"] is True and data["dialect"] == DIALECT[db] and data["server_version"]


@pytest.mark.parametrize("db", DBS)
def test_count_film_is_1000_on_every_path(salt, db):
    for path in ("cli", "call", "api"):
        data = assert_ok(
            salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, "count_film"))
        )
        assert data["rows"] == [[1000]], (path, data)
        assert (
            data["columns"][0].lower() == "n"
            and data["row_count"] == 1
            and data["truncated"] is False
        )


@pytest.mark.parametrize("db", DBS)
def test_joins(salt, db):
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "join_actor")))
    assert data["row_count"] == ACTOR1_FILMS and data["rows"][0] == ["ACADEMY DINOSAUR"]
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "join_rental")))
    assert data["rows"] == [[CUSTOMER1_RENTALS]]


EXPECTED_ROW1 = {
    # column -> expected JSON value per database (see the type mapping in tests-e2e/README.md)
    "t_text": {db: "Grüße ✓ naïve" for db in DBS},
    "t_num": {
        "pg": "12.50",
        "mysql": "12.50",
        "mariadb": "12.50",
        "oracle": "12.5",
        "mssql": "12.50",
    },
    "t_ts": {db: "2026-10-31T12:34:56.789012" for db in DBS},
    "t_uuid": {
        "pg": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "mysql": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "mariadb": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "oracle": {
            "$bytes_b64": base64.b64encode(
                bytes.fromhex("a0eebc999c0b4ef8bb6d6bb9bd380a11")
            ).decode()
        },
        "mssql": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    },
    "t_bin": {db: {"$bytes_b64": "3q2+7w=="} for db in DBS},
    "t_json": {
        "pg": {"a": 1, "b": [True, None]},
        "mysql": '{"a": 1, "b": [true, null]}',
        "mariadb": '{"a": 1, "b": [true, null]}',
        "oracle": {"a": "1", "b": [True, None]},
        "mssql": '{"a": 1, "b": [true, null]}',
    },
    "t_null": {db: None for db in DBS},
}


@pytest.mark.parametrize("db", DBS)
def test_type_probe(salt, db):
    data = assert_ok(salt.api("sqlx.query", profile=f"{db}_ro", sql=sql(db, "types")))
    cols = [c.lower() for c in data["columns"]]
    row1 = dict(zip(cols, data["rows"][0]))
    row2 = dict(zip(cols, data["rows"][1]))
    for col, expected in EXPECTED_ROW1.items():
        assert row1[col] == expected[db], (db, col, row1[col])
    assert all(v is None for k, v in row2.items() if k != "id"), row2


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("path", ("cli", "api"))
def test_cast_param_and_literal_colon(salt, db, path):
    data = assert_ok(
        salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, "cast"), params={"x": "41"})
    )
    assert data["rows"] == [[42]]
    data = assert_ok(salt.run(path, "sqlx.query", profile=f"{db}_ro", sql=sql(db, "colon")))
    assert data["rows"] == [["a:b"]]


@pytest.mark.parametrize("db", DBS)
def test_limit_and_truncation(salt, db):
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "films"), limit=3))
    assert data["rows"] == [[1], [2], [3]] and data["truncated"] is True
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "films"), max_bytes=40))
    assert data["truncated"] is True and 0 < data["row_count"] < 20
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_small", sql=sql(db, "films")))
    assert data["row_count"] == 5 and data["truncated"] is True
    # a call cannot raise the profile cap: clamped silently (D9)
    data = assert_ok(salt.cli("sqlx.query", profile=f"{db}_small", sql=sql(db, "films"), limit=100))
    assert data["row_count"] == 5 and data["truncated"] is True


@pytest.mark.parametrize("db", DBS)
def test_table_list(salt, db):
    data = assert_ok(
        salt.cli("sqlx.table_list", profile=f"{db}_ro", schema=SCHEMA[db], include_views=True)
    )
    assert data["tables"] == TABLES, data
    assert data["views"] == VIEWS, data
    data = assert_ok(
        salt.api(
            "sqlx.table_list",
            profile=f"{db}_ro",
            schema=SCHEMA[db],
            pattern="film%",
            include_views=True,
        )
    )
    assert data["tables"] == ["film", "film_actor", "film_category", "film_text"] and data[
        "views"
    ] == ["film_list"]
    data = assert_ok(salt.cli("sqlx.table_list", profile=f"{db}_ro", schema=SCHEMA[db]))
    assert data["views"] == [] and len(data["tables"]) == 17


@pytest.mark.parametrize("db", ("pg", "mysql", "mariadb", "mssql"))
def test_table_list_default_schema(salt, db):
    data = assert_ok(salt.cli("sqlx.table_list", profile=f"{db}_ro"))
    assert data["schema"] == SCHEMA[db] and data["tables"] == TABLES


ROW_COUNT_CASES = [
    ("==", 3, True),
    ("!=", 3, False),
    ("<", 4, True),
    ("<=", 2, False),
    (">", 2, True),
    (">=", 4, False),
]


@pytest.mark.parametrize("db", DBS)
def test_check_row_count_every_operator(salt, db):
    for op, expected, ok in ROW_COUNT_CASES:
        # CLI: the operator is YAML-quoted ('"=="'), which the harness does for every string value
        data = assert_ok(
            salt.cli(
                "sqlx.check_row_count",
                profile=f"{db}_ro",
                sql=sql(db, "actors3"),
                operator=op,
                expected=expected,
            )
        )
        assert data["ok"] is ok and data["actual"] == 3 and data["attempts"] == 1, (op, data)
    data = assert_ok(
        salt.api(
            "sqlx.check_row_count",
            profile=f"{db}_ro",
            sql=sql(db, "actors3"),
            operator=">=",
            expected=3,
        )
    )
    assert data["ok"] is True
    res = salt.cli(
        "sqlx.check_row_count",
        profile=f"{db}_ro",
        sql=sql(db, "actors3"),
        operator="contains",
        expected=3,
    )
    assert_error(res, "E_OPTION_INVALID")
    data = assert_ok(
        salt.cli(
            "sqlx.check_row_count",
            profile=f"{db}_small",
            sql=sql(db, "films"),
            operator="==",
            expected=5,
        )
    )
    assert data["ok"] is None and data["reason"] == "truncated"


QUERY_RESULT_CASES = [
    ("==", "PENELOPE", True),
    ("!=", "PENELOPE", False),
    ("<", "Q", True),
    ("<=", "A", False),
    (">", "A", True),
    (">=", "Z", False),
    ("contains", "NELO", True),
    ("not_contains", "NELO", False),
]


@pytest.mark.parametrize("db", DBS)
def test_check_query_result_every_operator(salt, db):
    for op, expected, ok in QUERY_RESULT_CASES:
        data = assert_ok(
            salt.cli(
                "sqlx.check_query_result",
                profile=f"{db}_ro",
                sql=sql(db, "actor1_name"),
                operator=op,
                expected=expected,
            )
        )
        assert data["ok"] is ok and data["actual"] == "PENELOPE", (op, data)
    data = assert_ok(
        salt.cli(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "actor1_name"),
            operator="==",
            expected="GUINESS",
            column=1,
        )
    )
    assert data["ok"] is True
    data = assert_ok(
        salt.cli(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "null_value"),
            operator="is_null",
        )
    )
    assert data["ok"] is True and data["actual"] is None
    data = assert_ok(
        salt.api(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "null_value"),
            operator="is_not_null",
        )
    )
    assert data["ok"] is False
    data = assert_ok(
        salt.cli(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "count_film"),
            operator="==",
            expected=1000,
        )
    )
    assert data["ok"] is True
    data = assert_ok(
        salt.cli(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "count_film"),
            operator="==",
            expected=1,
            row=5,
        )
    )
    assert data["ok"] is False and data["reason"] == "no_such_cell"


@pytest.mark.parametrize("db", DBS)
def test_check_retry_loop(salt, db):
    data = assert_ok(
        salt.cli(
            "sqlx.check_query_result",
            profile=f"{db}_ro",
            sql=sql(db, "count_film"),
            operator="==",
            expected=999,
            retry_timeout_s=2,
            retry_pause_s=0.5,
        )
    )
    assert data["ok"] is False and data["attempts"] >= 3, data
    data = assert_ok(
        salt.api(
            "sqlx.check_row_count",
            profile=f"{db}_ro",
            sql=sql(db, "actors3"),
            operator="==",
            expected=3,
            retry_timeout_s=5,
        )
    )
    assert data["ok"] is True and data["attempts"] == 1


@pytest.mark.parametrize("db", DBS)
def test_execute_disabled_on_and_dry_run(salt, db):
    note_id = 7000 + DBS.index(db)
    params = {"i": note_id, "n": f"e2e {db}"}
    rw = f"{db}_rw"
    salt.cli("sqlx.execute", profile=rw, sql=sql(db, "delete_note"), params={"i": note_id})
    # allow_execute off: refused before any connection
    assert_error(
        salt.cli(
            "sqlx.execute", profile=f"{db}_ro_exec_off", sql=sql(db, "insert_note"), params=params
        ),
        "E_EXECUTE_DISABLED",
    )
    assert_error(
        salt.api("sqlx.execute", profile=f"{db}_ro", sql=sql(db, "insert_note"), params=params),
        "E_EXECUTE_DISABLED",
    )
    # dry_run: the statement runs, reports what it changed, and nothing is persisted (checked on a new connection)
    data = assert_ok(
        salt.cli(
            "sqlx.execute", profile=rw, sql=sql(db, "insert_note"), params=params, dry_run=True
        )
    )
    assert data["rows_affected"] == 1 and data["committed"] is False and data["dry_run"] is True
    assert assert_ok(
        salt.api("sqlx.query", profile=rw, sql=sql(db, "count_note"), params={"i": note_id})
    )["rows"] == [[0]]
    # allow_execute on: committed
    data = assert_ok(
        salt.api(
            "sqlx.execute",
            profile=rw,
            sql=sql(db, "insert_note"),
            params=params,
            user="dba_writers",
        )
    )
    assert data["rows_affected"] == 1 and data["committed"] is True
    assert assert_ok(
        salt.cli("sqlx.query", profile=rw, sql=sql(db, "count_note"), params={"i": note_id})
    )["rows"] == [[1]]
    data = assert_ok(
        salt.call("sqlx.execute", profile=rw, sql=sql(db, "delete_note"), params={"i": note_id})
    )
    assert data["rows_affected"] == 1 and data["committed"] is True
    assert assert_ok(
        salt.cli("sqlx.query", profile=rw, sql=sql(db, "count_note"), params={"i": note_id})
    )["rows"] == [[0]]


@pytest.mark.parametrize("db", DBS)
def test_execute_verb_filter(salt, db):
    rw = f"{db}_rw"
    assert_error(
        salt.cli("sqlx.execute", profile=rw, sql=sql(db, "select_verb")),
        "E_POLICY",
        "execute_verbs",
    )
    assert_error(
        salt.api("sqlx.execute", profile=rw, sql="drop table scratch.e2e_notes"),
        "E_POLICY",
        "'drop'",
    )
    assert_error(
        salt.cli("sqlx.execute", profile=rw, sql=sql(db, "exec_comment")), "E_POLICY", "/*!"
    )
    assert_error(salt.cli("sqlx.execute", profile=rw, sql="-- only a comment"), "E_OPTION_INVALID")
