# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with,redefined-outer-name,unused-import
"""D16 (owner decision 2026-11-01): the connection may be sent in the call with NO ``sqlx:_policy``. Attack it.

The default is wider than anything the module allowed before, so the guarantees that no default may relax are tested here
with the same weapons as ``test_call_params_surface.py`` (a counting ``socket.connect``, a fake PostgreSQL server that asks
for a cleartext password, every error path with the secrets in the call):

* a link-local / cloud-metadata target never gets a connection, whatever its spelling, and no secret is resolved first;
* the password of a profile never follows a retargeted call; the minion's libpq environment is not offered either;
* an ad-hoc connection can never ``execute`` (nor ``psql_query write=True``), and neither can a call that carries
  connection fields on a writing profile;
* callers cannot raise caps, set ``allow_execute`` / ``error_detail`` / ``read_only=False``, or weaken TLS;
* a literal password is on no error path, audit line or log record;
* every behaviour above is the SAME as before D16 once the operator sets ``allow_call_params: []`` (mirror tests).
"""

import logging
import socket

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from saltext.sqlx.lib import secrets as sqlx_secrets
from tests.security.test_call_params_surface import BASE_PW
from tests.security.test_call_params_surface import HOST
from tests.security.test_call_params_surface import LITERAL
from tests.security.test_call_params_surface import SDB_REF
from tests.security.test_call_params_surface import SDB_VALUE
from tests.security.test_call_params_surface import SECRETS
from tests.security.test_call_params_surface import connects  # fixture
from tests.security.test_call_params_surface import fake  # fixture
from tests.security.test_secret_leak import surfaces

ADHOC = {"dialect": "postgresql", "database": "d", "username": "u"}


@pytest.fixture
def cfg(monkeypatch):
    """A minion with a profile and NO ``sqlx:_policy`` at all."""
    config = {
        "profiles": {
            "base": {
                "dialect": "postgresql",
                "host": HOST,
                "port": 1,
                "database": "d",
                "username": "u",
                "password": BASE_PW,
                "connect_timeout_s": 3,
            },
            "writer": {
                "dialect": "postgresql",
                "host": HOST,
                "port": 1,
                "database": "d",
                "username": "w",
                "password": BASE_PW,
                "allow_execute": True,
                "read_only": False,
                "connect_timeout_s": 3,
            },
        },
    }
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": {SDB_REF: SDB_VALUE}.__getitem__,
        },
        raising=False,
    )
    return config


# -- SSRF: metadata and link-local ------------------------------------------------------------------------------------

METADATA_HOSTS = [
    "169.254.169.254",
    "169.254.169.254.",
    "169.254.0.1",
    "169.254.170.2",
    "fe80::1",
    "::ffff:169.254.169.254",
    "fd00:ec2::254",
    "100.100.100.200",
    "2852039166",  # 169.254.169.254 as one decimal number (inet_aton)
    "0xa9fea9fe",
    "0251.0376.0251.0376",
    "169.254.43518",
]


@pytest.mark.parametrize("host", METADATA_HOSTS)
@pytest.mark.parametrize("function", ["ping", "query", "table_list"])
def test_a_metadata_or_link_local_target_never_gets_a_connection(cfg, connects, function, host):
    call = {"ping": {}, "query": {"sql": "select 1"}, "table_list": {}}[function]
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        getattr(mod, function)(
            **ADHOC, host=host, password=SDB_REF, **call
        )  # ad-hoc, with a secret reference
    assert str(exc.value).startswith("[E_POLICY]"), str(exc.value)
    assert connects == []
    assert all(s not in str(exc.value) for s in SECRETS)
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        getattr(mod, function)(profile="base", host=host, **call)  # overlay on a profile
    assert str(exc.value).startswith("[E_POLICY]")
    assert connects == []


@pytest.mark.parametrize(
    "answers",
    [["169.254.169.254"], ["10.1.1.1", "169.254.169.254"], ["fe80::1"], ["::ffff:169.254.169.254"]],
)
def test_a_name_that_resolves_to_a_metadata_address_never_gets_a_connection(
    cfg, connects, monkeypatch, answers
):
    def resolver(name, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 5432)) for a in answers]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(**ADHOC, host="rebind.example", password=LITERAL)
    assert connects == []


def test_an_unresolvable_name_fails_closed_without_a_connection(cfg, connects, monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(CommandExecutionError, match=r"^\[E_CONNECT\]") as exc:
        mod.ping(**ADHOC, host="nowhere.example", password=LITERAL)
    assert connects == [] and "nowhere.example" not in str(exc.value)


@pytest.mark.parametrize(
    "host",
    [
        "a@evil.example",
        "evil.example#x",
        "evil.example/x",
        " 127.0.0.1",
        "127.0.0.1\x00.evil",
        "fe80::1%eth0",
        "",
    ],
)
def test_odd_host_text_never_gets_a_connection(cfg, connects, host):
    with pytest.raises((SaltInvocationError, CommandExecutionError)):
        mod.ping(**ADHOC, host=host)
    assert connects == []


def test_listing_a_metadata_address_in_allowed_hosts_is_the_only_way_to_allow_it(cfg, connects):
    cfg["_policy"] = {"allowed_hosts": ["169.254.169.254"]}
    with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
        mod.ping(
            **ADHOC, host="169.254.169.254", port=1
        )  # allowed: the connection is tried (and refused)
    assert not str(exc.value).startswith("[E_POLICY]")
    assert any(a[0] == "169.254.169.254" for a in connects)


# -- loopback and private addresses stay allowed (control) ---------------------------------------------------------------


def test_loopback_is_allowed_by_default_and_reaches_the_server(cfg, fake):
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(**ADHOC, host=HOST, port=fake.port, password=LITERAL)
    assert fake.accepted >= 1


# -- credential relay -------------------------------------------------------------------------------------------------


def test_the_profiles_password_does_not_follow_a_retargeted_call_by_default(cfg, fake):
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port)
    assert fake.accepted >= 1 and BASE_PW.encode() not in fake.seen()


def test_a_literal_password_in_the_call_reaches_only_the_server_the_caller_chose(cfg, fake):
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(profile="base", host=HOST, port=fake.port, password=LITERAL)
    assert LITERAL.encode() in fake.seen() and BASE_PW.encode() not in fake.seen()


def test_an_ad_hoc_call_without_a_password_is_not_answered_from_the_minions_environment(
    cfg, fake, monkeypatch, tmp_path
):
    passfile = tmp_path / "pgpass"
    passfile.write_text(f"{HOST}:*:*:*:PGPASSFILE-PW-8d2f\n")
    passfile.chmod(0o600)
    monkeypatch.setenv("PGPASSWORD", "PGPASSWORD-ENV-PW-61ce")
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    with pytest.raises((CommandExecutionError, SaltInvocationError)):
        mod.ping(**ADHOC, host=HOST, port=fake.port)
    assert fake.accepted >= 1
    for leaked in (b"PGPASSWORD-ENV-PW-61ce", b"PGPASSFILE-PW-8d2f", BASE_PW.encode()):
        assert leaked not in fake.seen()


# -- writes ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: mod.execute(
            sql="insert into t values (1)", **ADHOC, host=HOST, port=1, password=LITERAL
        ),
        lambda: mod.execute(
            sql="insert into t values (1)", **ADHOC, host=HOST, port=1, dry_run=True
        ),
        lambda: mod.psql_query(
            "insert into t values (1)",
            user="u",
            host=HOST,
            port=1,
            maintenance_db="d",
            password=LITERAL,
            write=True,
        ),
        lambda: mod.psql_query(
            "insert into t values (1)",
            user="u",
            host=HOST,
            port=1,
            maintenance_db="d",
            write=True,
            dry_run=False,
        ),
    ],
)
def test_an_ad_hoc_connection_can_never_write(cfg, connects, fake, call):
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        call()
    assert connects == [] and fake.accepted == 0


@pytest.mark.parametrize(
    "fields",
    [{"host": HOST}, {"database": "x"}, {"username": "x"}, {"password": LITERAL}, {"port": 5}],
)
def test_a_writing_profile_does_not_execute_when_the_call_carries_connection_fields(
    cfg, connects, fake, fields
):
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(profile="writer", sql="insert into t values (1)", **fields)
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.psql_query(
            "insert into t values (1)",
            profile="writer",
            write=True,
            **{
                {"database": "maintenance_db", "username": "user"}.get(k, k): v
                for k, v in fields.items()
            },
        )
    assert connects == [] and fake.accepted == 0


def test_a_writing_profile_still_executes_without_connection_fields(cfg, connects):
    with pytest.raises((CommandExecutionError, SaltInvocationError)) as exc:
        mod.execute(profile="writer", sql="insert into t values (1)")
    assert not str(exc.value).startswith(
        "[E_EXECUTE_DISABLED]"
    )  # reached the connection (port 1 refuses)
    assert connects


# -- nothing a default relaxes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_execute": True},
        {"error_detail": True},
        {"execute_verbs": ["DROP"]},
        {"row_limit": 10**6},
        {"_policy": {"allow_call_params": ["*"]}},
        {"connection": {"allow_execute": True}},
        {"connection": {"error_detail": True}},
        {"profiles": {"x": {}}},
    ],
)
def test_callers_cannot_set_safety_switches_by_default(cfg, connects, extra):
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.query(sql="select 1", **ADHOC, host=HOST, port=1, **extra)
    assert connects == []


@pytest.mark.parametrize(
    "extra",
    [
        {"read_only": False},
        {"read_only": "false"},
        {"ssl_mode": "verify-ca"},
        {"ssl_certs_dir": "/etc/salt/sqlx-certs/x"},
        {"ssl_files": {"ca": "/etc/passwd"}},
        {"query": {"sslmode": "disable"}},
        {"connect_args": {"ssl_context": None}},
        {"statement_timeout_ms": 10**9},
        {"connect_timeout_s": 10**6},
        {"connection": {"read_only": False}},
    ],
)
def test_opt_in_fields_stay_opt_in(cfg, connects, extra):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.query(sql="select 1", **ADHOC, host=HOST, port=1, **extra)
    assert connects == []


def test_the_hard_caps_of_an_ad_hoc_connection_are_the_defaults(cfg, monkeypatch):
    seen = {}

    def spy(profile, password, timeout_ms=None, read_only=None):
        seen.update(profile=profile, timeout_ms=timeout_ms, read_only=read_only)
        raise RuntimeError("stop here")

    monkeypatch.setattr(mod.engine, "connection", spy)
    with pytest.raises(CommandExecutionError):
        mod.query(
            sql="select 1", **ADHOC, host=HOST, timeout_ms=10**9, limit=10**9, max_bytes=10**12
        )
    assert seen["timeout_ms"] == 30000
    assert seen["profile"]["row_limit"] == 500 and seen["profile"]["error_detail"] is False
    assert seen["profile"]["read_only"] is True and seen["profile"]["allow_execute"] is False


# -- secrets on every error path, literal password in the call -------------------------------------------------------

ERROR_PATHS = {
    "metadata-host": dict(password=LITERAL, host="169.254.169.254"),
    "metadata-host-sdb": dict(password=SDB_REF, host="169.254.169.254"),
    "field-not-allowed": dict(password=LITERAL, ssl_mode="verify-ca"),
    "read-only-off": dict(password=LITERAL, read_only=False),
    "unknown-key": dict(password=LITERAL, allow_execute=True),
    "both-flat-and-connection": dict(password=LITERAL, connection={"password": SDB_REF}),
    "bad-connection-type": dict(password=LITERAL, connection="host=evil"),
    "unknown-connection-key": dict(connection={"password": LITERAL, "allow_execute": True}),
    "bad-port": dict(password=LITERAL, host=HOST, port="5432"),
    "bad-dialect": dict(password=LITERAL, host=HOST, dialect="sqlite"),
    "bad-driver": dict(password=LITERAL, host=HOST, driver="evil"),
    "connect-refused-literal": dict(password=LITERAL, host=HOST, port=1),
    "connect-refused-sdb": dict(password=SDB_REF, host=HOST, port=1),
    "sdb-unresolvable": dict(password="sdb://vault/missing", host=HOST, port=1),
    "nul-in-password": dict(password=LITERAL + "\x00x", host=HOST, port=1),
}


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
@pytest.mark.parametrize("function", ["ping", "query", "execute"])
def test_no_secret_on_any_error_path(cfg, caplog, function, path):
    extra = {"sql": "select 1"} if function in ("query", "execute") else {}
    kwargs = {**ADHOC, **ERROR_PATHS[path]}
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
            getattr(mod, function)(**extra, **kwargs)
    for text in surfaces(exc.value, caplog):
        for secret in SECRETS:
            assert secret not in text, (path, function, secret)
        assert (LITERAL + "\x00x") not in text


def test_no_secret_on_any_psql_query_error_path(cfg, caplog):
    cases = [
        dict(host="169.254.169.254"),
        dict(host=HOST, port=1),
        dict(host=HOST, port="x"),
        dict(host=HOST, port=1, dialect="mysql"),
        dict(host=HOST, port=1, ssl_mode="verify-ca"),
    ]
    for extra in cases:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises((SaltInvocationError, CommandExecutionError)) as exc:
                mod.psql_query("select 1", user="u", maintenance_db="d", password=LITERAL, **extra)
        for text in surfaces(exc.value, caplog):
            assert LITERAL not in text, extra


def test_a_successful_looking_audit_line_and_every_record_omit_the_literal(cfg, caplog, fake):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises((CommandExecutionError, SaltInvocationError)):
            mod.ping(**ADHOC, host=HOST, port=fake.port, password=LITERAL)
    text = "\n".join(r.getMessage() + repr(vars(r)) for r in caplog.records)
    assert LITERAL not in text
    assert "overrides=database,dialect,host,password,port,username" in text
    assert LITERAL.encode() in fake.seen()  # it did reach the server the caller chose


def test_the_masking_registry_is_bounded_against_a_stream_of_passwords():
    """D16: any caller can register a password; the masking filter must not grow without limit."""
    flt = sqlx_secrets.MaskingFilter()
    for i in range(sqlx_secrets.MAX_SECRETS * 3):
        flt.add_secret(f"pw-{i:06d}-xxxxxxxx")
    assert len(flt._secrets) == sqlx_secrets.MAX_SECRETS  # pylint: disable=protected-access
    assert flt.mask_text(f"x pw-{sqlx_secrets.MAX_SECRETS * 3 - 1:06d}-xxxxxxxx y") == "x *** y"


# -- strict mirror: the operator can restore the old behaviour ---------------------------------------------------------


def test_strict_config_refuses_every_default_shape_with_no_connection(cfg, connects, fake):
    cfg["_policy"] = {"allow_call_params": []}
    shapes = [
        lambda: mod.ping(**ADHOC, host=HOST, port=fake.port, password=LITERAL),
        lambda: mod.ping(profile="base", host=HOST, port=fake.port),
        lambda: mod.ping(connection=dict(ADHOC, host=HOST, port=fake.port)),
        lambda: mod.execute(sql="insert into t values (1)", **ADHOC, host=HOST),
    ]
    for shape in shapes:
        with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]") as exc:
            shape()
        assert all(s not in str(exc.value) for s in SECRETS)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_call_params"):
        mod.psql_query(
            "select 1", user="u", host=HOST, port=fake.port, maintenance_db="d", password=LITERAL
        )
    assert connects == [] and fake.accepted == 0


def test_strict_config_still_runs_profile_calls(cfg, connects):
    cfg["_policy"] = {"allow_call_params": []}
    with pytest.raises((CommandExecutionError, SaltInvocationError)) as exc:
        mod.ping(profile="base")
    assert str(exc.value).startswith("[E_CONNECT]") and connects


def test_a_shorter_list_narrows_the_default(cfg, connects, fake):
    cfg["_policy"] = {"allow_call_params": ["database"]}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*'host'"):
        mod.ping(profile="base", host=HOST, port=fake.port)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*'password'"):
        mod.ping(profile="base", password=LITERAL)
    assert connects == []
