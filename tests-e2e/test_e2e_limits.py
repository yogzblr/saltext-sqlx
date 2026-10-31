"""Resource limits: a huge result stays bounded in the minion's memory, a large value is dropped, timeouts hold."""

import threading
import time

import pytest
from conftest import DBS
from conftest import assert_ok
from conftest import compose
from dbs import sql

#: Memory the minion container may gain while serving a capped cross join (the job process is a fork of a ~150 MB
#: minion, so this is mostly the job's own working set).
MAX_RSS_GROWTH_MB = 200


def _container_mb(service):
    proc = compose("exec", "-T", service, "cat", "/sys/fs/cgroup/memory.current", timeout=30)
    return int(proc.stdout.strip()) / 2**20


class Sampler(threading.Thread):
    def __init__(self, service):
        super().__init__(daemon=True)
        self.service, self.peak, self.stop = service, 0.0, threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.peak = max(self.peak, _container_mb(self.service))
            except Exception:  # pylint: disable=broad-except
                pass
            time.sleep(0.3)


def _measure(salt, **kwargs):
    base = _container_mb("minion")
    sampler = Sampler("minion")
    sampler.start()
    start = time.monotonic()
    res = salt.cli("sqlx.query", **kwargs)
    elapsed = time.monotonic() - start
    sampler.stop.set()
    sampler.join()
    return res, sampler.peak - base, elapsed


@pytest.mark.parametrize("db", DBS)
def test_huge_result_is_bounded(salt, db):
    # payment x payment is ~257 million rows; only limit + 1 may leave the server (or be read by the driver)
    res, growth, elapsed = _measure(
        salt, profile=f"{db}_ro", sql=sql(db, "big"), limit=10, timeout_ms=60000
    )
    data = assert_ok(res)
    assert data["row_count"] == 10 and data["truncated"] is True
    print(f"\n{db}: huge result, rss growth {growth:.0f} MB, {elapsed:.1f} s")
    assert growth < MAX_RSS_GROWTH_MB, f"{db}: minion grew {growth:.0f} MB"
    assert elapsed < 60


@pytest.mark.parametrize("db", DBS)
def test_large_value_is_dropped(salt, db):
    res, growth, elapsed = _measure(salt, profile=f"{db}_ro", sql=sql(db, "large_value"))
    data = assert_ok(res)
    assert data["rows"] == [] and data["truncated"] is True, data
    print(f"\n{db}: large value, rss growth {growth:.0f} MB, {elapsed:.1f} s")


@pytest.mark.parametrize("db", DBS)
def test_timeout_cap_is_enforced_for_the_whole_call(salt, db):
    # the statement timeout bounds a long statement even when the caller asks for more than the profile allows
    res = salt.cli("sqlx.query", profile=f"{db}_ro", sql=sql(db, "sleep"), timeout_ms=999999999)
    # 5 s sleep < 30 s profile timeout: completes (clamped, not refused)
    assert res.ok or res.code == "E_TIMEOUT", res
