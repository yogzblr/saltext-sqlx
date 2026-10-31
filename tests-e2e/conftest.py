"""
End-to-end suite: drives the extension the way an operator does, through a real Salt master, two minions and salt-api
in Docker (tests-e2e/docker-compose.yml, started by tests-e2e/up.sh). Nothing here imports the extension.

Three call paths, parsed from their real output:

* ``cli``  - ``salt <minion> sqlx.<fn> k=v ... --out=json --static`` on the master (a remote minion job);
* ``call`` - ``salt-call --out=json sqlx.<fn> ...`` on the minion;
* ``api``  - HTTP POST to salt-api ``/run`` on http://127.0.0.1:8000 with JSON ``kwarg`` and ``file`` eauth.

Every raw output is kept so that ``test_zz_leaks.py`` can grep all of them for the throw-away passwords.
"""

import json
import os
import pathlib
import re
import subprocess
import time
import urllib.error
import urllib.request

import pytest

HERE = pathlib.Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-f", str(HERE / "docker-compose.yml")]
API = os.environ.get("SQLX_E2E_API", "http://127.0.0.1:8000")
DBS = ("pg", "mysql", "mariadb", "oracle", "mssql")
PATHS = ("cli", "call", "api")

SECRETS = {
    k: v.strip()
    for k, v in (
        line.split(":", 1)
        for line in (HERE / "salt" / "e2e-secrets.yaml").read_text().splitlines()
        if line and not line.startswith("#")
    )
}
EAUTH = dict(
    line.split(":", 1)
    for line in (HERE / "salt" / "e2e-eauth.txt").read_text().splitlines()
    if line
)

CODE_RE = re.compile(r"\[(E_[A-Z_]+)\]")

#: every raw text the suite received (stdout, stderr, HTTP bodies), for the leak scan
TRANSCRIPT = []


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end through Docker Salt (tests-e2e/up.sh)")
    os.environ.setdefault("SQLX_E2E_DATA", str(pathlib.Path.home() / "e2e-data"))


def pytest_collection_modifyitems(items):
    for item in items:
        if HERE in pathlib.Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)


class Result:
    """What one call returned: ``data`` on success, ``error`` (the text holding ``[E_...]``) on failure."""

    def __init__(self, path, data=None, error=None, raw="", retcode=None, elapsed=0.0):
        self.path, self.data, self.error, self.raw, self.retcode, self.elapsed = (
            path,
            data,
            error,
            raw,
            retcode,
            elapsed,
        )

    @property
    def ok(self):
        return self.error is None

    @property
    def code(self):
        found = CODE_RE.search(self.error or "")
        return found.group(1) if found else None

    def __repr__(self):
        return f"Result({self.path}, ok={self.ok}, code={self.code}, data={self.data!r:.300}, error={self.error!r:.500})"


def _cli_value(value):
    """One CLI value: JSON is valid YAML, so strings become double-quoted YAML scalars and survive Salt's parsing."""
    return json.dumps(value, ensure_ascii=False)


def compose(*args, timeout=180, check=False, input_text=None):
    proc = subprocess.run(
        COMPOSE + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input_text,
    )
    TRANSCRIPT.append(proc.stdout + proc.stderr)
    if check and proc.returncode:
        raise AssertionError(f"{args}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


class Salt:
    def __init__(self):
        self.calls = 0

    # -- remote job from the master ---------------------------------------------------------------------------
    def cli(self, fn, *args, minion="minion", timeout=120, **kwargs):
        argv = [
            "exec",
            "-T",
            "master",
            "salt",
            "--out=json",
            "--static",
            "-t",
            str(timeout),
            minion,
            fn,
        ]
        argv += [_cli_value(a) for a in args]
        argv += [f"{k}={_cli_value(v)}" for k, v in kwargs.items() if v is not None]
        start = time.monotonic()
        proc = compose(*argv, timeout=timeout + 60)
        elapsed = time.monotonic() - start
        self.calls += 1
        try:
            ret = json.loads(proc.stdout)[minion]
        except (ValueError, KeyError, TypeError):
            return Result(
                "cli",
                error=f"unparsable: {proc.stdout}{proc.stderr}",
                raw=proc.stdout + proc.stderr,
            )
        if isinstance(ret, str) and ret.startswith("ERROR"):
            return Result(
                "cli", error=ret, raw=proc.stdout, retcode=proc.returncode, elapsed=elapsed
            )
        return Result("cli", data=ret, raw=proc.stdout, retcode=proc.returncode, elapsed=elapsed)

    # -- salt-call on the minion ------------------------------------------------------------------------------
    def call(self, fn, *args, minion="minion", timeout=120, **kwargs):
        argv = ["exec", "-T", minion, "salt-call", "--out=json", fn]
        argv += [_cli_value(a) for a in args]
        argv += [f"{k}={_cli_value(v)}" for k, v in kwargs.items() if v is not None]
        start = time.monotonic()
        proc = compose(*argv, timeout=timeout + 60)
        elapsed = time.monotonic() - start
        self.calls += 1
        if proc.returncode == 0:
            return Result(
                "call",
                data=json.loads(proc.stdout)["local"],
                raw=proc.stdout,
                retcode=0,
                elapsed=elapsed,
            )
        return Result(
            "call",
            error=(proc.stderr + proc.stdout).strip(),
            raw=proc.stderr + proc.stdout,
            retcode=proc.returncode,
            elapsed=elapsed,
        )

    # -- salt-api -----------------------------------------------------------------------------------------------
    def api_raw(self, low, timeout=120):
        body = json.dumps([low]).encode()
        req = urllib.request.Request(
            f"{API}/run",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
                status = resp.status
        except urllib.error.HTTPError as err:
            text = err.read().decode(errors="replace")
            status = err.code
        TRANSCRIPT.append(text)
        return status, text

    def api(self, fn, *args, minion="minion", user="e2e_full", timeout=120, **kwargs):
        low = {
            "client": "local",
            "tgt": minion,
            "fun": fn,
            "arg": list(args),
            "kwarg": {k: v for k, v in kwargs.items() if v is not None},
            "eauth": "file",
            "username": user,
            "password": EAUTH[user],
            "full_return": True,
            "timeout": timeout,
        }
        start = time.monotonic()
        status, text = self.api_raw(low, timeout=timeout + 60)
        elapsed = time.monotonic() - start
        self.calls += 1
        if status != 200:
            return Result("api", error=f"HTTP {status}", raw=text, retcode=status, elapsed=elapsed)
        full = json.loads(text)["return"][0].get(minion)
        if full is None:
            return Result(
                "api", error=f"no return from {minion}: {text}", raw=text, elapsed=elapsed
            )
        ret, retcode = full["ret"], full.get("retcode")
        if isinstance(ret, str) and ret.startswith("ERROR"):
            return Result("api", error=ret, raw=text, retcode=retcode, elapsed=elapsed)
        return Result("api", data=ret, raw=text, retcode=retcode, elapsed=elapsed)

    def run(self, path, fn, *args, **kwargs):
        return getattr(self, path)(fn, *args, **kwargs)


@pytest.fixture(scope="session")
def salt():
    proc = compose("exec", "-T", "master", "salt", "--out=txt", "-t", "20", "minion*", "test.ping")
    if proc.stdout.count("True") < 2:
        pytest.fail(f"the e2e stack is not up (run tests-e2e/up.sh): {proc.stdout}{proc.stderr}")
    return Salt()


@pytest.fixture(scope="session", autouse=True)
def event_log(tmp_path_factory):
    """Capture the master event bus for the whole run (``salt-run state.event``); read by ``test_zz_leaks.py``."""
    path = tmp_path_factory.mktemp("events") / "events.txt"
    with path.open("w") as out:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            COMPOSE + ["exec", "-T", "master", "salt-run", "state.event", "pretty=False"],
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)
        yield path
        proc.terminate()
        proc.wait(timeout=30)


def assert_error(res, code, *fragments):
    """``res`` failed with ``[code]`` and the real database text contains every fragment."""
    assert not res.ok, f"expected {code}, got success: {res!r}"
    assert res.code == code, f"expected {code}: {res!r}"
    for fragment in fragments:
        assert fragment in res.error, f"{fragment!r} not in {res.error!r}"


def assert_ok(res):
    assert res.ok, f"expected success: {res!r}"
    return res.data
