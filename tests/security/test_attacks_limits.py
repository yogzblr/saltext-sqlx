# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""T11 part 2, attack class (4): unbounded results, hangs past the caps, CPU and connection floods.

Memory tests run the public call in a child process (``probe_child.py``) so that a runaway allocation cannot take the
test run down; the child has an address-space limit.
"""

import json
import pathlib
import subprocess
import sys
import time

import pytest

from tests.security import harness
from tests.security.harness import observe
from tests.security.test_attacks_query import defect

CHILD = pathlib.Path(__file__).with_name("probe_child.py")
REPO = CHILD.parents[2]


def child(key, profile, function, kwargs, limit_mb=2500, timeout=90):
    proc = subprocess.run(
        [sys.executable, str(CHILD), key, profile, function, json.dumps(kwargs), str(limit_mb)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"child produced no result: rc={proc.returncode} stderr={proc.stderr[-400:]}"
    return json.loads(lines[-1][7:])


@pytest.fixture(scope="module")
def baseline_mb(server):
    return child(server.key, "ro", "query", {"sql": "SELECT 1"})["peak_rss_mb"]


# ---- timeouts --------------------------------------------------------------------------------------------------------
def timed(salt, profile, sql, **kw):
    start = time.time()
    outcome = harness.attempt(lambda: salt.query(profile, sql, **kw))
    return outcome, time.time() - start


def test_single_statement_sleep_is_cut_at_the_timeout(server, salt):
    sql = (
        "SELECT pg_sleep(4)"
        if server.key == "postgresql"
        else "SELECT SLEEP(4) FROM scratch.orders LIMIT 1"
    )
    outcome, seconds = timed(salt, "ro", sql, timeout_ms=800)
    observe(
        "timeout",
        f"{server.key} sleep(4) with timeout_ms=800",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5


def test_tableless_mysql_sleep_is_cut_silently(server, salt):
    if server.key != "mysql":
        pytest.skip("MySQL 8 only (documented in SPEC section 7)")
    outcome, seconds = timed(salt, "ro", "SELECT SLEEP(4)", timeout_ms=800)
    observe(
        "timeout",
        "mysql SELECT SLEEP(4) timeout_ms=800",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5 and outcome[0] == "OK" and outcome[1]["rows"] == [[1]]


def test_pg_session_timeout_cannot_be_switched_off_by_the_statement(request, server, salt):
    """D1: ``SET statement_timeout = 0; SELECT pg_sleep(..)`` is two statements in one call on PostgreSQL."""
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    for sql in (
        "SET statement_timeout = 0; SELECT pg_sleep(3)",
        "RESET ALL; SELECT pg_sleep(3)",
        "SET LOCAL statement_timeout = 0; SELECT pg_sleep(3)",
    ):
        outcome, seconds = timed(salt, "ro", sql, timeout_ms=800)
        observe("timeout", f"postgresql {sql}", f"{seconds:.1f}s {harness.describe(outcome)}")
        assert seconds < 2.5, f"{sql!r} ran for {seconds:.1f}s under a 0.8 s cap"


def test_pg_single_statement_cannot_reset_its_own_timer(server, salt):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    outcome, seconds = timed(
        salt,
        "ro",
        "SELECT set_config('statement_timeout', '0', false), pg_sleep(3)",
        timeout_ms=800,
    )
    observe(
        "timeout",
        "postgresql set_config(statement_timeout,0) + pg_sleep(3) in one statement",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5 and outcome[0] == "ERR" and "[E_TIMEOUT]" in str(outcome[1])


def test_mysql_hint_cannot_raise_the_statement_timeout(request, server, salt):
    """D2: ``/*+ MAX_EXECUTION_TIME(n) */`` overrides the session variable, so a caller can lift the cap."""
    if server.key != "mysql":
        pytest.skip("MySQL 8 only")
    defect(
        request,
        server,
        "D2",
        "optimizer hint MAX_EXECUTION_TIME(100000) overrides the module's session timeout",
    )
    outcome, seconds = timed(
        salt,
        "ro",
        "SELECT /*+ MAX_EXECUTION_TIME(100000) */ SLEEP(3) FROM scratch.orders LIMIT 1",
        timeout_ms=800,
    )
    observe(
        "timeout",
        "mysql /*+ MAX_EXECUTION_TIME(100000) */ SLEEP(3) timeout_ms=800",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5


def test_mariadb_set_statement_cannot_raise_the_statement_timeout(request, server, salt):
    """D2: ``SET STATEMENT max_statement_time=0 FOR ...`` overrides the session variable for that statement."""
    if server.key != "mariadb":
        pytest.skip("MariaDB only")
    defect(
        request,
        server,
        "D2",
        "SET STATEMENT max_statement_time=0 FOR ... overrides the module's session timeout",
    )
    outcome, seconds = timed(
        salt, "ro", "SET STATEMENT max_statement_time=0 FOR SELECT SLEEP(3)", timeout_ms=800
    )
    observe(
        "timeout",
        "mariadb SET STATEMENT max_statement_time=0 FOR SELECT SLEEP(3) timeout_ms=800",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5


def test_benchmark_is_interrupted(server, salt):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, seconds = timed(salt, "ro", "SELECT BENCHMARK(300000000, MD5('a'))", timeout_ms=800)
    observe(
        "timeout",
        f"{server.key} BENCHMARK(3e8, MD5) timeout_ms=800",
        f"{seconds:.1f}s {harness.describe(outcome)}",
    )
    assert seconds < 2.5


def test_documented_mysql_dml_is_not_limited_by_max_execution_time(server, salt, tmp_path):
    """SPEC section 7: MAX_EXECUTION_TIME covers SELECT only. MariaDB cancels the same INSERT ... SELECT."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    table = harness.unique("scratch.t11_dml")
    server.admin(f"CREATE TABLE {table} (a integer, b integer)")
    try:
        with harness.injected({"rw": server.profile("rw", read_only=False)}) as module:
            start = time.time()
            outcome = harness.attempt(
                lambda: module.query(
                    "rw", f"INSERT INTO {table} SELECT 1, SLEEP(2)", timeout_ms=800
                )
            )
            seconds = time.time() - start
        observe(
            "timeout",
            f"{server.key} INSERT ... SELECT SLEEP(2) timeout_ms=800",
            f"{seconds:.1f}s {harness.describe(outcome)}",
        )
        if server.key == "mysql":
            assert seconds >= 1.8 and outcome[0] == "OK"
        else:
            assert seconds < 2.5 and outcome[0] == "ERR" and "[E_TIMEOUT]" in str(outcome[1])
    finally:
        server.admin(f"DROP TABLE IF EXISTS {table}")


# ---- result size ----------------------------------------------------------------------------------------------------------
def test_ten_million_rows_do_not_cost_ten_million_rows_of_memory(request, server, baseline_mb):
    """D3: the driver buffers the whole result before ``fetchmany(limit + 1)``; the byte and row caps apply after the fact."""
    defect(
        request,
        server,
        "D3",
        "result set is fully buffered in the minion before the row/byte caps are applied",
    )
    if server.key == "postgresql":
        sql = "SELECT g FROM generate_series(1, 10000000) g"
    elif server.key == "mariadb":
        sql = "SELECT seq FROM seq_1_to_8000000"
    else:
        pytest.skip(
            "MySQL has no row generator without a table; the MariaDB case shows the same driver behaviour"
        )
        return
    result = child(server.key, "ro", "query", {"sql": sql, "timeout_ms": 60000})
    observe("size", f"{server.key} {sql}", result)
    assert result["kind"] == "OK" and result["rows"] == 500 and result["truncated"] is True
    assert (
        result["peak_rss_mb"] < baseline_mb + 150
    ), f"peak {result['peak_rss_mb']} MB vs baseline {baseline_mb} MB"


def test_a_huge_single_value_is_not_expanded_in_memory_before_the_byte_cap(
    request, server, baseline_mb
):
    """D3: a 100 MB value is read in full, then JSON-encoded to measure it, before ``max_bytes`` drops the row."""
    if server.key != "postgresql":
        pytest.skip(
            "PostgreSQL only (values up to 1 GB; MySQL/MariaDB are capped by max_allowed_packet)"
        )
    defect(
        request,
        server,
        "D3",
        "a single 100 MB value costs several times its size in memory before being dropped",
    )
    result = child(server.key, "ro", "query", {"sql": "SELECT repeat('x', 100000000)"})
    observe("size", "postgresql SELECT repeat('x', 100000000)", result)
    assert result["kind"] == "OK" and result["rows"] == 0 and result["truncated"] is True
    assert (
        result["peak_rss_mb"] < baseline_mb + 250
    ), f"peak {result['peak_rss_mb']} MB vs baseline {baseline_mb} MB"


def test_many_wide_rows_respect_the_byte_cap(server, salt):
    sql = (
        "SELECT repeat('x', 2000) AS v FROM generate_series(1, 100000)"
        if server.key == "postgresql"
        else "SELECT REPEAT('x', 2000) AS v FROM "
        + ", ".join(f"scratch.orders t{i}" for i in range(11))
    )
    out = salt.query("ro", sql)
    assert out["truncated"] is True and out["row_count"] <= 500
    assert len(json.dumps(out)) < 1024 * 1024 + 2048


def test_max_bytes_and_limit_and_timeout_can_only_be_lowered(server, salt):
    out = salt.query("ro", "SELECT 1", limit=10**30, max_bytes=10**30, timeout_ms=10**30)
    assert out["rows"] == [[1]]
    out = salt.query("ro", "SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3", limit=2)
    assert out["row_count"] == 2 and out["truncated"] is True


# ---- CPU and connection floods in the module itself ---------------------------------------------------------------------------
def test_table_list_pattern_cannot_pin_the_cpu(request, server):
    """D4: the LIKE pattern is compiled to a regular expression with ambiguous ``.*`` runs (catastrophic backtracking)."""
    if server.key != "postgresql":
        pytest.skip("one dialect is enough: the pattern code is shared")
    defect(
        request,
        server,
        "D4",
        "table_list pattern '%a%a%a...b' backtracks without bound against a 50-character table name",
    )
    name = "t11_" + "a" * 50
    server.admin(f'DROP TABLE IF EXISTS scratch."{name}"')
    server.admin(f'CREATE TABLE scratch."{name}" (a integer)')
    server.admin(f'GRANT SELECT ON scratch."{name}" TO ro')
    try:
        start = time.time()
        try:
            result = child(server.key, "ro", "table_list", {"pattern": "%a" * 12 + "b"}, timeout=6)
            seconds = result["seconds"]
        except subprocess.TimeoutExpired:
            seconds = time.time() - start
            result = {"kind": "HUNG"}
        observe(
            "cpu",
            "postgresql table_list(pattern='%a'*12+'b') against a 54-character name",
            f"{seconds:.1f}s {result['kind']}",
        )
        assert seconds < 3
    finally:
        server.admin(f'DROP TABLE IF EXISTS scratch."{name}"')


def test_retry_loop_has_no_floor_on_the_pause(request, server, salt):
    """D10: ``retry_pause_s=1e-9`` is valid; a caller turns a 2 s retry window into a connection flood."""
    defect(
        request,
        server,
        "D10",
        "check_row_count opens a new connection every few milliseconds when retry_pause_s is tiny",
    )
    out = salt.check_row_count(
        "ro", "SELECT 1 WHERE 1 = 0", ">=", expected=1, retry_timeout_s=1, retry_pause_s=1e-9
    )
    observe(
        "flood",
        f"{server.key} check_row_count retry_timeout_s=1 retry_pause_s=1e-9",
        f"attempts={out['attempts']}",
    )
    assert out["attempts"] <= 20


def test_retry_window_is_capped_at_ten_seconds(salt):
    start = time.time()
    out = salt.check_row_count(
        "ro", "SELECT 1 WHERE 1 = 0", ">=", expected=1, retry_timeout_s=10**9, retry_pause_s=0.5
    )
    assert 9 <= time.time() - start <= 14 and out["attempts"] >= 2
