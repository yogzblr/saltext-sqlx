# saltext-sqlx build pack

Everything a fresh Claude Code or Cursor session needs to build `saltext.sqlx`: one Salt extension that runs SQL on
PostgreSQL, MySQL/MariaDB, Oracle and SQL Server through SQLAlchemy Core, shaped like Robot Framework's
DatabaseLibrary (`query`, `execute`, `check_query_result`, `check_row_count`), and that returns the **database's real
error** instead of Salt's `[]` / `False` / generic traceback.

## Contents

| File | Purpose |
|---|---|
| `BUILD_PROMPT.md` | Rules R1–R20, mistakes table (all observed), phase order |
| `SPEC.md` | The contract: profiles, functions, limits, dialects, error codes, return types, open decisions D1–D11 |
| `ARCHITECTURE.md` | Layout, call flow, transaction handling, error translation, adapter interface |
| `SECURITY.md` | Threat model, the observed read-only bypass (PostgreSQL, MySQL, MariaDB), eauth examples, audit, checklist |
| `TEST_PLAN.md` | Unit / functional (per dialect) / Salt integration matrices |
| `TASKS.md` | Gated tasks T0–T11 |
| `CLAUDE_CODE_PROMPT.md` | Copy-paste Claude Code prompts for the repo `yogzblr/saltext-sqlx`: import, T0, T1, per-task template, T10, T11 |
| `CLAUDE.md`, `.cursor/rules/saltext-sqlx.mdc` | Claude Code memory and Cursor always-on rules |
| `scripts/sa_probe.py`, `scripts/probes/` | The PostgreSQL probe, and the per-dialect probes that produced `docs/WIRE_SHAPES.md` (databases via `tests-support/up.sh`) |

## Use with Claude Code / Cursor

```bash
mkdir saltext-sqlx && cd saltext-sqlx && git init
cp -r /path/to/saltext-sqlx-pack/. .
claude        # or open the folder in Cursor
```

First prompt: *"Read CLAUDE.md and the documents it lists. Do task T0 from TASKS.md only and stop at its gate."* Then one
task per prompt. T1 replaced the assumptions for PostgreSQL, MySQL and MariaDB with recorded facts (`docs/WIRE_SHAPES.md`); the Oracle and SQL Server half is T1b and needs a machine with registry access.

## What was verified, and what was not

**Observed** (task T1, 2026-10-29, natively started servers; evidence in `docs/WIRE_SHAPES.md`): PostgreSQL 16.15, MySQL 8.0.46
and MariaDB 10.11.14, with SQLAlchemy 2.0.55 and 2.1.6, psycopg 3.3.6 and PyMySQL 1.2.3 (and the Salt 3008.2 source):
- Salt's `postgres.psql_query` returns `[]` for any failing statement; `mysql.query` catches only `OperationalError`, and
  PyMySQL maps syntax errors (1064) and missing tables (1146) to `ProgrammingError`, which is not a subclass of it.
- Error shapes per dialect and case (class, SQLSTATE, vendor code, message field). PostgreSQL connect-phase errors have no
  SQLSTATE; MySQL/MariaDB are keyed on the vendor code; `SELECT 1/0` returns NULL on MySQL and MariaDB.
- Statement timeouts: PostgreSQL `statement_timeout` (57014); MySQL `MAX_EXECUTION_TIME` (3024, but silent for a table-less
  SELECT and not applied to DML); MariaDB `max_statement_time` (1969).
- The in-session read-only guard **can be switched off by caller SQL** on all three (SECURITY section 2); the database role held.
- `text()` traps: `:a::int` is not a bind; `'{"a":1}'` needs `\:`; `exec_driver_sql` rejects a literal `%` (psycopg and PyMySQL).
- No account password appeared in any observed exception text; `str()` of a SQLAlchemy exception embeds the SQL and the bound values.
- Not everything is a `DBAPIError`: `TypeError`, `RuntimeError`, `AttributeError`, `ValueError` and `InvalidRequestError` reached the caller.
- A SQL_ASCII PostgreSQL database makes psycopg 3 return bytes and breaks SQLAlchemy at first connect.
- SQLAlchemy 2.1.6 behaved identically to 2.0.55 on every recorded case.

**NOT RUN:** Oracle and SQL Server on a real server (Docker Hub, MCR and the Oracle registry returned HTTP 403 in the build
environment): every timeout mechanism, read-only behaviour, error class and code, and type mapping for them is unverified.
The owner runs these with `tests-support/up.sh` and `scripts/probes/` (T1b). Also not verified: the Docker images and
compose file, other server versions, TLS certificate verification, whether MySQL/MariaDB keep running a statement after
PyMySQL's `read_timeout` fires, the Salt integration (no minion was run with this module), and `pymssql` wheel availability
for Salt's Python 3.11 onedir.

## Decisions already made

Execute is included but disabled per profile until enabled (2026-10-29). LGPL drivers (psycopg, pymssql) are accepted as
unmodified pip dependencies; GPL is not (2026-10-29). Open decisions D1–D11 are at the end of `SPEC.md`.
