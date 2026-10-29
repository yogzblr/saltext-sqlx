# saltext-sqlx — SPEC

The contract. If code and this file disagree, fix this file in the same change.

`saltext.sqlx` is a Salt execution-module extension (virtualname `sqlx`) that runs SQL against Postgres, MySQL/MariaDB,
Oracle and SQL Server through **one** code path (SQLAlchemy Core) and returns **the database's real error** when a
statement fails. Its API shape is modelled on the Robot Framework `DatabaseLibrary` (Apache-2.0).

## 1. Why it exists

Salt's built-in SQL modules lose errors: `postgres.psql_query` returns `[]` on any `psql` failure; `mysql.query` catches
only `OperationalError` and returns `False`, while syntax errors escape as a raw traceback. This module has exactly one
error path (section 8), so every database fails the same, readable way.

## 2. Robot DatabaseLibrary mapping

| Robot keyword | Function | Notes |
|---|---|---|
| `Connect To Database` (+ `alias`) | **profile** (config/pillar) | Salt jobs are stateless. `alias` becomes `profile`. |
| `Query` | `sqlx.query` | Read-only. |
| `Execute Sql String` | `sqlx.execute` | **Disabled per profile until `allow_execute: true`.** |
| `Check Query Result` | `sqlx.check_query_result` | Returns data, not an error, when the check fails. |
| `Check Row Count` | `sqlx.check_row_count` | Same. |
| (none) | `sqlx.ping`, `sqlx.table_list` | Connectivity test; schema listing via `sqlalchemy.inspect`. |
| `Switch Database`, `Disconnect From All Databases`, `Set Auto Commit` | not implemented | No connection state exists between jobs. |

## 3. Profiles

Profiles live under `sqlx:profiles:<name>` in minion config or pillar. **Callers choose a profile; they do not supply
connection details** (D1).

```yaml
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql          # required: postgresql | mysql | oracle | mssql
      driver: psycopg              # optional; defaults to the dialect's only driver (psycopg | pymysql | oracledb | pymssql), any other value is rejected
      host: db1.internal
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg      # optional; sdb:// reference, or a literal (see SECURITY.md)
      query: {}                            # URL query args, e.g. {sslmode: verify-full}
      connect_args: {}                     # allowlisted driver args only (section 7)
      read_only: true                      # default true
      allow_execute: false                 # default false
      execute_verbs: [INSERT, UPDATE, DELETE]   # default shown; entries are upper-cased; best-effort first-keyword filter, NOT a security boundary
      statement_timeout_ms: 30000          # default 30000, hard max 300000
      connect_timeout_s: 10
      row_limit: 500                       # default 500, hard max 10000
      max_bytes: 1048576                   # response size cap, default 1 MiB, hard max 8 MiB
      error_detail: false                  # include the server's DETAIL line in errors (can contain row data)
      # TLS with a certificate directory (D12, below; needs sqlx:_policy:ssl_certs_dir_root):
      # ssl_certs_dir: /etc/salt/sqlx-certs/orders_pg   # absolute, under the policy root; holds ca.pem [client.pem client.key]
      # ssl_mode: verify-full                           # default when a directory is set; or verify-ca
      # ssl_files: {ca: ca.pem, cert: client.pem, key: client.key}   # optional plain file names inside the directory
  _policy:                                  # not a profile; read by profiles.load_profile
    ssl_certs_dir_root: /etc/salt/sqlx-certs
```

Validation (`lib/profiles.py`, `validate_profile`; the source of truth if this text and the code ever differ). Every
failure is `[E_PROFILE_INVALID]`, names keys only and never contains a value.

| Key | Rule |
|---|---|
| unknown key | rejected |
| `dialect` | **required**; one of `postgresql`, `mysql`, `oracle`, `mssql` (section 7; whether `mariadb` becomes a fifth value is D10) |
| `driver` | optional; defaults to the dialect's only driver (`postgresql` psycopg, `mysql` pymysql, `oracle` oracledb, `mssql` pymssql); any other value is rejected. The single source of truth for this table is `lib/dialects.py` (`DIALECT_DRIVERS`); `profiles.py` imports it |
| `host`, `database`, `username` | **required**, non-empty strings |
| `port` | **required**, integer 1-65535 (a bool is not an integer) |
| `password` | **optional**. If present: a non-empty string, and not the bare prefix `sdb://`. A literal or an `sdb://` reference; validated for shape only and resolved later (`lib/secrets.py`, ARCHITECTURE section 7). Absent stays absent in the validated profile |
| `query` | optional mapping of string keys to str/int/float/bool values; `null` means `{}`. Only the shape is checked here; the per-driver allowlist (section 7) is applied by the dialect adapter when the engine is built |
| `connect_args` | optional mapping with string keys; `null` means `{}`. Only the shape is checked here; the per-driver allowlist (section 7) is applied by the dialect adapter when the engine is built |
| `read_only`, `allow_execute`, `error_detail` | booleans; defaults `true`, `false`, `false` |
| `ssl_certs_dir`, `ssl_mode`, `ssl_files` | **optional, profile-only TLS settings (D12)**; rules in section 3.1. `ssl_mode` and `ssl_files` without `ssl_certs_dir` are rejected. The validated profile gains `ssl_certs_dir` (as given) and `tls` (resolved real paths; internal, never shown in a message) |
| `execute_verbs` | list of single-word alphabetic strings; default `[INSERT, UPDATE, DELETE]`; every entry is **upper-cased** by validation; an empty list is accepted; duplicates are kept |
| `statement_timeout_ms`, `connect_timeout_s`, `row_limit`, `max_bytes` | integer >= 1 (a bool is not an integer); defaults and hard maxima in section 6; **a value above the hard maximum is rejected** with `[E_PROFILE_INVALID]`, it is not clamped |

`load_profile(name, config)`: an unknown or empty name, or no `profiles` section, is `[E_PROFILE_UNKNOWN]` (the message does not
disclose other profile names); a `profiles` value that is not a mapping, or a profile that fails the table above, is
`[E_PROFILE_INVALID]`. Only the requested profile is validated. The result is a fresh copy with defaults filled in and
`name` added.

### 3.1 TLS certificate directory (`lib/tls.py`, decision D12)

Callers still never supply connection details: `ssl_certs_dir`, `ssl_mode` and `ssl_files` are profile keys, and the driver
arguments they produce are built by the module (`tls.query_args` / `tls.connect_args`, applied by the dialect adapter after
the allowlist check). A profile may **not** also set the keys the TLS layer owns in `query` / `connect_args` (PostgreSQL
`sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `sslcertmode`; MySQL `ssl_ca`, `ssl_cert`, `ssl_key`, `ssl_verify_cert`,
`ssl_verify_identity`, `ssl_disabled`; Oracle `ssl_server_dn_match`, `protocol`, `wallet_location`): `[E_PROFILE_INVALID]`
naming the key. The per-driver allowlist (section 7) is unchanged and still default-deny: no path-bearing key was added to it.

| Rule | Result |
|---|---|
| `sqlx:_policy:ssl_certs_dir_root` (read from the same `config.get("sqlx")` mapping that holds `profiles`) | the directory must resolve (`realpath`) to the root or below it. Root missing, not absolute, or not an existing directory: `[E_POLICY]`. `sqlx:_policy` not a mapping: `[E_PROFILE_INVALID]` |
| `ssl_certs_dir` | absolute string without `..`; relative or `..`: `[E_PROFILE_INVALID]`. Resolves outside the root (including through a symlink): `[E_POLICY]`. Missing or not a directory: `[E_PROFILE_INVALID]` |
| `ssl_mode` | `verify-full` (default when a directory is set) or `verify-ca`. `disable`, `prefer`, `allow` are refused with a directory. `require` is refused too: on MySQL/MariaDB it encrypts without verifying the server, and on PostgreSQL libpq treats `require` plus a root certificate as `verify-ca` (observed: `sslmode=require` with a wrong `sslrootcert` fails with `certificate verify failed`), so it adds nothing |
| `ssl_files` | optional mapping, keys `ca`, `cert`, `key`; each value a plain file name (no `/`, `\`, `..`, empty or NUL). Unknown key or bad name: `[E_PROFILE_INVALID]`. Not accepted for `oracle` |
| default names | `ca.pem` (fallback `ca.crt`), `client.pem` (fallback `client.crt`), `client.key`. A fallback is used only when it is the only candidate; if both `ca.pem` and `ca.crt` (or both client names) exist: `[E_PROFILE_INVALID]` asking for `ssl_files`. A name given in `ssl_files` disables the fallback and must exist |
| required / paired files | CA missing: `[E_PROFILE_INVALID]` naming the file name only. A client certificate without its key, or a key without its certificate: `[E_PROFILE_INVALID]`. No client pair at all is fine (server authentication only) |
| every file | must resolve under the root (a file symlink to somewhere else under the root is allowed, one that leaves it is `[E_POLICY]`), be a regular file (`[E_PROFILE_INVALID]`) and be openable by the minion (`[E_PROFILE_INVALID]`, "not readable") |
| private key mode | owner-only (`0600`/`0400`), or `0640` when owned by root (libpq's own rule). Any world access or group write/execute, or a group-readable key not owned by root: `[E_POLICY]` saying to `chmod 600` (or 640 when root-owned). Checked from `stat`; PyMySQL does not check key modes itself, PostgreSQL's libpq does |
| contents | **never read** by the module (only name, location, type, mode and openability are checked) and never in a message or log |
| order | everything above runs in `validate_profile` (called by `load_profile`), before the secret is resolved and before any network call |
| `mssql` | `[E_PROFILE_INVALID] ... 'ssl_certs_dir' is not supported for mssql yet`. pymssql 2.4.6 `connect()` has only `encryption` (`off`/`request`/`require`); it takes no CA file or client certificate, and FreeTDS reads its own configuration file, which this module does not manage |

Mapping to the drivers (`tls.py`; the file arguments are the real paths found above):

| Dialect | Arguments | Where |
|---|---|---|
| `postgresql` / psycopg 3 (libpq) | `sslmode` (`verify-full` or `verify-ca`), `sslrootcert`, and with a client pair `sslcert` and `sslkey`; without a pair `sslcertmode=disable`, so that libpq does not present `~/.postgresql/postgresql.crt` or `$PGSSLCERT` | URL query |
| `mysql` (MySQL and MariaDB) / PyMySQL | `ssl_ca`, `ssl_verify_cert=True`, `ssl_verify_identity=True` for `verify-full` and `False` for `verify-ca`, and with a client pair `ssl_cert`, `ssl_key` | `connect_args`, not the URL: SQLAlchemy moves URL-query `ssl_ca`/`ssl_cert`/`ssl_key` into PyMySQL's deprecated `ssl` dict, which PyMySQL then ignores as soon as an `ssl_verify_*` keyword is also present (PyMySQL 1.2.3 `connections.py`) |
| `oracle` / python-oracledb thin | `protocol="tcps"`, `wallet_location=<directory>`, `ssl_server_dn_match=True` for `verify-full`, `False` for `verify-ca`; the directory must contain `ewallet.pem` | `connect_args`. **UNVERIFIED(T1b)**: no Oracle server was available; only the argument mapping is unit-tested. The `ewallet.pem` name, the `wallet_password` case (an encrypted wallet is not supported: no password key exists) and the `verify-ca` meaning of `ssl_server_dn_match=False` come from the python-oracledb documentation |
| `mssql` | refused (above) | **UNVERIFIED**: never run |

Call arguments may **lower** `row_limit` (`limit`), `statement_timeout_ms` (`timeout_ms`) and `max_bytes`, never raise them.
A call value above the profile value or the hard maximum is **clamped silently** to the lowest of call, profile and hard
maximum (`options.apply_caps`); it is not an error (D9).

## 4. Functions

All arguments are keyword-friendly and JSON-typed, so they pass unchanged through the Salt REST API
(`"kwarg": {...}`). Validation is `options.validate(function, kwargs)`:

- On the `salt` / `salt-call` command line every value is parsed as YAML, which mangles the comparison symbols: pass
  operators **YAML-quoted**, for example `operator='"=="'`, `operator='">"'`, `operator='">="'` (single quotes for the shell,
  double quotes for YAML). The REST API and `LocalClient` send JSON and need no extra quoting (section 11). The word
  operators (`contains`, `is_null`, ...) need none.
- Keys beginning `__` (Salt's `__pub_*`, `__kwarg__`) are dropped first.
- Any other key outside the function's allowed set is `[E_OPTION_UNKNOWN]` (the message lists the key names only).
- A missing required key, or a value of the wrong type or range, is `[E_OPTION_INVALID]`. For an optional key, `null` means
  "not given" and the default applies. Integers must be real integers (a bool is rejected).
- `limit`, `timeout_ms` and `max_bytes` stay unset until `options.apply_caps` resolves them against the profile and the hard
  maxima (section 6). `retry_timeout_s` is capped at 10 there.

Allowed keywords per function (R = required; the type and default follow):

| Function | Keywords |
|---|---|
| `query` | `profile` R str, `sql` R non-empty str, `params` object with string keys (default none), `limit` int >= 1, `timeout_ms` int >= 1, `max_bytes` int >= 1 |
| `execute` | `profile` R, `sql` R, `params`, `dry_run` bool (default `false`), `timeout_ms` int >= 1 |
| `check_query_result` | `profile` R, `sql` R, `operator` R non-empty str, `expected` any JSON value (`null` is accepted); **required unless `operator` is `is_null` or `is_not_null`** (`options.NULL_OPERATORS`), `params`, `row` int >= 0 (default 0), `column` int >= 0 (default 0), `retry_timeout_s` number >= 0 (default 0), `retry_pause_s` finite number > 0 (default 1) |
| `check_row_count` | `profile` R, `sql` R, `operator` R, `expected` R (the key must be present), `params`, `retry_timeout_s` (as above), `retry_pause_s` (as above) |
| `ping` | `profile` R |
| `table_list` | `profile` R, `schema` str, `pattern` str, `include_views` bool (default `false`) |

The check functions take no `limit`, `timeout_ms` or `max_bytes`: they run with the profile's `row_limit`,
`statement_timeout_ms` and `max_bytes` (lowered to the hard maxima). `ping` and `table_list` take no limits from
`apply_caps`.

### 4.1 `sqlx.query(profile, sql, params=None, limit=None, timeout_ms=None, max_bytes=None)`
Runs one statement inside a transaction that is **always rolled back**. Returns:

```json
{"profile": "orders_pg", "columns": ["id", "total"], "rows": [[1, "12.50"]], "row_count": 1,
 "truncated": false, "elapsed_ms": 4}
```

`rows` are positional lists (smaller, and it matches Robot's `${rows}[0][1]`); `columns` gives names (each passed through
`str()`). `truncated` is true when `limit` or `max_bytes` cut the result (the runner fetches `limit+1` rows to know). A
statement that returns no rows (for example a `SET`) returns `columns: []`, `rows: []`.

What `max_bytes` counts (`serialize.build_result`): the UTF-8 length of the compact JSON (`separators=(",", ":")`, non-ASCII
written as is) of `columns` plus the length of the compact JSON of `rows`, nothing else. The fields the runner adds
(`profile`, `row_count`, `truncated`, `elapsed_ms`) and the key names are not counted. Rows are added in order while the
running total stays within `max_bytes`; the first row that would exceed it, and every row after it, is dropped and
`truncated` is true. If even the first row does not fit, the result has `rows: []`, `row_count: 0`, `truncated: true`.
Memory (T11 D3): on PostgreSQL (`SELECT`, `VALUES`, `TABLE`, and `WITH` without `INSERT`/`UPDATE`/`DELETE`/`MERGE`) the rows are
read through a server-side cursor and on MySQL/MariaDB through PyMySQL's unbuffered `SSCursor`, so only `limit + 1` rows
leave the server; if rows are left unread on MySQL/MariaDB the connection is dropped rather than drained. Other statements
(`SHOW`, `EXPLAIN`, `INSERT ... RETURNING`, `SELECT ... INTO`) and Oracle/SQL Server are read buffered. A single value
larger than `max_bytes` is dropped without being JSON-encoded, but the driver still receives it whole (measured: a 100 MB
`text` value costs about 270 MB of RSS, the floor of a bare psycopg cursor); on MySQL/MariaDB `max_allowed_packet` bounds
it, on PostgreSQL select `left(col, n)`.
`limit` is checked first: when another row exists beyond `limit`, `truncated` is true. The `columns` list is never itself
refused, so a result with no rows is not marked truncated even if `columns` alone is larger than `max_bytes`. Value
conversions are in section 9.

### 4.2 `sqlx.execute(profile, sql, params=None, dry_run=False, timeout_ms=None)`
Refused with `[E_EXECUTE_DISABLED]` unless the profile has `allow_execute: true` (checked by the module before any secret
is resolved or any connection is opened). Then, in `runner.run_execute`:

- `dry_run` must be exactly `True` or `False` (a real `bool`); anything else, including a string or a number, is
  `[E_OPTION_INVALID]`, so a malformed flag can never commit. The call-level default is `false` (D2). **`dry_run=None`
  means "not given", not "safe"**: it takes the default and the statement is COMMITTED (T11 D11). Pass `dry_run=True`
  explicitly to roll back.
- The first keyword of the statement must be in `execute_verbs` (compared case-insensitively), else `[E_POLICY]`. A statement
  that does not start with a keyword at all (`(select 1)`, `;drop table t`) is also `[E_POLICY]`. An empty or comment-only
  statement is `[E_OPTION_INVALID]`.
- The filter skips only whitespace, `--` line comments and plain `/* */` block comments (`/*+ ... */` included) before
  the keyword. A MySQL `#` comment is not skipped, so a statement that starts with one fails with `[E_POLICY]`. A leading
  `/*!` or `/*M!` comment (MySQL and MariaDB execute its content) and a leading block comment that contains `/*`
  (PostgreSQL nests block comments, MySQL does not, so the two servers disagree where the verb starts) are refused with
  `[E_POLICY]` on every dialect (T11 D5). This is a best-effort filter, **not a security boundary** (a CTE or a function
  call can still write): the database role limits what `execute` can do.
- One statement per call. On PostgreSQL the connection uses psycopg's extended protocol (`prepare_threshold=0`, set by the
  module and not settable from a profile), so a string with a second statement (`INSERT ...; COMMIT`) fails with
  `[E_SQL] postgresql 42601: cannot insert multiple commands into a prepared statement`; PyMySQL does not enable
  multi-statements, so MySQL and MariaDB answer 1064 (T11 D1). A trailing `;` is accepted.
- The connection is opened with `read_only` **False**, whatever the profile says: `allow_execute: true` wins over
  `read_only: true`, because an execute-enabled profile must be able to write (`sqlx_mod.execute`). The in-session
  read-only guard is therefore not applied to `execute`; set `read_only: false` on such a profile to keep the profile
  honest, and use a role that may write only what is intended.
- The statement runs in a transaction that is committed only if `dry_run` is false. Any exception rolls the transaction back
  and propagates.

Returns `{"profile", "rows_affected", "committed": bool, "dry_run": bool, "elapsed_ms"}`. With `dry_run: true` the
transaction is rolled back and `rows_affected` reports what would have changed.

### 4.3 `sqlx.check_query_result(profile, sql, operator, expected, params=None, row=0, column=0, retry_timeout_s=0, retry_pause_s=1)`
Runs `sql` as in 4.1, takes `rows[row][column]`, compares with `expected` (`lib/checks.py`). Returns
`{"ok": bool, "actual": ..., "expected": ..., "operator": ..., "attempts": n}` (plus `reason` when set). A failed comparison
is `ok: false`, **not** an error. A missing row or column is `ok: false` with `actual: null` and `reason: "no_such_cell"`.
The check does not look at `truncated`: it reads the cell from the rows that were returned.

Operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `not_contains`, `is_null`, `is_not_null`. Any other operator is
`[E_OPTION_INVALID]`. `is_null` and `is_not_null` ignore `expected`, and for those two operators the key may be left out
(`options.validate` requires it for every other operator).

Comparison rules:
- Numbers: `int`, finite `float` (taken from its `repr`), finite `Decimal`, and strings that fully match
  `[+-]?(digits[.digits]|.digits)([eE][+-]?digits)?` after stripping surrounding whitespace are compared as exact `Decimal`
  values, so `"12.50" == 12.5` is true and no float rounding occurs. The strings `"NaN"`, `"Infinity"`, `"inf"` are not numbers.
  A `bool` is never a number.
- `==`: if both sides are numbers, numeric equality; else if exactly one side is a bool, false; else Python equality. `!=` is
  the negation of `==`.
- `<`, `<=`, `>`, `>=`: numeric if both sides are numbers; else, if neither side is a bool, ordinary comparison of the two
  values (strings compare lexicographically); values that cannot be ordered (a bool on either side, `null`, or a string against a number it
  does not parse as) give `ok: false`.
- `contains`: false for `null`; for a list, true if any item `==` `expected` by the rules above; for an object, whether
  `str(expected)` is a key; otherwise `str(expected)` is a substring of `str(actual)`. `not_contains` is its negation
  (so it is true for `null`).
- A negative or non-integer `row` or `column` is `[E_OPTION_INVALID]`.

Retry semantics (`checks._retry`, shared with 4.4):
- The first attempt is immediate. With `retry_timeout_s: 0` there is exactly one attempt.
- The deadline is `retry_timeout_s` capped at 10 seconds (a larger value is capped, not rejected). `retry_timeout_s` must be a
  finite number >= 0.
- `retry_pause_s` must be a finite number **greater than 0**, otherwise `[E_OPTION_INVALID]`; `options.validate` and
  `checks` apply the same rule. Before each further attempt the pause is shortened to the time remaining.
- Only `ok: false` is retried, which includes `no_such_cell`. `ok: true` and `ok: null` (truncated, 4.4) are final.
- An exception raised while running the query (any of section 8) is never retried; it propagates at once.
- `attempts` is the number of attempts made.

### 4.4 `sqlx.check_row_count(profile, sql, operator, expected, params=None, retry_timeout_s=0, retry_pause_s=1)`
Same, comparing `len(rows)` with `expected`. Only `==`, `!=`, `<`, `<=`, `>`, `>=` apply; any other operator, including
`contains` and `is_null`, is `[E_OPTION_INVALID]`. `expected` is required. If the result is `truncated` (by the profile's
`row_limit` or `max_bytes`) the count is unknown: the result is `ok: null`, `actual: null`, `reason: "truncated"`, final and
not retried. Retry, number and pause rules are as in 4.3.

### 4.5 `sqlx.ping(profile)`
Connects and runs the dialect's trivial statement (`SELECT 1`; Oracle `SELECT 1 FROM DUAL`). Returns
`{"ok": true, "dialect", "server_version", "elapsed_ms"}`. Failures are errors with the usual codes.

### 4.6 `sqlx.table_list(profile, schema=None, pattern=None, include_views=False)`
Uses `sqlalchemy.inspect(...)` on a connection opened with the profile's own `read_only` setting. Returns
`{"profile", "schema", "tables": [...], "views": [...]}`, with `tables` and `views` sorted.

- `schema`: when absent (or an empty string) the inspector's default schema (`default_schema_name`) is used. The schema that was
  used is returned in `schema`; it can be `null` if the dialect reports no default.
- `pattern`: SQL `LIKE` syntax, `%` for any run of characters and `_` for exactly one, **anchored to the whole name**
  (`ord%` matches `orders`, `ord` does not). Every other character is literal; there is no escape character, so a literal `%`
  or `_` cannot be searched for. Matching is case-sensitive. It filters tables and views. An absent or empty `pattern` matches
  everything.
- `views`: `[]` unless `include_views` is true; with it, the schema's views (filtered by `pattern`).

## 5. Parameters and statement text

Observed on PostgreSQL 16.15, MySQL 8.0.46 and MariaDB 10.11.14 (WS 5), and unchanged on SQLAlchemy 2.1.6 (WS 8). For Oracle
and SQL Server only the client-side compilation was observed (no server needed); their server-side behaviour is NOT RUN.

- Callers pass values via `params` (a JSON object). Binds in the SQL are written `:name`. **Write casts as
  `CAST(:name AS INTEGER)`, never `:name::int`** — SQLAlchemy `text()` does not treat `:name::int` as a bind: the driver
  receives the literal `:a` and fails (PostgreSQL 42601, MySQL/MariaDB 1064).
- A literal colon followed by a word or digit inside a string, for example `'{"a":1}'`, is mis-read as a bind by
  `text()` (`sqlalchemy.exc.InvalidRequestError`: `A value is required for bind parameter '1'`, on all three servers).
  Callers escape it as `\:`. The module does not rewrite the SQL (D3).
- `%` is fine in `text()`: SQLAlchemy doubles it for psycopg and PyMySQL. Never use `exec_driver_sql` for caller SQL: a literal
  `%` with no parameters fails with `psycopg.ProgrammingError` (psycopg) and a bare `ValueError` (PyMySQL). pymssql keeps a
  single `%` in the compiled text; a statement with both a literal `%` and binds is NOT RUN.
- One statement per call. Multi-statement strings are not parsed or blocked by the module; the database role is the
  control (SECURITY.md).
- Stored procedures with multiple result sets, OUT parameters and cursors are not supported (D7).

## 6. Limits (hard caps in code, profile and call can only lower them)

A profile value above a hard maximum is rejected (`E_PROFILE_INVALID`, section 3). A call value above the cap is clamped
silently to the lowest of call, profile and hard maximum (`options.apply_caps`); `retry_timeout_s` above 10 is capped to 10.

| Limit | Default | Hard max |
|---|---|---|
| rows | 500 | 10 000 |
| statement timeout | 30 s | 300 s |
| response bytes (compact JSON of `columns` + `rows`, section 4.1) | 1 MiB | 8 MiB |
| connect timeout | 10 s | 60 s |
| `retry_timeout_s` | 0 | 10 |

## 7. Dialects and drivers

Labels. **observed**: seen on a real server by task T1 on 2026-10-29, evidence in `docs/WIRE_SHAPES.md` (`WS n` = its
section n). **unverified**: not run; kept from vendor documentation and must not be relied on until a server is available.
Servers actually run: PostgreSQL 16.15, MySQL 8.0.46, MariaDB 10.11.14, with SQLAlchemy 2.0.55 and 2.1.6, psycopg 3.3.6,
PyMySQL 1.2.3. Oracle and SQL Server could not be started in the T1 environment (registries blocked), so their rows are
unverified except where stated. They must be run by the owner on a machine with registry access, using
`tests-support/up.sh` and `scripts/probes/` (task T1b in `TASKS.md`).

| dialect | driver (pip extra) | URL | Licence (package metadata) | timeout mechanism | in-session read-only |
|---|---|---|---|---|---|
| `postgresql` | `psycopg[binary]` (extra `postgres`) | `postgresql+psycopg://` | LGPL-3.0-only | `SET statement_timeout = <ms>`: cancels at the limit, SQLSTATE 57014. **observed** [WS 2] | `SET TRANSACTION READ ONLY`: refuses writes (25006) but **bypassable** (SECURITY.md section 2). **observed** [WS 3] |
| `mysql` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` | MIT | `SET SESSION MAX_EXECUTION_TIME=<ms>`: raises 3024 (SQLSTATE `HY000`) for a SELECT that reads tables; a table-less `SELECT SLEEP(n)` is cut short **silently** (returns 1, no error); `INSERT ... SELECT` is **not limited** by it; a statement can also override it with `/*+ MAX_EXECUTION_TIME(n) */`. The module therefore also sets PyMySQL `read_timeout` = timeout + 1 s (a lower profile value is honoured), which ends the caller's wait with 2013 / `E_TIMEOUT` (T11 D2). **observed** on 8.0.46 only [WS 2]. The earlier "MySQL >= 5.7.8" is **unverified**. | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792, SQLSTATE 25006) but **bypassable** by four statements. `START TRANSACTION READ ONLY` resists the most statements but is also bypassable. **observed** [WS 3] |
| `mariadb` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` (SQLAlchemy sets `dialect.is_mariadb`, `False` on MySQL and `True` on MariaDB; use it to pick the variable) | MIT | `SET SESSION max_statement_time=<seconds>`: fractional values accepted; raises 1969 (SQLSTATE `70100`) for SELECT and for `INSERT ... SELECT`; `SET STATEMENT max_statement_time=0 FOR ...` overrides it, so `read_timeout` (timeout + 1 s) is the backstop here too (T11 D2). **observed** on 10.11.14 [WS 2]. `MAX_EXECUTION_TIME` does not exist here (1193). | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792) but **bypassable**; the session variable is `tx_read_only` (`transaction_read_only` does not exist, 1193). **observed** [WS 3] |
| `oracle` | `oracledb` thin mode (extra `oracle`) | `oracle+oracledb://` | UPL-1.0 OR Apache-2.0 | `connection.call_timeout` (ms): **unverified** (NOT RUN) | `SET TRANSACTION READ ONLY` as first statement: **unverified** (NOT RUN) |
| `mssql` | `pymssql` (extra `mssql`) | `mssql+pymssql://` | LGPL-2.1 | `timeout` / `login_timeout` connect args (s): **unverified** (NOT RUN) | none, role only: **unverified** (NOT RUN) |

Notes, all **observed** unless stated.
- MySQL and MariaDB share one driver and one URL scheme but differ in the timeout variable, the read-only variable, the
  error message wording, and SQLSTATE for the same vendor code (for example 1366 is `HY000` on MySQL and `22007` on MariaDB).
  The adapter must detect the flavour (`dialect.is_mariadb`) and may not assume one set of statements for both.
- PyMySQL 1.2.3 negotiated TLS with MySQL 8 by itself (self-signed server certificate, nothing verified by the caller). With
  `ssl_disabled=True`, MySQL 8's default `caching_sha2_password` needs the `cryptography` package: without it PyMySQL
  raises a bare `RuntimeError`; with it, the first connection after the server's auth cache was cleared raised a bare
  `AttributeError` and the next succeeded [WS 6]. `oracledb` already depends on `cryptography`; the `mysql` extra does not.
  Whether to add it is a dependency decision (R16) and was not made in T1.
- A PostgreSQL database created as SQL_ASCII fails on the first connect with a bare `TypeError: cannot use a string pattern
  on a bytes-like object` (not a `DBAPIError`) [WS 7].
- Choosing the MySQL/MariaDB read-only statement is a T4 decision; no tested statement is bypass-proof (WS 3).
- The dialect-to-driver table (`postgresql` psycopg, `mysql` pymysql, `oracle` oracledb, `mssql` pymssql) lives in
  `lib/dialects.py` (`DIALECT_DRIVERS`); `profiles.py` imports it.
- Only `mysql` is a valid `dialect` value; the `mariadb` row above is a flavour of it (D10). The module passes the profile's
  dialect name, so `errors.raise_for` and `errors.translate` receive `mysql` for MariaDB as well (the error table also accepts
  `mariadb`, but the module never passes it).
- Profile `query` entries and `connect_args` entries both reach the driver, so **both** are checked against the same
  per-driver allowlist (`dialects.Adapter._check_args`); a key outside it, or a value that is not a plain string, number or
  boolean, is `[E_POLICY]` naming the key only. Allowed today: `postgresql` `application_name`, `sslmode`,
  `target_session_attrs`, `keepalives`, `keepalives_idle`, `keepalives_interval`, `keepalives_count`; `mysql` `charset`,
  `read_timeout`, `write_timeout`, `ssl_disabled`, `ssl_verify_cert`, `ssl_verify_identity`; `oracle` `expire_time`,
  `retry_count`, `retry_delay`, `ssl_server_dn_match`; `mssql` `appname`, `charset`, `tds_version`. `ssl_context` and callables
  are rejected (not scalar), and **no path-bearing TLS option is in any allowlist**: a profile cannot put `sslrootcert`,
  `sslcert`, `sslkey` or `ssl_ca`/`ssl_cert`/`ssl_key` into `query` or `connect_args`. Certificates are configured only
  through `ssl_certs_dir` (section 3.1), which produces those arguments inside the module (decision D12). Without
  `ssl_certs_dir`, `sslmode: verify-full` verifies against the driver's default CA bundle only. (Design rule, not a
  database behaviour.) The Oracle, SQL Server and MySQL/MariaDB names are from vendor documentation, not observed
  (`UNVERIFIED(T1)` in `dialects.py`), except the PyMySQL TLS keyword names used by section 3.1, which were observed.
- `mysqlclient` (GPL) is excluded. LGPL drivers are accepted by the owner as unmodified pip dependencies (decision
  recorded 2026-10-29).

SQLAlchemy is pinned `>=2.0,<2.1` in `pyproject.toml`. T1 ran the same cases on SQLAlchemy 2.1.6: PostgreSQL, MySQL and
MariaDB results and the client-side Oracle and SQL Server results were identical to 2.0.55, and the `text()` traps in
section 5 still occur [WS 8]. Widening the pin remains decision D4.

## 8. Errors

One path (`lib/errors.py`): any exception raised while connecting or executing is mapped to
`CommandExecutionError("[E_CODE] <dialect> <sqlstate-or-vendor-code>: <message>")`; the native part is left out when there is
none. The module's `_run` sends every failure through `errors.raise_for`, and `engine.connection` does the same for connect,
session-setup and ping failures (the engine no longer classifies errors itself).

- An exception whose text already starts with a valid `[E_...]` (a code of the table below) is passed through by
  `errors.describe` / `raise_for` with its class and text unchanged. That is how validation (`SaltInvocationError`: options,
  profile shape, adapter allowlist, `runner` policy) and `CommandExecutionError` from policy and secrets
  (`E_EXECUTE_DISABLED`, `E_SECRET`, `E_DRIVER_MISSING`) keep their class. A `SaltInvocationError` or
  `CommandExecutionError` whose text does not start with a valid code becomes `E_INTERNAL` (class name only).
- The message never contains the SQL text, the bound values, the URL or the password. `raise_for` clears the exception
  chain, and `engine` removes the password (raw and URL-quoted) and any `scheme://...` text once more from the final message.
- How Salt shows it differs by entry point. The retcode is 1 in every case. This is read from the Salt 3008.2 source
  (`salt/cli/caller.py`, `salt/minion.py`); no minion was run with this module yet (task T10).

| Raised as | `salt-call` | remote minion job (`salt '<tgt>' ...`, `LocalClient`, salt-api) |
|---|---|---|
| `CommandExecutionError` | stderr `Error running 'sqlx.query': [E_...] ...` (`caller.py` 237-241) | return value `ERROR: [E_...] ...` (`minion.py` 3024-3031) |
| `SaltInvocationError` | the bare text `[E_...] ...` on stderr through `SystemExit` (`caller.py` 119-120) | return value `ERROR executing 'sqlx.query': [E_...] ...` (`minion.py` 3034-3041) |

  Clients must therefore look for the `[E_CODE]` token in the text, not for a fixed prefix. The words `Passed invalid
  arguments` that Salt prints are for a `TypeError` (a wrong Python signature), not for these errors.

**Catching `DBAPIError` is not enough**: these reached the caller as other types (observed, WS 5-7):
`TypeError` (PostgreSQL SQL_ASCII, first connect), `RuntimeError` and `AttributeError` (PyMySQL authentication),
`ValueError` (PyMySQL with `exec_driver_sql` and a literal `%`), and `sqlalchemy.exc.InvalidRequestError` for a mis-read bind
(`A value is required for bind parameter`). The error path therefore has to catch `Exception` around connect and execute.
Those map to `E_INTERNAL` (class name only) unless a rule says otherwise. The rules in `errors.py`, all from observed
cases: PostgreSQL SQL_ASCII `TypeError` -> `E_CONNECT` with a message asking for a UTF8 database; PyMySQL `RuntimeError` naming
the `cryptography` package -> `E_DRIVER_MISSING`; PyMySQL first-connect `AttributeError` (`is_auth_switch_request`) -> `E_CONNECT`
("retry the call"); `InvalidRequestError` `A value is required for bind parameter` and the `ValueError` / psycopg "only '%s',
'%b', '%t' are allowed as placeholders" from a literal `%` -> `E_OPTION_INVALID` with a hint (section 5). A bare `OSError`
raised while connecting (not wrapped by SQLAlchemy) is `E_INTERNAL`. For Oracle and SQL Server only the client-side
connect-refused code is mapped (`DPY-6005`, `20009` -> `E_CONNECT`); **every Oracle and SQL Server authentication, SQL, timeout
and read-only code is `E_INTERNAL` until task T1b** records the real ones.

**TLS failures** (observed, `docs/WIRE_SHAPES.md` section 12; the real driver message is kept, with paths reduced to file names by
`tls.scrub` in `engine`): PostgreSQL libpq text `SSL error: ...` (unknown CA, expired certificate on either side, `alert unknown ca`),
`... does not match host name ...`, `pg_hba.conf rejects connection ... no encryption`, `server does not support SSL`, `... private key
file ...` and `root certificate file ... does not exist` -> `E_CONNECT`; `connection requires a valid client certificate` and
`certificate authentication failed for user ...` -> `E_AUTH_FAILED`. MySQL/MariaDB: 2003 (certificate verify failed, hostname
mismatch) and 2013 (TLS alert from the server) -> `E_CONNECT`; 3159 (`require_secure_transport`) -> `E_CONNECT`; 1045 for an `X509`
or `SSL`-only account used without a client certificate or without TLS -> `E_AUTH_FAILED` (the server gives no TLS-specific reason);
PyMySQL's own `ssl.SSLError` (unusable key, key/certificate mismatch) and `FileNotFoundError`/`PermissionError` for a TLS file ->
`E_CONNECT` (the file name in the OS error is dropped).

`mysql` is the dialect name passed for MariaDB too (section 7), so messages read `[E_...] mysql <vendor code>: ...` on both
servers. Unless `error_detail` is true, MySQL/MariaDB messages have the duplicate-key value (`Duplicate entry '...'`) and the
`Incorrect ... value: '...'` value replaced by `'...'`, and PostgreSQL 22P02 `invalid input syntax ...: "..."` is scrubbed the same
way; PostgreSQL `DETAIL` is added only when `error_detail` is true.

| Code | Meaning | Source (observed unless marked unverified) |
|---|---|---|
| `E_PROFILE_UNKNOWN` | profile name not configured | validation |
| `E_PROFILE_INVALID` | missing, unknown, wrongly typed or inconsistent profile key; a profile value above a hard maximum; a `driver` that does not match the `dialect` | validation |
| `E_OPTION_UNKNOWN` / `E_OPTION_INVALID` | unknown call keyword / missing, mistyped or out-of-range value, unknown operator. A call limit above a cap is **not** an error: it is clamped (section 6, D9) | validation |
| `E_SECRET` | `sdb://` reference failed, resolved empty, resolved to a non-string, or was returned unresolved; the message carries the reference and the exception class name only | secrets |
| `E_POLICY` | `execute` verb not in `execute_verbs`, or a statement that does not start with a keyword; a `query` / `connect_args` key outside the driver allowlist (section 7); a statement timeout above the hard maximum passed to the adapter | policy (a call limit above a cap is not `E_POLICY`: it is clamped) |
| `E_EXECUTE_DISABLED` | `execute` on a profile without `allow_execute` | policy |
| `E_DRIVER_MISSING` | driver package not installed | import (`dialects.Adapter.load_driver`; the message names the pip extra only). The bare `RuntimeError` naming `cryptography` (PyMySQL authentication) maps here too |
| `E_CONNECT` | network/TLS/host failure | PostgreSQL `psycopg.OperationalError`, no SQLSTATE, text `Connection refused`; MySQL/MariaDB `OperationalError` 2003 (no SQLSTATE); Oracle `oracledb.exceptions.OperationalError` `DPY-6005`; SQL Server `pymssql.exceptions.OperationalError` 20009 (the last two are client-side rows, no server needed) |
| `E_AUTH_FAILED` | bad credentials | PostgreSQL: `psycopg.OperationalError` with **no SQLSTATE** (28P01 is not observable), only the text `FATAL:  password authentication failed`; MySQL/MariaDB: 1045 (SQLSTATE 28000). Access to an unknown/forbidden database as a non-admin gave 1044 (SQLSTATE 42000) on MySQL/MariaDB and `FATAL:  database ... does not exist` (no SQLSTATE) on PostgreSQL. Oracle `ORA-01017` and SQL Server 18456: **unverified**, and currently `E_INTERNAL` (see above) |
| `E_SQL` | syntax error, unknown object/column, permission denied | PostgreSQL `ProgrammingError`, class 42. MySQL/MariaDB: 1064 and 1146 are `ProgrammingError`, but 1054 (unknown column) and 1142 (permission denied) are `OperationalError`; all have SQLSTATE class 42, so key on the code, not the class |
| `E_SQL_DATA` | data exception (division by zero, bad cast) | PostgreSQL `DataError`, class 22. MySQL/MariaDB: 1365 (division by zero, only in data-changing statements; `SELECT 1/0` returns NULL) is `OperationalError`, SQLSTATE 22012; 1366 (bad value) is `DataError` with SQLSTATE `HY000` on MySQL and `22007` on MariaDB, so key on the vendor code |
| `E_CONSTRAINT` | integrity violation | PostgreSQL 23505 `IntegrityError`; MySQL/MariaDB 1062 `IntegrityError`, SQLSTATE 23000 |
| `E_READ_ONLY` | write attempted in a read-only context | PostgreSQL 25006 (`InternalError`); MySQL/MariaDB 1792 (`OperationalError`, SQLSTATE 25006). Oracle: **unverified** |
| `E_TIMEOUT` | statement timeout/cancel | PostgreSQL 57014 (`OperationalError`); MySQL 3024 (`HY000`); MariaDB 1969 (`70100`). MySQL does not raise for a table-less `SELECT SLEEP()` (WS 2). Oracle and SQL Server: **unverified** |
| `E_INTERNAL` | anything else; message is the exception **class name only** | catch-all, including the raw exceptions listed above |

Observed on PostgreSQL 16.15 (re-verified 2026-10-29):

| Input | SQLAlchemy / driver class | SQLSTATE | Code |
|---|---|---|---|
| `selec 1` | ProgrammingError / SyntaxError | 42601 | `E_SQL` |
| `select * from nope` | ProgrammingError / UndefinedTable | 42P01 | `E_SQL` |
| `select nope from t` | ProgrammingError / UndefinedColumn | 42703 | `E_SQL` |
| `select 1/0` | DataError / DivisionByZero | 22012 | `E_SQL_DATA` |
| duplicate key | IntegrityError / UniqueViolation | 23505 | `E_CONSTRAINT` |
| insert as a SELECT-only role | ProgrammingError / InsufficientPrivilege | 42501 | `E_SQL` |
| insert under `READ ONLY` | InternalError / ReadOnlySqlTransaction | 25006 | `E_READ_ONLY` |
| `pg_sleep(3)` with 300 ms timeout | OperationalError / QueryCanceled | 57014 | `E_TIMEOUT` |
| wrong password / unknown database / port closed | OperationalError / `psycopg.OperationalError` | none | `E_AUTH_FAILED` / `E_CONNECT` by message text only |

Observed on MySQL 8.0.46 and MariaDB 10.11.14 (PyMySQL 1.2.3; both servers unless two values are shown, MySQL / MariaDB):

| Input | SQLAlchemy / driver class | SQLSTATE | Vendor code | Code |
|---|---|---|---|---|
| `selec 1` | ProgrammingError / ProgrammingError | 42000 | 1064 | `E_SQL` |
| `select * from nope` | ProgrammingError / ProgrammingError | 42S02 | 1146 | `E_SQL` |
| `select nope from t` | OperationalError / OperationalError | 42S22 | 1054 | `E_SQL` |
| `select 1/0` | no error, returns `NULL` | - | - | none |
| `insert ... select 1/0` | OperationalError / OperationalError | 22012 | 1365 | `E_SQL_DATA` |
| string bound to an integer column | DataError / DataError | HY000 / 22007 | 1366 | `E_SQL_DATA` |
| duplicate key | IntegrityError / IntegrityError | 23000 | 1062 | `E_CONSTRAINT` |
| insert as a SELECT-only user | OperationalError / OperationalError | 42000 | 1142 | `E_SQL` |
| insert under `READ ONLY` | OperationalError / OperationalError | 25006 | 1792 | `E_READ_ONLY` |
| timeout | OperationalError / OperationalError | HY000 / 70100 | 3024 / 1969 | `E_TIMEOUT` |
| wrong password | OperationalError / OperationalError | 28000 | 1045 | `E_AUTH_FAILED` |
| port closed | OperationalError / OperationalError | none | 2003 | `E_CONNECT` |
| client `read_timeout` expired | OperationalError / OperationalError | none | 2013 | `E_TIMEOUT` (the text says "timed out"; any other 2013 is `E_CONNECT`; `errors._my`) |

Oracle and SQL Server: only the connection-refused rows are observed (see `E_CONNECT`). All other classes and codes for
them are **unverified**.

Message text. Observed fields: psycopg `exc.orig.diag.message_primary` (plus `message_hint` on some errors and
`message_detail` where the server sends one); PyMySQL `exc.orig.args == (code, message)` and `exc.orig.sqlstate`; oracledb
`exc.orig.args[0]` is an error object with `full_code` and `message`; pymssql `exc.orig.args == ((code, b"message"),)`
(bytes). Use the message of `exc.orig` and **never `str()` of the SQLAlchemy exception**: it appends `[SQL: ...]` and
`[parameters: ...]` with the bound values. `detail` is included only if `error_detail: true`, which works on
PostgreSQL (the row value is in `message_detail`) but not on MySQL/MariaDB, where the row value is part of the primary
message (`Duplicate entry 'a@example.com'`). The server's primary message can also echo a bound value (PostgreSQL 22P02,
MySQL/MariaDB 1366), so "never include bound values" cannot be guaranteed for the server's own text. Never include the SQL
text or the URL. No account password was found in any exception text or traceback in any observed case (WS 9).
Mapping must be keyed on the vendor code and SQLSTATE first, exception class second; SQLSTATE is absent for PostgreSQL
connect-phase errors and for MySQL/MariaDB client-side errors, and differs between MySQL and MariaDB for the same vendor
code, so for the MySQL family the vendor code is the primary key.

## 9. Return-value rules

Plain, strict JSON only: everything returned survives `json.dumps(..., allow_nan=False)` (`lib/serialize.py`). Conversions:

| Python value | Result |
|---|---|
| `None`, `bool`, `str`, `int` (any size) | unchanged (subclasses become the plain type; huge ints stay ints) |
| finite `float` | unchanged |
| `float` NaN, +Inf, -Inf | the strings `"NaN"`, `"Infinity"`, `"-Infinity"` (never a JSON `NaN`/`Infinity` token) |
| `Decimal` | `str(value)` (no float rounding; a non-finite Decimal gives `"NaN"`, `"Infinity"`, `"-Infinity"`) |
| `datetime`, `date`, `time` | ISO-8601 string (`isoformat()`) |
| `timedelta` | `str()` |
| `UUID` | string |
| `bytes`, `bytearray`, `memoryview` | `{"$bytes_b64": "<base64>"}` |
| `dict` | converted recursively; a key that is not a string becomes `str(key)` |
| `list`, `tuple` | list, converted recursively |
| anything else | `str()` |

Observed (WS 4, text query as account `rw`):
- PostgreSQL 16.15: numeric -> Decimal, timestamp -> datetime, uuid -> UUID, bytea -> bytes, jsonb -> dict. (Array -> list and
  interval -> timedelta were confirmed in the pre-T1 probe and are not in WS 4.)
- MySQL 8.0.46 and MariaDB 10.11.14: `DECIMAL` -> Decimal, `DATETIME(3)` -> datetime (microseconds kept), `BINARY`/`VARBINARY` ->
  bytes, NULL -> None; the JSON column and `CHAR(36)` identifiers come back as **`str`** (not dict, not UUID), so a JSON
  value from these servers is a string the caller has to parse.
- Oracle and SQL Server: NOT RUN.

## 10. Install

`pip install saltext-sqlx[postgres,mysql,oracle,mssql]` through `salt-pip` (Salt 3008's onedir is Python 3.11, so
reinstall after a Salt upgrade). `__virtual__` returns `(False, reason)` if SQLAlchemy is missing; each driver is imported
lazily, so a missing Oracle driver only affects Oracle profiles.

## 11. REST example

```bash
curl -sS -k https://salt-api.example:8000/run -H 'Content-Type: application/json' -d '[{
  "client":"local","tgt":"db-minion-01","fun":"sqlx.query",
  "kwarg":{"profile":"orders_pg","sql":"select id,total from orders where status = :s","params":{"s":"open"},"limit":50},
  "eauth":"pam","username":"<user>","password":"<password>"}]'
```

## 12. Open decisions (assumption used if unanswered)

| ID | Decision | Assumption |
|---|---|---|
| D1 | Allow ad-hoc connection details in the call? | No. Profile only. |
| D2 | Default for `execute(dry_run)` | `false` (matches Robot); consider `true` for agent use. |
| D3 | Rewrite SQL to escape literal colons? | No; document `\:`. |
| D4 | Support SQLAlchemy 2.1.x | Pin `<2.1` until tested. |
| D5 | Keep `table_list` in P0 | Yes. |
| D6 | Column-level result masking | Deferred. |
| D7 | Multiple result sets / OUT params | Not supported. |
| D8 | `pyodbc` as an alternative MSSQL driver | Deferred; keep the driver table pluggable. |

### Open decisions added after wave 1 (assumption used if unanswered)

| ID | Decision | Assumption |
|---|---|---|
| D9 | A call value above a cap (`limit`, `timeout_ms`, `max_bytes`, `retry_timeout_s`): clamp silently, or reject? | **Clamp** (current code: `options.apply_caps`). Rejecting with `E_OPTION_INVALID` would tell the caller its request was not honoured; clamping is friendlier to agents but hides the truncation until `truncated` is read. Profile values above a hard maximum are already rejected. |
| D10 | Is `mariadb` a separate `dialect` value, or detected at run time through `dialect.is_mariadb`? | Detect (`mysql` is the only value; WS 9 item 5 shows `is_mariadb` is `False` on MySQL and `True` on MariaDB). A separate value would make the choice of variable explicit in config and let `profiles.py` reject a wrong guess, at the cost of a fifth dialect and the same driver. |
| D11 | Use PyMySQL `read_timeout` as a client-side backstop for MySQL/MariaDB statement timeouts? | **Yes, implemented (T11 D2).** `read_timeout = statement_timeout_ms / 1000 + 1` s, module-owned; a profile may lower it, not raise it. Observed on MySQL 8.0.46: after the client gives up with 2013 the server keeps running the statement until it finishes, then rolls back (0 rows persisted for an `INSERT ... SELECT SLEEP(8)`). The backstop bounds the caller's wait, not the server's work. |

### Open decisions added after wave 3 (assumption used if unanswered)

| ID | Decision | Assumption |
|---|---|---|
| D12 | **Decided (owner) and implemented**: a TLS certificate directory so that profiles can use database TLS with verification and mutual TLS. | Profile keys `ssl_certs_dir`, `ssl_mode` (`verify-full` default, `verify-ca`), `ssl_files` (`ca`, `cert`, `key` as plain file names); default names `ca.pem`/`ca.crt`, `client.pem`/`client.crt`, `client.key`; the directory must resolve under `sqlx:_policy:ssl_certs_dir_root` (minion config or pillar), else `[E_POLICY]`; key files with group/world access are refused. Full rules and the per-driver mapping are in section 3.1. **Verified on real servers** (throw-away PKI, `tests-support/tls`): PostgreSQL 16.15 (libpq 18.0.6, psycopg 3.3.6), MySQL 8.0.46 and MariaDB 10.11.14 (PyMySQL 1.2.3): verify-full with and without a client certificate, mutual TLS, wrong CA, hostname not in the SANs, expired client and server certificate, client certificate from another CA, client certificate required but missing, plain connection to a TLS-only server, vanished or corrupt key file; error shapes in `docs/WIRE_SHAPES.md` section 12. **NOT verified**: Oracle (argument mapping only, `UNVERIFIED(T1b)`), SQL Server (refused as unsupported), `verify-ca` on Oracle, certificate revocation (no CRL support), encrypted private keys (there is no passphrase key; not tested), TLS 1.2-only or cipher policy (driver defaults). Open: whether the policy should also require the directory itself not to be group/world-writable (not checked today). |
| D13 | `MaskingFilter.add_secret` (called by `sqlx_mod._run` for every resolved password) only ever adds: the set of registered secrets gains one entry per distinct password and is never emptied for the life of the minion process (a memory and hygiene issue; retired passwords stay in memory and keep being masked). Clear or replace the registered secret after each call? | Not decided; today it is never cleared. Clearing after the call keeps the set small and drops stale passwords, but a log record emitted after the call (by another thread, or a late handler) would then no longer be masked by value; the key-based masking (`password|secret|token`) would still apply. Stricter is to keep it, so keep it until decided. |
