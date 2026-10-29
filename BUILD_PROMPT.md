# saltext-sqlx — BUILD PROMPT

You are building `saltext.sqlx`, a Salt execution-module extension that runs SQL on PostgreSQL, MySQL/MariaDB, Oracle and
SQL Server through SQLAlchemy Core and **always surfaces the database's real error**. Read `SPEC.md` (contract),
`ARCHITECTURE.md`, `SECURITY.md`, `TEST_PLAN.md`, `TASKS.md` first. Work through `TASKS.md` one task at a time and stop at
each gate.

## Rules (non-negotiable)

**Errors**
- R1. Exactly one error path: `lib/errors.py`. Every database failure becomes `CommandExecutionError("[E_CODE] <dialect> <code>: <message>")`. Never return `[]`, `False`, `None` or `{"error": ...}` for a failure.
- R2. Map on SQLSTATE / vendor code first, exception class second. Unknown → `E_INTERNAL` with the class name only.
- R3. Never put the SQL text, bound values, the URL, or a secret in any message, log record or return.

**Safety**
- R4. Callers select a **profile**; they never supply host, port, driver, URL, `connect_args` or credentials.
- R5. `query` never commits. `commit(` exists only inside `runner.run_execute`.
- R6. `execute` is refused unless the profile has `allow_execute: true`; apply the verb filter and `dry_run`.
- R7. The in-session read-only guard is best effort (it was **verified bypassable** on PostgreSQL). Never claim read-only is enforced by the module; the database role does that. Say so in docs and tests.
- R8. Hard caps for rows, bytes, time, retries live in code. Profile and call may only lower them.
- R9. Secrets are `sdb://` references in profiles; resolve late, hold in locals, mask in logs.

**SQLAlchemy / drivers**
- R10. SQLAlchemy Core only. No ORM, no reflection except `inspect()` in `table_list`.
- R11. Caller SQL goes through `text()` with bind params. Never `exec_driver_sql` for caller SQL.
- R12. Write casts as `CAST(:x AS type)`, never `:x::type`. Escape literal colons in strings as `\:`. (Both verified `text()` traps.)
- R13. Build URLs with `sqlalchemy.engine.URL.create`. Never f-string a URL.
- R14. One engine per call with `NullPool`; `dispose()` in `finally`.
- R15. Import each driver only inside `lib/dialects.py`, lazily. A missing driver affects only its dialect (`E_DRIVER_MISSING`).
- R16. Dependencies must be MIT/BSD/Apache-2.0, or LGPL as unmodified pip packages (owner decision 2026-10-29). **No GPL** (`mysqlclient`), no vendoring or patching of LGPL code. Ask before adding any runtime dependency.

**Process**
- R17. Test first for everything in `lib/`. `modules/sqlx_mod.py` stays thin.
- R18. If SPEC is wrong, change SPEC in the same commit. Record real database behaviour in `docs/WIRE_SHAPES.md`; do not guess exception classes or timeout mechanisms for dialects that have not been run.
- R19. Return plain JSON only (SPEC section 9). Drop kwargs whose key starts with `__`.
- R20. Never run Python with `src/saltext` as the working directory.

## Mistakes already observed (do not repeat)

| Mistake | What happens | Fix |
|---|---|---|
| Creating the PostgreSQL test database under the C locale (SQL_ASCII) | psycopg 3 returns `bytes`; SQLAlchemy crashes on first connect: `TypeError: cannot use a string pattern on a bytes-like object` | Create test databases as UTF8; wrap first-connect failures |
| `text("select :a::int")` | `:a` is not treated as a bind; the driver sees a literal `:a` and fails with a syntax error | `CAST(:a AS int)` |
| `text("select '{\"a\":1}'")` | `:1` read as a bind → `A value is required for bind parameter '1'` | write `\:` |
| `exec_driver_sql("... like '%b%'")` with psycopg | `only '%s', '%b', '%t' are allowed as placeholders` | use `text()` for caller SQL |
| Trusting `SET TRANSACTION READ ONLY` | caller SQL can `SET TRANSACTION READ WRITE` or `COMMIT` and write | read-only DB role |
| Using `postgres.psql_query`-style return-on-failure | the real error is lost | R1 |
| Treating `ProgrammingError` as a subclass of `OperationalError` (as Salt's `mysql.query` does) | syntax errors escape as raw tracebacks | catch `DBAPIError` |

## Phase order

T0 scaffold → T1 pin facts on real databases → T2–T6 libraries → T7 module → T8 checks → T9 execute → T10 Salt
integration → T11 security pass. After each task: run its gate, summarise the diff, stop.
