# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""Shared helpers for the T11 security tests: live servers, injected ``__salt__``, admin access.

Servers and accounts are those of ``tests-support/env.sh`` (PostgreSQL 54329, MySQL 33061, MariaDB 33062; accounts
``ro`` and ``rw``; admin ``postgres`` / ``root``). Scratch objects are named ``t11_*`` and dropped by the fixtures that
create them. A server that is not reachable is skipped with the reason (never silently passed).
"""

import contextlib
import os
import socket
import uuid

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

import saltext.sqlx.modules.sqlx_mod as mod

HOST = os.environ.get("SQLX_TEST_HOST", "127.0.0.1")
ADMIN_PW = os.environ.get("SQLX_ADMIN_PW", "admin_pw")

SERVERS = {
    "postgresql": {
        "dialect": "postgresql",
        "drivername": "postgresql+psycopg",
        "port": int(os.environ.get("SQLX_PG_PORT", "54329")),
        "database": "sqlx_test",
        "admin": "postgres",
    },
    "mysql": {
        "dialect": "mysql",
        "drivername": "mysql+pymysql",
        "port": int(os.environ.get("SQLX_MYSQL_PORT", "33061")),
        "database": "scratch",
        "admin": "root",
    },
    "mariadb": {
        "dialect": "mysql",
        "drivername": "mysql+pymysql",
        "port": int(os.environ.get("SQLX_MARIADB_PORT", "33062")),
        "database": "scratch",
        "admin": "root",
    },
}
KEYS = tuple(SERVERS)


class Server:
    """One live server: admin access, profile builder, and a module with ``__salt__`` injected."""

    def __init__(self, key):
        self.key = key
        self.cfg = SERVERS[key]
        self.dialect = self.cfg["dialect"]

    def reachable(self):
        """Return ``None`` when the server accepts a connection, else the reason."""
        try:
            with socket.create_connection((HOST, self.cfg["port"]), timeout=2):
                pass
        except OSError as exc:
            return f"{self.key} not reachable on {HOST}:{self.cfg['port']} ({type(exc).__name__}); run tests-support/up.sh auto"
        return None

    def url(self, account):
        user, password = (
            (self.cfg["admin"], ADMIN_PW) if account == "admin" else (account, f"{account}_pw")
        )
        return URL.create(
            self.cfg["drivername"],
            username=user,
            password=password,
            host=HOST,
            port=self.cfg["port"],
            database=self.cfg["database"],
        )

    def admin(self, sql, params=None):
        """Run ``sql`` as the admin account in its own committed transaction; rows as tuples, or ``None``."""
        eng = sa.create_engine(self.url("admin"), poolclass=NullPool)
        try:
            with eng.begin() as conn:
                result = conn.execute(text(sql), params or {})
                return [tuple(r) for r in result] if result.returns_rows else None
        finally:
            eng.dispose()

    def profile(self, account, **extra):
        out = {
            "dialect": self.dialect,
            "host": HOST,
            "port": self.cfg["port"],
            "database": self.cfg["database"],
            "username": account,
            "password": f"{account}_pw",
            "connect_timeout_s": 5,
        }
        out.update(extra)
        return out

    def profiles(self):
        return {
            "ro": self.profile("ro"),
            "rw": self.profile("rw", allow_execute=True, read_only=False),
            "rw_query": self.profile("rw", read_only=False),
            "rw_guard": self.profile("rw"),
        }


@contextlib.contextmanager
def injected(profiles, sdb=None, policy=None):
    """
    Point the module's ``__salt__`` at ``profiles`` (and an ``sdb`` dict) for the duration of the block.

    ``policy`` is the ``sqlx:_policy`` mapping; ``None`` means no policy at all (the D16 compat default).
    """
    config = {"profiles": profiles}
    if policy is not None:
        config["_policy"] = policy
    functions = {
        "config.get": lambda key, default=None: (config if key == "sqlx" else default),
        "sdb.get": (sdb or {}).__getitem__,
    }
    had = hasattr(mod, "__salt__")
    old = getattr(mod, "__salt__", None)
    mod.__salt__ = functions
    try:
        yield mod
    finally:
        if had:
            mod.__salt__ = old
        else:
            del mod.__salt__


def unique(prefix="t11"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


#: A password full of regex, format, URL and shell metacharacters (no quote or backslash, so it is safe to put in DDL).
META_PW = "Zq7{0}%s(.*)[a-z]+?|^$&=;@/#!~"
#: A value bound as a parameter; must never come back in an error unless the server itself echoes it.
BOUND = "BOUND-t11-VALUE-4242"
#: The value in the seeded row of ``scratch.t11_uniq`` (a unique violation echoes it).
SEED_EMAIL = "seed-t11@example.invalid"


def make_meta_account(srv):
    """Create the throwaway account ``t11_meta`` (SELECT on ``scratch.orders``, DML on ``scratch.t11_uniq``)."""
    k = srv.key
    drop_meta_account(srv)
    if k == "postgresql":
        srv.admin(f"CREATE ROLE t11_meta LOGIN PASSWORD '{META_PW}'")
        srv.admin("GRANT USAGE ON SCHEMA scratch TO t11_meta")
        srv.admin("ALTER ROLE t11_meta SET search_path = scratch")
        srv.admin("CREATE TABLE scratch.t11_uniq (email text PRIMARY KEY, n integer)")
        srv.admin(f"INSERT INTO scratch.t11_uniq VALUES ('{SEED_EMAIL}', 1)")
        srv.admin("GRANT SELECT ON scratch.orders TO t11_meta")
        srv.admin("GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.t11_uniq TO t11_meta")
    else:
        srv.admin(f"CREATE USER 't11_meta'@'%' IDENTIFIED BY '{META_PW}'")
        srv.admin("CREATE TABLE scratch.t11_uniq (email varchar(100) PRIMARY KEY, n integer)")
        srv.admin(f"INSERT INTO scratch.t11_uniq VALUES ('{SEED_EMAIL}', 1)")
        srv.admin("GRANT SELECT ON scratch.orders TO 't11_meta'@'%'")
        srv.admin("GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.t11_uniq TO 't11_meta'@'%'")


def drop_meta_account(srv):
    srv.admin("DROP TABLE IF EXISTS scratch.t11_uniq")
    if srv.key == "postgresql":
        (
            srv.admin("DROP OWNED BY t11_meta")
            if srv.admin("SELECT 1 FROM pg_roles WHERE rolname = 't11_meta'")
            else None
        )
        srv.admin("DROP ROLE IF EXISTS t11_meta")
    else:
        srv.admin("DROP USER IF EXISTS 't11_meta'@'%'")


def observe(kind, label, outcome):
    """Print one machine-greppable line (``pytest -s | grep '^OBSERVED'``) used to build docs/SECURITY_REVIEW.md."""
    print(f"OBSERVED|{kind}|{label}|{' '.join(str(outcome).split())[:200]}")


def attempt(call):
    """Run ``call``; return ``("OK", result)`` or ``("ERR", exception)``. BaseException is not swallowed."""
    try:
        return "OK", call()
    except Exception as exc:  # pylint: disable=broad-except
        return "ERR", exc


def describe(outcome):
    kind, value = outcome
    if kind == "OK":
        cols = value.get("columns") if isinstance(value, dict) else None
        return (
            f"OK {str(value.get('rows', value))[:60] if isinstance(value, dict) else value}"
            if cols is not None
            else f"OK {str(value)[:80]}"
        )
    return f"{type(value).__name__}: {str(value)[:140]}"
