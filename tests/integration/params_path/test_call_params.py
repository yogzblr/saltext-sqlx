"""
Call-time connection parameters through ``salt-call --local``, ``LocalClient`` (JSON-typed kwargs), ``salt-api`` and
the job cache / event bus, against a real master and minion (SPEC section 3.2, SECURITY section 4).

Real commands exercised here are quoted in README.md ("Call-time connection parameters").
"""

import json
import pathlib
import shutil
import socket
import subprocess
import threading
import time

import pytest
import salt.exceptions
import salt.utils.event

from tests.integration.params_path.conftest import EAUTH_PASSWORD
from tests.integration.params_path.conftest import EAUTH_USER
from tests.integration.params_path.conftest import OTHER_PASSWORD
from tests.integration.params_path.conftest import OTHER_USER
from tests.integration.params_path.conftest import PG_PORT
from tests.integration.params_path.conftest import RO_PASSWORD
from tests.integration.params_path.conftest import SDB_REF

pytestmark = [pytest.mark.pg]

TARGET = {
    "host": "127.0.0.1",
    "port": PG_PORT,
    "username": "ro",
    "database": "sqlx_test",
    "password": SDB_REF,
}


# -- salt-call --local ------------------------------------------------------------------------------------------------


def _cli(salt_call_cli, *args):
    ret = salt_call_cli.run("--local", "--out=json", "--log-level=error", *args)
    try:
        data = json.loads(ret.stdout)["local"] if ret.stdout.strip() else None
    except ValueError:
        data = ret.stdout
    return ret, data


def test_salt_call_flat_keywords_with_typed_port(salt_call_cli):
    ret, data = _cli(
        salt_call_cli,
        "sqlx.ping",
        "host=127.0.0.1",
        f"port={PG_PORT}",
        "username=ro",
        "database=sqlx_test",
        f"password={SDB_REF}",
    )
    assert ret.returncode == 0, (ret.stdout, ret.stderr)
    assert data["ok"] is True and data["dialect"] == "postgresql"


def test_salt_call_nested_connection_dict_is_yaml_parsed(salt_call_cli):
    conn = json.dumps({**TARGET})
    ret, data = _cli(salt_call_cli, "sqlx.ping", f"connection={conn}")
    assert ret.returncode == 0, (ret.stdout, ret.stderr)
    assert data["ok"] is True


def test_salt_call_quoted_port_is_a_string_and_refused(salt_call_cli):
    """YAML typing pitfall: ``port="5432"`` (quoted) arrives as a string; the extension never coerces it."""
    ret, data = _cli(
        salt_call_cli,
        "sqlx.ping",
        "host=127.0.0.1",
        f"port='{PG_PORT}'",
        "username=ro",
        "database=sqlx_test",
        f"password={SDB_REF}",
    )
    assert ret.returncode != 0
    # salt-call prints the function's docstring for a SaltInvocationError and logs the message (log level error)
    assert "CLI Example" in json.dumps(data)
    assert "[E_PROFILE_INVALID] profile 'pg_dead': 'port' must be an integer" in ret.stderr


def test_salt_call_unlisted_host_is_a_policy_error(salt_call_cli):
    ret, data = _cli(salt_call_cli, "sqlx.ping", "host=192.0.2.9")
    assert ret.returncode != 0
    assert "[E_POLICY] 'host' is not in sqlx:_policy:allowed_hosts" in ret.stderr


def test_salt_call_psql_query_shape(salt_call_cli):
    ret, data = _cli(
        salt_call_cli,
        "sqlx.psql_query",
        "select 1 as one, null as nothing",
        "user=ro",
        "host=127.0.0.1",
        f"port={PG_PORT}",
        "maintenance_db=sqlx_test",
        f"password={SDB_REF}",
    )
    assert ret.returncode == 0, (ret.stdout, ret.stderr)
    assert data == [{"one": "1", "nothing": None}]


def test_salt_call_psql_query_runas_is_refused(salt_call_cli):
    ret, data = _cli(salt_call_cli, "sqlx.psql_query", "select 1", "runas=postgres")
    assert ret.returncode != 0
    assert "[E_OPTION_INVALID] runas is not supported" in ret.stderr


# -- LocalClient with JSON-typed kwargs -------------------------------------------------------------------------------


def test_localclient_json_kwargs_flat_and_connection(call):
    ret, retcode, _ = call("sqlx.query", sql="select current_user", **TARGET)
    assert retcode == 0, ret
    assert ret["rows"] == [["ro"]]
    ret, retcode, _ = call("sqlx.query", sql="select current_user", connection=dict(TARGET))
    assert retcode == 0, ret
    assert ret["rows"] == [["ro"]]


def test_localclient_psql_query(call):
    ret, retcode, _ = call(
        "sqlx.psql_query",
        query="select current_user as who",
        user="ro",
        host="127.0.0.1",
        port=PG_PORT,
        maintenance_db="sqlx_test",
        password=SDB_REF,
    )
    assert retcode == 0, ret
    assert ret == [{"who": "ro"}]


def test_localclient_string_port_is_refused(call):
    ret, retcode, _ = call("sqlx.ping", **dict(TARGET, port=str(PG_PORT)))
    assert retcode == 1 and "[E_" in ret


def test_flat_and_connection_with_the_same_key_is_refused(call):
    ret, retcode, _ = call("sqlx.ping", host="127.0.0.1", connection={"host": "127.0.0.1"})
    assert retcode == 1
    assert ret.startswith("ERROR executing 'sqlx.ping': [E_OPTION_INVALID]")


def test_hard_rules_through_the_real_minion(call):
    for key, value in (
        ("allow_execute", True),
        ("row_limit", 10**9),
        ("error_detail", True),
        ("_policy", {}),
    ):
        ret, retcode, _ = call("sqlx.ping", **dict(TARGET, **{key: value}))
        assert retcode == 1 and "[E_OPTION_UNKNOWN]" in ret, (key, ret)
    ret, retcode, _ = call("sqlx.ping", **dict(TARGET, read_only=False))
    assert retcode == 1 and "[E_POLICY]" in ret
    ret, retcode, _ = call("sqlx.ping", **dict(TARGET, password="a-literal-password"))
    assert retcode == 1 and "[E_POLICY]" in ret and "a-literal-password" not in ret


def test_unlisted_host_never_gets_a_connection(call):
    """The minion refuses 127.0.0.2 before any socket is opened: a listener the test owns sees nothing."""
    sock = socket.socket()
    sock.bind(("127.0.0.2", 0))
    sock.listen(5)
    sock.setblocking(False)
    try:
        ret, retcode, _ = call(
            "sqlx.ping", **dict(TARGET, host="127.0.0.2", port=sock.getsockname()[1])
        )
        assert retcode == 1 and "[E_POLICY]" in ret
        time.sleep(0.3)
        with pytest.raises(BlockingIOError):
            sock.accept()
    finally:
        sock.close()


# -- secrets in the job cache and on the event bus --------------------------------------------------------------------


def _files_containing(root, needle):
    return sorted(
        str(path.relative_to(root))
        for path in pathlib.Path(root).rglob("*")
        if path.is_file() and needle.encode() in path.read_bytes()
    )


def test_resolved_password_is_nowhere_and_the_reference_is_only_in_the_job_load(
    call, master, minion
):
    ret, retcode, jid = call("sqlx.ping", **TARGET)
    assert retcode == 0, ret
    bad, bad_code, bad_jid = call("sqlx.ping", **dict(TARGET, username="nobody"))
    assert bad_code == 1
    cache = master.config["cachedir"]
    assert _files_containing(cache, RO_PASSWORD) == []
    for log in (master.config["log_file"], minion.config["log_file"]):
        assert RO_PASSWORD not in pathlib.Path(log).read_text(encoding="utf-8")
    assert SDB_REF not in json.dumps(ret) and SDB_REF not in str(bad)
    # documented: Salt stores the job LOAD, which holds the reference the caller typed (never the resolved value)
    assert any(f.endswith(".load.p") for f in _files_containing(cache, SDB_REF))
    assert not any(f.endswith("return.p") for f in _files_containing(cache, SDB_REF))


def test_literal_password_is_refused_but_salt_still_stores_what_the_caller_typed(
    call, master, minion
):
    literal = "T12-LITERAL-PW-b3e91c"
    ret, retcode, _ = call("sqlx.ping", **dict(TARGET, password=literal))
    assert retcode == 1 and "[E_POLICY]" in ret and "'password'" in ret and literal not in ret
    cache = master.config["cachedir"]
    assert any(
        f.endswith(".load.p") for f in _files_containing(cache, literal)
    ), "documented leak: the job load holds the literal even when the minion refuses it"
    assert not any(f.endswith("return.p") for f in _files_containing(cache, literal))
    lines = pathlib.Path(minion.config["log_file"]).read_text(encoding="utf-8").splitlines()
    # at debug level Salt itself logs the job load ("Command details"), the extension's own lines never hold it
    assert any(literal in line and "sqlx audit" not in line for line in lines)
    ours = [line for line in lines if "sqlx audit" in line or "saltext.sqlx" in line]
    assert ours and not any(literal in line for line in ours)


def test_event_bus_never_carries_the_resolved_password(master, minion, client):
    seen = []
    ready = threading.Event()
    done = threading.Event()

    def listen():
        with salt.utils.event.get_event(
            "master", sock_dir=master.config["sock_dir"], opts=master.config, listen=True
        ) as bus:
            ready.set()
            while not done.is_set():
                event = bus.get_event(wait=2, tag="salt/job/", full=True, match_type="startswith")
                if event is not None:
                    seen.append(event)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    assert ready.wait(10)
    client.cmd(minion.id, "sqlx.ping", kwarg=dict(TARGET), timeout=60)
    time.sleep(3)
    done.set()
    thread.join(10)
    assert [e for e in seen if e["tag"].endswith("/new")], [e["tag"] for e in seen]
    text = "".join(str(e["data"]) for e in seen)
    assert RO_PASSWORD not in text
    assert (
        SDB_REF in text
    )  # the reference typed by the caller is on the bus: why sdb:// refs, not values, are required


# -- eauth: regex limits on call-time parameters ----------------------------------------------------------------------


@pytest.fixture
def as_user(client, minion):
    def _cmd(function, username=EAUTH_USER, password=EAUTH_PASSWORD, **kwargs):
        return client.cmd(
            minion.id,
            function,
            full_return=True,
            timeout=60,
            eauth="file",
            username=username,
            password=password,
            **kwargs,
        )

    return _cmd


def test_eauth_permits_the_pinned_host(as_user, minion):
    result = as_user("sqlx.ping", kwarg=dict(TARGET, profile="pg_dead"))
    assert result[minion.id]["retcode"] == 0, result
    assert result[minion.id]["ret"]["ok"] is True


@pytest.mark.parametrize(
    "host", ["192.0.2.9", "127.0.0.1.evil.example", "127.0.0.10", "x127.0.0.1", "169.254.169.254"]
)
def test_eauth_refuses_an_unlisted_host(as_user, host):
    with pytest.raises(salt.exceptions.AuthorizationError):
        as_user("sqlx.ping", kwarg=dict(TARGET, profile="pg_dead", host=host))


def test_eauth_rule_that_pins_host_needs_host_present(as_user):
    """``connection={...}`` alone does not carry a host past a rule that pins ``host``: the key must be present."""
    with pytest.raises(salt.exceptions.AuthorizationError):
        as_user("sqlx.ping", kwarg={"profile": "pg_dead", "connection": dict(TARGET)})


def test_eauth_host_flat_plus_host_in_connection_is_refused_by_the_minion(as_user, minion):
    result = as_user(
        "sqlx.ping",
        kwarg={"profile": "pg_dead", "host": "127.0.0.1", "connection": {"host": "192.0.2.9"}},
    )
    assert result[minion.id]["retcode"] == 1
    assert "[E_OPTION_INVALID]" in result[minion.id]["ret"]


def test_omitted_kwargs_are_not_restricted_by_eauth_so_the_minion_policy_is_the_control(
    as_user, minion
):
    """bob's rule pins only ``profile``: eauth lets ``host=192.0.2.9`` through, the minion policy refuses it."""
    result = as_user(
        "sqlx.ping",
        username=OTHER_USER,
        password=OTHER_PASSWORD,
        kwarg={"profile": "pg_dead", "host": "192.0.2.9"},
    )
    assert result[minion.id]["retcode"] == 1
    assert "[E_POLICY]" in result[minion.id]["ret"]
    ok = as_user(
        "sqlx.ping",
        username=OTHER_USER,
        password=OTHER_PASSWORD,
        kwarg=dict(TARGET, profile="pg_dead"),
    )
    assert ok[minion.id]["retcode"] == 0


# -- salt-api ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api(master, minion, api_port):  # pylint: disable=unused-argument
    daemon = master.salt_api_daemon()
    with daemon.started():
        daemon.test_port = api_port
        yield daemon


def _post(api, minion, fun, kwarg, username=EAUTH_USER, password=EAUTH_PASSWORD):
    body = [
        {
            "client": "local",
            "tgt": minion.id,
            "fun": fun,
            "kwarg": kwarg,
            "eauth": "file",
            "username": username,
            "password": password,
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
def test_salt_api_flat_and_connection(api, minion):
    done, command = _post(
        api, minion, "sqlx.query", dict(TARGET, profile="pg_dead", sql="select current_user")
    )
    assert done.returncode == 0, done.stderr
    print("CURL:", command[:3], "-d", command[-1].replace(SDB_REF, "<sdb-ref>"))
    print("JSON:", done.stdout)
    result = json.loads(done.stdout)["return"][0][minion.id]
    assert result["rows"] == [["ro"]]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_salt_api_unlisted_host_is_refused_by_eauth(api, minion):
    done, _ = _post(api, minion, "sqlx.ping", dict(TARGET, profile="pg_dead", host="192.0.2.9"))
    assert done.returncode == 0, done.stderr
    assert "<html" in done.stdout.lower()
    assert minion.id not in done.stdout


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_salt_api_minion_policy_refuses_when_eauth_is_silent(api, minion):
    done, _ = _post(
        api,
        minion,
        "sqlx.ping",
        {"profile": "pg_dead", "host": "192.0.2.9"},
        username=OTHER_USER,
        password=OTHER_PASSWORD,
    )
    assert done.returncode == 0, done.stderr
    ret = json.loads(done.stdout)["return"][0][minion.id]
    assert "[E_POLICY]" in ret
