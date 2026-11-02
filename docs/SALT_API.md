# Calling saltext-sqlx through salt-api

This page shows, with real requests and real responses, how to call every `sqlx` function through **salt-api**
(`rest_cherrypy`) on PostgreSQL, MySQL, MariaDB, Oracle and SQL Server, and states exactly what a caller can send, what
is opt-in and what can never be sent. It complements [the user guide](USER_GUIDE.md) (install, profiles, errors) and
`SPEC.md` (the contract, sections 3.2 and 4).

**How the examples were produced.** Everything marked with a response was sent with `curl` (or, where shown, the `salt` command) to a real salt-api
(Salt 3008.5, Python 3.13, CherryPy 18.10, TLS with a throw-away certificate, `file` eauth) in front of a real master and
four minions, against PostgreSQL 16.15 (pg8000 1.31.6), MySQL 8.0.46 and MariaDB 10.11.14 (PyMySQL 1.2.3), SQLAlchemy 2.0.55,
through `tests-support/native/up.sh`. Responses are re-indented for reading (content untouched; `elapsed_ms` and job ids
differ on every run). **Oracle and SQL Server could not be run here** (no server, no Docker registry): their sections show
the request written from the code and `docs/WIRE_SHAPES.md` sections 14 and 15, each marked "Not run", and describe the
response shape without inventing values. The Docker end-to-end suite that does drive all five databases through salt-api is
`tests-e2e/` (`tests-e2e/up.sh`, then `python -m pytest tests-e2e -m e2e`; see `tests-e2e/README.md`). It was not run for this page.

**Test values.** Accounts and passwords in the examples are the documented test accounts of `tests-support`
(`ro` / `ro_pw`, `rw` / `rw_pw`; SQL Server `Ro_pw_12345!`), the salt-api users are throw-away sandbox users
(`api_admin`, `sre_ops`, `dba_writers`, `adhoc_ops`), and hosts are `127.0.0.1` with the ports of `tests-support/env.sh`
(PostgreSQL 54329, MySQL 33061, MariaDB 33062, Oracle 15211, SQL Server 14331). Replace all of them. The minion is called
`db-minion-01`.

## 1. How to call

### 1.1 salt-api set-up used here

> Everything in this section is Salt configuration, not part of the extension. The extension is installed on the **minion**
> (`saltext-sqlx[postgres,mysql]` for the three servers run here) and is called like any other execution module.

```yaml
# /etc/salt/master.d/api.conf   (sandbox values; production: pam, ldap, ... instead of file)
rest_cherrypy:
  host: 127.0.0.1
  port: 18000
  ssl_crt: /etc/pki/salt-api/api.crt
  ssl_key: /etc/pki/salt-api/api.key
netapi_enable_clients: [local, local_async, runner]
external_auth:
  file:
    ^filename: /etc/salt/eauth.txt
    ^hashtype: plaintext
    # pinning: sre_ops may run sqlx.query and sqlx.ping with these profiles only
    sre_ops:
      - 'db-minion-*':
          - sqlx.query:
              kwargs:
                profile: '^(pg_ro|mysql_ro|mariadb_ro)$'
          - sqlx.ping:
              kwargs:
                profile: '^(pg_ro|mysql_ro|mariadb_ro)$'
    dba_writers:
      - 'db-minion-*':
          - sqlx.execute:
              kwargs:
                profile: '^(pg_rw|mysql_rw|mariadb_rw)$'
    adhoc_ops:
      - 'db-minion-*':
          - sqlx.query:
              kwargs:
                host: '^(127\.0\.0\.1)$'
                dialect: '^(postgresql|mysql)$'
          - sqlx.ping:
              kwargs:
                host: '^(127\.0\.0\.1)$'
                dialect: '^(postgresql|mysql)$'
    api_admin:
      - '*':
          - 'sqlx.*'
          - test.ping
      - '@runner'            # only needed for GET /jobs/<jid> (section 1.6)
```

`client: local` must be enabled (it is by default); `local_async` and `runner` are only needed for section 1.6. The `file`
eauth backend is used because the sandbox has no PAM user; use `pam`, `ldap` or another backend in production and put salt-api
behind TLS (`ssl_crt` / `ssl_key`; `disable_ssl: true` exists only for a closed test network). The `external_auth` rules
above are explained in section 5.

### 1.2 Log in, then call

salt-api offers two ways to authenticate, and they use different URLs (observed):

* **Token:** `POST /login`, then `POST /` with the header `X-Auth-Token`. `POST /run` does **not** accept the header (it answered
  `401 No authentication credentials given`).
* **Stateless:** `POST /run` with `eauth`, `username` and `password` **inside every request object**.

Log in (the token lives 12 hours, `token_expire`; `perms` lists what the user may call):

```bash
curl -sS --cacert "$CACERT" "$API/login" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -d '{"username": "api_admin", "password": "<api_admin password>", "eauth": "file"}'
```

Output:

```json
{"return": [{"token": "9abdca2e7347113bb3a208a28c5e5f64d5f922a4", "expire": 1793626597.8928256, "start": 1793583397.8928254, "user": "api_admin", "eauth": "file", "perms": ["@runner", {"*": ["sqlx.*", "test.ping"]}]}]}
```

The stateless form, one request, no login (the object is the same as in all later examples plus the three eauth fields):

```bash
curl -sS --cacert "$CACERT" "$API/run" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -w '\nHTTP %{http_code}\n' -d @- <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro"},
  "eauth": "file",
  "username": "api_admin",
  "password": "<api_admin password>"
}]
JSON
```

Output:

```text
{"return": [{"db-minion-01": {"ok": true, "dialect": "postgresql", "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)", "elapsed_ms": 67, "profile": "pg_ro"}}]}
HTTP 200
```

From here on the examples use the token form, through a small shell function so that every example is a complete,
copy-pasteable command. Set it up once:

```bash
export API=https://salt-api.example:8000          # sandbox: https://localhost:18000
export CACERT=/etc/pki/salt-api/ca.crt            # the CA that signed the salt-api certificate (sandbox: the self-signed cert)
export TOKEN=$(curl -sS --cacert "$CACERT" "$API/login" -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -d '{"username": "api_admin", "password": "...", "eauth": "file"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["return"][0]["token"])')

sqlx() { curl -sS --cacert "$CACERT" "$API/" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' -w '\nHTTP %{http_code}\n' -d @-; }
```

`sqlx` reads the request from standard input, so the SQL can contain quotes without shell escaping, and prints the body and
then the HTTP status line (`HTTP 200`). Every "Response" below is that output.

A wrong password is `401`, and the body is an HTML page even though `Accept: application/json` was sent (observed):

```bash
curl -sS -i --cacert "$CACERT" "$API/login" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -d '{"username": "api_admin", "password": "wrong", "eauth": "file"}'
```

Output:

```text
HTTP/1.1 401 Unauthorized
Content-Type: text/html;charset=utf-8
Server: CherryPy/18.10.0
Date: Mon, 02 Nov 2026 01:36:38 GMT
Allow: GET, HEAD, POST
Access-Control-Allow-Origin: *
Access-Control-Expose-Headers: GET, POST
Access-Control-Allow-Credentials: true
Vary: Accept-Encoding
Content-Length: 762
Set-Cookie: session_id=e0c1a16bf201b7a34ef229567c1ace9ad4a8adb0; expires=Mon, 02 Nov 2026 11:36:38 GMT; Max-Age=36000; Path=/

(cherrypy's HTML page; only its heading and message are shown)
<h2>401 Unauthorized</h2>
<p>Could not authenticate using provided credentials</p>
```

### 1.3 The request object

The body is a JSON **list** with one object per call (Salt calls this lowstate):

| Key | Value |
|---|---|
| `client` | `local` runs on the minion(s) and waits for the answer; `local_async` returns a job id at once (section 1.6) |
| `tgt` | the minion id or glob. `sqlx` runs **on the minion**, so `tgt` selects which minion's profiles and policy apply. A glob answers once per matching minion; a target that matches nothing answers `{"return": [{}]}` (both below) |
| `fun` | `sqlx.query`, `sqlx.execute`, `sqlx.check_query_result`, `sqlx.check_row_count`, `sqlx.ping`, `sqlx.table_list`, `sqlx.psql_query` |
| `kwarg` | an **object** with the function's keywords (JSON types, section 1.4). This is what you use |
| `arg` | a list of positional arguments. Possible but not recommended: `sqlx.query` takes `profile, sql` positionally, `sqlx.psql_query` takes `query` first. Everything else must be in `kwarg` |
| `full_return` | `true` adds `retcode` and `jid` (section 1.5) |

Using `arg` for the first positionals works (observed):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {},
  "arg": ["pg_ro", "select 1 as one"]
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

and `psql_query` takes its statement first:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {"profile": "pg_ro"},
  "arg": ["select 1 as one"]
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"one": "1"}]}]}
```

A name given both positionally and in `kwarg`, or too many positionals, is Salt's `Passed invalid arguments`, not an `[E_...]` (observed):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1"},
  "arg": ["pg_ro"]
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "Passed invalid arguments to sqlx.query: query() got multiple values for argument 'profile'\n\nRun one statement in a transaction that is always rolled back.\n\nKeywords: ``params`` (object of bind values), ``limit``, ``timeout_ms``, ``max_bytes`` (each can only lower\nthe profile's cap). The connection can be sent in the call (``dialect``, ``host``, ``port``, ``database``,\n``username``, ``password``, ``driver``, or ``connection={...}``), with or without a profile, unless the operator\nnarrowed or switched this off in ``sqlx:_policy:allow_call_params`` (SPEC section 3.2, D16).\nReturns ``profile``, ``columns``, ``rows``, ``row_count``, ``truncated``, ``elapsed_ms``.\n\nCLI Example:\n\n.. code-block:: bash\n\n    salt '*' sqlx.query profile=orders_pg sql=\"select id, total from orders where status = :s\" params='{\"s\": \"open\"}'\n"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {},
  "arg": ["pg_ro", "select 1", "extra"]
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "Passed invalid arguments to sqlx.query: query() takes from 0 to 2 positional arguments but 3 were given\n\nRun one statement in a transaction that is always rolled back.\n\nKeywords: ``params`` (object of bind values), ``limit``, ``timeout_ms``, ``max_bytes`` (each can only lower\nthe profile's cap). The connection can be sent in the call (``dialect``, ``host``, ``port``, ``database``,\n``username``, ``password``, ``driver``, or ``connection={...}``), with or without a profile, unless the operator\nnarrowed or switched this off in ``sqlx:_policy:allow_call_params`` (SPEC section 3.2, D16).\nReturns ``profile``, ``columns``, ``rows``, ``row_count``, ``truncated``, ``elapsed_ms``.\n\nCLI Example:\n\n.. code-block:: bash\n\n    salt '*' sqlx.query profile=orders_pg sql=\"select id, total from orders where status = :s\" params='{\"s\": \"open\"}'\n"
}]}
```

A function that does not exist, and a target that matches no minion:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.nope",
  "kwarg": {"profile": "pg_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": "'sqlx.nope' is not available."}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "no-such-minion",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{}]}
```

A glob target answers once per matching minion, keyed by minion id (here every sandbox minion; each has its own config):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-*",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  },
  "db-minion-optin": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  },
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  },
  "db-minion-strict": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

### 1.4 JSON types versus the command line

salt-api passes `kwarg` as JSON, so numbers stay numbers, `params` is an object and nothing needs shell quoting. The `salt` and
`salt-call` command lines parse every `key=value` as YAML, which is why the docs elsewhere show `operator='">="'`. Verified both ways:

| Value | JSON (`kwarg`) | `salt` command line (observed) |
|---|---|---|
| `port` | `54329`, a number. `"54329"`, `54329.0`, `true`, `0` and `70000` are `[E_PROFILE_INVALID] ... 'port' must be an integer between 1 and 65535` | `port=54329` is a number; `port='"54329"'` is a string and is refused the same way |
| `params` | an object: `{"min_id": 2}`. A list is `[E_OPTION_INVALID] option 'params' must be an object with string keys` | `params='{"min_id": 2}'` |
| `limit`, `timeout_ms`, `max_bytes`, `row`, `column` | integers; `"10"`, `1.5` and `0` are `[E_OPTION_INVALID] option 'limit' must be an integer >= 1` | `limit=10` |
| `dry_run`, `include_views` | `true` / `false`; the string `"true"` is `[E_OPTION_INVALID] option 'dry_run' must be a boolean` | `dry_run=true` |
| `operator` | `">"`, `"=="`, `">="` as they are. No extra quoting | must be YAML-quoted: `operator='">"'`. Unquoted `>` arrives empty and unquoted `==` is parsed as something else (both below) |
| `expected` | any JSON value: a number, a string, `null`. `1` and `"1"` both equal a result of 1 (numeric strings compare as numbers, SPEC 4.3) | YAML: `expected=1` is a number, `expected='"1"'` a string |
| `password` | a string. A JSON number is refused (`[E_OPTION_INVALID] option 'password' must be a non-empty string`); the command line turns an all-digit password into a number, so there it is written `password='"12345"'` | |

Wrong types through salt-api are `[E_OPTION_INVALID]` and nothing is coerced:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "params": [1]}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_INVALID] option 'params' must be an object with string keys"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "limit": "10"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_INVALID] option 'limit' must be an integer >= 1"
}]}
```

The command line, run for real against the sandbox master. Quoted operator:

```bash
salt 'db-minion-01' sqlx.check_row_count profile=pg_ro sql="select 1" operator='">="' expected=1
```

Output:

```json
{
    "db-minion-01": {
        "ok": true,
        "actual": 1,
        "expected": 1,
        "operator": ">=",
        "attempts": 1,
        "profile": "pg_ro"
    }
}
```

The same with an unquoted `>`, which YAML reads as an empty folded scalar:

```bash
salt 'db-minion-01' sqlx.check_row_count profile=pg_ro sql="select 1" operator='>' expected=0
```

Output:

```text
{
    "db-minion-01": "ERROR executing 'sqlx.check_row_count': [E_OPTION_INVALID] option 'operator' must be a non-empty string"
}
ERROR: Minions returned with non-zero exit code
```

And an unquoted `==`:

```bash
salt 'db-minion-01' sqlx.check_row_count profile=pg_ro sql="select 1" operator='==' expected=1
```

Output:

```text
{
    "db-minion-01": "Passed invalid arguments to sqlx.check_row_count: check_row_count() got multiple values for argument 'profile'\n\nCompare the number of returned rows with ``expected``. A failed comparison is ``ok: false``, not an error.\n\nKeywords: ``expected``, ``params``, ``retry_timeout_s``, ``retry_pause_s``. Operators: ``== != < <= > >=``.\nOn the command line quote the symbols for YAML, for example ``operator='\">=\"'``.\n\nCLI Example:\n\n.. code-block:: bash\n\n    salt '*' sqlx.check_row_count profile=orders_pg sql=\"select 1 from orders\" operator='\">=\"' expected=1\n"
}
ERROR: Minions returned with non-zero exit code
```

A quoted port:

```bash
salt 'db-minion-01' sqlx.ping dialect=postgresql host=127.0.0.1 port='"54329"' database=sqlx_test username=ro password=sdb://sqlx_sdb/pg_ro
```

Output:

```text
{
    "db-minion-01": "ERROR executing 'sqlx.ping': [E_PROFILE_INVALID] profile '(adhoc)': 'port' must be an integer between 1 and 65535"
}
ERROR: Minions returned with non-zero exit code
```

(`operator='>='` unquoted also worked in the sandbox, because YAML reads `>=` as a plain string; do not rely on it.) The same
`">"` through salt-api needs no extra quotes; see `pg_cqr_fail` in section 2.1, which sends `"operator": ">"`.

### 1.5 What comes back

A successful call is HTTP 200 and a `return` list with **one object keyed by minion id** holding the function's result
(shapes in section 2):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "postgresql",
    "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
    "elapsed_ms": 69,
    "profile": "pg_ro"
  }
}]}
```

**An sqlx error is not an HTTP error.** The HTTP status is still 200 and the value under the minion id is a **string** that
starts with `ERROR`. Two prefixes occur (observed), and the `[E_...]` code is the part to match on:

| Text under the minion id | Raised by | Example in this page |
|---|---|---|
| `ERROR: [E_SQL] postgresql 42601: syntax error ...` | a failure while connecting or running (database errors: the database's own text; `E_CONNECT`, `E_AUTH_FAILED`, `E_TIMEOUT`, `E_SECRET`, `E_DRIVER_MISSING`, `E_EXECUTE_DISABLED`, `E_READ_ONLY`, ...) | `err_sql` below |
| `ERROR executing 'sqlx.query': [E_OPTION_INVALID] ...` | validation and policy, before anything connects (`E_OPTION_UNKNOWN`, `E_OPTION_INVALID`, `E_POLICY`, `E_PROFILE_UNKNOWN`, `E_PROFILE_INVALID`) | `err_unknown_opt` below |
| `Passed invalid arguments to sqlx.query: ...` | Salt, for a wrong positional call (section 1.3) | `err_dup_profile` |
| `'sqlx.nope' is not available.` | Salt, no such function on that minion (also what a minion without the extension answers) | `err_no_function` |

A database error:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "selec 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] postgresql 42601: syntax error at or near \"selec\""
}]}
```

A validation error (an unknown keyword; the message lists names only, never values):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "bogus": 1}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: 'bogus'"
}]}
```

A missing profile, and a profile that is not configured on that minion (the message does not disclose other profile names):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_INVALID] option 'profile' is required for sqlx.query"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "nope", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_PROFILE_UNKNOWN] profile is not configured"
}]}
```

With `"full_return": true` the job's `retcode` and `jid` come next to the value. `retcode` is **1 for every `[E_...]` error and
0 for a result**, which is the way to detect a failure without parsing the string (a failed comparison in `check_*` is
`ok: false` with retcode 0, not an error):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "selec 1"},
  "full_return": true
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ret": "ERROR: [E_SQL] postgresql 42601: syntax error at or near \"selec\"",
    "out": "nested",
    "retcode": 1,
    "jid": "20261102013505545424"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro"},
  "full_return": true
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ret": {
      "ok": true,
      "dialect": "postgresql",
      "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
      "elapsed_ms": 100,
      "profile": "pg_ro"
    },
    "retcode": 0,
    "jid": "20261102013505936601"
  }
}]}
```

An authentication or authorisation failure is the only case with a non-200 status: HTTP **401** and cherrypy's HTML page
(section 5 shows them), and no job runs.

### 1.6 Long calls: `local_async`

A `local` call blocks until the minion answers: a 12 second query with the default master timeout came back normally after
12.4 s (observed; the minion answers the master's job-status probe, so the default 5 s `timeout` did not cut it). For a call
that may exceed your HTTP client's timeout, use `local_async`, which returns a job id at once:

```bash
curl -sS --cacert "$CACERT" "$API/" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json' -H 'Content-Type: application/json' -d @- <<'JSON'
[{
  "client": "local_async",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select pg_sleep(2), 1 as one"
  }
}]
JSON
```

Output:

```json
{"return": [{"jid": "20261102013643339947", "minions": ["db-minion-01"]}]}
```

Fetch the job with `GET /jobs/<jid>`. This needs `runner` in `netapi_enable_clients` (otherwise `400 Client disabled: 'runner'`,
observed) and the eauth permission `@runner`, written as an entry of the user's list and **not** nested under a minion target
(nested under `'*'` it was `401 Authentication failure of type "token"`, observed). While the job runs `Result` is empty:

```bash
curl -sS --cacert "$CACERT" "$API/jobs/$JID" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json'
```

Output:

```json
{
  "info": [
    {
      "jid": "20261102013643339947",
      "Function": "sqlx.query",
      "Arguments": [{"__kwarg__": true, "profile": "pg_ro", "sql": "select pg_sleep(2), 1 as one"}],
      "Target": "db-minion-01",
      "Target-type": "glob",
      "User": "api_admin",
      "Minions": ["db-minion-01"],
      "StartTime": "2026, Nov 02 01:36:43.339947",
      "Result": {}
    }
  ],
  "return": [{}]
}
```

When it has finished, `Result` holds the minion's return, the retcode and `success`, and `return` the same value as a synchronous
call. An sqlx error appears as `"return": "ERROR: [E_...] ..."`, `"retcode": 1`, `"success": false`.

```bash
curl -sS --cacert "$CACERT" "$API/jobs/$JID" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json'
```

Output:

```json
{
  "info": [
    {
      "jid": "20261102013643339947",
      "Function": "sqlx.query",
      "Arguments": [{"__kwarg__": true, "profile": "pg_ro", "sql": "select pg_sleep(2), 1 as one"}],
      "Target": "db-minion-01",
      "Target-type": "glob",
      "User": "api_admin",
      "Minions": ["db-minion-01"],
      "StartTime": "2026, Nov 02 01:36:43.339947",
      "Result": {
        "db-minion-01": {
          "return": {
            "columns": ["pg_sleep", "one"],
            "rows": [["", 1]],
            "row_count": 1,
            "truncated": false,
            "elapsed_ms": 2004,
            "profile": "pg_ro"
          },
          "retcode": 0,
          "success": true
        }
      }
    }
  ],
  "return": [
    {
      "db-minion-01": {
        "columns": ["pg_sleep", "one"],
        "rows": [["", 1]],
        "row_count": 1,
        "truncated": false,
        "elapsed_ms": 2004,
        "profile": "pg_ro"
      }
    }
  ]
}
```

## 2. Examples per database

Every database section has the same order: the facts that differ, the minion profile, then each function in two forms,
**(a) by profile** (`profile=`: the connection lives in the minion config) and **(b) with the connection in the call**
(`dialect`, `host`, `port`, `database`, `username`, `password`; on by default since SPEC decision D16). The password in (b) is
shown as an `sdb://` reference (recommended; section 6) and, for `query`, also as a literal.

What is the same on all five: binds are written `:name` and sent in `params`; casts are `CAST(:x AS <type>)`, never `:x::type`; a
literal colon in a string is `\:`; one statement per call; `limit`, `timeout_ms` and `max_bytes` can only lower the minion's
caps (defaults 500 rows, 30 s, 1 MiB); `query` always rolls back and never commits.

Fields of the connection (the same names for every dialect):

| Field | Type | Meaning |
|---|---|---|
| `dialect` | string | `postgresql`, `mysql`, `oracle`, `mssql`; in a **call** `mariadb` is accepted too and means `mysql` (observed: the result reports `"dialect": "mysql"`). A profile accepts the four names only (`dialect: mariadb` in a profile is `[E_PROFILE_INVALID] 'dialect' must be one of: mssql, mysql, oracle, postgresql`, checked by calling the validator directly) |
| `driver` | string, optional | `postgresql`: `pg8000` or `psycopg`; `mysql`: `pymysql`; `oracle`: `oracledb`; `mssql`: `pymssql`. Any other value is `[E_PROFILE_INVALID] ... 'driver' does not match dialect ...` |
| `host` | string | name or IP (`[A-Za-z0-9._:-]` only) |
| `port` | integer | optional; defaults to 5432 / 3306 / 1521 / 1433 for the dialect |
| `database` | string | PostgreSQL / MySQL / MariaDB / SQL Server: the database. **Oracle: the service name** (sent as `service_name`), for example `FREEPDB1` |
| `username` | string | |
| `password` | string | a literal or an `sdb://<profile>/<key>` reference resolved on the minion |

### 2.1 PostgreSQL

* `dialect`: `postgresql`. Default port 5432. Driver `pg8000` (pure Python, extra `postgres`) or `psycopg` (extra
  `postgres-psycopg`). Without `driver` the minion uses psycopg if it is installed, else pg8000. The sandbox minion has only pg8000.
* Sample statements use the schema `scratch` (`scratch.orders`, `scratch.t_probe`); the test accounts' `search_path` is `scratch`.
* `CAST(:min_id AS INTEGER)` for binds with a type.
* `table_list` without `schema` lists the connection's default schema.

The minion's profiles (as run; `sqlx_sdb` is the sdb profile of section 6):

```yaml
# /etc/salt/minion.d/sqlx.conf   (these are the profiles of the sandbox minion, as run)
sqlx:
  profiles:
    pg_ro:     {dialect: postgresql, host: 127.0.0.1, port: 54329, database: sqlx_test, username: ro, password: "sdb://sqlx_sdb/pg_ro"}
    pg_rw:     {dialect: postgresql, host: 127.0.0.1, port: 54329, database: sqlx_test, username: rw, password: "sdb://sqlx_sdb/pg_rw", allow_execute: true, read_only: false}
```

**query**

By profile; the response is the function's result plus the profile name:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select id, note, amount from scratch.orders order by id"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note", "amount"],
    "rows": [[1, "café € 日", "12.50"], [2, "plain", "0.00"]],
    "row_count": 2,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

With a bind, `limit`, `timeout_ms` and `max_bytes` (each can only lower the minion's cap). `CAST(:min_id AS INTEGER)` is the portable cast:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10,
    "timeout_ms": 5000,
    "max_bytes": 65536
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

With the connection in the call, a literal password (a test value; it is stored in the job cache, section 6):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "(adhoc)"
  }
}]}
```

The same with an `sdb://` reference. Note `"profile": "(adhoc)"` in the response when no profile was named:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 3,
    "profile": "(adhoc)"
  }
}]}
```

`limit` cuts the result and says so; `max_bytes` counts the compact JSON of `columns` plus `rows` (here 40 bytes allow one row):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select id from scratch.orders order by id", "limit": 1}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": true,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select id, note from scratch.orders order by id",
    "max_bytes": 40
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[1, "café € 日"]],
    "row_count": 1,
    "truncated": true,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

`timeout_ms` is a database statement timeout; the error is the database's own:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select pg_sleep(3)", "timeout_ms": 500}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_TIMEOUT] postgresql 57014: canceling statement due to statement timeout"
}]}
```

**check_query_result** takes the cell `rows[row][column]` (default 0, 0) and compares with `expected`. A failed comparison is `ok: false`, not an error:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"ok": true, "actual": 2, "expected": 1, "operator": ">=", "attempts": 1, "profile": "pg_ro"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 2,
    "operator": "==",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": ">",
    "expected": 5,
    "retry_timeout_s": 0
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"ok": false, "actual": 2, "expected": 5, "operator": ">", "attempts": 1, "profile": "pg_ro"}
}]}
```

`row` and `column` pick another cell, `contains` works on strings, `is_null` needs no `expected`, and `retry_timeout_s` / `retry_pause_s`
repeat the query until the comparison holds or the time (at most 10 s) is up (`attempts` says how often it ran):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select id, note from scratch.orders order by id",
    "operator": "==",
    "expected": "plain",
    "row": 1,
    "column": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": "plain",
    "expected": "plain",
    "operator": "==",
    "attempts": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select note from scratch.orders where id = 2",
    "operator": "contains",
    "expected": "lai"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": "plain",
    "expected": "lai",
    "operator": "contains",
    "attempts": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select nothing from scratch.orders where id = 1",
    "operator": "is_null"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": null,
    "expected": null,
    "operator": "is_null",
    "attempts": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select count(*) from scratch.orders",
    "operator": ">",
    "expected": 100,
    "retry_timeout_s": 2,
    "retry_pause_s": 0.5
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": false,
    "actual": 2,
    "expected": 100,
    "operator": ">",
    "attempts": 5,
    "profile": "pg_ro"
  }
}]}
```

**check_row_count** compares the number of rows. More rows than the minion's `row_limit` (default 500) cannot be counted: the result is
`ok: null, reason: "truncated"`; count in SQL instead.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select id from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"ok": true, "actual": 2, "expected": 2, "operator": "==", "attempts": 1, "profile": "pg_ro"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select id from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 1,
    "operator": ">=",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select generate_series(1, 5000)",
    "operator": "==",
    "expected": 5000
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": null,
    "actual": null,
    "expected": 5000,
    "operator": "==",
    "attempts": 1,
    "reason": "truncated",
    "profile": "pg_ro"
  }
}]}
```

**ping**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "postgresql",
    "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
    "elapsed_ms": 69,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "postgresql",
    "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
    "elapsed_ms": 74,
    "profile": "(adhoc)"
  }
}]}
```

**table_list** takes `schema`, `pattern` (SQL `LIKE`, `%` and `_`, anchored to the whole name, case-sensitive) and `include_views`:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "pg_ro", "schema": "scratch", "pattern": "ord%", "include_views": true}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"schema": "scratch", "tables": ["orders"], "views": [], "profile": "pg_ro"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "schema": "scratch"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "schema": "scratch",
    "tables": ["orders", "t_probe", "uniq"],
    "views": [],
    "profile": "(adhoc)"
  }
}]}
```

**execute** needs a profile with `allow_execute: true` (here `pg_rw`, which also sets `read_only: false`). `dry_run: true` runs the
statement and rolls back; `dry_run: false` commits (the default when the key is left out, so always send it):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"rows_affected": 1, "committed": false, "dry_run": true, "elapsed_ms": 1, "profile": "pg_rw"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": false
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"rows_affected": 1, "committed": true, "dry_run": false, "elapsed_ms": 3, "profile": "pg_rw"}
}]}
```

The row is really there (read through the read-only profile) and is removed again:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select count(*) as n from scratch.t_probe where id = CAST(:i AS INTEGER)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["n"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "sql": "delete from scratch.t_probe where id = CAST(:i AS INTEGER)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"rows_affected": 1, "committed": true, "dry_run": false, "elapsed_ms": 2, "profile": "pg_rw"}
}]}
```

`execute` is refused when the connection comes from the call (an ad-hoc connection has no profile, so it can never write) and on a
profile without `allow_execute`:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "rw",
    "password": "sdb://sqlx_sdb/pg_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile 'pg_ro' does not set allow_execute: true"
}]}
```

**What the database says.** A write through `query` fails in the read-only transaction; an unknown table, a wrong password come back
with the database's code and text:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_READ_ONLY] postgresql 25006: cannot execute INSERT in a read-only transaction"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select * from no_such_table_xyz"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] postgresql 42P01: relation \"no_such_table_xyz\" does not exist"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "not-the-password",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_AUTH_FAILED] postgresql 28P01: password authentication failed for user \"ro\""
}]}
```

**psql_query** is the look-alike of Salt's `postgres.psql_query` and takes Salt's argument names (`query`, `user`, `host`, `port`,
`maintenance_db`, `password`). It returns a list of objects with every value as text (`NULL` is `null`), like Salt does, not the
`columns`/`rows` object. PostgreSQL only.

By profile:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {"query": "select id, note from scratch.orders order by id", "profile": "pg_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"id": "1", "note": "café € 日"}, {"id": "2", "note": "plain"}]}]}
```

With the connection in the call (`user` and `maintenance_db` are Salt's names for `username` and `database`; without a profile the
dialect is PostgreSQL by itself):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "select id, note from scratch.orders order by id",
    "host": "127.0.0.1",
    "port": 54329,
    "user": "ro",
    "maintenance_db": "sqlx_test",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"id": "1", "note": "café € 日"}, {"id": "2", "note": "plain"}]}]}
```

A literal password:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "select 1 as one",
    "host": "127.0.0.1",
    "port": 54329,
    "user": "ro",
    "maintenance_db": "sqlx_test",
    "password": "ro_pw"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"one": "1"}]}]}
```

`write: true` goes through the `execute` path: it needs a writing profile, and it is refused without one:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "insert into scratch.t_probe values (9002)",
    "write": true,
    "profile": "pg_rw"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"rows_affected": "1"}]}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "delete from scratch.t_probe where id = 9002",
    "write": true,
    "profile": "pg_rw"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{"db-minion-01": [{"rows_affected": "1"}]}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "insert into scratch.t_probe values (1)",
    "write": true,
    "host": "127.0.0.1",
    "port": 54329,
    "user": "rw",
    "maintenance_db": "sqlx_test",
    "password": "sdb://sqlx_sdb/pg_rw"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
}]}
```

`runas` is refused (there is no `psql` and no OS user), and a profile of another dialect is refused:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {"query": "select 1", "runas": "postgres", "profile": "pg_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.psql_query': [E_OPTION_INVALID] runas is not supported; sqlx does not use psql or an OS user"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.psql_query",
  "kwarg": {"query": "select 1", "profile": "mysql_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.psql_query': [E_OPTION_INVALID] sqlx.psql_query needs a postgresql profile"
}]}
```

**Choosing the driver.** `driver` in the call selects pg8000 or psycopg. The sandbox minion has no psycopg, so that call shows
what a missing driver looks like; a driver that does not belong to the dialect is refused before anything connects:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "driver": "pg8000"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "postgresql",
    "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
    "elapsed_ms": 71,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "driver": "psycopg"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_DRIVER_MISSING] postgresql driver is not installed; install the pip extra 'saltext-sqlx[postgres-psycopg]'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "driver": "pymysql"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_PROFILE_INVALID] profile '(adhoc)': 'driver' does not match dialect 'postgresql' (expected one of: pg8000, psycopg)"
}]}
```

### 2.2 MySQL

* `dialect`: `mysql`. Default port 3306. Driver `pymysql` (extra `mysql`).
* `database` is the MySQL database (`scratch`), so statements are not schema-qualified here (`orders`).
* **`CAST(:x AS SIGNED)`**, not `INTEGER` (MySQL rejects `AS INTEGER` with error 1064).
* Errors carry the vendor code: `[E_SQL] mysql 1146: ...`. A write by the read-only account is the server's `1142`, not `E_READ_ONLY`.

```yaml
# /etc/salt/minion.d/sqlx.conf   (these are the profiles of the sandbox minion, as run)
sqlx:
  profiles:
    mysql_ro:  {dialect: mysql, host: 127.0.0.1, port: 33061, database: scratch, username: ro, password: "sdb://sqlx_sdb/mysql_ro"}
    mysql_rw:  {dialect: mysql, host: 127.0.0.1, port: 33061, database: scratch, username: rw, password: "sdb://sqlx_sdb/mysql_rw", allow_execute: true, read_only: false}
```

**query**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "mysql_ro", "sql": "select id, note, amount from orders order by id"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note", "amount"],
    "rows": [[1, "café € 日", "12.50"], [2, "plain", "0.00"]],
    "row_count": 2,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "select id, note from orders where id >= CAST(:min_id AS SIGNED) order by id",
    "params": {"min_id": 2},
    "limit": 10,
    "timeout_ms": 5000,
    "max_bytes": 65536
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select id, note from orders where id >= CAST(:min_id AS SIGNED) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mysql_ro",
    "sql": "select id, note from orders where id >= CAST(:min_id AS SIGNED) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

**check_query_result**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "select count(*) as n from orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 1,
    "operator": ">=",
    "attempts": 1,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mysql_ro",
    "sql": "select count(*) as n from orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 2,
    "operator": "==",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "select count(*) as n from orders",
    "operator": ">",
    "expected": 5,
    "retry_timeout_s": 0
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": false,
    "actual": 2,
    "expected": 5,
    "operator": ">",
    "attempts": 1,
    "profile": "mysql_ro"
  }
}]}
```

**check_row_count**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {"profile": "mysql_ro", "sql": "select id from orders", "operator": "==", "expected": 2}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 2,
    "operator": "==",
    "attempts": 1,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mysql_ro",
    "sql": "select id from orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 1,
    "operator": ">=",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

**ping**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "mysql_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "8.0.46-0ubuntu0.24.04.4",
    "elapsed_ms": 39,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mysql_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "8.0.46-0ubuntu0.24.04.4",
    "elapsed_ms": 40,
    "profile": "(adhoc)"
  }
}]}
```

**table_list** (`schema` is the MySQL database name)

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "mysql_ro", "schema": "scratch", "pattern": "ord%", "include_views": true}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"schema": "scratch", "tables": ["orders"], "views": [], "profile": "mysql_ro"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mysql_ro",
    "schema": "scratch"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "schema": "scratch",
    "tables": ["orders", "t_probe", "uniq"],
    "views": [],
    "profile": "(adhoc)"
  }
}]}
```

**execute**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mysql_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS SIGNED))",
    "params": {"i": 9001},
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": false,
    "dry_run": true,
    "elapsed_ms": 1,
    "profile": "mysql_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mysql_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS SIGNED))",
    "params": {"i": 9001},
    "dry_run": false
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": true,
    "dry_run": false,
    "elapsed_ms": 2,
    "profile": "mysql_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "select count(*) as n from t_probe where id = CAST(:i AS SIGNED)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["n"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 0,
    "profile": "mysql_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mysql_rw",
    "sql": "delete from t_probe where id = CAST(:i AS SIGNED)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": true,
    "dry_run": false,
    "elapsed_ms": 2,
    "profile": "mysql_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "rw",
    "password": "sdb://sqlx_sdb/mysql_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS SIGNED))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "insert into t_probe (id) values (CAST(:i AS SIGNED))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile 'mysql_ro' does not set allow_execute: true"
}]}
```

**What the database says**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mysql_ro",
    "sql": "insert into t_probe (id) values (CAST(:i AS SIGNED))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] mysql 1142: INSERT command denied to user 'ro'@'localhost' for table 't_probe'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "mysql_ro", "sql": "select * from no_such_table_xyz"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] mysql 1146: Table 'scratch.no_such_table_xyz' doesn't exist"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33061,
    "database": "scratch",
    "username": "ro",
    "password": "not-the-password",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_AUTH_FAILED] mysql 1045: Access denied for user 'ro'@'localhost' (using password: YES)"
}]}
```

### 2.3 MariaDB

* In a **call**, `dialect` may be `mariadb` or `mysql`; both reach the same driver (PyMySQL) and the module detects MariaDB itself.
  The result reports `"dialect": "mysql"` either way. In a **profile** only `mysql` is valid.
* Default port 3306. `database` is the MariaDB database.
* `CAST(:x AS INTEGER)` works (unlike MySQL). The profiles below are written with `dialect: mysql`.

```yaml
# /etc/salt/minion.d/sqlx.conf   (these are the profiles of the sandbox minion, as run)
sqlx:
  profiles:
    mariadb_ro: {dialect: mysql, host: 127.0.0.1, port: 33062, database: scratch, username: ro, password: "sdb://sqlx_sdb/mariadb_ro"}
    mariadb_rw: {dialect: mysql, host: 127.0.0.1, port: 33062, database: scratch, username: rw, password: "sdb://sqlx_sdb/mariadb_rw", allow_execute: true, read_only: false}
```

**query**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "mariadb_ro", "sql": "select id, note, amount from orders order by id"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note", "amount"],
    "rows": [[1, "café € 日", "12.50"], [2, "plain", "0.00"]],
    "row_count": 2,
    "truncated": false,
    "elapsed_ms": 0,
    "profile": "mariadb_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "select id, note from orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10,
    "timeout_ms": 5000,
    "max_bytes": 65536
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "mariadb_ro"
  }
}]}
```

The connection in the call, with `dialect: "mariadb"` and a literal password (test value):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select id, note from orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro",
    "sql": "select id, note from orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["id", "note"],
    "rows": [[2, "plain"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

`mysql` is the same value for this server:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "mysql",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "10.11.14-MariaDB-0ubuntu0.24.04.1",
    "elapsed_ms": 39,
    "profile": "(adhoc)"
  }
}]}
```

**check_query_result**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "select count(*) as n from orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 1,
    "operator": ">=",
    "attempts": 1,
    "profile": "mariadb_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro",
    "sql": "select count(*) as n from orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 2,
    "operator": "==",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "select count(*) as n from orders",
    "operator": ">",
    "expected": 5,
    "retry_timeout_s": 0
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": false,
    "actual": 2,
    "expected": 5,
    "operator": ">",
    "attempts": 1,
    "profile": "mariadb_ro"
  }
}]}
```

**check_row_count**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "select id from orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 2,
    "operator": "==",
    "attempts": 1,
    "profile": "mariadb_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro",
    "sql": "select id from orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "actual": 2,
    "expected": 1,
    "operator": ">=",
    "attempts": 1,
    "profile": "(adhoc)"
  }
}]}
```

**ping** (the server version shows the MariaDB flavour)

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "mariadb_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "10.11.14-MariaDB-0ubuntu0.24.04.1",
    "elapsed_ms": 35,
    "profile": "mariadb_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "10.11.14-MariaDB-0ubuntu0.24.04.1",
    "elapsed_ms": 47,
    "profile": "(adhoc)"
  }
}]}
```

**table_list**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "mariadb_ro", "schema": "scratch", "pattern": "ord%", "include_views": true}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"schema": "scratch", "tables": ["orders"], "views": [], "profile": "mariadb_ro"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mariadb_ro",
    "schema": "scratch"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "schema": "scratch",
    "tables": ["orders", "t_probe", "uniq"],
    "views": [],
    "profile": "(adhoc)"
  }
}]}
```

**execute**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mariadb_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": false,
    "dry_run": true,
    "elapsed_ms": 1,
    "profile": "mariadb_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mariadb_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": false
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": true,
    "dry_run": false,
    "elapsed_ms": 1,
    "profile": "mariadb_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "select count(*) as n from t_probe where id = CAST(:i AS INTEGER)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["n"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "mariadb_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mariadb_rw",
    "sql": "delete from t_probe where id = CAST(:i AS INTEGER)",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "rows_affected": 1,
    "committed": true,
    "dry_run": false,
    "elapsed_ms": 1,
    "profile": "mariadb_rw"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "rw",
    "password": "sdb://sqlx_sdb/mariadb_rw",
    "sql": "insert into t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "insert into t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile 'mariadb_ro' does not set allow_execute: true"
}]}
```

**What the database says**

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mariadb_ro",
    "sql": "insert into t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] mysql 1142: INSERT command denied to user 'ro'@'localhost' for table `scratch`.`t_probe`"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "mariadb_ro", "sql": "select * from no_such_table_xyz"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] mysql 1146: Table 'scratch.no_such_table_xyz' doesn't exist"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mariadb",
    "host": "127.0.0.1",
    "port": 33062,
    "database": "scratch",
    "username": "ro",
    "password": "not-the-password",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_AUTH_FAILED] mysql 1045: Access denied for user 'ro'@'localhost' (using password: YES)"
}]}
```

### 2.4 Oracle

> **None of the examples in this section was run against a server.** There is no Oracle server in the environment this page was
> produced in. The requests follow the code and the same salt-api calls as above; Oracle itself was exercised end to end by the
> Docker suite `tests-e2e/` (Oracle AI Database 26ai Free 23.26.2, python-oracledb 26.1.0 thin; `docs/WIRE_SHAPES.md` section 14),
> not by this page. The seed schema used below (`tests-support/seed/oracle.sql`) is marked untested in the repository.

* `dialect`: `oracle`. Default port 1521. Driver `oracledb` (thin mode, extra `oracle`; no Oracle client needed).
* **`database` is the service name** (a PDB service such as `FREEPDB1`), not a SID and not a schema. An unknown service is a connect error.
* Sample statements are schema-qualified (`scratch.orders`): an account that is not the schema owner reaches tables through
  grants plus qualified names or synonyms. `table_list` needs `schema` (the owner), given lower case (`"scratch"`) as the e2e suite does.
* A `SELECT` without a table needs `from dual` (`select CAST(:x AS INTEGER) + 1 as v from dual`). There is no `LIMIT`: use the call's
  `limit`, or `fetch first n rows only`. `CAST(:x AS INTEGER)` works.
* Column names come back **lower case**; `NUMBER(10,2)` values come back as strings such as `"12.5"` (Oracle keeps no trailing zero);
  `RAW` and `BLOB` come back as `{"$bytes_b64": "..."}`.
* A trailing `;` is accepted; a second statement after it is `ORA-03405`.
* The in-session read-only guard (`SET TRANSACTION READ ONLY`) can be bypassed by a PL/SQL block: the **account's grants are the control**.

```yaml
# /etc/salt/minion.d/sqlx.conf   (NOT RUN: written from the code; database is the Oracle SERVICE NAME)
sqlx:
  profiles:
    oracle_ro: {dialect: oracle, host: 127.0.0.1, port: 1521, database: FREEPDB1, username: ro, password: "sdb://sqlx_sdb/oracle_ro"}
    oracle_rw: {dialect: oracle, host: 127.0.0.1, port: 1521, database: FREEPDB1, username: rw, password: "sdb://sqlx_sdb/oracle_rw", allow_execute: true, read_only: false}
```

**query**

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "oracle_ro",
    "sql": "select id, note, amount from scratch.orders order by id"
  }
}]
JSON
```

Response shape: `{"return": [{"db-minion-01": {"columns": ["id", "note", "amount"], "rows": [[...], ...], "row_count": <n>,
"truncated": false, "elapsed_ms": <n>, "profile": "oracle_ro"}}]}`, with lower-case column names and `NUMBER` values as strings.

With the connection in the call, a literal password (test value) with a bind and `limit`:

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10,
    "timeout_ms": 5000
  }
}]
JSON
```

and with an `sdb://` reference:

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "sdb://sqlx_sdb/oracle_ro",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

(`"profile": "(adhoc)"` in the response.) Statements Oracle needs written differently:

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "oracle_ro",
    "sql": "select CAST(:x AS INTEGER) + 1 as v from dual",
    "params": {"x": 41}
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "oracle_ro",
    "sql": "select id, note from scratch.orders order by id fetch first 1 rows only"
  }
}]
JSON
```

**check_query_result**, **check_row_count**:

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "oracle_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "sdb://sqlx_sdb/oracle_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "oracle_ro",
    "sql": "select id from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "sdb://sqlx_sdb/oracle_ro",
    "sql": "select id from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

Response shape: `{"ok": true|false|null, "actual": ..., "expected": ..., "operator": "...", "attempts": <n>, "profile": "..."}`, as on the
other databases.

**ping** runs `SELECT 1 FROM DUAL`; the server version is the driver's `Connection.version` (`23.26.2.0.0` in WIRE_SHAPES section 14):

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "oracle_ro"}
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "sdb://sqlx_sdb/oracle_ro"
  }
}]
JSON
```

Response shape: `{"ok": true, "dialect": "oracle", "server_version": "<x.y.z.w.v>", "elapsed_ms": <n>, "profile": "..."}`.

**table_list** (names come back lower case):

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "oracle_ro", "schema": "scratch", "pattern": "ord%", "include_views": true}
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 15211,
    "database": "FREEPDB1",
    "username": "ro",
    "password": "sdb://sqlx_sdb/oracle_ro",
    "schema": "scratch"
  }
}]
JSON
```

Response shape: `{"schema": "scratch", "tables": [...], "views": [...], "profile": "..."}`.

**execute** (a profile with `allow_execute: true`; `dry_run` rolls back):

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "oracle_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": true
  }
}]
JSON
```

Response shape: `{"rows_affected": <n>, "committed": false, "dry_run": true, "elapsed_ms": <n>, "profile": "oracle_rw"}`. With the
connection in the call, `execute` answers `[E_EXECUTE_DISABLED]` exactly as on PostgreSQL (checked by the module before any
connection is opened, so the behaviour does not depend on the database).

**Errors** are `ERROR: [E_...] oracle ORA-nnnnn: <message>`; the observed texts are in the user guide, section 5
(`ORA-00942`, `ORA-01017`).

### 2.5 SQL Server

> **None of the examples in this section was run against a server.** There is no SQL Server in the environment this page was
> produced in. The requests follow the code; SQL Server 2022 CU25 with pymssql 2.4.6 was exercised end to end by `tests-e2e/`
> (`docs/WIRE_SHAPES.md` section 15), not by this page. The seed schema (`tests-support/seed/mssql.sql`) is marked untested.

* `dialect`: `mssql`. Default port 1433. Driver `pymssql` (extra `mssql`). `database` is the database (`sqlx_test`); schema-qualify
  tables (`scratch.orders`); `table_list` without `schema` lists `dbo`.
* **No `LIMIT`**: use the call's `limit`, `select top n ...`, or `order by ... offset 0 rows fetch next n rows only`. A `SELECT` without a
  table needs no `from`.
* `CAST(:x AS INTEGER)` works; binds are written `:name` as everywhere.
* **One statement per call**, and SQL Server runs every statement of a batch, so the module refuses a `;` followed by more SQL
  with `[E_POLICY] SQL Server runs every statement of a batch ...`. A batch without `;` and without a result set cannot be detected.
* **There is no in-session read-only guard on SQL Server** (a write through `query` runs and is rolled back): the account's
  permissions are the only control. `ssl_certs_dir` TLS is not supported (`[E_PROFILE_INVALID]`).
* `ping` returns the whole multi-line `@@VERSION` text as `server_version`.

```yaml
# /etc/salt/minion.d/sqlx.conf   (NOT RUN: written from the code; database is the SQL Server database)
sqlx:
  profiles:
    mssql_ro: {dialect: mssql, host: 127.0.0.1, port: 1433, database: sqlx_test, username: ro, password: "sdb://sqlx_sdb/mssql_ro"}
    mssql_rw: {dialect: mssql, host: 127.0.0.1, port: 1433, database: sqlx_test, username: rw, password: "sdb://sqlx_sdb/mssql_rw", allow_execute: true, read_only: false}
```

**query**

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mssql_ro",
    "sql": "select id, note, amount from scratch.orders order by id"
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "Ro_pw_12345!",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10,
    "timeout_ms": 5000
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mssql_ro",
    "sql": "select id, note from scratch.orders where id >= CAST(:min_id AS INTEGER) order by id",
    "params": {"min_id": 2},
    "limit": 10
  }
}]
JSON
```

Statements written for SQL Server (no `LIMIT`):

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "mssql_ro", "sql": "select top 1 id, note from scratch.orders order by id"}
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "mssql_ro",
    "sql": "select id, note from scratch.orders order by id offset 0 rows fetch next 1 rows only"
  }
}]
JSON
```

**check_query_result**, **check_row_count**:

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "profile": "mssql_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mssql_ro",
    "sql": "select count(*) as n from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "mssql_ro",
    "sql": "select id from scratch.orders",
    "operator": "==",
    "expected": 2
  }
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mssql_ro",
    "sql": "select id from scratch.orders",
    "operator": ">=",
    "expected": 1
  }
}]
JSON
```

**ping**

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "mssql_ro"}
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mssql_ro"
  }
}]
JSON
```

Response shape: `{"ok": true, "dialect": "mssql", "server_version": "Microsoft SQL Server 2022 (RTM-CU25) ... (several lines)", "elapsed_ms": <n>, "profile": "..."}`.

**table_list**

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "mssql_ro", "schema": "scratch", "pattern": "ord%", "include_views": true}
}]
JSON
```

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {
    "dialect": "mssql",
    "host": "127.0.0.1",
    "port": 14331,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/mssql_ro",
    "schema": "scratch"
  }
}]
JSON
```

**execute**

> **Not run: no Oracle / SQL Server server exists in this environment.** The request is written from the code; the response shape is described below it.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "mssql_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001},
    "dry_run": true
  }
}]
JSON
```

**Errors** read `ERROR: [E_SQL] mssql 208: Invalid object name 'nope'.` (user guide, section 5).

## 3. What a caller can and cannot send

Everything in this section was sent to the sandbox minions; the refusals are real responses. "Default" means a minion with no
`sqlx:_policy` at all (SPEC decision D16).

### 3.1 The function's own keywords (always allowed)

| Function | Keywords (type; R = required) |
|---|---|
| `query` | `profile` (string; may be left out when the call sends a whole connection), `sql` R (non-empty string), `params` (object with string keys), `limit`, `timeout_ms`, `max_bytes` (integers >= 1) |
| `execute` | `profile`, `sql` R, `params`, `dry_run` (boolean), `timeout_ms` |
| `check_query_result` | `profile`, `sql` R, `operator` R (`==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `not_contains`, `is_null`, `is_not_null`), `expected` (any JSON value; required unless the operator is `is_null` or `is_not_null`), `params`, `row`, `column` (integers >= 0), `retry_timeout_s` (number >= 0, capped at 10), `retry_pause_s` (number > 0) |
| `check_row_count` | `profile`, `sql` R, `operator` R (`==`, `!=`, `<`, `<=`, `>`, `>=` only), `expected` R, `params`, `retry_timeout_s`, `retry_pause_s` |
| `ping` | `profile` |
| `table_list` | `profile`, `schema` (string), `pattern` (string, `LIKE` syntax), `include_views` (boolean) |
| `psql_query` | `query` R, `user`, `host`, `port`, `maintenance_db`, `password`, `write` (boolean), `profile`, plus the `query`/`execute` keywords `params`, `limit`, `timeout_ms`, `max_bytes`, `dry_run` and the connection fields; `runas` is refused |

`null` means "not given" for an optional key. A key outside the table, the connection fields and the opt-in fields is
`[E_OPTION_UNKNOWN]`; a wrong type or range is `[E_OPTION_INVALID]` (examples in sections 1.4 and 1.5):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "operator": "=~", "expected": 1}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.check_query_result': [E_OPTION_INVALID] operator must be one of: ==, !=, <, <=, >, >=, contains, not_contains, is_null, is_not_null"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_query_result",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "operator": "=="}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.check_query_result': [E_OPTION_INVALID] option 'expected' is required for sqlx.check_query_result"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "operator": "contains", "expected": 1}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.check_row_count': [E_OPTION_INVALID] operator must be one of: ==, !=, <, <=, >, >="
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {"profile": "pg_rw", "sql": "select 1", "dry_run": "true"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.execute': [E_OPTION_INVALID] option 'dry_run' must be a boolean"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1; select 2"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_SQL] postgresql 42601: cannot insert multiple commands into a prepared statement"
}]}
```

### 3.2 Connection fields allowed by default

On `query`, `execute`, `check_query_result`, `check_row_count`, `ping` and `table_list` (and on `psql_query` under Salt's names `user`, `maintenance_db`), with or without `profile`: `dialect`, `driver`, `host`, `port`, `database`,
`username`, `password` (types and meaning in section 2), or the same keys inside one `connection` object. Details observed:

* With no profile the call must send `dialect`, `host`, `database` and `username`; otherwise the error names what is missing:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"dialect": "postgresql", "host": "127.0.0.1", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_INVALID] option 'profile' is required for sqlx.query unless the call sends a whole connection (dialect, host, database, username; missing: database, username)"
}]}
```

* `connection` is an object; a key sent both flat and inside it is refused, and an unknown key inside it is refused:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "connection": {"dialect": "postgresql", "host": "127.0.0.1", "port": 54329, "database": "sqlx_test", "username": "ro", "password": "sdb://sqlx_sdb/pg_ro"},
    "sql": "select 1 as one"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "connection": {"dialect": "postgresql", "host": "127.0.0.1", "port": 54329, "database": "sqlx_test", "username": "ro", "password": "sdb://sqlx_sdb/pg_ro"},
    "host": "127.0.0.1",
    "sql": "select 1 as one"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_INVALID] key(s) given both as options and in connection: 'host'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "connection": {"dialect": "postgresql", "host": "127.0.0.1", "port": 54329, "database": "sqlx_test", "username": "ro", "password": "sdb://sqlx_sdb/pg_ro", "bogus": 1},
    "sql": "select 1 as one"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown key(s) in connection: 'bogus'"
}]}
```

* `port` left out means the dialect's standard port (the PostgreSQL call below went to 5432, where nothing listens in the sandbox):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_CONNECT] postgresql: connection to server at \"127.0.0.1\", port 5432 failed: Connection refused"
}]}
```

* `port` as a string is refused (never coerced):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": "54329",
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_PROFILE_INVALID] profile '(adhoc)': 'port' must be an integer between 1 and 65535"
}]}
```

* A dialect that does not exist:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "sqlite",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_PROFILE_INVALID] profile '(adhoc)': 'dialect' must be one of: mssql, mysql, oracle, postgresql"
}]}
```

* With a profile, the fields in the call **replace** the profile's. If `host`, `port`, `username` or `dialect` changes, the profile's
  password is **not** used (the connection is made without one) unless the call sends its own `password`. Here `pg_ro` is aimed at
  `localhost` without a password, then with one, then as another user:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "host": "localhost", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_AUTH_FAILED] postgresql: server requesting password authentication, but no password was provided"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "host": "localhost",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select current_user as who"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["who"],
    "rows": [["ro"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "username": "rw",
    "password": "sdb://sqlx_sdb/pg_rw",
    "sql": "select current_user as who"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["who"],
    "rows": [["rw"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

* `execute` on a **writing profile** keeps working with connection fields in the call (`allow_execute` belongs to the profile, as in
  Salt): this dry run went to `localhost` with the caller's `sdb://` password. It is not an ad-hoc connection, so it is allowed.
  The operator's controls are section 4.

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "host": "localhost",
    "password": "sdb://sqlx_sdb/pg_rw",
    "sql": "insert into scratch.t_probe (id) values (9003)",
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"rows_affected": 1, "committed": false, "dry_run": true, "elapsed_ms": 1, "profile": "pg_rw"}
}]}
```

* Passwords: a literal or an `sdb://<profile>/<key>` reference. A reference that does not resolve is `E_SECRET`:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "not-the-password",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_AUTH_FAILED] postgresql 28P01: password authentication failed for user \"ro\""
}]}
```

### 3.3 Opt-in only: `sqlx:_policy:allow_call_params`

These fields are **refused by default** and become possible only when the operator lists them in the minion config. The exact names
that may be listed: `dialect`, `driver`, `host`, `port`, `database`, `username`, `password`, `ssl_mode`, `ssl_certs_dir`, `ssl_files`,
`query`, `connect_args`, `connect_timeout_s`, `statement_timeout_ms`, `read_only` (no `*`, no other name).

| Field | Refusal on a default minion | What the operator's opt-in allows |
|---|---|---|
| `ssl_mode`, `ssl_certs_dir`, `ssl_files` | `[E_POLICY] 'ssl_mode' may not be set from a call (sqlx:_policy:allow_call_params)` | TLS with a certificate directory under `sqlx:_policy:ssl_certs_dir_root` only; `ssl_mode` can only be tightened (`verify-full` > `verify-ca`) |
| `query` | the same text | driver URL arguments, from a **per-driver allow-list** (PostgreSQL pg8000: `application_name` only) |
| `connect_args` | the same text | the same allow-list |
| `read_only` | the same text | `true` only; `false` is always refused |
| `statement_timeout_ms`, `connect_timeout_s` | the same text | may only **lower** the profile's value |

Refusals on the default minion (a call with `profile=pg_ro` and the one extra field; the call is otherwise valid):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "ssl_mode": "verify-full"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'ssl_mode' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "ssl_certs_dir": "/etc/ssl/certs"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'ssl_certs_dir' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "ssl_files": {"ca": "ca.pem"}}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'ssl_files' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "query": {"application_name": "x"}}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'query' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "connect_args": {"application_name": "x"}}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'connect_args' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "read_only": false}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'read_only' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "statement_timeout_ms": 1000}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'statement_timeout_ms' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "connect_timeout_s": 3}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'connect_timeout_s' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

On a minion whose operator listed them (the minion `db-minion-optin`, policy below), the same fields work within their limits:

```yaml
sqlx:
  _policy:
    allow_call_params: [dialect, driver, host, port, database, username, password, query, connect_args,
                        read_only, connect_timeout_s, statement_timeout_ms, ssl_mode, ssl_certs_dir, ssl_files]
    allow_adhoc_connection: true            # an explicit list turns ad-hoc connections off unless this is set
    allowed_hosts: [127.0.0.1]              # required with an explicit list that has host or ad-hoc
    ssl_certs_dir_root: /tmp/sqlxapi/sslroot     # sandbox path (production: e.g. /etc/salt/sqlx-certs)
```

`query` and `connect_args` carry driver arguments from the allow-list:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "query": {"application_name": "salt-api-demo"},
    "sql": "select current_setting('application_name') as app"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": {
    "columns": ["app"],
    "rows": [["salt-api-demo"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "connect_args": {"application_name": "via-connect-args"},
    "sql": "select current_setting('application_name') as app"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": {
    "columns": ["app"],
    "rows": [["via-connect-args"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 2,
    "profile": "pg_ro"
  }
}]}
```

Anything outside the allow-list, and TLS arguments smuggled through them, are refused:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "query": {"sslmode": "require"}, "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_POLICY] 'query' key(s) sslmode cannot be set from a call; use ssl_mode and ssl_certs_dir"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "connect_args": {"ssl_context": "x"}, "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_POLICY] connect_args key 'ssl_context' is not allowed for postgresql"
}]}
```

`read_only` may be turned on, never off:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "read_only": false, "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_POLICY] 'read_only' can be turned on from a call, never off"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "read_only": true, "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

`statement_timeout_ms` lowers the statement timeout of the connection (the database cancels the statement). A value above the cap is
accepted without an error and does not raise anything (the minimum of call, profile and hard maximum is used):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "statement_timeout_ms": 9999999, "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

A low value cancels a long statement:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "statement_timeout_ms": 500, "sql": "select pg_sleep(3)"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR: [E_TIMEOUT] postgresql 57014: canceling statement due to statement timeout"
}]}
```

The TLS fields are checked against the operator's certificate root before any connection is made. **A successful TLS connection was
not run here** (the sandbox servers have no TLS); the refusals are:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "ssl_mode": "verify-full", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_PROFILE_INVALID] profile 'pg_ro': 'ssl_mode' and 'ssl_files' need 'ssl_certs_dir'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "ssl_certs_dir": "/etc/ssl/certs", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_POLICY] profile 'pg_ro': ssl_certs_dir resolves outside sqlx:_policy:ssl_certs_dir_root"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "ssl_certs_dir": "/tmp/sqlxapi/sslroot", "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_PROFILE_INVALID] profile 'pg_ro': CA file 'ca.pem' not found in ssl_certs_dir"
}]}
```

On this minion a literal password is refused, because an explicit list turns `allow_call_literal_password` off unless it is set:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-optin",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-optin": "ERROR executing 'sqlx.query': [E_POLICY] 'password' from a call must be an sdb:// reference (a literal would be stored in the job cache and on the event bus)"
}]}
```

### 3.4 Never, whatever the policy

None of these can be sent from a call, on any minion, with any policy:

| Item | Result |
|---|---|
| `allow_execute`, `execute_verbs`, `error_detail`, `row_limit`, `_policy` | `[E_OPTION_UNKNOWN]`: they are not keywords at all |
| raising a cap or a timeout (`limit`, `timeout_ms`, `max_bytes`, `statement_timeout_ms`, `connect_timeout_s`, `retry_timeout_s`) | silently **clamped** to the lowest of call, profile and hard maximum (10 000 rows, 300 s, 8 MiB, 60 s connect, 10 s retry; SPEC section 6; observed for `limit`, `timeout_ms`, `max_bytes` and `statement_timeout_ms`); not an error |
| `read_only: false` | `[E_POLICY] 'read_only' can be turned on from a call, never off` (when `read_only` is allowed at all) |
| `execute` (or `psql_query write=true`) on a connection without a profile | `[E_EXECUTE_DISABLED]` |
| a profile's password sent to a retargeted host, port, username or dialect | not sent (section 3.2) |
| link-local and cloud-metadata hosts | `[E_POLICY]` (below) |

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "allow_execute": true}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: 'allow_execute'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "execute_verbs": ["INSERT"]}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: 'execute_verbs'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "error_detail": true}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: 'error_detail'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "row_limit": 100000}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: 'row_limit'"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1", "_policy": {"allow_call_params": ["host"]}}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: '_policy'"
}]}
```

A cap cannot be raised. `limit: 99999999` on a 5000-row query returned 500 rows with `truncated: true` (the profile's `row_limit`; checked,
the rows are not reproduced here), and `check_row_count` has no `limit` at all, so it reports the truncation:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.check_row_count",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select generate_series(1, 5000)",
    "operator": "==",
    "expected": 5000
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": null,
    "actual": null,
    "expected": 5000,
    "operator": "==",
    "attempts": 1,
    "reason": "truncated",
    "profile": "pg_ro"
  }
}]}
```

A call that asks for more than the cap is still answered (the value is clamped, not refused):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "select 1 as one",
    "limit": 99999999,
    "timeout_ms": 99999999,
    "max_bytes": 99999999
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

`read_only: false`, even on a profile (a default minion does not accept the field at all):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "read_only": false, "sql": "select 1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.query': [E_POLICY] 'read_only' may not be set from a call (sqlx:_policy:allow_call_params)"
}]}
```

An ad-hoc or read-only profile cannot write (section 2.1):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "rw",
    "password": "sdb://sqlx_sdb/pg_rw",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_ro",
    "sql": "insert into scratch.t_probe (id) values (CAST(:i AS INTEGER))",
    "params": {"i": 9001}
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_EXECUTE_DISABLED] profile 'pg_ro' does not set allow_execute: true"
}]}
```

### 3.5 Hosts

By default any host the minion can reach is allowed, **loopback and private addresses included**, except link-local and cloud-metadata
targets: `169.254.0.0/16` (the metadata address `169.254.169.254`), `fe80::/10`, `fd00:ec2::254` and `100.100.100.200`. The address the
name **resolves to** is compared, so decimal, hex and IPv4-mapped spellings are refused too (all observed):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "169.254.169.254"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "2852039166"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "0xa9fea9fe"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "fe80::1"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "::ffff:169.254.169.254"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "100.100.100.200"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts"
}]}
```

A name that does not resolve fails closed, and characters outside `[A-Za-z0-9._:-]` are refused:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "no-such-host.invalid"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR: [E_CONNECT] host name could not be resolved for the policy check"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "pg_ro", "host": "db1.internal/x"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": "ERROR executing 'sqlx.ping': [E_POLICY] 'host' must be a plain host name or IP address"
}]}
```

## 4. How the operator tightens it

All of it is minion config or pillar (`sqlx:_policy`); a call can never read, send or change it. A minion must be restarted (or the
pillar refreshed) after a change.

| Goal | Config | Effect (observed on the sandbox minions) |
|---|---|---|
| profile-only, nothing from the call | `allow_call_params: []` | every connection keyword is `[E_OPTION_UNKNOWN]`; `psql_query`'s `host`, `user`, ... are `[E_POLICY]` |
| fewer fields | `allow_call_params: [host, database, username, password]` plus `allowed_hosts` | the list **replaces** the default; the old rules apply: `allowed_hosts` is required when `host` is listed, ad-hoc and literal passwords are off unless `allow_adhoc_connection: true` / `allow_call_literal_password: true` are set |
| only known targets | `allowed_hosts`, `allowed_ports`, `allowed_dialects` | anything else is `[E_POLICY]`; with `allowed_hosts` the loopback address must be listed too |
| ad-hoc connections off | `allow_adhoc_connection: false` | a call must name a profile |
| no literal passwords | `allow_call_literal_password: false` | `[E_POLICY] 'password' from a call must be an sdb:// reference ...` |

Strict (`pol_strict`, minion `db-minion-strict`):

```yaml
sqlx:
  _policy:
    allow_call_params: []
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-strict",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-strict": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: database, dialect, host, password, port, username"
}]}
```

A profile still works, and a profile with a field of the call does not:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-strict",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-strict": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-strict",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "host": "127.0.0.1", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-strict": "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: host"
}]}
```

`psql_query` gets its own message, because `host`, `user`, ... are its normal arguments:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-strict",
  "fun": "sqlx.psql_query",
  "kwarg": {
    "query": "select 1",
    "host": "127.0.0.1",
    "port": 54329,
    "user": "ro",
    "maintenance_db": "sqlx_test",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-strict": "ERROR executing 'sqlx.psql_query': [E_POLICY] call-time connection parameters are disabled (host, maintenance_db, password, port, user); the operator enables fields with sqlx:_policy:allow_call_params"
}]}
```

Narrowed (`pol_narrow`, minion `db-minion-narrow`; the default fields stay, only these targets and `sdb://` passwords):

```yaml
sqlx:
  _policy:
    allowed_hosts: [127.0.0.1]
    allowed_ports: [54329, 33061, 33062]
    allowed_dialects: [postgresql, mysql]
    allow_call_literal_password: false
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-narrow",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "ro_pw",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": "ERROR executing 'sqlx.query': [E_POLICY] 'password' from a call must be an sdb:// reference (a literal would be stored in the job cache and on the event bus)"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-narrow",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1 as one"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-narrow",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.2",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": "ERROR executing 'sqlx.query': [E_POLICY] 'host' is not in sqlx:_policy:allowed_hosts"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-narrow",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 5432,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": "ERROR executing 'sqlx.query': [E_POLICY] 'port' is not in sqlx:_policy:allowed_ports"
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-narrow",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 1521,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1 from dual"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-narrow": "ERROR executing 'sqlx.query': [E_POLICY] 'dialect' is not in sqlx:_policy:allowed_dialects"
}]}
```

A malformed `_policy` (an unknown field name, `"*"`, an empty `allowed_hosts` list) fails closed with `[E_POLICY]` naming the key; that
was not run here (SPEC 3.2).

## 5. Pinning callers with eauth

`external_auth` decides **before any job is sent** who may call which function with which keyword values. A refusal is HTTP **401**,
the body is cherrypy's HTML page, and no job runs. The rules of the sandbox master are in section 1.1. A rule that lists a function
with `kwargs` matches each named keyword against an anchored regex; a keyword the rule does not name is **not restricted**.

**`sre_ops`** may run `sqlx.query` and `sqlx.ping` with `profile` matching `^(pg_ro|mysql_ro|mariadb_ro)$`, and nothing else.

Allowed (`sqlx.query` with `profile=pg_ro`, `sqlx.ping` with `profile=mysql_ro`):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {"profile": "mysql_ro"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "ok": true,
    "dialect": "mysql",
    "server_version": "8.0.46-0ubuntu0.24.04.4",
    "elapsed_ms": 40,
    "profile": "mysql_ro"
  }
}]}
```

Refused (all HTTP 401, observed): `sqlx.execute` (this user's rule does not list it), a profile outside the regex, any call without
`profile` (that includes every call that sends the connection in the call), another sqlx function, another module:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "sql": "insert into scratch.t_probe (id) values (1)",
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_rw", "sql": "select 1"}
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.table_list",
  "kwarg": {"profile": "pg_ro"}
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "cmd.run",
  "kwarg": {"cmd": "id"}
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

**The gap.** The rule restricts `profile` only, so a call that names an allowed profile **and** adds `host=` is not refused by eauth
(observed, HTTP 200). With the default minion policy it connects to that host; the profile's password is not sent, so it needs a
password of the caller's own (an `sdb://` reference works):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "host": "127.0.0.1", "sql": "select 1 as one"}
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "pg_ro"
  }
}]}
```

eauth cannot forbid a keyword (a rule can only require a match for the keywords it names). To keep `sre_ops`-type users on their profiles
the **minion** must refuse the fields (`allow_call_params: []`, section 4), or `allowed_hosts` must list only the hosts you accept.

**`dba_writers`** may run `sqlx.execute` with `profile` matching `^(pg_rw|mysql_rw|mariadb_rw)$`:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.execute",
  "kwarg": {
    "profile": "pg_rw",
    "sql": "insert into scratch.t_probe (id) values (1)",
    "dry_run": true
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {"rows_affected": 1, "committed": false, "dry_run": true, "elapsed_ms": 1, "profile": "pg_rw"}
}]}
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_rw", "sql": "select 1"}
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

**`adhoc_ops`** sends the connection in the call, so there is no profile to match; the rule pins `host` and `dialect` instead (each
regex is checked on its own; a rule does not tie a host to a dialect):

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1 as one"
  }
}]
JSON
```

Response, HTTP 200:

```json
{"return": [{
  "db-minion-01": {
    "columns": ["one"],
    "rows": [[1]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1,
    "profile": "(adhoc)"
  }
}]}
```

A dialect outside the regex, a host outside the regex, the connection inside a `connection` object (not looked into), and a call
with a profile but no `host` / `dialect` are all refused:

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "oracle",
    "host": "127.0.0.1",
    "port": 1521,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1 from dual"
  }
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "dialect": "postgresql",
    "host": "localhost",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro",
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {
    "connection": {"dialect": "postgresql", "host": "127.0.0.1", "port": 54329, "database": "sqlx_test", "username": "ro", "password": "sdb://sqlx_sdb/pg_ro"},
    "sql": "select 1"
  }
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

```bash
sqlx <<'JSON'
[{
  "client": "local",
  "tgt": "db-minion-01",
  "fun": "sqlx.query",
  "kwarg": {"profile": "pg_ro", "sql": "select 1"}
}]
JSON
```

Response, HTTP 401 (cherrypy's HTML error page; only its heading and message are shown):

```text
<h2>401 Unauthorized</h2>
<p>Authorization error occurred.</p>   (the rest of the HTML page omitted)
```

The minion policy (sections 3 and 4) is the control that cannot be bypassed by a rule that names too little; set both.

## 6. Security notes

### 6.1 A literal password in a call is stored in clear text

Salt keeps the job (its arguments included) in the master job cache, publishes it on the event bus and prints it in the minion's debug
log (this page did not examine the debug log). Observed in the sandbox: a `local_async` call with `"password": "ro_pw"` and one with
`"password": "sdb://sqlx_sdb/pg_ro"`, then `GET /jobs/<jid>` for each. The job record of the first shows the literal, the second only the
reference. Anyone who may read jobs (`GET /jobs/<jid>`, the `jobs.list_job` runner, `salt-run state.event`) can read the literal. A
separate probe with a marker password found it in one job-cache file and in 3 events on the event bus; the `sdb://` call put only the reference on the bus, and the
database password it resolved to (`ro_pw`) did not appear on the bus at all.

```bash
curl -sS --cacert "$CACERT" "$API/" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json' -H 'Content-Type: application/json' -d @- <<'JSON'
[{
  "client": "local_async",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "ro_pw"
  }
}]
JSON
```

Output:

```json
{"return": [{"jid": "20261102013647017297", "minions": ["db-minion-01"]}]}
```

```bash
curl -sS --cacert "$CACERT" "$API/jobs/$JID" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json'
```

Output:

```json
{
  "info": [
    {
      "jid": "20261102013647017297",
      "Function": "sqlx.ping",
      "Arguments": [
        {
          "__kwarg__": true,
          "dialect": "postgresql",
          "host": "127.0.0.1",
          "port": 54329,
          "database": "sqlx_test",
          "username": "ro",
          "password": "ro_pw"
        }
      ],
      "Target": "db-minion-01",
      "Target-type": "glob",
      "User": "api_admin",
      "Minions": ["db-minion-01"],
      "StartTime": "2026, Nov 02 01:36:47.017297",
      "Result": {
        "db-minion-01": {
          "return": {
            "ok": true,
            "dialect": "postgresql",
            "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
            "elapsed_ms": 69,
            "profile": "(adhoc)"
          },
          "retcode": 0,
          "success": true
        }
      }
    }
  ],
  "return": [
    {
      "db-minion-01": {
        "ok": true,
        "dialect": "postgresql",
        "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
        "elapsed_ms": 69,
        "profile": "(adhoc)"
      }
    }
  ]
}
```

```bash
curl -sS --cacert "$CACERT" "$API/" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json' -H 'Content-Type: application/json' -d @- <<'JSON'
[{
  "client": "local_async",
  "tgt": "db-minion-01",
  "fun": "sqlx.ping",
  "kwarg": {
    "dialect": "postgresql",
    "host": "127.0.0.1",
    "port": 54329,
    "database": "sqlx_test",
    "username": "ro",
    "password": "sdb://sqlx_sdb/pg_ro"
  }
}]
JSON
```

Output:

```json
{
  "info": [
    {
      "jid": "20261102013649390503",
      "Function": "sqlx.ping",
      "Arguments": [
        {
          "__kwarg__": true,
          "dialect": "postgresql",
          "host": "127.0.0.1",
          "port": 54329,
          "database": "sqlx_test",
          "username": "ro",
          "password": "sdb://sqlx_sdb/pg_ro"
        }
      ],
      "Target": "db-minion-01",
      "Target-type": "glob",
      "User": "api_admin",
      "Minions": ["db-minion-01"],
      "StartTime": "2026, Nov 02 01:36:49.390503",
      "Result": {
        "db-minion-01": {
          "return": {
            "ok": true,
            "dialect": "postgresql",
            "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
            "elapsed_ms": 72,
            "profile": "(adhoc)"
          },
          "retcode": 0,
          "success": true
        }
      }
    }
  ],
  "return": [
    {
      "db-minion-01": {
        "ok": true,
        "dialect": "postgresql",
        "server_version": "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)",
        "elapsed_ms": 72,
        "profile": "(adhoc)"
      }
    }
  ]
}
```

`keep_jobs` and the cleanup of the job cache decide for how long it stays. The extension itself never logs or returns the password.

### 6.2 Use `sdb://`

`sdb://sqlx_sdb/pg_ro` means "sdb profile `sqlx_sdb`, key `pg_ro`": the profile is a top-level key of the **minion** config that names an
sdb driver (the `yaml` driver below is for a test host; use Vault or another driver in production), and the secret is resolved on the
minion when the call runs. Only the reference travels through the master.

```yaml
# /etc/salt/minion.d/sdb.conf   (test only: the yaml driver; use vault or another driver in production)
sqlx_sdb:                  # the name used after sdb://
  driver: yaml
  files: [/etc/salt/sqlx-secrets.yaml]
```

```yaml
# /etc/salt/sqlx-secrets.yaml   (chmod 600; test values only)
pg_ro: ro_pw
pg_rw: rw_pw
mysql_ro: ro_pw
mysql_rw: rw_pw
mariadb_ro: ro_pw
mariadb_rw: rw_pw
```

Check the reference on the minion (this prints the secret, so do it on the minion only; restart the minion after changing its config):

```bash
salt-call --local sdb.get sdb://sqlx_sdb/pg_ro
```

Output:

```text
local:
    ro_pw
```

Then call with the reference, as in all the `*_conn_sdb` examples. A key that does not exist is `[E_SECRET] ... resolved to an empty value`
(the reference is not repeated).

### 6.3 Other notes

* **A caller can use any sdb key the minion can read** as `password`, and send it to a host it chooses. With the default policy there is
  no `allowed_hosts`: scope the sdb backend, set `allowed_hosts`, or take `password` out of `allow_call_params` (SECURITY.md).
* **Any identity that may run `sqlx.*` can aim the minion at any host it can reach** (except the denied targets of section 3.5) and read
  the real database error: a connectivity and port-scanning oracle from the minion's network position. That is the accepted D16 default
  (SPEC 3.2). Pin who may call what with eauth (section 5) and the minion policy (section 4).
* **TLS to salt-api itself.** The token, the eauth password and every literal database password cross that connection: use `ssl_crt` /
  `ssl_key` (not `disable_ssl`), verify the certificate (`curl --cacert`, never `-k` outside a throw-away test) and keep salt-api on a
  restricted interface.
* **The database account is the real boundary.** `query` rolls back and sets a read-only guard, but the guard is bypassable
  (PostgreSQL, MySQL, MariaDB, Oracle) or absent (SQL Server): give the profile an account that can only read what it must.

## 7. Verification status

| Examples | Status |
|---|---|
| sections 1, 2.1 to 2.3, 3, 4, 5, 6 | **run**: each request above was sent with `curl` to the sandbox salt-api and the response is the one printed |
| section 2.4 (Oracle), 2.5 (SQL Server) | **not run**: requests written from the code and WIRE_SHAPES sections 14 and 15; response shapes described, no values invented. The Docker suite `tests-e2e/` (`tests-e2e/up.sh`, `python -m pytest tests-e2e -m e2e`) runs all five databases through salt-api; it was not run for this page |
| successful TLS to a database (`ssl_certs_dir` / `ssl_mode`) | **not run** (refusals only); verified elsewhere on PostgreSQL, MySQL and MariaDB (`tests/functional`, USER_GUIDE section 3.1) |
| `eauth: pam` | not run (the sandbox used `file`; the rule syntax is identical) |
| `driver: psycopg` | the sandbox minion has no psycopg; only the `E_DRIVER_MISSING` answer was observed |
| malformed `_policy` | from the code (`E_POLICY`), not run |
