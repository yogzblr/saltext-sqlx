# CLAUDE.md — saltext-sqlx

Salt extension `saltext.sqlx`: run SQL on PostgreSQL, MySQL/MariaDB, Oracle, SQL Server via SQLAlchemy Core and return the
**real database error** (`ERROR: [E_SQL] postgresql 42P01: relation "x" does not exist`). Functions: `query`, `execute`
(off by default), `check_query_result`, `check_row_count`, `ping`, `table_list`. API shape follows Robot Framework's
DatabaseLibrary; connection details live in **profiles**, never in the call.

## Read first, in this order
1. `BUILD_PROMPT.md` — rules R1–R20 and the mistakes table
2. `SPEC.md` — the contract
3. `ARCHITECTURE.md`, `SECURITY.md`, `TEST_PLAN.md`, `TASKS.md`

## How to work
- One task from `TASKS.md` at a time; stop at its gate and summarise the diff.
- Test first in `lib/`. Keep `modules/sqlx_mod.py` thin. No Salt dunders in `lib/`.
- If SPEC is wrong, fix SPEC in the same commit. Real database behaviour goes in `docs/WIRE_SHAPES.md`.
- Ask before adding a runtime dependency (MIT/BSD/Apache-2.0; LGPL only as unmodified pip packages; never GPL).

## Hard rules (summary)
- One error path (`lib/errors.py`); never return `[]`/`False` for a failure.
- Callers pick a profile only. `query` never commits. `execute` needs `allow_execute: true`.
- The in-session read-only guard is bypassable (observed on PostgreSQL, MySQL and MariaDB); the DB role is the real control.
- Catch `Exception`, not only `DBAPIError`; take the message from `exc.orig`, never `str()` of the SQLAlchemy exception (it embeds SQL and bound values); map on the vendor code first.
- `text()` + binds only; `CAST(:x AS t)`, never `:x::t`; literal colons as `\:`; never `exec_driver_sql` for caller SQL.
- Test PostgreSQL databases as UTF8 (SQL_ASCII breaks psycopg 3).
- Never run Python from inside `src/saltext/`.

## Commands (after T0)
```bash
python -m venv .venv && source .venv/bin/activate && pip install -e '.[tests,dev]'
nox -s lint ; nox -s unit ; nox -s functional-pg
SQLX_PROBE_URL='postgresql+psycopg://user@host:5432/db' python -I scripts/sa_probe.py
```

## Known open items
Observed on real servers (T1, 2026-10-29): PostgreSQL 16.15, MySQL 8.0.46, MariaDB 10.11.14, with SQLAlchemy 2.0.55 and 2.1.6
(`docs/WIRE_SHAPES.md`). **Oracle and SQL Server are NOT RUN**: Docker Hub, MCR and the Oracle registry returned HTTP 403 in
the build environment, so every Oracle and SQL Server server-side row in SPEC sections 7-8 is unverified. The owner must run
them on a machine with registry access (`tests-support/up.sh`, then `scripts/probes/`); this is task T1b in `TASKS.md`.
Other open items: the dialect-to-driver table moves from `profiles.DIALECT_DRIVERS` to `lib/dialects.py` (T4); whether to
add `cryptography` to the `mysql` extra; D9 (clamp vs reject), D10 (`mariadb` as a dialect value), D11 (PyMySQL
`read_timeout`). Open decisions D1–D11 are at the end of `SPEC.md`.
