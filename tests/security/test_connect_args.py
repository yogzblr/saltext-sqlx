# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, box 6: the ``connect_args`` / ``query`` allowlist, reviewed per driver.

Profiles are operator configuration, so these keys are not caller-reachable (see ``test_call_surface``); the allowlist
is the second line of defence if a profile is ever built from untrusted input. The expected sets below ARE the review:
a change to an allowlist fails the first test until this file (and docs/SECURITY_REVIEW.md) is updated.
"""

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import engine
from saltext.sqlx.lib import profiles
from tests.security import harness
from tests.security.harness import observe

#: The key is ``<dialect>`` for the default driver and ``<dialect>-<driver>`` for another one (decision D15: PostgreSQL
#: has pg8000 by default and psycopg when asked).
REVIEWED = {
    "postgresql": {
        "application_name"
    },  # pg8000: TLS comes from ssl_certs_dir as an ssl_context, never from a profile key
    "postgresql-psycopg": {
        "application_name",
        "sslmode",
        "target_session_attrs",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
    },
    "mysql": {
        "charset",
        "read_timeout",
        "write_timeout",
        "ssl_disabled",
        "ssl_verify_cert",
        "ssl_verify_identity",
    },
    "oracle": {"expire_time", "retry_count", "retry_delay", "ssl_server_dn_match"},
    "mssql": {"appname", "charset", "tds_version"},
}

#: Keys that must never be allowed for any driver: they redirect the connection, load code or files, or carry credentials.
DANGEROUS = {
    "host",
    "hostaddr",
    "port",
    "dbname",
    "database",
    "db",
    "user",
    "username",
    "password",
    "passfile",
    "service",
    "options",
    "sslcert",
    "sslkey",
    "sslrootcert",
    "sslcrl",
    "ssl_context",
    "ssl_ca",
    "ssl_cert",
    "ssl_key",
    "ssl",
    "local_infile",
    "init_command",
    "unix_socket",
    "read_default_file",
    "read_default_group",
    "plugin_dir",
    "client_flag",
    "conv",
    "cursorclass",
    "defer_connect",
    "auth_plugin_map",
    "server_public_key",
    "binary_prefix",
    "creator",
    "sock",
    "socket",
    "unix_sock",
    "source_address",
    "startup_params",
    "replication",
    "tcp_keepalive",
    "client_encoding",
    "connect_timeout",
    "load_data_local",
    "sql_mode",
    "autocommit",
    "isolation_level",
    "dsn",
    "wallet_location",
    "wallet_password",
    "config_dir",
    "externalauth",
    "events",
    "edition",
    "handle",
    "login_timeout",
    "timeout",
    "server",
    "conn_properties",
    "autocommit_mode",
    "prepare_threshold",
    "row_factory",
    "cursor_factory",
    "context",
    "pool",
}


def split(name):
    dialect, _, driver = name.partition("-")
    return dialect, driver or dialects.DIALECT_DRIVER_CHOICES[dialect][0]  # not the session default


def adapter(name):
    return dialects.adapter(*split(name))


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
def test_allowlist_matches_the_reviewed_set(dialect):
    assert set(adapter(dialect).allowed_args) == REVIEWED[dialect]


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
def test_no_dangerous_key_is_allowed(dialect):
    assert not set(adapter(dialect).allowed_args) & DANGEROUS


def base(name, **extra):
    dialect, driver = split(name)
    return profiles.validate_profile(
        "p",
        {
            "dialect": dialect,
            "driver": driver,
            "host": "127.0.0.1",
            "port": 5432,
            "database": "d",
            "username": "u",
            **extra,
        },
    )


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
@pytest.mark.parametrize("where", ["connect_args", "query"])
def test_every_dangerous_key_is_refused_in_both_places(dialect, where):
    refused = 0
    for key in sorted(DANGEROUS - REVIEWED[dialect]):
        prof = base(dialect, **{where: {key: "x"}})
        with pytest.raises(SaltInvocationError) as exc:
            (
                adapter(dialect).connect_args(prof)
                if where == "connect_args"
                else adapter(dialect).url(prof, "pw")
            )
        assert (
            str(exc.value).startswith("[E_POLICY]")
            and key in str(exc.value)
            and "'x'" not in str(exc.value)
        )
        refused += 1
    observe("connect-args", f"{dialect} {where}", f"{refused} dangerous keys refused with E_POLICY")


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
def test_local_infile_ssl_context_and_callables_by_name(dialect):
    for key, value in (
        ("local_infile", 1),
        ("local_infile", True),
        ("ssl_context", object()),
        ("ssl_context", "ctx"),
        ("host", "evil.example"),
        ("init_command", "DROP TABLE x"),
        ("creator", lambda: None),
    ):
        for where in ("connect_args", "query"):
            with pytest.raises(SaltInvocationError):
                prof = base(dialect, **{where: {key: value}})
                (
                    adapter(dialect).connect_args(prof)
                    if where == "connect_args"
                    else adapter(dialect).url(prof, "pw")
                )


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
def test_allowed_keys_still_need_plain_scalar_values(dialect):
    key = sorted(REVIEWED[dialect])[0]
    for value in (None, lambda: 1, object(), [1], {"a": 1}, b"x"):
        with pytest.raises(SaltInvocationError) as exc:
            adapter(dialect).connect_args(base(dialect, connect_args={key: value}))
        assert str(exc.value).startswith("[E_POLICY]")


@pytest.mark.parametrize("dialect", sorted(REVIEWED))
def test_module_owned_timeouts_win_over_connect_args(dialect):
    out = adapter(dialect).connect_args(base(dialect, connect_timeout_s=7))
    assert 7 in out.values()


def test_url_is_built_with_url_create_so_nothing_in_a_field_can_redirect(server):
    prof = profiles.validate_profile(
        "x",
        {
            "dialect": server.dialect,
            "host": harness.HOST,
            "port": server.cfg["port"],
            "database": server.cfg["database"] + "?host=evil.example&port=1",
            "username": "ro@evil.example",
        },
    )
    eng = engine.build_engine(prof, "p@ss/word:#?host=evil")
    try:
        url = eng.url
        assert url.host == harness.HOST and url.port == server.cfg["port"]
        assert url.username == "ro@evil.example" and url.password == "p@ss/word:#?host=evil"
        assert "evil.example" in url.database  # swallowed whole as a database name, not parsed
        assert dict(url.query) == {} or "host" not in url.query
    finally:
        eng.dispose()
    observe(
        "connect-args",
        f"{server.key} host/port/user injected through database and username fields",
        "kept literal by URL.create",
    )


def test_postgres_application_name_is_a_value_not_more_options(server):
    """``application_name='x host=evil port=1'`` is quoted by the driver: it names the session, nothing else."""
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    name = "t11 host=evil.example port=1 dbname=postgres"
    prof = {"p": server.profile("ro", connect_args={"application_name": name})}
    with harness.injected(prof) as module:
        assert module.query("p", "SHOW application_name")["rows"] == [[name]]
        assert module.query("p", "SELECT current_database()")["rows"] == [[server.cfg["database"]]]
    observe(
        "connect-args",
        "postgresql application_name with embedded host=/port=/dbname=",
        "stayed one literal application_name",
    )


def test_mysql_charset_value_cannot_inject_sql(server):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    prof = {
        "p": server.profile(
            "ro", connect_args={"charset": "utf8mb4'; DROP TABLE scratch.orders; --"}
        )
    }
    with harness.injected(prof) as module:
        kind, value = harness.attempt(lambda: module.query("p", "SELECT 1"))
    observe(
        "connect-args",
        f"{server.key} charset with SQL metacharacters",
        harness.describe((kind, value)),
    )
    assert kind == "ERR"
    assert server.admin("SELECT count(*) FROM scratch.orders") == [(2,)]


def test_local_infile_in_a_profile_is_refused_and_the_default_driver_does_not_enable_it(server):
    """Both halves: the allowlist rejects the key, and a profile without it cannot LOAD DATA LOCAL even where the server allows."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    with harness.injected(
        {"p": server.profile("ro", connect_args={"local_infile": True})}
    ) as module:
        with pytest.raises(SaltInvocationError) as exc:
            module.query("p", "SELECT 1")
        assert str(exc.value).startswith("[E_POLICY]") and "local_infile" in str(exc.value)
    flag = server.admin("SHOW VARIABLES LIKE 'local_infile'")[0][1]
    table = harness.unique("scratch.t11_li")
    server.admin(f"CREATE TABLE {table} (a text)")
    try:
        with harness.injected({"rw": server.profile("rw", read_only=False)}) as module:
            kind, value = harness.attempt(
                lambda: module.query(
                    "rw", f"LOAD DATA LOCAL INFILE '/etc/hostname' INTO TABLE {table}"
                )
            )
        observe(
            "connect-args",
            f"{server.key} (server local_infile={flag}) LOAD DATA LOCAL INFILE as rw",
            harness.describe((kind, value)),
        )
        assert kind == "ERR"
        assert server.admin(f"SELECT count(*) FROM {table}") == [(0,)]
    finally:
        server.admin(f"DROP TABLE IF EXISTS {table}")


def test_engine_level_checks_do_not_open_a_connection_for_a_bad_key():
    """``build_engine`` raises E_POLICY before ``create_engine`` is reached."""
    with pytest.raises(SaltInvocationError):
        engine.build_engine(base("postgresql", connect_args={"host": "evil"}), "pw")
