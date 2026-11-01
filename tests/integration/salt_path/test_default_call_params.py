# pylint: disable=use-dict-literal
"""D16 through a REAL master and minion: the connection in the call, with NO ``sqlx:_policy`` in the minion config.

The ``salt_path`` minion has profiles and no policy, so this is the compat default end to end: calls travel
``LocalClient().cmd`` -> master -> minion job -> ``sqlx`` module -> live database, and the answers come back through the
master. Real databases from ``tests-support/env.sh``; nothing is started or stopped here.
"""

import pytest

from tests.integration.salt_path.conftest import HOST
from tests.integration.salt_path.conftest import MARIADB_PORT
from tests.integration.salt_path.conftest import MYSQL_PORT
from tests.integration.salt_path.conftest import PG_PORT
from tests.integration.salt_path.conftest import RO_PASSWORD

LITERAL_PW = RO_PASSWORD  # a literal, as Salt's own psql_query callers send


@pytest.mark.pg
def test_psql_query_with_salts_argument_names_and_a_literal_password(call):
    ret, retcode, _ = call(
        "sqlx.psql_query",
        query="select 1 as one, null as nothing",
        host=HOST,
        port=PG_PORT,
        user="ro",
        maintenance_db="sqlx_test",
        password=LITERAL_PW,
    )
    assert retcode == 0, ret
    assert ret == [{"one": "1", "nothing": None}]


@pytest.mark.pg
@pytest.mark.mysql
@pytest.mark.mariadb
@pytest.mark.parametrize(
    "dialect,port,database",
    [
        ("postgresql", PG_PORT, "sqlx_test"),
        ("mysql", MYSQL_PORT, "scratch"),
        ("mariadb", MARIADB_PORT, "scratch"),
    ],
)
def test_every_function_with_the_connection_in_the_call(call, dialect, port, database):
    conn = dict(
        dialect=dialect, host=HOST, port=port, database=database, username="ro", password=LITERAL_PW
    )
    ret, retcode, _ = call("sqlx.ping", **conn)
    assert retcode == 0 and ret["ok"] is True and ret["profile"] == "(adhoc)", ret
    ret, retcode, _ = call("sqlx.query", sql="select 1 as one", **conn)
    assert retcode == 0 and ret["rows"] == [[1]], ret
    ret, retcode, _ = call(
        "sqlx.check_row_count", sql="select 1", operator="==", expected=1, **conn
    )
    assert retcode == 0 and ret["ok"] is True, ret
    ret, retcode, _ = call(
        "sqlx.check_query_result", sql="select 1", operator="==", expected=1, **conn
    )
    assert retcode == 0 and ret["ok"] is True, ret
    ret, retcode, _ = call("sqlx.table_list", **conn)
    assert retcode == 0 and "tables" in ret, ret
    assert LITERAL_PW not in repr(ret)


@pytest.mark.pg
def test_write_with_an_ad_hoc_connection_is_refused(call):
    conn = dict(host=HOST, port=PG_PORT, database="sqlx_test", username="rw", password="rw_pw")
    ret, retcode, _ = call("sqlx.execute", sql="select 1", dialect="postgresql", **conn)
    assert retcode == 1 and "[E_EXECUTE_DISABLED]" in ret, ret
    ret, retcode, _ = call(
        "sqlx.psql_query",
        query="create table scratch.t_never (a int)",
        host=HOST,
        port=PG_PORT,
        user="rw",
        maintenance_db="sqlx_test",
        password="rw_pw",
        write=True,
    )
    assert retcode == 1 and "[E_EXECUTE_DISABLED]" in ret, ret


@pytest.mark.pg
def test_a_writing_profile_with_connection_fields_does_not_execute(call):
    ret, retcode, _ = call(
        "sqlx.execute", profile="pg_rw", sql="select 1", host=HOST, password="rw_pw"
    )
    assert retcode == 1 and "[E_EXECUTE_DISABLED]" in ret, ret


@pytest.mark.pg
@pytest.mark.parametrize("host", ["169.254.169.254", "2852039166", "fe80::1"])
def test_metadata_addresses_are_refused(call, host):
    ret, retcode, _ = call(
        "sqlx.ping", dialect="postgresql", host=host, database="d", username="u", password="x"
    )
    assert retcode == 1 and "[E_POLICY]" in ret and host not in ret, ret


@pytest.mark.pg
def test_the_literal_password_is_not_echoed_in_an_error(call):
    ret, retcode, _ = call(
        "sqlx.ping",
        dialect="postgresql",
        host=HOST,
        port=PG_PORT,
        database="sqlx_test",
        username="ro",
        password="WRONG-LITERAL-PW-4471",
    )
    assert retcode == 1 and "WRONG-LITERAL-PW-4471" not in ret, ret
    assert ret.startswith("ERROR: [E_AUTH_FAILED]"), ret


@pytest.mark.pg
def test_opt_in_fields_are_refused_by_default(call):
    ret, retcode, _ = call(
        "sqlx.ping",
        dialect="postgresql",
        host=HOST,
        port=PG_PORT,
        database="sqlx_test",
        username="ro",
        password=LITERAL_PW,
        read_only=False,
    )
    assert retcode == 1 and "[E_POLICY]" in ret and "read_only" in ret, ret
