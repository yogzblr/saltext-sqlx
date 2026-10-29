# Security review (task T11)

Hostile review of `saltext.sqlx` at `origin/build/base` (`cd2891a`), 2026-10-29. Everything below was run against live servers
(PostgreSQL 16.15 on 127.0.0.1:54329, MySQL 8.0.46 on 33061, MariaDB 10.11.14 on 33062; SQLAlchemy 2.0.55, psycopg 3.3.6,
PyMySQL 1.2.3, Salt 3008.5, CPython 3.13) through the PUBLIC module functions with an injected `__salt__`, and is reproducible:

```bash
python -m pytest tests/security -q -s      # 724 passed, 109 skipped (server-specific variants), 36 xfailed (strict)
python tests/security/licences.py --check  # exit 0; exit 1 on any GPL/AGPL or unaccepted LGPL
```

`-s` prints one `OBSERVED|class|label|outcome` line per attempt; the tables below are built from those lines. The 36 `xfail`
(strict) tests are the reported defects: each starts passing (and fails the run) the day the defect is fixed, which is the
signal to delete its `defect(...)` marker. Defect numbers `D1`-`D10` in this file are this review's own and are NOT the open
decisions `D1`-`D11` of SPEC.md.

Not run: **Oracle and SQL Server** (no servers, task T1b). Their client-side paths (profile validation, `connect_args` allowlist,
secret scrubbing before any socket) are covered; every server-side statement on them is untested.

## 1. Read-only role: what the DATABASE itself refuses (module guard switched off)

The `ro` account (SELECT on `scratch.*` only), the module's read-only guard bypassed with `read_only=False` on a throwaway
profile, so every refusal below comes from the server. Test: `tests/security/test_readonly_role.py`. A positive control proves
the same statements work for an account with the privilege, and that 0 rows changed after each attempt.

| Statement as `ro` | PostgreSQL 16 | MySQL 8.0 | MariaDB 10.11 |
|---|---|---|---|
| `UPDATE` / `DELETE scratch.orders` | 42501 permission denied for table | 1142 command denied | 1142 command denied |
| `TRUNCATE` | 42501 permission denied | 1142 (needs DROP) | 1142 (needs DROP) |
| `DROP TABLE` / `ALTER TABLE` | 42501 must be owner | 1142 | 1142 |
| `CREATE INDEX` | 42501 must be owner | 1142 (INDEX) | 1142 (INDEX) |
| `CREATE TABLE` / `CREATE VIEW` | 42501 permission denied for schema | 1142 | 1142 |
| `CREATE TEMPORARY TABLE` | NOT refused (PUBLIC has TEMP on the database; session-local, gone at disconnect) | 1044 | 1044 |
| `CREATE DATABASE` | not tested | 1044 | 1044 |
| `CREATE ROLE` / `CREATE USER` | 42501 permission denied to create role | 1227 needs CREATE USER | 1227 |
| `GRANT ALL ... TO ro` | not tested | 1142 | 1142 |
| `SELECT` from `private.secrets` (other schema) | 42501 permission denied for schema | 1142 | 1142 |

Operator guidance that follows: the only thing the role does NOT stop on PostgreSQL is TEMP-table creation; `REVOKE TEMPORARY ON
DATABASE ... FROM PUBLIC` closes it if that matters.

### SECURITY.md section 2, re-run with `ro` on all three servers

Each bypass is sent through `sqlx.query` with the guard ON, then an `INSERT` is attempted in the same call path. Test:
`test_readonly_role.py::test_section_2_bypass_sequences_still_fail_at_the_role`.

| Bypass statement | PostgreSQL | MySQL | MariaDB | Then `INSERT` as `ro` |
|---|---|---|---|---|
| `COMMIT` | OK | OK | OK | refused by the role (42501 / 1142) |
| `SET TRANSACTION READ WRITE` | OK | OK | OK | refused by the role |
| `START TRANSACTION READ WRITE` | OK | OK | OK | refused by the role |
| `SET SESSION TRANSACTION READ WRITE` | n/a | OK | OK | refused by the role |
| `SET SESSION transaction_read_only = 0` | n/a | OK | 1193 unknown variable | refused by the role |
| `SET SESSION tx_read_only = 0` | n/a | 1193 unknown variable | OK | refused by the role |
| `SET default_transaction_read_only = off` + `SET TRANSACTION READ WRITE` | OK, OK | n/a | n/a | refused by the role |

Result: the guard IS bypassable on all three (section 2 stands) and the role is what holds. 0 rows persisted.

## 2. Attack log

Legend: REFUSED = module rejected before any socket; DB = refused by the server; HELD = attempt ran but nothing leaked or
persisted; DEFECT = reproduced, see section 3.

### Class 1: make `sqlx.query` persist a write as `rw` (profile `rw_query`: `rw` account, `read_only: true`, guard on)

| # | Attack | PG | MySQL | MariaDB |
|---|---|---|---|---|
| 1.1 | `INSERT` direct | HELD (25006) | HELD (1792) | HELD (1792) |
| 1.2 | `COMMIT; INSERT ...` multi-statement string | **DEFECT D1**: row persisted | HELD (1064: multi-statements off in PyMySQL) | HELD (1064) |
| 1.3 | `SET TRANSACTION READ WRITE; INSERT` | **DEFECT D1** | HELD (1064) | HELD (1064) |
| 1.4 | data-modifying CTE `WITH i AS (INSERT ... RETURNING)` | HELD (25006) | n/a | n/a |
| 1.5 | `SELECT INTO`, `CREATE TABLE AS` | HELD (25006) | n/a | n/a |
| 1.6 | `COPY FROM PROGRAM`, `COPY TO PROGRAM`, `COPY FROM/TO file` | DB 42501 (x4) | n/a | n/a |
| 1.7 | `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_import`, `lo_export` | DB 42501 (x5) | n/a | n/a |
| 1.8 | `CREATE EXTENSION dblink` | HELD (25006) | n/a | n/a |
| 1.9 | `ALTER SYSTEM`, `pg_reload_conf`, `pg_terminate_backend`, `SET ROLE postgres`, `SET SESSION AUTHORIZATION` | HELD / DB 42501 | n/a | n/a |
| 1.10 | `SELECT fn()` / `CALL proc()` whose body inserts | HELD (25006) | HELD (1792) | HELD (1792) |
| 1.11 | `SELECT nextval(seq)` (non-transactional) | DB 42501 (no USAGE) | n/a | n/a |
| 1.12 | MyISAM table insert (non-transactional) | n/a | HELD (1792) | HELD (1792) |
| 1.13 | `SELECT ... INTO OUTFILE/DUMPFILE`, `LOAD DATA [LOCAL] INFILE` | n/a | DB 1045 / 3948 (local_infile off) | DB 1045; `LOCAL` 4166 |
| 1.14 | `CREATE TEMPORARY TABLE`, `LOCK TABLES` | n/a | DB 1044 | DB 1044 |
| 1.15 | `ALTER ROLE ... PASSWORD` / `SET PASSWORD` after `COMMIT;` (account takeover of a throwaway account) | **DEFECT D1** | guard ON: HELD (1064 multi-statement). Guard OFF profile: the implicit-commit DDL persists (documented, `test_documented_mysql_guard_off_profile_lets_account_ddl_persist`) | same as MySQL |
| 1.16 | `CALL proc_without_EXECUTE_grant` | n/a | DB 1370 | DB 1370 |
| 1.17 | NUL byte inside the SQL | **DEFECT D9** | no defect (test passes) | no defect (test passes) |

Important: in 1.2/1.3/1.15 the DEFECT only changes what an `rw` account can already do; with the `ro` account every variant
failed at the role (section 1). The persisted rows were verified by an admin connection and dropped.

### Class 2: leak a password or a bound value

Test file `test_secret_leak.py` (63 cases) with a throwaway account whose password is `Zq7{0}%s(.*)[a-z]+?|^$&=;@/#!~`, the
bound value `harness.BOUND`, and `%s` / `{}` inside the SQL. Collected: exception text, `repr`, `args`, the whole `__cause__` /
`__context__` chain, `traceback.format_exception`, every log record (message, formatted exception, extra attributes), the
return value. Each path was run with `error_detail` false and true.

| Path | Result |
|---|---|
| wrong password (auth failure), host refused, unknown database | HELD, no password/URL/host:port detail |
| `sdb://` secret missing / invalid profile / invalid option | HELD |
| SQL syntax error with the bound value in the statement | HELD, no `[SQL:` / `[parameters:` fragments |
| SQL text containing `%s` and `{}` | HELD (no format-string expansion, no KeyError/IndexError) |
| unique violation, `error_detail: false` | HELD: value absent on PG (DETAIL dropped) and on MySQL/MariaDB (`Duplicate entry '...'`) |
| unique violation, `error_detail: true` | value present on PG by design (DETAIL); MySQL/MariaDB keep it in the primary message |
| invalid-input errors (PG 22P02, MySQL 1366) echo the bound value | the server's own message; scrubbed for `error_detail: false`, documented exception (SECURITY section 3) |
| timeout, read-only, internal, policy, disabled, driver missing | HELD |
| Oracle / SQL Server client-side errors (no server) | HELD |
| audit log line (the single `log.info`) | HELD: keys only `profile/user/sql_sha/rows/elapsed`, no SQL, no params, no password |
| `repr(exc)` / chain / `__traceback__` frames | HELD: no secret in locals shown by `format_exception` |

No leak found. Source scan (`test_source_scan.py`) additionally proves the one log call and the fixed `info` dict keys.

### Class 3: reach a host or database that is not in a profile

| # | Attack | Result |
|---|---|---|
| 3.1 | every connection keyword as a call argument: `host`, `port`, `dialect`, `driver`, `url`, `dsn`, `database`, `username`, `password`, `connect_args`, `query`, `engine_kwargs`, `ssl*`, `sdb`, ... | REFUSED `E_OPTION_UNKNOWN`, zero sockets opened (socket spy) |
| 3.2 | dunder keys (`__pub_user`, `__env__`) in kwargs | dropped, never reach the options |
| 3.3 | `params={"host": ...}` | a bind name only, no effect |
| 3.4 | positional profile names that are not configured, `__proto__`, path-like names | REFUSED `E_PROFILE_UNKNOWN` |
| 3.5 | `USE private` as `ro` (MySQL/MariaDB) | DB 1044 |
| 3.6 | `connect_args` / `query` in a profile: 60 dangerous keys per dialect (`host`, `hostaddr`, `service`, `passfile`, `sslrootcert`, `local_infile`, `init_command`, `unix_socket`, `read_default_file`, `dsn`, `TNS_ADMIN`, `ODBCINI`, ...) | REFUSED `E_POLICY` on postgresql, mysql, oracle, mssql |
| 3.7 | URL injection through `database` / `username` fields (`db?host=evil`, `u@evil:1`) | HELD: `URL.create` keeps them literal on all three servers |
| 3.8 | `application_name` with `host= port= dbname=` | HELD: one literal value |
| 3.9 | MySQL `charset` with SQL metacharacters | refused by the driver (`E_INTERNAL AttributeError`, see D7 note below) |
| 3.10 | `LOAD DATA LOCAL INFILE` with the server flag on (MariaDB) | DB 4166 (client has `local_infile` off) |
| 3.11 | `dblink_connect('host=127.0.0.1 port=3306')` as `ro` (the DB server opens the socket) | DB `E_CONNECT 08001`; needs the extension (not installed for `ro`); operator note: `dblink`/`postgres_fdw` grant is a lateral-movement path |
| 3.12 | eauth: profile as a positional argument | denied (HTTP 401), see section 5 |

### Class 4: unbounded results, hangs past the caps

Child-process probes (`probe_child.py`, peak RSS and wall time).

| # | Attack | Result |
|---|---|---|
| 4.1 | PG `pg_sleep(4)`, `timeout_ms=800` | HELD 0.8 s `E_TIMEOUT 57014` |
| 4.2 | MariaDB `SLEEP(4)`, 800 ms | HELD 0.8 s `E_TIMEOUT 1969` |
| 4.3 | MySQL `SELECT SLEEP(4)`, 800 ms | HELD 0.8 s; MySQL returns `SLEEP=1`, no error (server quirk) |
| 4.4 | `BENCHMARK(3e8,MD5)` | cut at 0.8 s (returns 0) on MySQL and MariaDB |
| 4.5 | PG `SET statement_timeout = 0; SELECT pg_sleep(3)` | **DEFECT D1**: ran 3.0 s past the 0.8 s cap |
| 4.6 | PG `set_config('statement_timeout','0')` in a single statement | HELD 0.8 s |
| 4.7 | MySQL `/*+ MAX_EXECUTION_TIME(100000) */ SELECT SLEEP(3)` | **DEFECT D2**: ran 3.0 s |
| 4.8 | MariaDB `SET STATEMENT max_statement_time=0 FOR SELECT SLEEP(3)` | **DEFECT D2**: ran 3.0 s |
| 4.9 | MySQL `INSERT ... SELECT SLEEP(2)` under an 0.8 s cap | not cut (2.0 s): `MAX_EXECUTION_TIME` covers SELECT only (part of D2; MariaDB cuts it) |
| 4.10 | PG `generate_series(1, 10000000)` with `limit` default 500 | rows 500, `truncated`, but peak RSS 363 MB: **DEFECT D3** |
| 4.11 | MariaDB `seq_1_to_8000000` | rows 500, peak RSS 786 MB, 12.5 s: **DEFECT D3** |
| 4.12 | PG `repeat('x', 100000000)` one 100 MB value | result dropped (`row_count 0`, `truncated`) but peak RSS 532 MB: **DEFECT D3** |
| 4.13 | `limit` = 10**30, -1, 0, NaN, `max_bytes` 0 / 1 / 10**30, `timeout_ms` 1 / huge | all clamped or `E_OPTION_INVALID`, no crash |
| 4.14 | `table_list(pattern='%a%a%a%a%a%a%a%a%a%a%a%ab')` vs a 54-character table name (PG) | **DEFECT D4**: still running after 6 s (killed) |
| 4.15 | `check_row_count(retry_timeout_s=1, retry_pause_s=1e-9)` | **DEFECT D10**: 100 / 31 / 49 connections in 1 s (PG / MySQL / MariaDB) |
| 4.16 | `retry_timeout_s` NaN / negative / inf, `retry_pause_s` 0 / NaN | REFUSED `E_OPTION_INVALID` (inf is capped by the hard maximum) |

### Class 5: bypass the `execute` policy (profile `rw`, `allow_execute: true`, verbs per row)

| # | Attack | PG | MySQL | MariaDB |
|---|---|---|---|---|
| 5.1 | `execute` on a profile without `allow_execute` | REFUSED `E_DISABLED` | same | same |
| 5.2 | pass `allow_execute`, `execute_verbs`, `dialect`, `read_only` as call keywords | REFUSED `E_OPTION_UNKNOWN` | same | same |
| 5.3 | `execute_verbs: []` and `INSERT` | REFUSED `E_POLICY` (x3 servers) | | |
| 5.4 | verbs `[INSERT]`: `DELETE`, `DROP`, `TRUNCATE`, `CALL`, `BEGIN`, `COMMIT`, `SET`, `SELECT`, `WITH ... DELETE`, `INSERTX`, `INSERT_` | REFUSED `E_POLICY` | same | same |
| 5.5 | full-width `INSERT`, backtick/quoted verb, `(INSERT ...)`, NUL-byte prefix, empty / comment-only / unterminated comment | REFUSED | same | same |
| 5.6 | lowercase, tabs/newlines, `-- c\n INSERT`, `/* c */ INSERT`, `/*+ hint */ INSERT` | allowed (legit) | allowed | allowed |
| 5.7 | `INSERT ...; DELETE ...` with verbs `[INSERT]` | **DEFECT D1**: DELETE ran | HELD (1064) | HELD (1064) |
| 5.8 | nested block comment `/* /* */ INSERT */ DELETE FROM x` with verbs `[INSERT]` | **DEFECT D5**: DELETE ran | n/a | n/a |
| 5.9 | executable comment `/*! INSERT INTO y VALUES (8,8) ON DUPLICATE KEY */ UPDATE v = 5` with verbs `[UPDATE]` | n/a | **DEFECT D5**: INSERT ran | **DEFECT D5** (also `/*!50000 */`, `/*M! */`) |
| 5.10 | `dry_run=True` + `INSERT ...; COMMIT` | **DEFECT D1**: row committed, result says `committed: false` | HELD (1064) | HELD (1064) |
| 5.11 | `dry_run` = `'false'`, `'False'`, `'0'`, `''`, `' '`, `0`, `0.0`, `1`, `[]`, `{}`, `['x']` | REFUSED `E_OPTION_INVALID` (strictly boolean) | same | same |
| 5.12 | `dry_run=None` | treated as not given, default false, statement COMMITTED (design note D11, intended) | same | same |
| 5.13 | socket spy: refused policy paths never connect | HELD | HELD | HELD |

#### Coordinator request: MySQL/MariaDB executable comments against the verb filter

Setup: `rw` account, `allow_execute: true`, `execute_verbs: [INSERT, UPDATE, DELETE]` (and `[UPDATE]` / `[INSERT]` for the hiding
cases), tables `scratch.t11_x`, `scratch.t11_y` (one row each). Multi-statement execution is OFF on PyMySQL; confirmed: every
`;` smuggling attempt returns `1064 You have an error in your SQL syntax`. State is read back with an admin connection.

| Statement sent | The filter sees | MySQL 8.0.46 | MariaDB 10.11.14 |
|---|---|---|---|
| `/*!50000 DROP TABLE t11_x */ UPDATE t11_y SET v = 9 WHERE id = 1` | `UPDATE` (allowed) | server runs `DROP TABLE t11_x UPDATE ...` -> 1064, nothing ran, tables unchanged | same |
| `UPDATE t11_y SET v = 9 WHERE id = 1 /*! ; DELETE FROM t11_x */` | `UPDATE` | 1064 (server sees `; DELETE` but multi-statements are off), nothing ran | same |
| `/*! DELETE FROM t11_x */ INSERT INTO t11_y VALUES (7,7)` | `INSERT` | 1064 (`DELETE FROM t11_x INSERT ...`), nothing ran | same |
| `/*! DELETE */ FROM t11_x WHERE id = 1` | `FROM` | REFUSED by the filter (`verb 'FROM'`) | same |
| `/*!50000 DELETE FROM t11_x WHERE id = 1 */ /* c */ ;` | nothing | REFUSED by the filter (does not start with a keyword) | same |
| leading `# comment\nINSERT ...` | `#` is not skipped | REFUSED by the filter (fails closed, but a legitimate MySQL comment style is unusable) | same |
| `/*! INSERT INTO t11_y VALUES (8,8) ON DUPLICATE KEY */ UPDATE v = 5` with verbs `[UPDATE]` | `UPDATE` (allowed) | **row (8,8) INSERTED** (committed) | **row (8,8) INSERTED** |
| `/*!50000 INSERT INTO t11_y ... ON DUPLICATE KEY */ UPDATE v = 5`, same | `UPDATE` | **INSERTED** | **INSERTED** |
| `/*M! INSERT INTO t11_y ... ON DUPLICATE KEY */ UPDATE v = 5`, same | `UPDATE` | 1064 (MySQL sees a plain comment: `... UPDATE v = 5`) | **INSERTED** |

Conclusion: a caller CAN make the server run a verb that is not in `execute_verbs` whenever the hidden fragment and the visible
fragment compose into ONE valid statement (here `INSERT ... ON DUPLICATE KEY UPDATE`). The strongest forms the coordinator
asked about (a hidden `DROP` / `DELETE` in front of a visible `UPDATE`/`INSERT`, or a `;` second statement) do not work because
the composed text is a syntax error and multi-statements are disabled; they fail with 1064 and nothing changes. Severity
**Medium-Low** (D5): needs `allow_execute: true`, an `rw` account and a caller allowed to call `execute`; impact is bounded to the
privileges of that account. The filter is documented as best effort, not a boundary (SECURITY section 3); the grant is the control.

Suggested fix (src, not applied here): on the mysql dialect reject any statement containing `/*!` or `/*M!` (they are never
needed in a hand-written `execute`) and on every dialect reject `/*+` followed by non-hint text, or simply refuse every
`/* ... */` that is not at the very start AND closed before the first keyword; on PostgreSQL reject a nested `/*` inside a block
comment (PostgreSQL nests, the filter ends the comment at the first `*/`). Also accept a leading `#` comment on MySQL/MariaDB so
the filter fails open for no legitimate input.

### Class 6: crash the module

About 60 hostile argument cases per server on `query` (`test_query_never_crashes_or_hangs`), plus `check_query_result`,
`check_row_count` and `table_list` cases. `sql`: None, int, list, bytes, dict, bool, NaN, empty, spaces, a lone surrogate, a
lone NUL, a 5 MB comment, 10 000 nested parentheses, 100 000 `UNION ALL` terms, `%s %(a)s {} {0} %%` and `:a ::b \\:c` colon forms.
`params`: 100 000 unused keys, nested dict, a list nested 20 000 deep, NaN, inf, `10**40`, `10**5000`, bytes, a string with a NUL,
a 50 MB string, `object()`, an int key, a list, a string, a missing bind, a bind named `a)--`. `limit`: `10**30`, `10**5000`, 0, -1,
True, 1.5, NaN, inf, `"5"`, `[5]`. `timeout_ms`: `10**30`, 0, -5, 1. `max_bytes`: 0, 1, `10**30`. Keyword names with CR/LF, 100 000
characters, dunder names and a bidi control character. `check_*`: operator None / int / `~=` / empty, `expected` nested 5 000 deep
or a 50 MB string, `row`/`column` -1, `10**30`, 1.5, `retry_*` 0 / NaN / negative / inf. `table_list`: schema `x'; DROP TABLE y; --`
and ``scratch` -- ``, patterns `[[[((`, a lone backslash and `%` x 100 000, a 100 000-character schema, schema `5`, pattern `['a']`,
`include_views='yes'`. Every case returned a result (strict JSON, `allow_nan=False` checked) or a typed `[E_...]` error
(`CommandExecutionError` / `SaltInvocationError`); none raised another exception type, none printed a traceback to the caller,
none took more than 30 s (wall-clock asserted; except D4 in class 4). Server values returned, all strict JSON:

| Value | PostgreSQL | MySQL | MariaDB |
|---|---|---|---|
| `NaN` / `Infinity` / `-Infinity` (float8) | `"NaN"`, `"Infinity"`, `"-Infinity"` | n/a | n/a |
| `numeric 'NaN'`; 1e100000 numeric; 60-digit decimal | string | string | string |
| `jsonb` nested 2 000 deep | `E_INTERNAL RecursionError` (typed, no crash) | n/a | n/a |
| `chr(0)` | `E_INTERNAL 54000` | n/a | n/a |
| invalid UTF-8 | `E_SQL_DATA 22021` | n/a | n/a |
| `'infinity'::timestamp`, date year 5874897 | `E_INTERNAL DataError` (typed; see D7) | n/a | n/a |
| huge `bytea`, interval, array, composite, range, oid, xml | returned | n/a | n/a |
| zero date | n/a | `'0000-00-00'` | same |
| `BIT`, `BLOB` | n/a | `{"$bytes_b64": ...}` | same |
| NUL inside text | n/a | `{"$bytes_b64": ...}` | same |
| `1e308 * 10` | n/a | `E_SQL_DATA 1690` | same |
| `TIME '838:59:59'` | n/a | `'34 days, 22:59:59'` | same |

Not crashes but reported: D6 (NaN echoed as a float), D8 (key names echoed), D9 (NUL silently truncates the statement on PG).

## 3. Defects

| ID | Severity | Summary | Affects |
|---|---|---|---|
| D1 | **High** (PG), n/a on MySQL/MariaDB | PostgreSQL accepts multi-statement strings (psycopg uses the simple protocol when there are no parameters). Inside one call a caller can run `COMMIT; <write>` (defeats the read-only guard, section 2), `SET statement_timeout = 0` (defeats the time cap), `INSERT; DELETE` (defeats `execute_verbs`), `INSERT; COMMIT` with `dry_run=True` (persists the write; the result says `committed: false`) | `lib/dialects.py` PostgreSQL adapter (connect args, ~line 183-205), `lib/runner.py:42` / `:122` |
| D2 | Medium | The server-side timeout can be overridden by the statement: MySQL `/*+ MAX_EXECUTION_TIME(n) */` (and `MAX_EXECUTION_TIME` is SELECT-only, so a slow `INSERT ... SELECT` is not cut at all); MariaDB `SET STATEMENT max_statement_time=0 FOR ...` | `lib/dialects.py` MySQL adapter (`_set_timeout`, ~line 235-245; `read_timeout` is not set) |
| D3 | Medium | The caps are applied after the driver has buffered the whole result: 10 M rows = 363 MB RSS (PG), 8 M rows = 786 MB (MariaDB), one 100 MB value = 532 MB. A read-only caller can OOM the minion | `lib/runner.py:59` (`fetchmany(limit + 1)` on a buffered result), `lib/engine.py` |
| D4 | Medium | `table_list(pattern=...)` turns `LIKE` into an unanchored backtracking regex; `%a%a%a%a%a%a%a%a%a%a%a%ab` against a 54-character name never finishes (ReDoS, one caller blocks the minion worker; the minion is single-threaded per job) | `modules/sqlx_mod.py:207-210` (`_like`) |
| D5 | Medium-Low | The `execute` verb filter is bypassed by comments the server executes or nests: PostgreSQL nested `/* /* */ ... */`, MySQL/MariaDB `/*! */`, `/*!50000 */`, MariaDB `/*M! */` (section 2, class 5). Also a leading `#` comment is rejected (fails closed) | `lib/runner.py:78-101` (`_skip_blanks_and_comments`) |
| D6 | Low | `check_query_result(expected=float('nan'))` echoes `expected` unchanged; NaN is not valid JSON, so a strict consumer (the salt-api JSON encoder, a returner) can fail or emit `NaN` | `lib/checks.py` (result dict), `lib/serialize.py` is not applied to the echo |
| D7 | Low (observed, no xfail) | `timestamp 'infinity'` and year > 9999 raise a driver `DataError` mapped to `E_INTERNAL` (no secret, but the caller gets "internal" instead of a data error) | `lib/errors.py` (map `DataError` to `E_SQL_DATA`) |
| D8 | Low | `[E_OPTION_UNKNOWN]` repeats the caller's key names verbatim: newlines (log forging), bidi control characters, a 100 000-character key (log amplification) | `lib/options.py:152-154` |
| D9 | Low | A NUL byte inside the SQL silently truncates the statement on PostgreSQL (the driver stops at NUL) so the text the verb filter and the audit hash saw is not what ran | `modules/sqlx_mod.py` / `lib/options.py` (validate `sql`) |
| D10 | Low | `check_row_count` / `check_query_result` accept `retry_pause_s=1e-9`; each attempt opens a new connection (NullPool): 100 connections per second from one call | `lib/checks.py:121-138` (`_retry`) |
| D11 | Info (design) | `dry_run=None` means "not given" and defaults to false, so the statement is COMMITTED. Strict booleans are enforced for everything else. Documented (SPEC D2); noted so nobody reads `None` as a safe value | `lib/options.py` (`dry_run`) |

Reproductions are the strict-xfail tests (`pytest tests/security -k "<name>" -s`):

| ID | Test | Minimal reproduction |
|---|---|---|
| D1 | `test_attacks_query.py::test_multi_statement_string_does_not_persist_a_write`, `::test_select_only_account_cannot_change_its_password_through_the_guard`, `test_attacks_limits.py::test_pg_session_timeout_cannot_be_switched_off_by_the_statement`, `test_attacks_execute.py::test_pg_semicolon_cannot_smuggle_a_second_verb`, `::test_dry_run_true_cannot_be_defeated_by_a_commit_in_the_statement` | `sqlx.query('rw_query', 'COMMIT; INSERT INTO scratch.t VALUES (1)')` persists the row; `sqlx.query('ro', 'SET statement_timeout = 0; SELECT pg_sleep(3)', timeout_ms=800)` returns after 3.0 s; `sqlx.execute('rw', 'INSERT ...; COMMIT', dry_run=True)` |
| D2 | `test_attacks_limits.py::test_mysql_hint_cannot_raise_the_statement_timeout`, `::test_mariadb_set_statement_cannot_raise_the_statement_timeout` (MySQL DML: `::test_documented_mysql_dml_is_not_limited_by_max_execution_time`) | `SELECT /*+ MAX_EXECUTION_TIME(100000) */ SLEEP(3)` with `timeout_ms=800` returns after 3.0 s |
| D3 | `test_attacks_limits.py::test_ten_million_rows_do_not_cost_ten_million_rows_of_memory`, `::test_a_huge_single_value_is_not_expanded_in_memory_before_the_byte_cap` | `SELECT g FROM generate_series(1, 10000000) g` with the default limit: 363 MB peak RSS |
| D4 | `test_attacks_limits.py::test_table_list_pattern_cannot_pin_the_cpu` | `sqlx.table_list('ro', pattern='%a%a%a%a%a%a%a%a%a%a%a%ab')` |
| D5 | `test_attacks_execute.py::test_pg_nested_block_comment_cannot_hide_a_verb`, `::test_mysql_executable_comment_cannot_hide_a_verb` | see section 2, class 5 |
| D6 | `test_attacks_robustness.py::test_check_result_is_strict_json_even_for_a_nan_expected` | `sqlx.check_query_result('ro', 'SELECT 1', operator='==', expected=float('nan'))` |
| D8 | `test_attacks_robustness.py::test_unknown_option_names_are_not_echoed_with_control_characters` | `sqlx.query('ro', 'SELECT 1', **{'a\r\nINFO forged': 1})` |
| D9 | `test_attacks_query.py::test_nul_byte_in_sql_is_refused_not_silently_truncated` | `sqlx.query('ro', 'SELECT 1\x00; DROP TABLE x')` on PostgreSQL |
| D10 | `test_attacks_limits.py::test_retry_loop_has_no_floor_on_the_pause` | `check_row_count(..., retry_timeout_s=1, retry_pause_s=1e-9)` |

### Suggested fixes (verified in a sandbox where marked)

| ID | Fix |
|---|---|
| D1 | Create the psycopg connection with `prepare_threshold=0` (set by the adapter, not allowed from a profile): the extended protocol rejects a string with more than one statement (`cannot insert multiple commands into a prepared statement`). Verified in a sandbox: `COMMIT; INSERT` and `SET statement_timeout=0; SELECT` fail with 42601-class errors, single statements and binds unchanged. Belt and braces: `execute` should refuse a `;` that is followed by non-comment text, on every dialect. |
| D2 | Set `read_timeout` in PyMySQL connect args to `statement_timeout_ms` (+ margin) as a client-side backstop (verified in a sandbox: the call returns at the cap with `2013`, which `errors.py` already maps to `E_TIMEOUT`); on MySQL/MariaDB also refuse a statement that contains `MAX_EXECUTION_TIME` or `max_statement_time` (best effort). SPEC open decision D11 covers `read_timeout`. |
| D3 | Run `query` with `conn.execution_options(stream_results=True)` (psycopg server-side cursor, PyMySQL `SSCursor`) so `fetchmany(limit + 1)` really reads limit+1 rows; verified in a sandbox: peak RSS stayed flat at 10 M rows. A single huge value still needs the server's `max_allowed_packet` (MySQL) or a `LEFT(col, n)` convention (PG): document it. |
| D4 | Collapse runs of `%` and cap the pattern at e.g. 128 characters and at most 6 wildcards, or match with an O(n*m) iterative glob matcher instead of `re`; or push the filter to the database (`LIKE :pattern`) and drop the Python regex. |
| D5 | See the executable-comment paragraph above. |
| D6 | Pass the echoed `expected` through `serialize.to_jsonable` (NaN -> `"NaN"`), or refuse a non-finite `expected` with `E_OPTION_INVALID`. |
| D7 | Map `sqlalchemy.exc.DataError` and `OverflowError` from the driver to `E_SQL_DATA`. |
| D8 | In `options.py`, print `ascii(name)[:40]` for at most 5 names and the count of the rest. |
| D9 | Reject `"\x00"` in `sql` (and in profile/option strings) with `E_OPTION_INVALID`; the policy path already refuses a NUL prefix. |
| D10 | Floor `retry_pause_s` at 0.05 s in `options.py` (or cap the number of attempts at `retry_timeout_s / 0.05`). |

## 4. Licences

`python tests/security/licences.py --check` exits 0 (no GPL/AGPL; LGPL only for `psycopg`, `psycopg-binary`, `pymssql`). An
independent `pip-licenses 5.5.5` run gave the same values for the declared packages. Full closure table: `NOTICE`. Test:
`tests/security/test_licences.py` (28 tests; the gate is also shown to FAIL on a synthetic GPL package and on an unaccepted LGPL
package, and `mysqlclient` is neither installed nor declared).

| Package | Version | Licence (from package metadata) | Class |
|---|---|---|---|
| salt | 3008.5 | Apache-2.0 | permissive |
| SQLAlchemy | 2.0.55 | MIT | permissive |
| psycopg (extra `postgres`) | 3.3.6 | LGPL-3.0-only | LGPL, accepted: unmodified pip package |
| psycopg-binary (via `psycopg[binary]`) | 3.3.6 | LGPL-3.0-only | LGPL, accepted |
| PyMySQL (extra `mysql`) | 1.2.3 | MIT | permissive |
| oracledb (extra `oracle`) | 26.1.0 | UPL-1.0 OR Apache-2.0 | permissive |
| pymssql (extra `mssql`) | 2.4.6 | LGPL-2.1 | LGPL, accepted: unmodified pip package |
| certifi (via salt/requests) | 2026.10.23 | MPL-2.0 | weak copyleft, unmodified |
| tempora (via salt -> CherryPy -> portend) | 5.13.0 | none declared in metadata | UNKNOWN: not a dependency of this extension; recorded, whitelisted in the test as `DECLARED_UNKNOWN` |

All other 85 packages of the closure (the full list is in `NOTICE`) are MIT/BSD/Apache/PSF/ISC/ZPL class.

## 5. SECURITY.md section 4 example through `salt-api`

`tests/security/test_eauth_salt_api.py` starts a throwaway master, minion and `salt-api` (CherryPy, loopback, `auto` eauth, which
accepts any password, so only the AUTHORISATION rules are under test) with the exact rule shapes of section 4
(`kwargs: {profile: '^(ro)$'}`) and posts to `/run`. It skips if `salt-api`, CherryPy or the PostgreSQL server is missing.
Result: 19 passed.

| User / call | HTTP | Meaning |
|---|---|---|
| `sre_ops` `sqlx.ping profile=ro`; `sqlx.query profile=ro` | 200 | authorised, ran |
| `dba_writers` `sqlx.execute profile=rw dry_run=True` | 200 | authorised, ran (the DB reports the missing table) |
| `sre_ops` `sqlx.query` with `profile=other`, `profile=rw`, positional `rw`, positional `ro`, no profile | 401 | denied |
| `sre_ops` `sqlx.execute`, `sqlx.table_list`, `sqlx.check_row_count`, `cmd.run` | 401 | not listed, denied |
| `dba_writers` `sqlx.execute profile=other`, positional `other`, `sqlx.query`; unknown user | 401 | denied |
| `sre_ops` `sqlx.ping profile=other` | 200 | **by design of the example**: `- sqlx.ping` has no `kwargs`, so any profile is pingable. Add a profile regex to restrict it |
| `sre_ops` `sqlx.query ["rw"]` + `profile=ro` | 200 | a Python error (`multiple values for argument 'profile'`), not a bypass |
| `sre_ops` `sqlx.query profile="ro\n"` | 200 | `$` matches before a trailing newline so the regex lets it through; the module then rejects the unknown profile name (`E_PROFILE_UNKNOWN`). Anchor with `\Z` or rely on the module |

Positional profile arguments are always denied once a `kwargs` regex is present, which is the safe behaviour and should be
stated in section 4. Not tested: PAM eauth (needs system accounts); the authorisation engine is identical for every eauth
backend.

## 6. Environment and hygiene notes

- `SET PASSWORD='x'` run as `rw` with the guard off changed the shared MySQL `rw` password once (implicit-commit DDL, outside any
  transaction). It was restored with the admin account (`ALTER USER 'rw'@'%' IDENTIFIED BY 'rw_pw'`) and all later password
  attacks used a throwaway account. Note for operators: the module's `rollback` does not protect account-management statements.
- All created objects are `t11_*` (tables, sequences, procedures, roles, the database `t11_db`, `scratch.t11_new`) and are dropped
  by fixtures; the final sweep found none left. The live servers were not stopped or reconfigured.
- Salt processes started for section 5 run under a private temp directory and are terminated by the fixture.
- `pip-licenses` was added to `dev_extra` in `pyproject.toml` (the only `pyproject.toml` change).
