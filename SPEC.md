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

Profiles live under `sqlx:profiles:<name>` in minion config or pillar. **By default callers choose a profile and do not
supply connection details** (D1: profile-only by default). An operator can opt in to call-time connection parameters
under a policy that lives only in minion config or pillar (section 3.2); with nothing configured the behaviour is exactly
the profile-only one.

```yaml
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql          # required: postgresql | mysql | oracle | mssql
      driver: pg8000               # optional; defaults to the dialect's first driver (postgresql: pg8000 | psycopg; mysql: pymysql; oracle: oracledb; mssql: pymssql), any other value is rejected (D15)
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
| `driver` | optional; defaults to the first driver of the dialect (`postgresql` **pg8000**, alternative `psycopg`; `mysql` pymysql, `oracle` oracledb, `mssql` pymssql, decision D15); any other value is rejected, and the message names the choices. The single source of truth for this table is `lib/dialects.py` (`DIALECT_DRIVER_CHOICES`, `DIALECT_DRIVERS` is its first column); `profiles.py` imports both. A call cannot change it (section 3.2) |
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
| `ssl_certs_dir` | absolute string without `..` (between either separator on Windows, where `/` and `\` both separate); relative or `..`: `[E_PROFILE_INVALID]`. Resolves outside the root (including through a symlink): `[E_POLICY]`. Missing or not a directory: `[E_PROFILE_INVALID]` |
| `ssl_mode` | `verify-full` (default when a directory is set) or `verify-ca`. `disable`, `prefer`, `allow` are refused with a directory. `require` is refused too: on MySQL/MariaDB it encrypts without verifying the server, and on PostgreSQL libpq treats `require` plus a root certificate as `verify-ca` (observed: `sslmode=require` with a wrong `sslrootcert` fails with `certificate verify failed`), so it adds nothing |
| `ssl_files` | optional mapping, keys `ca`, `cert`, `key`; each value a plain file name (no `/`, `\`, `..`, empty or NUL). Unknown key or bad name: `[E_PROFILE_INVALID]`. Not accepted for `oracle` |
| default names | `ca.pem` (fallback `ca.crt`), `client.pem` (fallback `client.crt`), `client.key`. A fallback is used only when it is the only candidate; if both `ca.pem` and `ca.crt` (or both client names) exist: `[E_PROFILE_INVALID]` asking for `ssl_files`. A name given in `ssl_files` disables the fallback and must exist |
| required / paired files | CA missing: `[E_PROFILE_INVALID]` naming the file name only. A client certificate without its key, or a key without its certificate: `[E_PROFILE_INVALID]`. No client pair at all is fine (server authentication only) |
| every file | must resolve under the root (a file symlink to somewhere else under the root is allowed, one that leaves it is `[E_POLICY]`), be a regular file (`[E_PROFILE_INVALID]`) and be openable by the minion (`[E_PROFILE_INVALID]`, "not readable") |
| private key mode | owner-only (`0600`/`0400`), or `0640` when owned by root (libpq's own rule). Any world access or group write/execute, or a group-readable key not owned by root: `[E_POLICY]` saying to `chmod 600` (or 640 when root-owned). Checked from `stat`; PyMySQL does not check key modes itself, PostgreSQL's libpq does. On a Windows minion `stat` reports only the read-only bit, so every client key is `[E_POLICY]` ("cannot be used on a Windows minion"); a directory with the CA file only works there |
| contents | **never read** by the module (only name, location, type, mode and openability are checked) and never in a message or log |
| order | everything above runs in `validate_profile` (called by `load_profile`), before the secret is resolved and before any network call |
| `mssql` | `[E_PROFILE_INVALID] ... 'ssl_certs_dir' is not supported for mssql yet`. pymssql 2.4.6 `connect()` has only `encryption` (`off`/`request`/`require`); it takes no CA file or client certificate, and FreeTDS reads its own configuration file, which this module does not manage |

Mapping to the drivers (`tls.py`; the file arguments are the real paths found above):

| Dialect | Arguments | Where |
|---|---|---|
| `postgresql` / pg8000 (default, D15) | `ssl_context`: `ssl.create_default_context(cafile=<profile CA>)` (only that CA is trusted, `CERT_REQUIRED`), `check_hostname` true for `verify-full` and false for `verify-ca`, and with a client pair `load_cert_chain(cert, key)`. Built by `tls.pg8000_context` when the engine is created, so a key that cannot be loaded fails there with `[E_CONNECT]` (WS 16.3) | `connect_args` |
| `postgresql` / psycopg 3 (libpq), `driver: psycopg` | `sslmode` (`verify-full` or `verify-ca`), `sslrootcert`, and with a client pair `sslcert` and `sslkey`; without a pair `sslcertmode=disable`, so that libpq does not present `~/.postgresql/postgresql.crt` or `$PGSSLCERT` | URL query |
| `mysql` (MySQL and MariaDB) / PyMySQL | `ssl_ca`, `ssl_verify_cert=True`, `ssl_verify_identity=True` for `verify-full` and `False` for `verify-ca`, and with a client pair `ssl_cert`, `ssl_key` | `connect_args`, not the URL: SQLAlchemy moves URL-query `ssl_ca`/`ssl_cert`/`ssl_key` into PyMySQL's deprecated `ssl` dict, which PyMySQL then ignores as soon as an `ssl_verify_*` keyword is also present (PyMySQL 1.2.3 `connections.py`) |
| `oracle` / python-oracledb thin | `protocol="tcps"`, `wallet_location=<directory>`, `ssl_server_dn_match=True` for `verify-full`, `False` for `verify-ca`; the directory must contain `ewallet.pem` | `connect_args`. **UNVERIFIED(T1b)**: Oracle ran in T1b, but the Sakila image has no TCPS listener and none was configured, so only the argument mapping (unit tests) and the profile validation (`ewallet.pem` required, `[E_PROFILE_INVALID]` without it, observed end to end) are verified. The `ewallet.pem` name, the `wallet_password` case (an encrypted wallet is not supported: no password key exists) and the `verify-ca` meaning of `ssl_server_dn_match=False` come from the python-oracledb documentation |
| `mssql` | refused (above) | observed (T1b, end to end): `[E_PROFILE_INVALID] ... 'ssl_certs_dir' is not supported for mssql yet`, before any connection |

Call arguments may **lower** `row_limit` (`limit`), `statement_timeout_ms` (`timeout_ms`) and `max_bytes`, never raise them.
A call value above the profile value or the hard maximum is **clamped silently** to the lowest of call, profile and hard
maximum (`options.apply_caps`); it is not an error (D9).

### 3.2 Call-time connection parameters (`lib/overrides.py`, decision D1)

**Off by default.** Without `sqlx:_policy:allow_call_params` (absent, `null` or `[]`) every connection keyword below is
`[E_OPTION_UNKNOWN]` exactly as before, `psql_query`'s connection arguments are `[E_POLICY]`, and no code path of this
section runs. The policy is read by the module from `__salt__["config.get"]("sqlx")` and passed into `lib/` (no Salt
dunders there); it is never taken from the call, and a call that names `_policy` is `[E_OPTION_UNKNOWN]`.

**Call keywords** (on `query`, `execute`, `check_query_result`, `check_row_count`, `ping`, `table_list`, and the
equivalents on `psql_query`): `connection` (an object) and the flat keywords `dialect`, `driver`, `host`, `port`, `database`,
`username`, `password`, `ssl_mode`, `ssl_certs_dir`, `ssl_files`, `query`, `connect_args`, `connect_timeout_s`,
`statement_timeout_ms`, `read_only`. Flat keywords and `connection` are merged; a key present in both (with a non-null
value) is `[E_OPTION_INVALID]`. An unknown key inside `connection` is `[E_OPTION_UNKNOWN]`; `connection` that is not an object is
`[E_OPTION_INVALID]`. `port` must be an integer (a quoted `"5432"` is refused, never coerced). `profile` stays and means
"the base profile to overlay"; when it is absent `sqlx:default_profile` is used. `profile` may be omitted altogether only when
`allow_adhoc_connection` is true and the call supplies `dialect`, `host`, `database` and `username`; otherwise
`[E_PROFILE_UNKNOWN]`/`[E_OPTION_INVALID]`/`[E_POLICY]` as before. `mariadb` is accepted as a `dialect` value and means `mysql` (D10).

**Policy** (`sqlx:_policy`, minion config or pillar):

| Key | Meaning |
|---|---|
| `allow_call_params` | list of field names (from the keyword list above) a caller may set. Default `[]` = feature off. `"*"` and unknown names are refused (`[E_POLICY]`, the module fails closed). A profile may carry its own `allow_call_params`; it can only **narrow** the global list (the intersection is used) |
| `allowed_hosts` | **required** (non-empty) when `host` is in `allow_call_params` or `allow_adhoc_connection` is true. Entries: exact names, `fnmatch` globs (`*.corp.example`), IP addresses, CIDR ranges. An entry made only of `*`/`?` is refused (`[E_POLICY]`); `0.0.0.0/0` is accepted as a CIDR but cannot lift the denied ranges below |
| `allowed_ports` | optional list of ports or `"low-high"` ranges; a call port outside it is `[E_POLICY]` |
| `allowed_dialects` | optional list; a call (or ad-hoc) dialect outside it is `[E_POLICY]` |
| `allow_adhoc_connection` | default `false`; see above. An ad-hoc connection can never execute (it has no profile, so no `allow_execute`) |
| `allow_call_literal_password` | default `false`; see "Secrets" |
| `inherit_password` | default `false`; see "Credential relay". **Deviation from the owner's design, recorded in section 12 (D14)** |

**Host checks** (run before any secret is resolved or socket opened, only for a `host` that came from the call; the base
profile's own host is operator configuration and is not checked): the text must be a plain host name or IP (`[A-Za-z0-9._:-]`,
no `@`, `/`, `#`, whitespace, NUL); then (1) an IP literal must be inside a CIDR/IP entry (or be matched by a name entry);
(2) a name matching a name/glob entry is allowed, any other name is resolved (`getaddrinfo`) **only if the list contains a CIDR
or IP** and is allowed only if every resolved address is inside the list (a name that does not resolve and is not listed is
`[E_POLICY]`; a listed name that does not resolve is `[E_CONNECT]`); (3) whatever allowed it, every resolved address is
checked against the denied ranges `0.0.0.0/8`, `127.0.0.0/8`, `169.254.0.0/16` (the cloud metadata address
`169.254.169.254` is inside it), `224.0.0.0/4`, `::`, `::1`, `fe80::/10`, `ff00::/8`, `fd00:ec2::254`; a denied address is allowed
only by an IP/CIDR entry that lies **inside** that range (for loopback: `127.0.0.1` or `127.0.0.0/8` in the list, never a name,
a glob, or `0.0.0.0/0`). IPv4-mapped IPv6 is unwrapped; a scope id is dropped. The driver resolves the name again when it
connects, so a DNS record that changes between the check and the connect is a residual risk (SECURITY section 3).
Messages name the key only, never the host.

**Per-field rules** (every row is `[E_POLICY]` unless another code is named):

| Field | Rule |
|---|---|
| any field | must be listed in the effective `allow_call_params` (global, narrowed by the profile's own list) |
| `host`, `port`, `dialect`, `username` | `host`/`port`: checks above. A call that changes `host`, `port`, `username` or `dialect` relative to the base profile does **not** inherit the profile's `password` (the connection is made without one) unless the call supplies `password` or `inherit_password` is true. For PostgreSQL the adapter also passes an explicitly empty `password` and an unreadable `passfile`, so libpq does not answer the target's password request from the minion's `$PGPASSWORD` or `~/.pgpass` (observed: without this the fake server received `$PGPASSWORD`). Ad-hoc connections without a `password` get the same treatment |
| `database`, `driver` | text; `driver` must be one of the dialect's drivers (profile validation); `dialect` change drops the base `driver` |
| `password` | must start with `sdb://` (resolved on the minion through `sdb.get`, `[E_SECRET]` if it fails) unless `allow_call_literal_password: true`; a literal with the policy off is `[E_POLICY]` naming the key only |
| `read_only` | may only be `true`; `false` is refused; non-boolean is `[E_OPTION_INVALID]` |
| `statement_timeout_ms`, `connect_timeout_s` | may only **lower** the profile value (clamped to the lowest of call, profile, hard maximum) |
| `ssl_mode` | may only tighten: `verify-full` > `verify-ca`; weakening relative to the base profile's `ssl_mode` (default `verify-full` when it has a certificate directory) is refused; `ssl_mode` without a certificate directory is rejected by TLS validation (`[E_PROFILE_INVALID]`) |
| `ssl_certs_dir`, `ssl_files` | go through `lib/tls.py` unchanged: absolute, no `..`, realpath under `sqlx:_policy:ssl_certs_dir_root` (`[E_POLICY]` otherwise), plain file names, key mode, file types |
| `query`, `connect_args` | merged over the profile's own entries; every key must be in the per-driver allowlist (section 7, default deny) and may not be a TLS-owned key (`[E_POLICY]`/`[E_PROFILE_INVALID]`) |

**Hard rules no policy relaxes.** A call can never set `row_limit`, `allow_execute`, `execute_verbs`, `error_detail` or `_policy`:
they are not in the schema and are `[E_OPTION_UNKNOWN]` (the profile-only keys stay profile-only). The existing call options
`limit`, `timeout_ms` and `max_bytes` can only lower the cap (D9). `read_only` can be turned on, never off. Caps can only be
lowered. `execute` still needs `allow_execute: true` on the **base profile** and the verb filter.

**Secrets and audit.** A resolved password is never in a message, log record, return or audit line. The audit line (SECURITY
section 5) gains `overrides=<comma list of field names>` (`-` when none) and `target=<host>:<port>` (the one actually used;
`-` when the call was refused before a profile was built). A literal `password` that the policy allows is added to the log
masking filter. Salt stores the job load (and prints it at debug level in the minion log) before the extension sees it, so a
`password=sdb://...` reference, or a literal, is in the master job cache and on the event bus: this is why a literal is refused
by default (verified: `tests/integration/params_path`).

### 4.7 `sqlx.psql_query(query, user=None, host=None, port=None, maintenance_db=None, password=None, runas=None, write=False, profile=None)`

Look-alike of Salt 3008 `postgres.psql_query(query, user=None, host=None, port=None, maintenance_db=None, password=None,
runas=None, write=False)` (the same first eight parameters in the same order; `profile` is added). Mapping: `query` -> `sql`,
`user` -> `username`, `maintenance_db` -> `database`, `host`, `port`, `password` as in section 3.2. `profile` defaults to
`sqlx:default_profile` and must name a postgresql profile (`[E_OPTION_INVALID]` otherwise). Other keywords of `query`/`execute`
(`params`, `limit`, `timeout_ms`, `max_bytes`, `dry_run`, and the flat connection keywords) are accepted. Using `user`, `host`,
`port`, `maintenance_db` or `password` needs the policy (`[E_POLICY]` when the feature is off or the field is not listed);
the call with only `query` (and a profile) always works.

Differences from `postgres.psql_query`, all deliberate:

| Aspect | `postgres.psql_query` | `sqlx.psql_query` |
|---|---|---|
| mechanism | runs the `psql` binary as `runas` (OS user) | SQLAlchemy and the profile's driver; no `psql`, no OS user. For a retargeted connection without its own password libpq's `$PGPASSWORD` and `~/.pgpass` are switched off (see section 3.2); an unchanged profile keeps libpq's normal behaviour |
| `runas` | OS user to run `psql` as | any non-`None` value is `[E_OPTION_INVALID] runas is not supported; sqlx does not use psql or an OS user` |
| failure | returns `[]`/`False`/the `psql` stderr as data | raises `[E_...]` with the real database error (`CommandExecutionError`, or `SaltInvocationError` for argument/policy errors); never returns an empty list for a failure |
| `write=True` | allows DML | goes through the `execute` path: needs `allow_execute: true` on the profile and the verb filter; returns one row `{"rows_affected": "<n>"}`; never bypasses them. `write=False` runs in a transaction that is rolled back |
| result | list of dicts, column -> str or `None` | the same shape: values are text, `NULL` is `None`, booleans are `t`/`f`, JSON values are compact JSON text; a trailing `;` is removed like Salt does. Rows beyond the row or byte cap are dropped without a flag (use `sqlx.query` to see `truncated`) |
| `password` | literal | an `sdb://` reference unless `allow_call_literal_password` |
| `host`/`port`/`user`/`maintenance_db` | free | policy-gated (section 3.2), not free |
| multi-statement | `psql -c` may run several | single statement; the read-only guard is bypassable on PostgreSQL (SECURITY section 2) |

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
- One statement per call. On PostgreSQL the connection uses the extended protocol for EVERY statement: psycopg through
  `prepare_threshold=0`, pg8000 through a cursor that always calls `Connection.execute_unnamed` (stock pg8000 uses the simple
  protocol when there are no binds and ran `select 1; select 2` and `commit; create ...` as written, WS 16.1); both are set
  by the module and not settable from a profile, and pg8000's behaviour is proved on the first connection of the process
  (`[E_DRIVER_MISSING]` if it fails, so no unguarded SQL ever runs). So a string with a second statement (`INSERT ...; COMMIT`) fails with
  `[E_SQL] postgresql 42601: cannot insert multiple commands into a prepared statement`; PyMySQL does not enable
  multi-statements, so MySQL and MariaDB answer 1064 (T11 D1); Oracle answers ORA-03405 `End of query reached; no additional
  text should follow` (T1b). SQL Server runs every statement of a batch (observed, T1b: `a; b`, `a b` without a separator,
  and `a; commit`, whose COMMIT survived the rollback), so on `mssql` `runner` refuses, before sending, a top-level `;` that
  is followed by more SQL (string literals, `[..]`/`".."` identifiers and comments are skipped), and after execution a second
  result set (`select 1 select 2`), rolling back first: `[E_POLICY] SQL Server runs every statement of a batch: the statement
  string contains more than one statement (...)`. A separator-less batch of statements that return no rows is **not**
  detected (residual risk, SECURITY section 2). A trailing `;` is accepted on every dialect.
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

Observed on PostgreSQL 16.15, MySQL 8.0.46 and MariaDB 10.11.14 (WS 5), and unchanged on SQLAlchemy 2.1.6 (WS 8); on Oracle
23.26.2 and SQL Server 2022 by T1b (WS 14, 15, SQLAlchemy 2.0.55 only), and through the module on all five by `tests-e2e/`.

- Callers pass values via `params` (a JSON object). Binds in the SQL are written `:name`. **Write casts as
  `CAST(:name AS <type>)`, never `:name::int`**, with the dialect's own type name: `INTEGER` works on PostgreSQL, MariaDB,
  Oracle and SQL Server, but **MySQL's CAST takes `SIGNED`/`UNSIGNED`** (`CAST(:x AS INTEGER)` is 1064 on MySQL 9.7.1,
  observed in T1b; `CAST(:x AS SIGNED)` works) — SQLAlchemy `text()` does not treat `:name::int` as a bind: the driver
  receives the literal `:a` and fails (PostgreSQL 42601, MySQL/MariaDB 1064, SQL Server 102 `Incorrect syntax near ':'`;
  python-oracledb sees an unbound placeholder: `DPY-4010 ... placeholder ":A" was not provided`, `E_OPTION_INVALID`).
- A literal colon followed by a word or digit inside a string, for example `'{"a":1}'`, is mis-read as a bind by
  `text()` (`sqlalchemy.exc.InvalidRequestError`: `A value is required for bind parameter '1'`, on all five servers).
  Callers escape it as `\:`. The module does not rewrite the SQL (D3).
- `%` is fine in `text()`: SQLAlchemy doubles it for psycopg and PyMySQL, and with pg8000 (`numeric_dollar`, no `%` scanning) it reaches the server as written, so `5 % 3`, `LIKE '%b'` and `'%s'` are data (WS 16.1; stock pg8000 raised `Only %s and %% are supported` when binds were present). Never use `exec_driver_sql` for caller SQL: a literal
  `%` with no parameters fails with `psycopg.ProgrammingError` (psycopg) and a bare `ValueError` (PyMySQL); on Oracle and SQL
  Server it runs (observed). On SQL Server a literal `%` together with binds runs correctly through `text()` (observed:
  `case when 'abc' like '%b%' then cast(:x as int) ...`).
- One statement per call. PostgreSQL (extended protocol), MySQL/MariaDB and Oracle (ORA-03405) refuse a second statement
  themselves; SQL Server runs a whole batch, so on SQL Server the module refuses a `;` followed by another statement and a
  second result set (`[E_POLICY]`, section 4.2). A SQL Server batch without separators and without a result set is not
  detected; the database role is the control (SECURITY.md).
- A NUL character in `sql` is refused on every dialect (`[E_OPTION_INVALID]`, `options.validate`): psycopg and pymssql cut the
  statement at the NUL (observed in T1b through raw SQLAlchemy: `delete from t\x00 where id = 99999` deleted every row on
  PostgreSQL and SQL Server). On SQL Server a NUL inside a bound string is refused too, because pymssql interpolates bound
  values into the statement text (observed: error 105, unclosed quotation mark).
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

Labels. **observed**: seen on a real server, evidence in `docs/WIRE_SHAPES.md` (`WS n` = its section n): by task T1 on
2026-10-29 (PostgreSQL 16.15, MySQL 8.0.46, MariaDB 10.11.14, SQLAlchemy 2.0.55 and 2.1.6, psycopg 3.3.6, PyMySQL 1.2.3)
and by task T1b on 2026-10-31 through the end-to-end suite `tests-e2e/` (Salt 3008.5 onedir, Python 3.14.7, SQLAlchemy
2.0.55; Oracle AI Database 26ai Free 23.26.2 with python-oracledb 26.1.0 thin, SQL Server 2022 CU25 16.0.4255.1 with
pymssql 2.4.6, and PostgreSQL 18.4, MySQL 9.7.1, MariaDB 12.3.2 from the Sakila images). **unverified**: not run; kept from
vendor documentation and must not be relied on.

| dialect | driver (pip extra) | URL | Licence (package metadata) | timeout mechanism | in-session read-only |
|---|---|---|---|---|---|
| `postgresql` | **`pg8000`** (extra `postgres`, default; pure Python, pinned `>=1.31.6,<1.32`) | `postgresql+pg8000://` | BSD-3-Clause | the same statements as for psycopg; the socket read timeout is set to `statement_timeout_ms / 1000 + 1` s after connecting as a backstop (pg8000 would keep the connect timeout there, WS 16.1). **observed** [WS 16] | the same; pg8000 refuses writes with 25006 too. **observed** [WS 16] |
| `postgresql` | `psycopg[binary]` (extra `postgres-psycopg`, `driver: psycopg`) | `postgresql+psycopg://` | LGPL-3.0-only | `SET statement_timeout = <ms>`: cancels at the limit, SQLSTATE 57014. **observed** [WS 2] | `SET TRANSACTION READ ONLY`: refuses writes (25006) but **bypassable** (SECURITY.md section 2). **observed** [WS 3] |
| `mysql` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` | MIT | `SET SESSION MAX_EXECUTION_TIME=<ms>`: raises 3024 (SQLSTATE `HY000`) for a SELECT that reads tables; a table-less `SELECT SLEEP(n)` is cut short **silently** (returns 1, no error); `INSERT ... SELECT` is **not limited** by it; a statement can also override it with `/*+ MAX_EXECUTION_TIME(n) */`. On MySQL 9.7.1 (T1b) a long SELECT that does real work fails
with 3024, but `SLEEP()` is cut short **silently** even in a SELECT that reads a table (`select sleep(5) from actor limit 1`
returned 1 after 1.0 s), and the hint override is ended by the `read_timeout` backstop (2013, `E_TIMEOUT`). The module therefore also sets PyMySQL `read_timeout` = timeout + 1 s (a lower profile value is honoured), which ends the caller's wait with 2013 / `E_TIMEOUT` (T11 D2). **observed** on 8.0.46 only [WS 2]. The earlier "MySQL >= 5.7.8" is **unverified**. | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792, SQLSTATE 25006) but **bypassable** by four statements. `START TRANSACTION READ ONLY` resists the most statements but is also bypassable. **observed** [WS 3] |
| `mariadb` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` (SQLAlchemy sets `dialect.is_mariadb`, `False` on MySQL and `True` on MariaDB; use it to pick the variable) | MIT | `SET SESSION max_statement_time=<seconds>`: fractional values accepted; raises 1969 (SQLSTATE `70100`) for SELECT and for `INSERT ... SELECT`; `SET STATEMENT max_statement_time=0 FOR ...` overrides it, so `read_timeout` (timeout + 1 s) is the backstop here too (T11 D2). **observed** on 10.11.14 [WS 2]. `MAX_EXECUTION_TIME` does not exist here (1193). | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792) but **bypassable**; the session variable is `tx_read_only` (`transaction_read_only` does not exist, 1193). **observed** [WS 3] |
| `oracle` | `oracledb` thin mode (extra `oracle`) | `oracle+oracledb://` (profile `database` is the **service name**, sent as `service_name`) | UPL-1.0 OR Apache-2.0 | `Connection.call_timeout` (ms), set on the driver connection: a statement over the limit fails with `DPY-4024: call timeout of <n> ms exceeded` (`E_TIMEOUT`, about 80 ms after the limit); a PL/SQL call (`dbms_session.sleep`) ended with `DPY-4011 ... socket timed out while recovering from previous socket timeout` and a closed connection (`E_TIMEOUT`). Client-side, so a statement cannot lift it. Connect: `tcp_connect_timeout` (s), observed `DPY-6005 ... timed out` after 3.0 s. **observed** [WS 14] | `SET TRANSACTION READ ONLY` as the first statement: refuses writes with ORA-01456 (`E_READ_ONLY`); a later `SET TRANSACTION READ WRITE` is ORA-01453; **bypassable** by one PL/SQL block (`begin commit; insert ...; commit; end;` persisted the row). **observed** [WS 14] |
| `mssql` | `pymssql` (extra `mssql`) | `mssql+pymssql://` | LGPL-2.1 | pymssql connect argument `timeout` (whole seconds, rounded up from `statement_timeout_ms`): a statement over the limit fails with DB-Lib 20003 `TDS server connection timed out` + 20047 `DBPROCESS is dead or not enabled` (`E_TIMEOUT`) and the connection is unusable afterwards. Client-side. Connect: `login_timeout` (s), observed 20009 `... Connection timed out (110)` after 3.0 s. **observed** [WS 15] | **none**: a write through `sqlx.query` runs and is rolled back; the role is the only control. A batch can carry its own `COMMIT` (observed), so the module refuses a second statement after `;` and a second result set (`[E_POLICY]`, section 4.2); a separator-less batch without result sets cannot be detected client-side. **observed** [WS 15] |

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
- The dialect-to-driver table (`postgresql` pg8000 or psycopg, `mysql` pymysql, `oracle` oracledb, `mssql` pymssql) lives in
  `lib/dialects.py` (`DIALECT_DRIVER_CHOICES`, first entry = default); `profiles.py` imports it.
- PostgreSQL drivers compared (D15, every difference observed, WS 16): the error text is the same for statement errors; pg8000 adds the
  SQLSTATE to connect errors (`28P01`, `3D000`) and words socket failures from `__cause__`; pg8000 allows only `application_name` in
  `query`/`connect_args` (TLS is the `ssl_context` of `ssl_certs_dir`; no `sslmode`, `keepalives*`, `target_session_attrs`); pg8000 does
  not read `$PGPASSWORD`/`~/.pgpass` or Unix-socket hosts; without `ssl_certs_dir` both use TLS opportunistically and unverified
  (libpq `prefer`); values psycopg cannot hold (`infinity`, year > 9999) come back as text with pg8000.
- Only `mysql` is a valid `dialect` value; the `mariadb` row above is a flavour of it (D10). The module passes the profile's
  dialect name, so `errors.raise_for` and `errors.translate` receive `mysql` for MariaDB as well (the error table also accepts
  `mariadb`, but the module never passes it).
- Oracle: SQLAlchemy reports the server version from `Connection.version` (`23.26.2.0.0`), and normalises Oracle's upper-case
  names to lower case in `columns` and in `table_list`. An account other than the schema owner needs synonyms (or qualified
  names) to reach the tables; `table_list` takes `schema` (for example `sakila`) and lists what that account can see.
  SQL Server: `server_version` is the full `@@VERSION` text (several lines). [WS 14, 15]
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
  database behaviour.) Every allowlisted name was passed to a real driver by the T1b end-to-end suite (`<db>_args` profiles in
  `tests-e2e/salt/minion.d/10-sqlx.conf`): the connection succeeded and, where the server shows it, the value arrived
  (PostgreSQL `application_name`, SQL Server `app_name()`, MySQL/MariaDB `character_set_client`). Oracle `expire_time`,
  `retry_count`, `retry_delay` are accepted by python-oracledb, but their effect (keep-alive probes, connect retries) was not
  measured; `ssl_server_dn_match` was not run against a TLS listener (section 3.1).
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
- How Salt shows it differs by entry point. **Observed** on Salt 3008.5 (onedir) by the T1b end-to-end suite
  (`tests-e2e/`), on all five databases:

| Raised as | `salt-call` (exit code 1) | remote minion job (`salt '<tgt>' ...`, `LocalClient`, salt-api; job `retcode` 1) |
|---|---|---|
| `CommandExecutionError` | stderr `Error running 'sqlx.query': [E_...] ...` | return value `ERROR: [E_...] ...` |
| `SaltInvocationError` | `Passed invalid arguments: [E_...] ....` followed by the function's docstring | return value `ERROR executing 'sqlx.query': [E_...] ...` |

  Clients must therefore look for the `[E_CODE]` token in the text, not for a fixed prefix. Salt prints the same `Passed
  invalid arguments` words for a `TypeError` (a wrong Python signature, for example an unquoted `operator===`), but then
  without an `[E_...]` token. The `salt` command itself exits 0 when the minion returned an error (observed with
  `--out=json --static`); read the job's `retcode` (salt-api `full_return`) or the `[E_...]` token.

**Catching `DBAPIError` is not enough**: these reached the caller as other types (observed, WS 5-7):
`TypeError` (PostgreSQL SQL_ASCII, first connect), `RuntimeError` and `AttributeError` (PyMySQL authentication),
`ValueError` (PyMySQL with `exec_driver_sql` and a literal `%`), and `sqlalchemy.exc.InvalidRequestError` for a mis-read bind
(`A value is required for bind parameter`). The error path therefore has to catch `Exception` around connect and execute.
Those map to `E_INTERNAL` (class name only) unless a rule says otherwise. The rules in `errors.py`, all from observed
cases: PostgreSQL SQL_ASCII `TypeError` -> `E_CONNECT` with a message asking for a UTF8 database; PyMySQL `RuntimeError` naming
the `cryptography` package -> `E_DRIVER_MISSING`; PyMySQL first-connect `AttributeError` (`is_auth_switch_request`) -> `E_CONNECT`
("retry the call"); `InvalidRequestError` `A value is required for bind parameter` and the `ValueError` / psycopg "only '%s',
'%b', '%t' are allowed as placeholders" from a literal `%` -> `E_OPTION_INVALID` with a hint (section 5). A bare `OSError`
raised while connecting (not wrapped by SQLAlchemy) is `E_INTERNAL`, except `socket.gaierror` (name resolution), which
python-oracledb thin lets escape unwrapped for an unknown host (observed, T1b): `E_CONNECT` `cannot resolve the database host
name: Name or service not known`. Oracle and SQL Server codes are mapped from the T1b observations (tables below); a code
that was not observed is `E_INTERNAL` (class name only).

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
| `E_CONNECT` | network/TLS/host failure, **including a connect timeout** (`E_TIMEOUT` is for statements only) | PostgreSQL `psycopg.OperationalError` (or `ConnectionTimeout`), no SQLSTATE, matched on text: `Connection refused`, `connection timeout expired`, `failed to resolve host`, `server closed the connection`, `no pg_hba.conf entry`, `pg_hba.conf rejects`, `too many connections`, and any other `connection failed: ...` not claimed by `E_AUTH_FAILED` (WS section 13); MySQL/MariaDB `OperationalError` 2003 (no SQLSTATE); Oracle `OperationalError` `DPY-6005` (refused, timed out, `DPY-6001` service not registered) and an unwrapped `socket.gaierror` (unknown host); SQL Server `OperationalError` with DB-Lib 20009 in the text (refused, timed out, unknown host), **decided on the text** because the number pymssql reports can be stale (WS 15) |
| `E_AUTH_FAILED` | bad credentials | PostgreSQL: `psycopg.OperationalError` with **no SQLSTATE** (28P01 is not observable), only the text `FATAL:  password authentication failed`, `fe_sendauth: no password supplied` (no password given, or withheld after a retarget, SPEC 3.2) or `role ... is not permitted to log in`; MySQL/MariaDB: 1045 (SQLSTATE 28000). Access to an unknown/forbidden database as a non-admin gave 1044 (SQLSTATE 42000) on MySQL/MariaDB and `FATAL:  database ... does not exist` (no SQLSTATE) on PostgreSQL. Oracle: `DatabaseError` ORA-01017 (wrong password and unknown user give the same text) and DPY-4001 `no credentials specified` (no password). SQL Server: 18456 `Login failed for user '...'` for a wrong password, no password and an unknown database at login alike |
| `E_SQL` | syntax error, unknown object/column, permission denied | PostgreSQL `ProgrammingError`, class 42. MySQL/MariaDB: 1064 and 1146 are `ProgrammingError`, but 1054 (unknown column) and 1142 (permission denied) are `OperationalError`; all have SQLSTATE class 42, so key on the code, not the class. Oracle: `DatabaseError` for all of them (ORA-00900, 00904, 00923, 00942, 01031, 01453, 03405, 41900). SQL Server: `ProgrammingError` for 102, 207, 208, 2812 but `OperationalError` for 229 and 262 (permission), so key on the number |
| `E_SQL_DATA` | data exception (division by zero, bad cast) | PostgreSQL `DataError`, class 22. MySQL/MariaDB: 1365 (division by zero, only in data-changing statements; `SELECT 1/0` returns NULL) is `OperationalError`, SQLSTATE 22012; 1366 (bad value) is `DataError` with SQLSTATE `HY000` on MySQL and `22007` on MariaDB, so key on the vendor code. Oracle `DatabaseError` ORA-01476 (division by zero), ORA-01722 (bad number), ORA-12899 (value too large). SQL Server `OperationalError` 8134, 245, 2628 |
| `E_CONSTRAINT` | integrity violation | PostgreSQL 23505 `IntegrityError`; MySQL/MariaDB 1062 `IntegrityError`, SQLSTATE 23000, and (T1b, MySQL 9.7.1, MariaDB 12.3.2) 1452 foreign key, 1048 explicit NULL, 1364 NOT NULL column left out in strict mode (SQLSTATE `HY000`, so only the vendor code finds it); Oracle `IntegrityError` ORA-00001, 01400, 02291; SQL Server `IntegrityError` 2627, 515, 547 |
| `E_READ_ONLY` | write attempted in a read-only context | PostgreSQL 25006 (`InternalError`); MySQL/MariaDB 1792 (`OperationalError`, SQLSTATE 25006); Oracle ORA-01456 (`DatabaseError`). SQL Server has no in-session read-only |
| `E_TIMEOUT` | statement timeout/cancel | PostgreSQL 57014 (`OperationalError`); MySQL 3024 (`HY000`); MariaDB 1969 (`70100`). MySQL does not raise for a table-less `SELECT SLEEP()` (WS 2). Oracle DPY-4024 (and DPY-4011 when its text says "timed out"); SQL Server DB-Lib 20003 in the text (the reported number is 20047) |
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
| no password supplied / connect timeout / unknown host / `pg_hba.conf` reject | `psycopg.OperationalError` / `ConnectionTimeout` | none | `E_AUTH_FAILED` / `E_CONNECT` / `E_CONNECT` / `E_CONNECT` by message text only (WS section 13) |

PostgreSQL with **pg8000** (default driver, D15; observed 2026-11-01 on 16.15, pg8000 1.31.6, WS 16). The server's ErrorResponse is the dict in
`exc.orig.args[0]` (`C` SQLSTATE, `M` message, `D` detail, `H` hint; the source-position keys are ignored); the mapping is the SQLSTATE
table above, so every statement error gives the same code and the same text as psycopg. Differences, all in the connect phase: pg8000
**does** report a SQLSTATE there (`[E_AUTH_FAILED] postgresql 28P01: password authentication failed for user "rw"`, `[E_CONNECT] postgresql 3D000:
database "nodb" does not exist`; 3D000, 53300 and 57P01/02/03 map to `E_CONNECT`, class 28 stays `E_AUTH_FAILED`, but a FATAL error or class 28/08 is first
matched on its text so that `no pg_hba.conf entry` stays `E_CONNECT`); a socket failure is one `InterfaceError("Can't create a connection to host H and port P ...")`
whose `__cause__` (`ConnectionRefusedError`, `TimeoutError`, `socket.gaierror`) gives `[E_CONNECT] postgresql: connection to server at "H", port P failed: Connection refused`
(`timeout expired` for a timeout, still `E_CONNECT`); a server alert after the handshake is `InterfaceError("network error")` with the `ssl.SSLError` in the cause chain;
a certificate that does not verify arrives as an unwrapped `ssl.SSLCertVerificationError`; a missing password with SCRAM is a bare `AttributeError`, turned by
the adapter into pg8000's own `server requesting password authentication, but no password was provided` (`E_AUTH_FAILED`).

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

Observed on Oracle AI Database 26ai Free 23.26.2 (python-oracledb 26.1.0 thin; T1b, WS 14). The message is the driver's
`message` with the code prefix, the `Help: https://...` line and `CONNECTION_ID` removed; the `ORA-03301`/`ORA-03302`
"details" lines (row and bound values) are kept only with `error_detail`:

| Input | SQLAlchemy / driver class | Code | `E_` |
|---|---|---|---|
| `select * fro film` / `selec 1 from dual` | DatabaseError / DatabaseError | ORA-00923 / ORA-00900 | `E_SQL` |
| missing table / missing column | DatabaseError / DatabaseError | ORA-00942 / ORA-00904 | `E_SQL` |
| `select 1/0 from dual` | DatabaseError / DatabaseError | ORA-01476 | `E_SQL_DATA` |
| duplicate key / NOT NULL / foreign key | IntegrityError / IntegrityError | ORA-00001 / ORA-01400 / ORA-02291 | `E_CONSTRAINT` |
| DELETE or UPDATE as a SELECT-only account / CREATE TABLE | DatabaseError / DatabaseError | ORA-41900 / ORA-01031 | `E_SQL` |
| table of another schema without a grant | DatabaseError / DatabaseError | ORA-00942 (not a privilege message) | `E_SQL` |
| insert under `SET TRANSACTION READ ONLY` | DatabaseError / DatabaseError | ORA-01456 | `E_READ_ONLY` |
| second statement after `;` | DatabaseError / DatabaseError | ORA-03405 | `E_SQL` |
| `call_timeout` expired | DatabaseError / DatabaseError | DPY-4024 (DPY-4011 for a PL/SQL call) | `E_TIMEOUT` |
| wrong password, unknown user / no password | DatabaseError / DatabaseError | ORA-01017 / DPY-4001 | `E_AUTH_FAILED` |
| port closed, host unreachable, unknown service | OperationalError / OperationalError | DPY-6005 | `E_CONNECT` |
| unknown host name | (not SQLAlchemy) / `socket.gaierror` | - | `E_CONNECT` |

Observed on SQL Server 2022 CU25 16.0.4255.1 (pymssql 2.4.6; T1b, WS 15). Statement errors carry `args == (number,
b"<server text>DB-Lib error message 20018, ...")`, connect errors `args == ((number, b"..."),)`; the message is the server
text with the DB-Lib noise removed (connect failures: the DB-Lib lines themselves, de-duplicated). Echoed values (duplicate
key value, truncated value, conversion value) are replaced by `...` unless `error_detail`:

| Input | SQLAlchemy / driver class | Number | `E_` |
|---|---|---|---|
| `select * fro film` / `selec 1` | ProgrammingError / ProgrammingError | 102 / 2812 (`Could not find stored procedure 'selec'`) | `E_SQL` |
| missing table / missing column | ProgrammingError / ProgrammingError | 208 / 207 | `E_SQL` |
| `select 1/0` | OperationalError / OperationalError | 8134 | `E_SQL_DATA` |
| duplicate key / NOT NULL / foreign key | IntegrityError / IntegrityError | 2627 / 515 / 547 | `E_CONSTRAINT` |
| write or SELECT without the permission / CREATE TABLE | OperationalError / OperationalError | 229 / 262 | `E_SQL` |
| `cast('abc' as int)`, value too long | OperationalError / OperationalError | 245, 2628 | `E_SQL_DATA` |
| `timeout` expired | OperationalError / OperationalError | 20047 reported; text has 20003 | `E_TIMEOUT` |
| wrong password, no password, unknown database | OperationalError / OperationalError | 18456 | `E_AUTH_FAILED` |
| port closed, host unreachable, unknown host | OperationalError / OperationalError | 20009 (or a stale earlier number) | `E_CONNECT` |

Message text. Observed fields: psycopg `exc.orig.diag.message_primary` (plus `message_hint` on some errors and
`message_detail` where the server sends one); pg8000 `exc.orig.args[0]['M']`, `['H']`, `['D']`; PyMySQL `exc.orig.args == (code, message)` and `exc.orig.sqlstate`; oracledb
`exc.orig.args[0]` is an error object with `full_code`, `code` and `message`; pymssql `exc.orig.args == (code, b"message")` for
a statement error and `((code, b"message"),)` for a connect error (bytes in both). Use the message of `exc.orig` and **never `str()` of the SQLAlchemy exception**: it appends `[SQL: ...]` and
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
- Oracle 23.26.2 (python-oracledb 26.1.0, T1b, `type_probe`): `NUMBER(10,2)` -> Decimal (`12.5`: Oracle keeps no trailing zero),
  `TIMESTAMP(6)` -> datetime, `RAW(16)` -> bytes, `BLOB` -> bytes (SQLAlchemy reads the LOB), native `JSON` -> dict whose
  numbers are **Decimal**, so `{"a": 1}` is returned as `{"a": "1"}` (section 9 turns Decimal into a string), `NVARCHAR2` ->
  str, NULL -> None. Column names come back lower case.
- SQL Server 2022 (pymssql 2.4.6): `decimal` -> Decimal (`12.50`), `datetime2(6)` -> datetime, `uniqueidentifier` -> UUID,
  `varbinary` -> bytes, `nvarchar(max)` holding JSON -> str, NULL -> None.
- PostgreSQL 18.4, MySQL 9.7.1 and MariaDB 12.3.2 (T1b, `type_probe`) gave the same mappings as above.

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
| D1 | Allow connection details in the call? | **Profile only by default; opt-in call-time parameters under policy** (owner decision 2026-10-29, section 3.2). Off unless `sqlx:_policy:allow_call_params` lists field names; ad-hoc connections need `allow_adhoc_connection`. |
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
| D14 | Should a call that retargets the connection (`host`, `port`, `username` or `dialect` differs from the base profile) keep the base profile's password? (Added with section 3.2.) | **No** (deviation from the owner's design, which did not mention it): the password is dropped unless the call supplies `password` or `sqlx:_policy:inherit_password: true`. Reason: otherwise `profile=prod host=<listed but attacker-influenced name>` sends the production password to another server (credential relay). Costs one extra argument for legitimate retargeting. |

### Decision added with the pg8000 driver

| ID | Decision | Assumption |
|---|---|---|
| D15 | **Decided (owner) and implemented**: pg8000 (pure Python, BSD-3) is the default PostgreSQL driver, psycopg stays available. Reason: one wheel set for all four platforms, including Windows ARM64 (no `psycopg-binary` wheel), and no LGPL package in the default PostgreSQL install. | Dialect URL `postgresql+pg8000`; the extra `postgres` installs `pg8000>=1.31.6,<1.32` and no longer psycopg; new extra `postgres-psycopg` (`psycopg[binary]`); profile key `driver: pg8000 \| psycopg`, default `pg8000` (a profile that relied on the default and has psycopg installed but not pg8000 gets `[E_DRIVER_MISSING] ... saltext-sqlx[postgres]`; the fix is `driver: psycopg` or the other extra). Only `application_name` is allowed in `query`/`connect_args` for pg8000; TLS is `ssl_certs_dir` only. **Risk accepted**: the one-statement-per-call guarantee needs one pg8000 internal (`Connection.execute_unnamed`) because stock pg8000 runs `a; b` through the simple protocol when there are no binds (WS 16.1); the minor version is pinned, a once-per-process proof refuses a driver that does not behave (`[E_DRIVER_MISSING]`), and `tests/functional/test_pg8000_functional.py` pins the behaviour. Everything observed to differ from psycopg is in WS 16. |
