"""
What Salt itself stores or logs for a call (SECURITY.md sections 3 and 5): the job cache, the event bus and the logs.

Salt stores the job LOAD (function, args, kwargs) on the master, so the SQL text and the bound ``params`` of every call
ARE in the master job cache and, at debug level, in the master and minion logs. That is Salt behaviour, not something the
extension can prevent. The account password lives only in the minion config and must appear nowhere.
"""

import pathlib
import threading

import pytest
import salt.utils.event

from tests.integration.salt_path.conftest import BOUND_SECRET
from tests.integration.salt_path.conftest import RO_PASSWORD
from tests.integration.salt_path.conftest import SQL_MARKER

pytestmark = [pytest.mark.pg]

SQL = f"select id from t10_orders where note = :s or 'a' = '{SQL_MARKER}'"


def _files_containing(root, needle):
    return sorted(
        str(path.relative_to(root))
        for path in pathlib.Path(root).rglob("*")
        if path.is_file() and needle.encode() in path.read_bytes()
    )


@pytest.fixture(scope="module")
def job(call):
    """One successful call and one failing call, each with the marker SQL and the bound secret."""
    ok = call("sqlx.query", profile="pg_ro", sql=SQL, params={"s": BOUND_SECRET})
    bad = call(
        "sqlx.query",
        profile="mysql_ro",
        sql=f"select id from t10_nope where note = :s or 'a' = '{SQL_MARKER}'",
        params={"s": BOUND_SECRET},
    )
    return {"ok": ok, "bad": bad}


def test_password_is_not_in_the_job_cache_or_logs(
    job, master, minion
):  # pylint: disable=unused-argument
    cache = master.config["cachedir"]
    assert _files_containing(cache, RO_PASSWORD) == []
    assert RO_PASSWORD not in pathlib.Path(master.config["log_file"]).read_text(encoding="utf-8")
    assert RO_PASSWORD not in pathlib.Path(minion.config["log_file"]).read_text(encoding="utf-8")


def test_sql_text_and_params_are_stored_by_salt(job, master):  # pylint: disable=unused-argument
    """Documented, not prevented: the job load on the master holds the SQL text and the bound value."""
    cache = master.config["cachedir"]
    assert any(f.endswith(".load.p") for f in _files_containing(cache, SQL_MARKER))
    assert any(f.endswith(".load.p") for f in _files_containing(cache, BOUND_SECRET))


def test_job_return_does_not_carry_the_sql_or_the_secret(job, master, salt_run_cli):
    """``jobs.lookup_jid`` shows the stored return: rows and metadata only, for both the ok and the failed call."""
    for kind in ("ok", "bad"):
        ret = salt_run_cli.run("jobs.lookup_jid", job[kind][2], "--out=json")
        assert ret.returncode == 0
        assert SQL_MARKER not in ret.stdout
        assert BOUND_SECRET not in ret.stdout
        assert RO_PASSWORD not in ret.stdout
    failed = salt_run_cli.run("jobs.lookup_jid", job["bad"][2], "--out=json").stdout
    assert "[E_SQL] mysql 1146" in failed
    for path in pathlib.Path(master.config["cachedir"]).rglob("return.p"):
        assert SQL_MARKER.encode() not in path.read_bytes()
        assert RO_PASSWORD.encode() not in path.read_bytes()


def test_minion_audit_line_has_the_hash_and_not_the_sql(
    job, minion
):  # pylint: disable=unused-argument
    text = pathlib.Path(minion.config["log_file"]).read_text(encoding="utf-8")
    audit = [line for line in text.splitlines() if "sqlx audit" in line]
    assert audit, "no audit line in the minion log"
    for line in audit:
        assert SQL_MARKER not in line
        assert BOUND_SECRET not in line
        assert RO_PASSWORD not in line
    assert any("profile=pg_ro" in line and "outcome=OK" in line for line in audit)
    assert any("profile=mysql_ro" in line and "outcome=E_SQL" in line for line in audit)


def test_event_bus_carries_the_job_args(master, minion, client):
    """The ``salt/job/<jid>/new`` event holds the args and kwargs of the call: the SQL and the bound value are on the bus."""
    seen = []
    ready = threading.Event()

    def listen():
        with salt.utils.event.get_event(
            "master", sock_dir=master.config["sock_dir"], opts=master.config, listen=True
        ) as bus:
            ready.set()
            while True:
                event = bus.get_event(wait=15, tag="salt/job/", full=True, match_type="startswith")
                if event is None:
                    return
                seen.append(event)
                if event["tag"].endswith("/new") and SQL_MARKER in str(event["data"]):
                    return

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    assert ready.wait(10)
    client.cmd(
        minion.id,
        "sqlx.query",
        kwarg={"profile": "pg_ro", "sql": SQL, "params": {"s": BOUND_SECRET}},
        timeout=60,
    )
    thread.join(30)
    new = [e for e in seen if e["tag"].endswith("/new")]
    assert new, f"no job event seen: {[e['tag'] for e in seen]}"
    text = str(new[-1]["data"])
    assert SQL_MARKER in text
    assert BOUND_SECRET in text
    assert RO_PASSWORD not in "".join(str(e["data"]) for e in seen)
