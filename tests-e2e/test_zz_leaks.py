"""Runs last: no password or resolved sdb value anywhere Salt keeps data (SPEC 3.2, SECURITY section 5).

Searched: the master job cache, the master and minion logs (debug level in the log files), the event bus as captured
during the run by ``salt-run state.event``, and every output this suite received (CLI, salt-call, HTTP).
"""

import pytest
from conftest import SECRETS
from conftest import TRANSCRIPT
from conftest import compose

# Values that must never be stored. The literal-password test deliberately sends a value that is not one of these.
NEVER = sorted(set(SECRETS.values()))


def _grep(service, paths):
    found = {}
    for secret in NEVER:
        proc = compose(
            "exec",
            "-T",
            service,
            "sh",
            "-c",
            f"grep -rIl -F -- '{secret}' {paths} 2>/dev/null || true",
            timeout=120,
        )
        if proc.stdout.strip():
            found[secret[:4] + "..."] = proc.stdout.split()
    return found


def test_no_secret_in_job_cache_or_master_log(salt):  # pylint: disable=unused-argument
    assert _grep("master", "/var/cache/salt/master /var/log/salt") == {}


@pytest.mark.parametrize("minion", ["minion", "minion-policy"])
def test_no_secret_in_minion_cache_or_log(salt, minion):  # pylint: disable=unused-argument
    assert _grep(minion, "/var/cache/salt/minion /var/log/salt") == {}


def test_no_secret_on_the_event_bus(event_log):
    text = event_log.read_text(errors="replace")
    assert "sqlx." in text, "event capture saw no sqlx job"
    assert [s[:4] for s in NEVER if s in text] == []


def test_no_secret_in_any_return(salt):  # pylint: disable=unused-argument
    joined = "\n".join(TRANSCRIPT)
    assert len(TRANSCRIPT) > 100
    assert [s[:4] for s in NEVER if s in joined] == []
