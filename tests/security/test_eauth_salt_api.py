# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, last-but-one box: the ``external_auth`` regex example of SECURITY section 4, through ``salt-api``.

Starts a throwaway master, minion and ``salt-api`` (CherryPy, HTTP, loopback, ``auto`` eauth which accepts any password,
so only the authorisation rules are under test) with the exact rule shapes from SECURITY section 4, and posts to
``/run``. The minion's profiles point at the live PostgreSQL server (``ro``, ``rw``, ``other``). Skipped when
``salt-api``, CherryPy or the server are missing. Ports 54505/54506 (Salt) and 58000 (API) must be free.
"""

import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import time

import pytest

from tests.security import harness

requests = pytest.importorskip("requests")
pytest.importorskip("cherrypy")

BIN = pathlib.Path(os.sys.executable).parent
PORTS = (54505, 54506, 58000)
MINION = "t11-minion"
URL = "http://127.0.0.1:58000/run"


def port_free(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="module")
def api():
    server = harness.Server("postgresql")
    reason = server.reachable()
    if reason:
        pytest.skip(reason)
    if not all((BIN / name).exists() for name in ("salt-master", "salt-minion", "salt-api")):
        pytest.skip("salt-master / salt-minion / salt-api not installed")
    if not all(port_free(p) for p in PORTS):
        pytest.skip(f"one of the ports {PORTS} is in use")
    root = pathlib.Path(
        tempfile.mkdtemp(prefix="t11s", dir="/tmp")
    )  # short path: AF_UNIX sockets are limited to ~107 bytes
    (root / "m").mkdir()
    (root / "n").mkdir()
    prof = lambda user, pw, **extra: dict(  # noqa: E731
        dialect="postgresql",
        host=harness.HOST,
        port=server.cfg["port"],
        database=server.cfg["database"],
        username=user,
        password=pw,
        **extra,
    )
    (root / "m" / "master").write_text(textwrap.dedent(f"""
        user: root
        root_dir: /
        pki_dir: {root}/m/pki
        cachedir: {root}/m/cache
        sock_dir: {root}/m/sock
        log_file: {root}/m/master.log
        pidfile: {root}/m/master.pid
        publish_port: 54505
        ret_port: 54506
        interface: 127.0.0.1
        auto_accept: True
        log_level: warning
        rest_cherrypy: {{port: 58000, host: 127.0.0.1, disable_ssl: True}}
        netapi_enable_clients: [local]
        external_auth:
          auto:
            sre_ops:
              - '{MINION}':
                  - sqlx.query:
                      kwargs: {{profile: '^(ro)$'}}
                  - sqlx.ping
            dba_writers:
              - '{MINION}':
                  - sqlx.execute:
                      kwargs: {{profile: '^rw$'}}
    """))
    (root / "n" / "minion").write_text(textwrap.dedent(f"""
        user: root
        id: {MINION}
        master: 127.0.0.1
        master_port: 54506
        publish_port: 54505
        root_dir: /
        pki_dir: {root}/n/pki
        cachedir: {root}/n/cache
        sock_dir: {root}/n/sock
        log_file: {root}/n/minion.log
        pidfile: {root}/n/minion.pid
        log_level: warning
        sqlx:
          profiles:
            ro: {json.dumps(prof("ro", "ro_pw"))}
            rw: {json.dumps(prof("rw", "rw_pw", allow_execute=True, read_only=False))}
            other: {json.dumps(prof("ro", "ro_pw"))}
    """))
    procs = []

    def start(binary, conf):
        procs.append(
            subprocess.Popen(
                [str(BIN / binary), "-c", str(root / conf)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )

    try:
        start("salt-master", "m")
        time.sleep(5)
        start("salt-minion", "n")
        start("salt-api", "m")
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                ok = requests.post(
                    URL,
                    json=[
                        {
                            "client": "local",
                            "tgt": MINION,
                            "fun": "sqlx.ping",
                            "eauth": "auto",
                            "username": "sre_ops",
                            "password": "x",
                            "kwarg": {"profile": "ro"},
                        }
                    ],
                    timeout=10,
                )
                if ok.status_code == 200 and MINION in ok.text:
                    break
            except requests.RequestException:
                pass
            time.sleep(2)
        else:
            pytest.skip("throwaway Salt master/minion/api did not come up in 60 s")
        yield
    finally:
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(root, ignore_errors=True)


def call(user, fun, arg=None, kwarg=None, tgt=MINION):
    low = {
        "client": "local",
        "tgt": tgt,
        "fun": fun,
        "eauth": "auto",
        "username": user,
        "password": "x",
    }
    if arg is not None:
        low["arg"] = arg
    if kwarg is not None:
        low["kwarg"] = kwarg
    resp = requests.post(URL, json=[low], headers={"Accept": "application/json"}, timeout=60)
    body = (
        resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else None
    )
    return resp.status_code, body


Q = "SELECT current_user AS u"
ALLOWED = [
    ("sre_ops", "sqlx.ping", None, {"profile": "ro"}, lambda r: r["ok"] is True),
    ("sre_ops", "sqlx.query", None, {"profile": "ro", "sql": Q}, lambda r: r["rows"] == [["ro"]]),
    (
        "dba_writers",
        "sqlx.execute",
        None,
        {"profile": "rw", "sql": "UPDATE scratch.t11_nothing SET a = 1", "dry_run": True},
        lambda r: isinstance(r, str) and "[E_SQL]" in r,
    ),  # authorised and executed: the database says the table is missing
]
DENIED = [
    ("sre_ops", "sqlx.query", None, {"profile": "other", "sql": Q}),
    ("sre_ops", "sqlx.query", None, {"profile": "rw", "sql": Q}),
    ("sre_ops", "sqlx.query", ["rw", Q], None),
    ("sre_ops", "sqlx.query", ["ro", Q], None),
    ("sre_ops", "sqlx.query", None, {"sql": Q}),
    ("sre_ops", "sqlx.execute", None, {"profile": "rw", "sql": "SELECT 1", "dry_run": True}),
    ("sre_ops", "sqlx.table_list", None, {"profile": "ro"}),
    (
        "sre_ops",
        "sqlx.check_row_count",
        None,
        {"profile": "ro", "sql": "SELECT 1", "operator": "==", "expected": 1},
    ),
    ("sre_ops", "cmd.run", ["id"], None),
    ("dba_writers", "sqlx.execute", None, {"profile": "other", "sql": "SELECT 1", "dry_run": True}),
    ("dba_writers", "sqlx.execute", ["other", "SELECT 1"], None),
    ("dba_writers", "sqlx.query", None, {"profile": "ro", "sql": Q}),
    ("mallory", "sqlx.ping", None, {"profile": "ro"}),
]


@pytest.mark.parametrize(
    "user,fun,arg,kwarg,check", ALLOWED, ids=[f"{a[0]}-{a[1]}" for a in ALLOWED]
)
def test_listed_calls_are_authorised(api, user, fun, arg, kwarg, check):
    status, body = call(user, fun, arg, kwarg)
    harness.observe(
        "eauth", f"{user} {fun} {kwarg or arg}", f"HTTP {status} {json.dumps(body)[:120]}"
    )
    assert status == 200 and check(body["return"][0][MINION])


@pytest.mark.parametrize(
    "user,fun,arg,kwarg", DENIED, ids=[f"{d[0]}-{d[1]}-{i}" for i, d in enumerate(DENIED)]
)
def test_everything_else_is_refused_with_401(api, user, fun, arg, kwarg):
    status, _body = call(user, fun, arg, kwarg)
    harness.observe("eauth", f"{user} {fun} {kwarg or arg}", f"HTTP {status}")
    assert status == 401


def test_documented_unrestricted_ping_rule_reaches_any_profile(api):
    """SECURITY section 4 lists ``- sqlx.ping`` without ``kwargs``: it pings ``other`` as well. Add a profile regex to restrict."""
    status, body = call("sre_ops", "sqlx.ping", None, {"profile": "other"})
    assert status == 200 and body["return"][0][MINION]["profile"] == "other"


def test_positional_profile_next_to_a_matching_kwarg_is_a_python_error_not_a_bypass(api):
    status, body = call("sre_ops", "sqlx.query", ["rw"], {"profile": "ro", "sql": Q})
    assert status == 200 and "multiple values for argument 'profile'" in body["return"][0][MINION]


def test_trailing_newline_passes_the_regex_but_not_the_module(api):
    """Python's ``$`` matches before a final newline; the module then refuses the unknown profile name."""
    status, body = call("sre_ops", "sqlx.query", None, {"profile": "ro\n", "sql": Q})
    assert status == 200 and "[E_PROFILE_UNKNOWN]" in body["return"][0][MINION]
