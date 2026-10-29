# saltext-sqlx — SECURITY

## 1. Posture

This module runs **arbitrary SQL chosen by the caller** (a human, or an AI agent through an MCP server). It cannot make
arbitrary SQL safe. Safety comes from four places, in this order: the database role, the profile allowlist, Salt eauth,
and the module's limits. The module is a convenience and a second line of defence, not the boundary.

## 2. Verified fact: the in-session read-only guard is bypassable

Observed on three databases (task T1, 2026-10-29; full tables in `docs/WIRE_SHAPES.md` section 3). The same results were
obtained with SQLAlchemy 2.0.55 and 2.1.6. Test account `rw` (DML rights), a guard statement, then caller SQL through the
same connection, then `INSERT` and `COMMIT`; "persisted" was read back on a second connection.

**PostgreSQL 16.15** (psycopg 3.3.6), after `SET TRANSACTION READ ONLY`:

| Caller SQL sequence | Result |
|---|---|
| `INSERT` | refused, SQLSTATE 25006 |
| `SET TRANSACTION READ WRITE` then `INSERT` | **write succeeded and persisted** |
| `COMMIT` then `INSERT` | **write succeeded and persisted** |
| connection option `default_transaction_read_only=on`, then `SET TRANSACTION READ WRITE`, then `INSERT` | **write succeeded and persisted** |

**MySQL 8.0.46 and MariaDB 10.11.14** (PyMySQL 1.2.3). Every guard refused a plain `INSERT` (error 1792, SQLSTATE 25006).
Bypasses (W = write succeeded and persisted, R = refused; MySQL / MariaDB where they differ):

| Guard statement | `SET SESSION TRANSACTION READ WRITE` | `SET TRANSACTION READ WRITE` | `START TRANSACTION READ WRITE` | `SET SESSION transaction_read_only=0` | `SET SESSION tx_read_only=0` | `COMMIT` then write |
|---|---|---|---|---|---|---|
| `SET SESSION TRANSACTION READ ONLY` (the `init_command` variant gave the same result in the first five columns; `COMMIT` not tested for it) | W | W | W | W / R (variable unknown, 1193) | R (unknown, 1193) / W | R |
| `SET TRANSACTION READ ONLY` (next transaction only) | W | W | W | W / R (1193) | R (1193) / W | W |
| `START TRANSACTION READ ONLY` | R | R (error 1568) | **W** | R (1792) / R (1193) | R (1193) / R (1792) | **W** |

Every guard tested on MySQL and MariaDB is therefore bypassable by a statement a caller can send through `query`.
`START TRANSACTION READ ONLY` resisted the most variants and still fell to `START TRANSACTION READ WRITE` and to
`COMMIT` followed by a write.

**The database role did hold** on PostgreSQL, MySQL and MariaDB: account `ro` was refused by the database for INSERT, UPDATE, DELETE
and CREATE TABLE, and for SELECT in a schema it was not granted (PostgreSQL 42501; MySQL and MariaDB 1142). Account `rw`
used through a transaction that is rolled back left no row behind.

**Oracle and SQL Server: NOT RUN.** No server could be started (Docker Hub, MCR and the Oracle registry returned HTTP 403 in
the build environment), so neither `SET TRANSACTION READ ONLY` on Oracle nor the role behaviour on either was observed.
Treat them as unprotected by the guard until observed. The owner must run them on a machine with registry access, with
`tests-support/up.sh` and `scripts/probes/` (task T1b).

Therefore **every profile used by `query` must connect as a role that has only read privileges** (SELECT on the needed
schemas; no INSERT/UPDATE/DELETE/DDL; no EXECUTE on write-capable functions; no superuser; on Oracle no `ANY` system privileges such as `SELECT ANY TABLE`
beyond what is needed, and no `EXECUTE` on write-capable packages). `read_only: true` stays on as a backstop and as documentation of intent.

## 3. Threats and controls

| Threat | Control |
|---|---|
| Caller writes through `query` | Read-only DB role (section 2); `query` never commits |
| Caller uses `execute` without permission | `allow_execute` false by default; verb filter; `dry_run`; separate profile/role with write rights |
| Verb filter bypass (comments, CTEs, functions) | Filter is best-effort only; the **role** limits what `execute` can do, so give the write profile minimal grants. A leading `/*!`, `/*M!` or nested comment is refused; a second statement after `;` is refused by the driver on every dialect (extended protocol on PostgreSQL, no multi-statements on MySQL/MariaDB) |
| Statement lifts its own time cap, or a huge result exhausts minion memory | `statement_timeout` / `MAX_EXECUTION_TIME` / `max_statement_time` plus the PyMySQL `read_timeout` backstop; results are streamed so memory is bounded by `limit` / `max_bytes` (one very large value still costs about 2.7x its size on PostgreSQL, so give the role no access to such columns or select `left(col, n)`) |
| Caller points the minion at an arbitrary host (SSRF, credential relay) | Profiles only; call arguments cannot set host, port, driver, URL or `connect_args` (D1) |
| Secrets in the Salt job cache | `password` is `sdb://` only in production; `allow_inline_secrets` is not provided. The call never carries a secret. Salt stores job args and returns on the master and event bus |
| SQL text and results in the job cache | They are stored by Salt. Prefer `params` for values, keep `row_limit` small, set `keep_jobs`, consider an external job cache disabled for this function. Treat results as data in the master's job cache for DPDP and sovereignty purposes |
| Error text leaking data | The module never adds the SQL text, `params`, the URL or a secret to a message, and never uses `str()` of the SQLAlchemy exception (it appends `[SQL: ...]` and `[parameters: ...]` with bound values; observed). `DETAIL` lines (for example `Key (email)=(x) already exists`) are excluded unless `error_detail: true`. **Limit, observed:** the server's own primary message can still echo a bound value (PostgreSQL 22P02, MySQL/MariaDB 1366) and, on MySQL/MariaDB, a row value (`Duplicate entry '...'`, 1062), which `error_detail: false` cannot remove. Keep secrets out of `params` and out of unique keys that the caller can probe |
| Driver option abuse (`ssl_context`, callables, local file read via `local_infile`, `allowLoadLocalInfile`) | Per-driver `connect_args` allowlist; reject anything not listed |
| Profile or caller points TLS at an arbitrary file (CA, certificate, key read from outside the intended directory; path traversal; symlink escape) | `ssl_certs_dir` is profile-only and must `realpath` under `sqlx:_policy:ssl_certs_dir_root` (no root configured: `[E_POLICY]`); `..` and relative paths refused; every file name in `ssl_files` is a plain name; each file's `realpath` must stay under the root and be a regular file. No path-bearing key is in any `query` / `connect_args` allowlist, and a profile cannot combine them with `ssl_certs_dir`. Everything is checked at profile load, before any network call (SPEC section 3.1; unit tests `tests/unit/lib/test_tls.py`) |
| Weak or silent downgrade of database TLS | With a directory, `ssl_mode` is `verify-full` (default) or `verify-ca`; `disable`, `prefer`, `allow` and `require` are refused. PostgreSQL gets `sslcertmode=disable` when no client pair is configured, so a stray `~/.postgresql/postgresql.crt` is never presented. The profile `query` cannot carry `sslmode` next to a directory |
| Private key readable by other users, or key material in output | Key files with group/world access are `[E_POLICY]` (owner-only, or 0640 when root-owned). File contents are never read by the module. Messages name files by plain name only; `tls.scrub` (applied in `engine`) removes paths from driver messages, which libpq otherwise quotes in full (observed). Functional tests check that no path or `BEGIN` text reaches the caller |
| Wrong trust anchor: a CA directory outside operator control | The root is minion configuration (`sqlx:_policy`), never a call argument; make the root and its directories writable by root only. The module does not check directory ownership or mode (open: SPEC D12) |
| Oracle and SQL Server TLS | **UNVERIFIED.** Oracle: the argument mapping (wallet directory, `tcps`, `ssl_server_dn_match`) is unit-tested but was never run against a server (T1b). SQL Server: `ssl_certs_dir` is refused (`not supported for mssql yet`); do not assume TLS verification there |
| Resource exhaustion | Row, byte, time and retry caps; hard maxima in code |
| Credential in logs/URLs | Build URLs with `URL.create`; never log a URL or password; masking filter (`secrets.MaskingFilter`, `secrets.mask`). Observed (T1): the account password did not appear in `str`, `repr`, `repr(orig)`, `repr(orig.args)` or the formatted traceback of any case on PostgreSQL, MySQL and MariaDB, nor in the Oracle and SQL Server connection-failure rows. Not examined: successful-connect logging and driver debug output. Oracle and SQL Server server-side cases: **NOT RUN** (owner task T1b) |
| Unreviewed LGPL code paths | LGPL drivers (psycopg, pymssql) are accepted as unmodified pip dependencies (owner decision 2026-10-29); do not vendor or patch them |
| Supply chain on an air-gapped host | Mirror wheels for each driver for Salt's Python version; pin versions; check hashes |

## 4. Authorization in Salt

Use `external_auth` to restrict who may call which function and which profile. Salt supports regex limits on `args` and
`kwargs`; arguments you do not list are unrestricted, so list `profile` explicitly:

```yaml
external_auth:
  pam:
    sre_ops:
      - 'db-minion-*':
          - sqlx.query:
              kwargs: {profile: '^(orders_pg|hr_oracle)$'}
          - sqlx.ping
    dba_writers:
      - 'db-minion-*':
          - sqlx.execute:
              kwargs: {profile: '^orders_pg_rw$'}
```

Give `execute` its own function entry and its own profile with a different DB account, so that read access never implies
write access.

Verified through `salt-api` (T11, `tests/security/test_eauth_salt_api.py`, `auto` eauth with these rule shapes; see
`docs/SECURITY_REVIEW.md` section 5): a call whose `profile` keyword matches the regex runs (HTTP 200); any other profile, a
profile passed positionally, a missing profile, an unlisted function and an unlisted user are all refused (HTTP 401). Two
cautions from that run: an entry such as `- sqlx.ping` without `kwargs` is unrestricted and pings every profile, so give it a
`profile` regex too if that matters; and Python's `$` also matches before a trailing newline, so `^(ro)$` lets `profile="ro\n"`
through to the module, which then refuses it as an unknown profile (use `\Z` if you want the regex alone to decide).

## 5. Audit

Log one line per call at INFO: function, profile, dialect, statement SHA-256 prefix (first 12 hex), row count or
`rows_affected`, elapsed ms, outcome code. Never log the SQL text, params, rows or secrets. Include the Salt `__pub_user`
when present so actions are attributable.

## 6. Pre-release checklist

State after the T11 review (2026-10-29, `docs/SECURITY_REVIEW.md`). Evidence is in `tests/security/`; run
`python -m pytest tests/security -q` and `python tests/security/licences.py --check`. PostgreSQL 16.15, MySQL 8.0.46 and MariaDB
10.11.14 were live; Oracle and SQL Server were NOT RUN.

- [ ] A read-only role test on every supported dialect proves writes fail at the database (not in the module)
  Proven on PostgreSQL, MySQL and MariaDB (`test_readonly_role.py`: guard off, the `ro` account is refused by the server for
  UPDATE/DELETE/TRUNCATE/DROP/ALTER/CREATE/GRANT, and the section 2 bypass sequences still fail at the role).
  **Exception: Oracle and SQL Server have no server in the build environment (T1b); tick when the owner has run the same test.**
- [x] Source scan: `commit(` appears only in `runner.run_execute`
  `test_source_scan.py::test_commit_only_in_run_execute` (AST scan; also no `begin`, `autocommit`, `isolation_level`).
- [x] No `exec_driver_sql` on caller SQL
  `test_source_scan.py`: every `exec_driver_sql` argument is a constant or an `int()`-formatted f-string; caller SQL goes through
  `text()` with binds.
- [x] Secret never appears in any log record, exception, or return (parametrised over every error path)
  `test_secret_leak.py` (63 cases, three live servers, password full of metacharacters, `error_detail` true and false, `%s`/`{}`
  in the SQL). Oracle and SQL Server: client-side error paths only. Documented exception unchanged: the server's own message
  can echo a bound value (PG 22P02, MySQL/MariaDB 1366), scrubbed when `error_detail` is false.
- [x] Call arguments cannot set host/port/driver/URL/connect_args
  `test_call_surface.py`: every connection keyword is refused with `E_OPTION_UNKNOWN` and no socket is opened; dunder keys are
  dropped; `params={"host": ...}` is only a bind.
- [x] `connect_args` allowlist reviewed per driver
  `test_connect_args.py`: allowlist snapshot for psycopg, PyMySQL, oracledb and pymssql; 60 dangerous keys refused in
  `connect_args` and `query` on every dialect; values must be scalars; `URL.create` keeps host/port/user injection literal.
- [x] `error_detail: false` verified not to leak `DETAIL` on a unique violation (PostgreSQL only; on MySQL/MariaDB the value is in the primary message, so document it instead)
  `test_secret_leak.py::test_unique_violation_detail_off_vs_on`: with `false` the value is absent on all three servers
  (`Duplicate entry '...'` on MySQL/MariaDB); with `true` it is present on PostgreSQL by design.
- [ ] The Oracle and SQL Server probes have been run by the owner (T1b) and section 2 and SPEC sections 7-8 updated from them
  **Exception: open. Docker Hub, MCR and the Oracle registry returned HTTP 403 in the build environment. Owner action
  (`tests-support/up.sh`, then `scripts/probes/`).**
- [x] eauth regex example from section 4 tested through `salt-api`
  `test_eauth_salt_api.py` (19 cases) with `auto` eauth, not PAM (no system accounts in the build environment; the
  authorisation engine is shared). Results and two cautions are in section 4 above.
- [x] Dependency licences listed in `NOTICE` (MIT, BSD, Apache-2.0, LGPL accepted)
  `NOTICE` lists the declared dependencies and the full closure from real package metadata; `test_licences.py` fails on any GPL
  or AGPL and on any LGPL outside `psycopg`, `psycopg-binary`, `pymssql`. No GPL present.

### Defects found by the T11 review (fixed)

D1 to D10 from `docs/SECURITY_REVIEW.md` section 3 are fixed on branch `fix/hardening-t11`; the status of each, with the
before/after measurements, is in section 3 of that file. D11 is a documented design point: `dry_run=None` means "not given"
and the statement is **committed**. The remaining limits are the ones this file already states: the in-session read-only
guard is bypassable, so the DB role is the control; and a client-side timeout does not stop the server's work.
