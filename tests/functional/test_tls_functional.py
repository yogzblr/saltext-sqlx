"""Functional TLS tests against REAL TLS-enabled servers (SPEC section 7, D12).

Servers and certificates come from ``tests-support/tls`` (``up.sh`` starts PostgreSQL 54339, MySQL 33071 and MariaDB
33072 on their own data directories; ``gen_certs.sh`` writes the throw-away PKI under ``$SQLX_TLS_ROOT/pki``). A
server that is not reachable skips with the reason. The standard servers of ``tests-support/native`` are not used.

Every failure case asserts the code, a fragment of the REAL driver message, and that neither a path nor key material
appears in what the caller sees. ``__salt__`` is injected as in the other functional module tests.
"""

import json
import os
import pathlib
import socket
import subprocess
import sys

import pytest
import yaml
from salt.exceptions import CommandExecutionError

import saltext.sqlx.modules.sqlx_mod as mod
from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import engine
from saltext.sqlx.lib import profiles

pytestmark = pytest.mark.tls

TLS_ROOT = pathlib.Path(os.environ.get("SQLX_TLS_ROOT", "/var/tmp/sqlx-tls-dbs"))
PKI = TLS_ROOT / "pki"
HERE = pathlib.Path(__file__).resolve().parents[2] / "tests-support" / "tls"
POLICY = {"ssl_certs_dir_root": str(PKI)}

SERVERS = {
    "postgresql": {
        "dialect": "postgresql",
        "port": 54339,
        "pw": ("tls_pw", "tls_pw_pw"),
        "cert": ("tls_cert", None),
    },
    "mysql": {
        "dialect": "mysql",
        "port": 33071,
        "pw": ("tls_pw", "tls_pw_pw"),
        "cert": ("tls_cert", "tls_cert_pw"),
    },
    "mariadb": {
        "dialect": "mysql",
        "port": 33072,
        "pw": ("tls_pw", "tls_pw_pw"),
        "cert": ("tls_cert", "tls_cert_pw"),
    },
}

# fragments of the real driver messages (docs/WIRE_SHAPES.md section 12), per server
UNKNOWN_CA = {
    "postgresql": "certificate verify failed",
    "mysql": "certificate verify failed",
    "mariadb": "certificate verify failed",
}
# pg8000 (the default PostgreSQL driver) hands the TLS errors of Python's ssl module through; libpq words them itself
# (docs/WIRE_SHAPES.md section 16). The key "postgresql" is the libpq text, "postgresql-pg8000" the ssl module's.
HOSTNAME = {
    "postgresql": 'does not match host name "127.0.0.2"',
    "postgresql-pg8000": "IP address mismatch",
    "mysql": "IP address mismatch",
    "mariadb": "IP address mismatch",
}
EXPIRED_CLIENT = {
    "postgresql": "alert certificate expired",
    "postgresql-pg8000": "CERTIFICATE_EXPIRED",
    "mysql": "CERTIFICATE_EXPIRED",
    "mariadb": "CERTIFICATE_EXPIRED",
}
CLIENT_CA_UNKNOWN = {
    "postgresql": "alert unknown ca",
    "postgresql-pg8000": "UNKNOWN_CA",
    "mysql": "UNKNOWN_CA",
    "mariadb": "UNKNOWN_CA",
}


def frag(table, srv):
    """The fragment for ``srv`` with the PostgreSQL driver the session runs (``SQLX_PG_DRIVER``, default pg8000)."""
    if srv["key"] == "postgresql" and dialects.DIALECT_DRIVERS["postgresql"] == "pg8000":
        return table["postgresql-pg8000"]
    return table[srv["key"]]


def _reachable(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return None
    except OSError as exc:
        return f"TLS test server on port {port} is not reachable ({exc.__class__.__name__}); run tests-support/tls/up.sh"


@pytest.fixture(scope="module", params=sorted(SERVERS))
def srv(request):
    info = dict(SERVERS[request.param], key=request.param)
    reason = _reachable(info["port"]) or (
        None if (PKI / "good" / "client.key").exists() else f"no PKI in {PKI}"
    )
    if reason:
        pytest.skip(reason)
    return info


def make_raw(srv, directory="good", who="cert", host="localhost", mode=None, **extra):
    user, password = srv[who]
    raw = {
        "dialect": srv["dialect"],
        "host": host,
        "port": srv["port"],
        "database": "tlsdb",
        "username": user,
        "connect_timeout_s": 5,
    }
    if password:
        raw["password"] = password
    if directory:
        raw["ssl_certs_dir"] = str(PKI / directory)
    if mode:
        raw["ssl_mode"] = mode
    if srv["dialect"] == "postgresql" and os.environ.get("SQLX_PG_DRIVER"):
        raw["driver"] = os.environ[
            "SQLX_PG_DRIVER"
        ]  # subprocesses (salt-call) do not see the pytest default
    raw.update(extra)
    return raw


def lib_ping(srv, **kw):
    raw = make_raw(srv, **kw)
    profile = profiles.validate_profile("t", raw, POLICY)
    return engine.ping(profile, raw.get("password"))


def expect_failure(srv, code, fragment, **kw):
    with pytest.raises((CommandExecutionError,)) as exc:
        lib_ping(srv, **kw)
    text = str(exc.value)
    assert text.startswith(f"[{code}]"), text
    assert fragment in text, text
    assert str(PKI) not in text and "BEGIN" not in text and "PRIVATE KEY" not in text, text
    return text


# -- accepted ---------------------------------------------------------------------------------------------------------
def test_verify_full_with_password_and_no_client_certificate(srv):
    out = lib_ping(srv, directory="ca-only", who="pw")
    assert out["ok"] is True and out["server_version"]


def test_verify_full_mutual_tls(srv):
    assert lib_ping(srv, directory="good", who="cert")["ok"] is True


def test_verify_full_password_user_may_also_present_a_certificate(srv):
    assert lib_ping(srv, directory="good", who="pw")["ok"] is True


def test_verify_ca_ignores_the_host_name(srv):
    assert lib_ping(srv, host="127.0.0.2", mode="verify-ca")["ok"] is True


def test_fallback_file_names(srv):
    assert lib_ping(srv, directory="alt-names")["ok"] is True


def test_explicit_ssl_files(srv):
    out = lib_ping(
        srv,
        directory="alt-names",
        ssl_files={"ca": "ca.crt", "cert": "client.crt", "key": "client.key"},
    )
    assert out["ok"] is True


# -- refused ----------------------------------------------------------------------------------------------------------
def test_wrong_ca_is_refused(srv):
    expect_failure(srv, "E_CONNECT", UNKNOWN_CA[srv["key"]], directory="wrong-ca")


def test_hostname_not_in_the_san_is_refused_by_verify_full(srv):
    expect_failure(srv, "E_CONNECT", frag(HOSTNAME, srv), host="127.0.0.2")


def test_expired_client_certificate_is_refused(srv):
    expect_failure(srv, "E_CONNECT", frag(EXPIRED_CLIENT, srv), directory="expired-client")


def test_client_certificate_from_another_ca_is_refused(srv):
    expect_failure(srv, "E_CONNECT", frag(CLIENT_CA_UNKNOWN, srv), directory="other-client")


def test_certificate_user_without_a_client_certificate_is_refused(srv):
    fragment = (
        "requires a valid client certificate" if srv["key"] == "postgresql" else "Access denied"
    )
    expect_failure(srv, "E_AUTH_FAILED", fragment, directory="ca-only", who="cert")


def test_plain_connection_to_a_tls_only_server_is_refused(srv):
    """No ssl_certs_dir and TLS switched off through the allowlisted driver option (the only way to ask for plain)."""
    if srv["dialect"] == "postgresql":
        if dialects.DIALECT_DRIVERS["postgresql"] == "pg8000":
            pytest.skip("pg8000 has no way to ask for plain TCP; see the next test")
        raw = make_raw(srv, directory=None, who="pw", query={"sslmode": "disable"})
        code, fragment = "E_CONNECT", "no encryption"
    else:
        raw = make_raw(srv, directory=None, who="pw", query={"ssl_disabled": True})
        code, fragment = "E_AUTH_FAILED", "Access denied"
    with pytest.raises(CommandExecutionError) as exc:
        profile = profiles.validate_profile("t", raw)
        engine.ping(profile, raw["password"])
    assert str(exc.value).startswith(f"[{code}]") and fragment in str(exc.value), str(exc.value)


def test_pg8000_without_ssl_certs_dir_is_opportunistic_unverified_tls():
    """Same strength as libpq's default ``sslmode=prefer``: TLS is used when offered, nothing is verified (section 16)."""
    srv = dict(SERVERS["postgresql"], key="postgresql")
    if _reachable(srv["port"]):
        pytest.skip("PostgreSQL TLS server not reachable")
    if dialects.DIALECT_DRIVERS["postgresql"] != "pg8000":
        pytest.skip("pg8000 behaviour")
    out = lib_ping(
        srv, directory=None, who="pw"
    )  # the server refuses plain TCP, so this proves TLS was used
    assert out["ok"] is True


def test_postgresql_certificate_name_mismatch_is_an_auth_failure():
    srv = dict(SERVERS["postgresql"], key="postgresql")
    if _reachable(srv["port"]):
        pytest.skip("PostgreSQL TLS server not reachable")
    srv["cert"] = ("tls_mismatch", None)
    expect_failure(srv, "E_AUTH_FAILED", "certificate authentication failed", directory="good")


def test_tls_files_that_vanish_after_validation_are_a_clean_error(srv, tmp_path):
    """The file is removed between profile load and connect: the driver error is mapped, no path is shown."""
    root = tmp_path / "root"
    shutil_tree = root / "d"
    shutil_tree.mkdir(parents=True)
    for name in ("ca.pem", "client.pem", "client.key"):
        (shutil_tree / name).write_bytes((PKI / "good" / name).read_bytes())
    (shutil_tree / "client.key").chmod(0o600)
    raw = make_raw(srv, directory=None, ssl_certs_dir=str(shutil_tree))
    profile = profiles.validate_profile("t", raw, {"ssl_certs_dir_root": str(root)})
    (shutil_tree / "client.key").unlink()
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(profile, raw.get("password"))
    text = str(exc.value)
    assert text.startswith("[E_CONNECT]"), text
    assert str(tmp_path) not in text and "BEGIN" not in text


def test_garbage_key_is_a_clean_error_without_key_material(srv, tmp_path):
    root = tmp_path / "root"
    d = root / "d"
    d.mkdir(parents=True)
    for name in ("ca.pem", "client.pem"):
        (d / name).write_bytes((PKI / "good" / name).read_bytes())
    (d / "client.key").write_text(
        "-----BEGIN PRIVATE KEY-----\nSECRETSECRET\n-----END PRIVATE KEY-----\n"
    )
    (d / "client.key").chmod(0o600)
    raw = make_raw(srv, directory=None, ssl_certs_dir=str(d))
    profile = profiles.validate_profile("t", raw, {"ssl_certs_dir_root": str(root)})
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(profile, raw.get("password"))
    text = str(exc.value)
    assert text.startswith("[E_CONNECT]"), text
    assert "SECRETSECRET" not in text and str(tmp_path) not in text and "BEGIN" not in text


# -- through the Salt module (inject __salt__) --------------------------------------------------------------------------
@pytest.fixture
def salt(srv, monkeypatch):
    profiles_cfg = {
        "tls_mtls": make_raw(srv, directory="good", who="cert"),
        "tls_pw": make_raw(srv, directory="ca-only", who="pw"),
        "tls_wrong_ca": make_raw(srv, directory="wrong-ca", who="pw"),
    }
    for raw in profiles_cfg.values():
        raw.pop("connect_timeout_s")
        raw["connect_timeout_s"] = 5
    functions = {
        "config.get": lambda key, default=None: (
            {"_policy": POLICY, "profiles": profiles_cfg} if key == "sqlx" else default
        ),
        "sdb.get": lambda ref: ref,
    }
    monkeypatch.setattr(mod, "__salt__", functions, raising=False)
    return srv


def test_module_ping_and_query_over_mutual_tls(salt):
    assert mod.ping("tls_mtls")["ok"] is True
    if salt["dialect"] == "postgresql":
        out = mod.query(
            "tls_mtls", "SELECT ssl, client_dn FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        assert out["rows"][0][0] is True and "tls_cert" in out["rows"][0][1]
    else:
        out = mod.query("tls_mtls", "SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        assert out["rows"][0][1], out
        who = mod.query("tls_mtls", "SELECT CURRENT_USER()")
        assert who["rows"][0][0].startswith("tls_cert@")
    assert out["row_count"] == 1


def test_module_query_over_server_auth_tls(salt):
    out = mod.query("tls_pw", "SELECT 1 AS one")
    assert out["rows"] == [[1]]
    if salt["dialect"] == "postgresql":
        out = mod.query("tls_pw", "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        assert out["rows"] == [[True]]


@pytest.mark.usefixtures("salt")
def test_module_failure_keeps_code_and_real_message():
    with pytest.raises(CommandExecutionError) as exc:
        mod.query("tls_wrong_ca", "SELECT 1")
    text = str(exc.value)
    assert text.startswith("[E_CONNECT]") and "certificate verify failed" in text
    assert str(PKI) not in text


def test_module_policy_failure_comes_before_any_network_call(salt, monkeypatch):
    functions = dict(mod.__salt__)
    functions["config.get"] = lambda key, default=None: (
        {"profiles": {"p": make_raw(salt, directory="good")}} if key == "sqlx" else default
    )
    monkeypatch.setattr(mod, "__salt__", functions, raising=False)
    monkeypatch.setattr(engine, "connection", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(Exception) as exc:
        mod.ping("p")
    assert str(exc.value).startswith("[E_POLICY]")


# -- salt-call --local -------------------------------------------------------------------------------------------------------
def test_salt_call_local_with_a_tls_profile(srv, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    cfg = {
        "root_dir": str(tmp_path / "root"),
        "file_client": "local",
        "id": "tls-test",
        "cachedir": str(tmp_path / "cache"),
        "pki_dir": str(tmp_path / "pki"),
        "log_file": str(tmp_path / "salt.log"),
        "sqlx": {
            "_policy": POLICY,
            "profiles": {
                "mtls": make_raw(srv, directory="good", who="cert"),
                "wrong_ca": make_raw(srv, directory="wrong-ca", who="pw"),
            },
        },
    }
    (conf / "minion").write_text(yaml.safe_dump(cfg))
    salt_call = pathlib.Path(sys.executable).with_name("salt-call")
    base = [str(salt_call), "--local", "-c", str(conf), "--out=json", "--log-level=quiet"]
    ok = subprocess.run(
        base + ["sqlx.ping", "profile=mtls"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    result = json.loads(ok.stdout)["local"]
    assert result["ok"] is True and result["dialect"] == srv["dialect"]
    bad = subprocess.run(
        base + ["sqlx.ping", "profile=wrong_ca"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert bad.returncode != 0
    assert (
        "[E_CONNECT]" in bad.stdout + bad.stderr
        and "certificate verify failed" in bad.stdout + bad.stderr
    )
    assert str(PKI) not in bad.stdout + bad.stderr


# -- expired SERVER certificate (restarts / reloads the TLS test servers; always restored) -----------------------------------
@pytest.fixture
def expired_server(srv):
    which = srv["key"]
    subprocess.run(
        ["bash", str(HERE / "swap_server_cert.sh"), "expired", which],
        check=True,
        capture_output=True,
    )
    try:
        yield
    finally:
        subprocess.run(
            ["bash", str(HERE / "swap_server_cert.sh"), "good", which],
            check=True,
            capture_output=True,
        )


@pytest.mark.usefixtures("expired_server")
def test_expired_server_certificate_is_refused(srv):
    expect_failure(srv, "E_CONNECT", "certificate verify failed", directory="good")


def test_server_is_fine_again_after_the_certificate_is_restored(srv):
    assert lib_ping(srv, directory="good")["ok"] is True
