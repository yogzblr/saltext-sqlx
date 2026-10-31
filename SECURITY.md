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

**Oracle and SQL Server (observed by task T1b, 2026-10-31, `docs/WIRE_SHAPES.md` sections 14 and 15).** Oracle 23.26.2:
`SET TRANSACTION READ ONLY` as the first statement refuses writes (ORA-01456, `E_READ_ONLY`) and a later `SET TRANSACTION READ
WRITE` fails (ORA-01453), but **one PL/SQL block bypasses it**: `begin commit; insert ...; commit; end;` ends the read-only
transaction, writes and commits, and the row persisted (observed). SQL Server 2022 has **no in-session read-only at all**: a
write through `query` runs and is only rolled back, and a batch can carry its own `COMMIT` (`insert ...; commit` persisted the
row, observed). The module refuses a SQL Server batch with a `;` followed by another statement and a second result set
(`[E_POLICY]`), but T-SQL also runs `insert ... insert ...` without any separator, which no client-side check can see.

**The database role held on both**: account `ro` (SELECT on the Sakila schema only) was refused by the database for UPDATE and
DELETE (Oracle ORA-41900 `missing DELETE privilege`, SQL Server 229), CREATE TABLE (ORA-01031, 262) and a schema it was not granted
(ORA-00942, which reports the object as missing rather than forbidden; SQL Server 229), also through an `execute`-enabled profile
on that account (end to end, `tests-e2e`). Account `rw` used through a transaction that is rolled back left no row behind.

Therefore **every profile used by `query` must connect as a role that has only read privileges** (SELECT on the needed
schemas; no INSERT/UPDATE/DELETE/DDL; no EXECUTE on write-capable functions; no superuser; on Oracle no `ANY` system privileges such as `SELECT ANY TABLE`
beyond what is needed, and no `EXECUTE` on write-capable packages). `read_only: true` stays on as a backstop and as documentation of intent.

## 3. Threats and controls

| Threat | Control |
|---|---|
| Caller writes through `query` | Read-only DB role (section 2); `query` never commits |
| Caller uses `execute` without permission | `allow_execute` false by default; verb filter; `dry_run`; separate profile/role with write rights |
| Verb filter bypass (comments, CTEs, functions, batches) | Filter is best-effort only; the **role** limits what `execute` can do, so give the write profile minimal grants. A leading `/*!`, `/*M!` or nested comment is refused; a second statement after `;` is refused by the server on PostgreSQL (extended protocol), MySQL/MariaDB (no multi-statements) and Oracle (ORA-03405), and by the module on SQL Server (T1b). **Residual, observed:** SQL Server runs `insert ... delete ...` (no separator, no result set) as one batch; only the role stops the second statement |
| Statement lifts its own time cap, or a huge result exhausts minion memory | `statement_timeout` / `MAX_EXECUTION_TIME` / `max_statement_time` plus the PyMySQL `read_timeout` backstop; results are streamed so memory is bounded by `limit` / `max_bytes` (one very large value still costs about 2.7x its size on PostgreSQL, so give the role no access to such columns or select `left(col, n)`) |
| Caller points the minion at an arbitrary host (SSRF, credential relay) | By default profiles only: call arguments cannot set host, port, driver, URL or `connect_args` (D1). With the opt-in call-time parameters enabled, see the table "Call-time connection parameters" below |
| Secrets in the Salt job cache | `password` is `sdb://` only in production; `allow_inline_secrets` is not provided. The call never carries a secret (unless the operator enables a literal `password` from the call, which is refused by default, see below). Salt stores job args and returns on the master and event bus |
| SQL text and results in the job cache | They are stored by Salt. Prefer `params` for values, keep `row_limit` small, set `keep_jobs`, consider an external job cache disabled for this function. Treat results as data in the master's job cache for DPDP and sovereignty purposes |
| Error text leaking data | The module never adds the SQL text, `params`, the URL or a secret to a message, and never uses `str()` of the SQLAlchemy exception (it appends `[SQL: ...]` and `[parameters: ...]` with bound values; observed). `DETAIL` lines (for example `Key (email)=(x) already exists`) are excluded unless `error_detail: true`. **Limit, observed:** the server's own primary message can still echo a bound value (PostgreSQL 22P02, MySQL/MariaDB 1366) and, on MySQL/MariaDB, a row value (`Duplicate entry '...'`, 1062), which `error_detail: false` cannot remove. Keep secrets out of `params` and out of unique keys that the caller can probe |
| Driver option abuse (`ssl_context`, callables, local file read via `local_infile`, `allowLoadLocalInfile`) | Per-driver `connect_args` allowlist; reject anything not listed |
| Profile or caller points TLS at an arbitrary file (CA, certificate, key read from outside the intended directory; path traversal; symlink escape) | `ssl_certs_dir` is profile-only and must `realpath` under `sqlx:_policy:ssl_certs_dir_root` (no root configured: `[E_POLICY]`); `..` and relative paths refused; every file name in `ssl_files` is a plain name; each file's `realpath` must stay under the root and be a regular file. No path-bearing key is in any `query` / `connect_args` allowlist, and a profile cannot combine them with `ssl_certs_dir`. Everything is checked at profile load, before any network call (SPEC section 3.1; unit tests `tests/unit/lib/test_tls.py`) |
| Weak or silent downgrade of database TLS | With a directory, `ssl_mode` is `verify-full` (default) or `verify-ca`; `disable`, `prefer`, `allow` and `require` are refused. PostgreSQL gets `sslcertmode=disable` when no client pair is configured, so a stray `~/.postgresql/postgresql.crt` is never presented. The profile `query` cannot carry `sslmode` next to a directory |
| Private key readable by other users, or key material in output | Key files with group/world access are `[E_POLICY]` (owner-only, or 0640 when root-owned). File contents are never read by the module. Messages name files by plain name only; `tls.scrub` (applied in `engine`) removes paths from driver messages, which libpq otherwise quotes in full (observed). Functional tests check that no path or `BEGIN` text reaches the caller |
| Wrong trust anchor: a CA directory outside operator control | The root is minion configuration (`sqlx:_policy`), never a call argument; make the root and its directories writable by root only. The module does not check directory ownership or mode (open: SPEC D12) |
| Oracle and SQL Server TLS | **UNVERIFIED for Oracle** (T1b ran Oracle, but the Sakila image has no TCPS listener): the argument mapping (wallet directory, `tcps`, `ssl_server_dn_match`) is unit-tested and a profile without `ewallet.pem` is refused at load (observed end to end). SQL Server: `ssl_certs_dir` is refused (`not supported for mssql yet`, observed end to end); do not assume TLS verification there |
| Resource exhaustion | Row, byte, time and retry caps; hard maxima in code |
| Credential in logs/URLs | Build URLs with `URL.create`; never log a URL or password; masking filter (`secrets.MaskingFilter`, `secrets.mask`). Observed (T1): the account password did not appear in `str`, `repr`, `repr(orig)`, `repr(orig.args)` or the formatted traceback of any case on PostgreSQL, MySQL and MariaDB, nor in the Oracle and SQL Server connection-failure rows. T1b (end to end, all five databases, Salt 3008.5): no password or resolved `sdb://` value in the master job cache, the master and minion logs at debug level, the event bus, or any CLI, `salt-call` or salt-api output (`tests-e2e/test_zz_leaks.py`). Not examined: driver debug output |
| NUL character truncating a statement | psycopg and pymssql cut the statement text at a NUL (observed, T1b: `delete from t\x00 where id = 99999` deleted every row on PostgreSQL and SQL Server through raw SQLAlchemy). The module refuses a NUL in `sql` on every dialect and in a bound string on SQL Server (`[E_OPTION_INVALID]`); only salt-api/LocalClient can carry one (argv cannot) |
| Unreviewed LGPL code paths | LGPL drivers (psycopg, pymssql) are accepted as unmodified pip dependencies (owner decision 2026-10-29); do not vendor or patch them |
| Supply chain on an air-gapped host | Mirror wheels for each driver for Salt's Python version; pin versions; check hashes |

### Call-time connection parameters (opt-in, SPEC section 3.2, D1)

Off by default. Enabled only by `sqlx:_policy:allow_call_params` (explicit field names) in minion config or pillar; the policy
is never read from a call. Turning it on moves part of the trust from the minion's configuration to **whoever may publish a job
for `sqlx.*`**, so set `allowed_hosts` tightly and constrain the kwargs in `external_auth` (section 4).

| Threat | Control | State |
|---|---|---|
| SSRF: caller makes the minion connect to an internal or metadata endpoint | `allowed_hosts` (exact names, globs, IPs, CIDR) is **required** when `host` is overridable; a host outside it is `[E_POLICY]` before any socket; a name is resolved and **every** address must be allowed; `0.0.0.0/8`, loopback, `169.254.0.0/16` (metadata), multicast and the IPv6 equivalents are refused unless an IP/CIDR entry lies inside that range; host text with `@`, `/`, `#`, whitespace or NUL is refused; `allowed_ports`, `allowed_dialects` | verified: unit, `tests/security/test_call_params_surface.py` (counting sockets), functional (listener sees 0 connections), integration through the real minion |
| DNS rebinding / TOCTOU: the name resolves differently at check time and connect time | the driver resolves again when it connects | **residual, not mitigated.** Prefer IP/CIDR entries over names in `allowed_hosts` |
| Credential relay: caller aims a profile at a server it controls to collect the profile's password | the base profile's `password` is **not** sent when `host`, `port`, `username` or `dialect` is changed (unless the call supplies its own `password` or `inherit_password: true`, which the operator should not set); on PostgreSQL libpq is also told not to use `$PGPASSWORD` or `~/.pgpass` for that connection (found during testing: it otherwise answered the target's password request from the minion's environment); MySQL/Oracle/SQL Server drivers have no such environment fallback that was checked (**UNVERIFIED** for Oracle/SQL Server); `password` from a call must be an `sdb://` reference | verified with a fake PostgreSQL server that requests a cleartext password: the profile's password never arrives; the `sdb` value arrives only when the caller asked for it |
| Caller reads another secret: `password=sdb://vault/other` resolves any sdb key the minion can read | the caller can send a secret only to a host in `allowed_hosts`. **Limit:** any sdb URI the minion can resolve is usable, so scope the sdb backend, and do not list hosts the caller does not control as targets for it. Not mitigated by this extension | **open by design**; restrict `password` out of `allow_call_params` if callers must not choose credentials |
| Secrets in the job cache, event bus, minion debug log | a literal `password` is `[E_POLICY]` unless `allow_call_literal_password: true` (leaks into the job cache and event bus; do not enable). Messages name the key only. The extension never logs a password or `sdb://` reference; the audit line has override key **names** and `host:port` | verified: every error path with three secret forms, `tests/security`; integration: the resolved value is in no cache file, log or event; the **reference** and any literal the caller typed are in the job load, the event bus and Salt's own minion debug log (`Command details`), which the extension cannot prevent |
| Caller raises limits or removes safety | `row_limit`, `allow_execute`, `execute_verbs`, `error_detail`, `_policy` are not call keywords; `max_bytes`/`limit`/`timeout_ms`, `statement_timeout_ms`, `connect_timeout_s` only lower; `read_only` only `true`; `execute` still needs `allow_execute` on the base profile; an ad-hoc connection can never execute | verified: unit, functional (PostgreSQL, MySQL, MariaDB), integration |
| TLS weakening or file read through a call | `ssl_mode` may only tighten; `ssl_certs_dir` must resolve under `ssl_certs_dir_root` (same code as for profiles: realpath, `..`, key modes); `query`/`connect_args` go through the per-driver allowlist and cannot set TLS-owned keys; verification is never relaxed when the host is changed (`verify-full` checks the new host name) | verified on the TLS servers of `tests-support/tls` (PostgreSQL, MySQL, MariaDB): `tests/functional/test_call_params_tls.py` |
| Trusting a client-chosen CA directory | the root is minion configuration; a call can only choose a directory **under** it. Anything under the root is trusted: keep only trust anchors the operator accepts there | by design |
| Oracle and SQL Server overrides | same code path | **UNVERIFIED against a server:** unit tests only (host/port/dialect gates, merge rules); `ssl_certs_dir` for mssql is refused |

Not covered: the decision of *who* may call. `external_auth` must constrain the kwargs (next section); the minion policy is
the control for everything eauth omits.

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

### Call-time parameters and `external_auth`

With call-time parameters enabled, a `profile` regex alone no longer pins where a job connects. Verified through a real
master, `LocalClient` and `salt-api` (`tests/integration/params_path`, `file` eauth):

```yaml
external_auth:
  pam:
    sre_ops:
      - 'db-minion-*':
          - sqlx.ping:
              kwargs: {profile: '^orders_pg$', host: '^db1\.corp\.example$'}
          - sqlx.query:
              kwargs: {profile: '^orders_pg$', host: '^db1\.corp\.example$'}
```

* A `kwargs` rule requires every key it names to be **present** in the call (`k not in kwargs` refuses) and matches
  `re.match(rule, str(value))`: anchor with `^...$` (the shape tested; `\Z` instead of `$` also rejects a trailing newline, see above). So the rule above refuses `host=192.0.2.9` and
  `host=db1.corp.example.evil.example` (HTTP 401 / `AuthorizationError`) and also refuses a call that leaves `host` out, even
  when it carries `connection={"host": ...}`. If a job may legitimately omit `host`, add a second function entry.
* A call that gives `host` flat (to satisfy the rule) **and** inside `connection` is `[E_OPTION_INVALID]` on the minion, so
  `connection` cannot smuggle a second host. A rule on `connection` itself would match the Python `repr` of the dict; do not
  rely on it, pin the flat keywords and keep `connection` out of the policy field list where possible.
* **Keys that a rule does not name are unrestricted by eauth** (verified: a user whose rule pins only `profile` can send
  `host=192.0.2.9` through eauth; the minion policy then refuses it with `[E_POLICY]`). Pin `host`, `port`, `username`,
  `password`, `dialect`, `database` and `connection` for every user and function that matters, and treat the minion-side
  `allowed_hosts` and `allow_call_params` as the control that holds when a rule is incomplete.

## 5. Audit

Log one line per call at INFO: function, profile, dialect, statement SHA-256 prefix (first 12 hex), row count or
`rows_affected`, elapsed ms, outcome code. Never log the SQL text, params, rows or secrets. Include the Salt `__pub_user`
when present so actions are attributable. With call-time parameters the line also carries `overrides=<field names>` (names only, `-` when none) and `target=<host>:<port>` actually used; never a value, a password or an `sdb://` reference.

## 6. Pre-release checklist

State after the T11 review (2026-10-29, `docs/SECURITY_REVIEW.md`), updated by T1b (2026-10-31). Evidence is in `tests/security/`
and `tests-e2e/`; run `python -m pytest tests/security -q`, `python tests/security/licences.py --check` and, with
`tests-e2e/up.sh` running, `python -m pytest tests-e2e -m e2e`. T11: PostgreSQL 16.15, MySQL 8.0.46 and MariaDB 10.11.14 were
live. T1b: all five (PostgreSQL 18.4, MySQL 9.7.1, MariaDB 12.3.2, Oracle 23.26.2, SQL Server 2022 CU25) through a real Salt
master, minions and salt-api.

- [x] A read-only role test on every supported dialect proves writes fail at the database (not in the module)
  Proven on PostgreSQL, MySQL and MariaDB (`test_readonly_role.py`: guard off, the `ro` account is refused by the server for
  UPDATE/DELETE/TRUNCATE/DROP/ALTER/CREATE/GRANT, and the section 2 bypass sequences still fail at the role). Oracle and SQL
  Server (T1b): the `ro` account is refused for UPDATE, DELETE, CREATE TABLE and an ungranted schema (WS 14, 15), and an
  `execute`-enabled profile on `ro` gets the database's refusal end to end (`tests-e2e/test_e2e_errors.py`).
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
- [x] The Oracle and SQL Server probes have been run (T1b) and section 2 and SPEC sections 7-8 updated from them
  `tests-e2e/probe_wire.py` against the Sakila Oracle and SQL Server images (the T1 probes in `scripts/probes/` expect the
  T1 seed schema and were not used); evidence `docs/wire_shapes/t1b/`, `docs/WIRE_SHAPES.md` sections 14-15. Oracle TLS
  (no TCPS listener) remains unverified.
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
