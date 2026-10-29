"""
The SPEC.md section 11 request against a real ``salt-api`` (rest_cherrypy) in front of the test master.

The ``curl`` command is the one in SPEC section 11 with three changes forced by the test setup: plain HTTP on a free port
(``disable_ssl``, so no ``-k``), the throwaway ``file`` eauth user instead of ``pam``, and the live minion id and profile.
"""

import json
import shutil
import subprocess

import pytest

from tests.integration.salt_path.conftest import EAUTH_PASSWORD
from tests.integration.salt_path.conftest import EAUTH_USER

pytestmark = [pytest.mark.pg]


@pytest.fixture(scope="module")
def api(master, minion, api_port):  # pylint: disable=unused-argument
    daemon = master.salt_api_daemon()
    with daemon.started():
        daemon.test_port = api_port
        yield daemon


def _post(api, minion, kwarg, username=EAUTH_USER):
    body = [
        {
            "client": "local",
            "tgt": minion.id,
            "fun": "sqlx.query",
            "kwarg": kwarg,
            "eauth": "file",
            "username": username,
            "password": EAUTH_PASSWORD,
        }
    ]
    command = [
        "curl",
        "-sS",
        f"http://127.0.0.1:{api.test_port}/run",
        "-H",
        "Content-Type: application/json",
        "-d",
        json.dumps(body),
    ]
    done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)
    return done, command


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_spec_section_11_request_returns_typed_rows(api, minion):
    done, command = _post(
        api,
        minion,
        {
            "profile": "pg_ro",
            "sql": "select id,note from t10_orders where id >= CAST(:s AS INTEGER) order by id",
            "params": {"s": 2},
            "limit": 1,
        },
    )
    assert done.returncode == 0, done.stderr
    print("CURL:", " ".join(command[:3] + ["-H", "'Content-Type: application/json'", "-d", repr(command[-1])]))
    print("JSON:", done.stdout)
    result = json.loads(done.stdout)["return"][0][minion.id]
    assert result["profile"] == "pg_ro"
    assert result["rows"] == [[2, "two"]]
    assert result["truncated"] is True
    assert result["row_count"] == 1


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_other_profile_through_the_api_is_refused(api, minion):
    """The eauth regex applies through salt-api too: no job runs and no rows come back."""
    done, _ = _post(api, minion, {"profile": "mysql_ro", "sql": "select 1"})
    assert done.returncode == 0, done.stderr
    assert "<html" in done.stdout.lower()  # cherrypy's HTML error page, not JSON
    assert "rows" not in done.stdout
    assert minion.id not in done.stdout
