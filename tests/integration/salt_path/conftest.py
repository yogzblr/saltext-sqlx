"""
Fixtures for the Salt integration suite (TASKS T10, TEST_PLAN section 5).

A REAL master and minion are started by ``pytest-salt-factories`` with the extension installed (entry point) and
``sqlx:`` profiles that point at the live test databases from ``tests-support/env.sh``. The databases must already be
running (``tests-support/up.sh auto``); this suite never starts, stops or deletes them. Tables are ``t10_`` prefixed
and dropped at the end of the session.
"""

import contextlib
import os
import socket

import pytest
import salt.client
import sqlalchemy
import yaml

PG_PORT = int(os.environ.get("SQLX_PG_PORT", "54329"))
MYSQL_PORT = int(os.environ.get("SQLX_MYSQL_PORT", "33061"))
MARIADB_PORT = int(os.environ.get("SQLX_MARIADB_PORT", "33062"))
ADMIN_PW = os.environ.get("SQLX_ADMIN_PW", "admin_pw")
HOST = "127.0.0.1"

# Values that must never reach a log or the job cache (the password) and a bound value (which Salt does store).
RO_PASSWORD = "ro_pw"
BOUND_SECRET = "T10_BOUND_SECRET_7f3a9c"
SQL_MARKER = "T10_SQL_MARKER_41d2e8"

EAUTH_USER = "t10_alice"
EAUTH_PASSWORD = "t10_alice_pw_5b1e"

ADMIN_URLS = {
    "pg": f"postgresql+psycopg://postgres:{ADMIN_PW}@{HOST}:{PG_PORT}/sqlx_test",
    "mysql": f"mysql+pymysql://root:{ADMIN_PW}@{HOST}:{MYSQL_PORT}/scratch",
    "mariadb": f"mysql+pymysql://root:{ADMIN_PW}@{HOST}:{MARIADB_PORT}/scratch",
}
PORTS = {"pg": PG_PORT, "mysql": MYSQL_PORT, "mariadb": MARIADB_PORT}


def _profile(dialect, port, database, username, password):
    return {
        "dialect": dialect,
        "host": HOST,
        "port": port,
        "database": database,
        "username": username,
        "password": password,
        "connect_timeout_s": 5,
    }


def _free_port():
    with contextlib.closing(socket.socket()) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session", autouse=True)
def databases_up():
    """Fail loudly (not skip) when a live database is unreachable."""
    down = []
    for name, port in PORTS.items():
        with contextlib.closing(socket.socket()) as sock:
            sock.settimeout(3)
            if sock.connect_ex((HOST, port)) != 0:
                down.append(f"{name}:{port}")
    if down:
        pytest.fail(f"test databases not reachable: {down}; run tests-support/up.sh auto")


@pytest.fixture(scope="package")
def work_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("t10")


@pytest.fixture(scope="package")
def sdb_file(work_dir):
    path = work_dir / "t10_sdb.yaml"
    path.write_text(yaml.safe_dump({"t10_pg_ro_password": RO_PASSWORD}))
    return path


@pytest.fixture(scope="package")
def eauth_file(work_dir):
    path = work_dir / "t10_eauth.txt"
    path.write_text(f"{EAUTH_USER}:{EAUTH_PASSWORD}\n")
    return path


@pytest.fixture(scope="package")
def api_port():
    return _free_port()


@pytest.fixture(scope="package")
def master_config(eauth_file, api_port):
    """The master: ``file`` eauth with the SECURITY.md section 4 regex limit on ``profile``, and rest_cherrypy."""
    return {
        "rest_cherrypy": {"port": api_port, "host": HOST, "disable_ssl": True},
        "netapi_enable_clients": ["local"],
        "log_level": "debug",
        "log_level_logfile": "debug",
        "external_auth": {
            "file": {
                "^filename": str(eauth_file),
                EAUTH_USER: [
                    {
                        "*": [
                            {"sqlx.query": {"kwargs": {"profile": "^pg_ro$"}}},
                            "sqlx.ping",
                        ]
                    }
                ],
            }
        },
    }


@pytest.fixture(scope="package")
def minion_config(sdb_file):
    profiles = {
        "pg_ro": _profile("postgresql", PG_PORT, "sqlx_test", "ro", RO_PASSWORD),
        "pg_rw": dict(
            _profile("postgresql", PG_PORT, "sqlx_test", "rw", "rw_pw"), allow_execute=True
        ),
        "mysql_ro": _profile("mysql", MYSQL_PORT, "scratch", "ro", RO_PASSWORD),
        "mariadb_ro": _profile("mysql", MARIADB_PORT, "scratch", "ro", RO_PASSWORD),
        "pg_sdb": _profile(
            "postgresql", PG_PORT, "sqlx_test", "ro", "sdb://t10_sdb/t10_pg_ro_password"
        ),
        "pg_sdb_missing": _profile(
            "postgresql", PG_PORT, "sqlx_test", "ro", "sdb://t10_sdb/t10_no_such_key"
        ),
        "pg_badpw": _profile("postgresql", PG_PORT, "sqlx_test", "ro", "t10_wrong_pw_c94d"),
        "mysql_badpw": _profile("mysql", MYSQL_PORT, "scratch", "ro", "t10_wrong_pw_c94d"),
        "mariadb_badpw": _profile("mysql", MARIADB_PORT, "scratch", "ro", "t10_wrong_pw_c94d"),
    }
    return {
        "log_level": "debug",
        "log_level_logfile": "debug",
        "sqlx": {"profiles": profiles},
        "t10_sdb": {"driver": "yaml", "files": [str(sdb_file)]},
    }


@pytest.fixture(scope="package", autouse=True)
def t10_tables():
    """Create ``t10_orders`` on each database as the admin account, grant ro/rw, drop it at the end."""
    engines = {name: sqlalchemy.create_engine(url) for name, url in ADMIN_URLS.items()}
    for name, engine in engines.items():
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS scratch.t10_orders"))
            conn.execute(
                sqlalchemy.text(
                    "CREATE TABLE scratch.t10_orders (id integer PRIMARY KEY, note varchar(50))"
                )
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO scratch.t10_orders VALUES (1, 'one'), (2, 'two'), (3, 'three')"
                )
            )
            if name == "pg":
                conn.execute(sqlalchemy.text("GRANT SELECT ON scratch.t10_orders TO ro"))
                conn.execute(
                    sqlalchemy.text(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.t10_orders TO rw"
                    )
                )
    try:
        yield engines
    finally:
        for engine in engines.values():
            with engine.begin() as conn:
                conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS scratch.t10_orders"))
            engine.dispose()


@pytest.fixture(scope="package")
def client(master, minion, t10_tables):  # pylint: disable=unused-argument
    """A LocalClient on the test master, with the minion connected and the tables ready."""
    return salt.client.LocalClient(c_path=master.config["conf_file"])


@pytest.fixture(scope="package")
def call(client, minion):
    """``call(function, **kwargs) -> (return, retcode, jid)`` through ``LocalClient().cmd(..., kwarg=...)``."""

    def _call(function, **kwargs):
        result = client.cmd(
            minion.id, function, kwarg=kwargs, full_return=True, timeout=60, tgt_type="glob"
        )
        assert minion.id in result, f"no return from the minion: {result!r}"
        full = result[minion.id]
        return full["ret"], full["retcode"], full["jid"]

    return _call
