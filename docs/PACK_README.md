# saltext-sqlx build pack

Everything a fresh Claude Code or Cursor session needs to build `saltext.sqlx`: one Salt extension that runs SQL on
PostgreSQL, MySQL/MariaDB, Oracle and SQL Server through SQLAlchemy Core, shaped like Robot Framework's
DatabaseLibrary (`query`, `execute`, `check_query_result`, `check_row_count`), and that returns the **database's real
error** instead of Salt's `[]` / `False` / generic traceback.

## Contents

| File | Purpose |
|---|---|
| `BUILD_PROMPT.md` | Rules R1–R20, mistakes table (all observed), phase order |
| `SPEC.md` | The contract: profiles, functions, limits, dialects, error codes, return types, open decisions D1–D8 |
| `ARCHITECTURE.md` | Layout, call flow, transaction handling, error translation, adapter interface |
| `SECURITY.md` | Threat model, the verified read-only bypass, eauth examples, audit, checklist |
| `TEST_PLAN.md` | Unit / functional (per dialect) / Salt integration matrices |
| `TASKS.md` | Gated tasks T0–T11 |
| `CLAUDE_CODE_PROMPT.md` | Copy-paste Claude Code prompts for the repo `yogzblr/saltext-sqlx`: import, T0, T1, per-task template, T10, T11 |
| `CLAUDE.md`, `.cursor/rules/saltext-sqlx.mdc` | Claude Code memory and Cursor always-on rules |
| `scripts/sa_probe.py` | The probe used to verify SQLAlchemy behaviour (PostgreSQL; set `SQLX_PROBE_URL`) |

## Use with Claude Code / Cursor

```bash
mkdir saltext-sqlx && cd saltext-sqlx && git init
cp -r /path/to/saltext-sqlx-pack/. .
claude        # or open the folder in Cursor
```

First prompt: *"Read CLAUDE.md and the documents it lists. Do task T0 from TASKS.md only and stop at its gate."* Then one
task per prompt. T1 is the important one: it replaces the assumptions for MySQL, Oracle and SQL Server with recorded facts.

## What was verified, and what was not

**Verified** against a real PostgreSQL 16 with SQLAlchemy 2.0.55 + psycopg 3.3.6 (and the Salt 3008.2 source):
- Salt's `postgres.psql_query` returns `[]` for any failing statement; `mysql.query` catches only `OperationalError`, and
  PyMySQL maps syntax errors (1064) and missing tables (1146) to `ProgrammingError`, which is not a subclass of it.
- Error extraction: `orig.sqlstate` and `orig.diag.message_primary` give `42601 syntax error at or near "selec"`,
  `42P01`, `22012`, `25006`, `57014` for the five tested cases.
- `SET statement_timeout` cancels as `QueryCanceled`; `fetchmany(limit+1)` works; the types listed in SPEC section 9 map
  as stated.
- The in-session read-only guard **can be switched off by caller SQL** (SECURITY section 2).
- `text()` traps: `:a::int` is not a bind; `'{"a":1}'` needs `\:`; `exec_driver_sql` rejects `%` under psycopg.
- A failed-connect exception from psycopg did not contain the password.
- A SQL_ASCII PostgreSQL database makes psycopg 3 return bytes and breaks SQLAlchemy at first connect.

**Not verified:** anything on MySQL, MariaDB, Oracle, SQL Server (including every timeout and read-only mechanism in SPEC
section 7), SQLAlchemy 2.1.x, the Salt integration (no minion was run with this module), the proposed test images, and
`pymssql` wheel availability for Salt's Python 3.11 onedir. The `pymysql` hierarchy check was done on the library;
no MySQL server was run.

## Decisions already made

Execute is included but disabled per profile until enabled (2026-10-29). LGPL drivers (psycopg, pymssql) are accepted as
unmodified pip dependencies; GPL is not (2026-10-29). Open decisions D1–D8 are at the end of `SPEC.md`.
