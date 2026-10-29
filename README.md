# saltext-sqlx
Salt extension to run generic salt

## Install

```bash
pip install saltext-sqlx                      # SQLAlchemy only; no database driver
pip install 'saltext-sqlx[postgres]'          # psycopg[binary]
pip install 'saltext-sqlx[mysql]'             # PyMySQL
pip install 'saltext-sqlx[oracle]'            # oracledb
pip install 'saltext-sqlx[mssql]'             # pymssql
```

On a Salt onedir install use `salt-pip install ...` instead of `pip`.

## Usage

The execution module has the virtualname `sqlx`. Connection details live in profiles under
`sqlx:profiles:<name>` in minion config or pillar; callers select a profile only. `SPEC.md` is the contract.

Profile example (SPEC section 3):

```yaml
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql          # required: postgresql | mysql | oracle | mssql
      driver: psycopg              # optional; defaults to the dialect's only driver
      host: db1.internal
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg      # sdb:// reference, or a literal (see SECURITY.md)
      read_only: true                      # default true
      allow_execute: false                 # default false
      statement_timeout_ms: 30000          # default 30000, hard max 300000
      connect_timeout_s: 10
      row_limit: 500                       # default 500, hard max 10000
```

TLS with a certificate directory (SPEC section 3.1, decision D12). The operator sets the root once; a profile names a
directory below it. Nothing about it can be chosen by a caller:

```yaml
sqlx:
  _policy:
    ssl_certs_dir_root: /etc/salt/sqlx-certs        # required for any ssl_certs_dir, else [E_POLICY]
  profiles:
    orders_pg_tls:
      dialect: postgresql                           # also mysql (MySQL and MariaDB); oracle is UNVERIFIED; mssql is refused
      host: db1.internal                            # must be in the server certificate's SANs for verify-full
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg               # omit for certificate-only accounts
      ssl_certs_dir: /etc/salt/sqlx-certs/orders_pg # ca.pem (required); client.pem + client.key (optional, mutual TLS)
      ssl_mode: verify-full                         # default; or verify-ca (checks the chain, not the host name)
      # ssl_files: {ca: ca.crt, cert: client.crt, key: client.key}   # only if your file names differ
```

Keep private keys at mode `0600` (or `0640` owned by root); a key with group or world access is refused. Verified on
PostgreSQL 16, MySQL 8.0 and MariaDB 10.11 against throw-away certificates (`tests-support/tls/`). **Oracle and SQL Server
TLS are unverified**: Oracle's argument mapping is unit-tested only, and `ssl_certs_dir` is rejected for SQL Server.

```bash
salt 'db-minion-01' sqlx.ping profile=orders_pg
salt 'db-minion-01' sqlx.query profile=orders_pg sql="select id, total from orders where status = :s" params='{"s": "open"}' limit=50
```

A failing statement comes back with the database's own text and retcode 1, without the SQL or the bound values:
`ERROR: [E_SQL] postgresql 42P01: relation "t10_nope" does not exist`.

### The read-only guard is NOT the control

`read_only: true` sets a read-only session as a best-effort guard. It is bypassable from inside the session (observed on
PostgreSQL, MySQL and MariaDB; `SECURITY.md` section 2). **Point every profile at a database account that has only the
privileges it needs (a read-only role for `query`)**; the database role is the control. Keep `allow_execute: false`
except on a profile that uses a separate, write-capable account, and restrict who can call `sqlx.execute` with
`external_auth` (below).

### CLI quoting caveat

On the command line Salt parses each `key=value` with YAML, so comparison operators must be YAML-quoted, inside shell
quotes: `operator='"=="'`. Unquoted, `operator===` is split at the first `=` and fails with
`Passed invalid arguments ... got multiple values for argument 'profile'`, and `operator=>` fails with
`[E_OPTION_INVALID] option 'operator' must be a non-empty string` (both observed, `tests/integration/salt_path`).

```bash
salt 'db-minion-01' sqlx.check_query_result profile=orders_pg sql="select count(*) from orders" operator='"=="' expected=3
```

Validation errors are wrapped by the minion as `ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] ...`; errors raised by
the database path read `ERROR: [E_SQL] ...`. Both have retcode 1. Salt adds `__pub_*` keys to every job; they are
ignored, a misspelt option is `E_OPTION_UNKNOWN`.

### Usage via salt-api

Recorded from a real run of `tests/integration/salt_path/test_salt_api.py`: a test master with `rest_cherrypy` (plain
HTTP on a free port, `disable_ssl: true`), the `file` eauth backend with a throwaway user (SPEC section 11 uses `pam`;
a system user was not created for the test), and the live PostgreSQL test database. The request is the SPEC section 11
`curl` with `-k` and `https` replaced by plain `http`, and the minion id, profile and credentials of the test:

```bash
curl -sS http://127.0.0.1:51565/run -H 'Content-Type: application/json' -d '[{"client": "local", "tgt": "minion-A2zVLT", "fun": "sqlx.query", "kwarg": {"profile": "pg_ro", "sql": "select id,note from t10_orders where id >= CAST(:s AS INTEGER) order by id", "params": {"s": 2}, "limit": 1}, "eauth": "file", "username": "t10_alice", "password": "t10_alice_pw_5b1e"}]'
```

```json
{"return": [{"minion-A2zVLT": {"columns": ["id", "note"], "rows": [[2, "two"]], "row_count": 1, "truncated": true, "elapsed_ms": 1, "profile": "pg_ro"}}]}
```

`params` arrives as an object and `limit` as an integer. A request for a profile the user is not allowed
(`mysql_ro`) returns cherrypy's HTML error page and no job runs.

### Authorization and what Salt stores

`external_auth` regex limits on `profile` work as in `SECURITY.md` section 4 (verified with `file` eauth): the permitted
user can call `sqlx.query` with `pg_ro`, is refused for any other profile (including a positional profile, a missing
profile and a longer name), and is refused for `sqlx.execute`. A function listed **without** `kwargs` (for example a bare
`- sqlx.ping`) has no profile limit at all, so give every entry its own `kwargs: {profile: ...}`.

Salt stores the job load: the **SQL text and the bound `params` of every call are in the master job cache** (`.load.p`),
on the event bus (`salt/job/<jid>/new`) and, at debug level, in the master and minion logs. The account password never
appeared in any of them, and the stored job *return* holds the rows only. Do not put secrets in `sql` or `params`.

## Security

If you discover a security vulnerability, please refer
to [Salt's security guide][security].

## User Documentation

For setup and usage instructions, please refer to the
module docstrings (for now, documentation is coming!).

## Contributing

The saltext-sqlx project welcomes contributions from anyone!

The [Salt Extensions guide][salt-extensions-guide] provides comprehensive instructions on all aspects
of Salt extension development, including [writing tests][writing-tests], [running tests][running-tests],
[writing documentation][writing-docs] and [rendering the docs][rendering-docs].

### Quickstart

To get started contributing, first clone this repository (or your fork):

```bash
# Clone the repo
git clone --origin upstream git@github.com:yogzblr/saltext-sqlx.git

# Change to the repo dir
cd saltext-sqlx
```

#### Automatic
If you have installed [direnv][direnv], copying the included `.envrc.example` to `.envrc` and
allowing it to run ensures a proper development environment is present and the virtual environment is active.

Without `direnv`, you can still run the automation explicitly:

```bash
make dev  # or python3 tools/initialize.py
source .venv/bin/activate
```

#### Manual
Please follow the [first steps][first-steps], skipping the repository initialization and first commit.

### Pull request

Always make changes in a feature branch:

```bash
git switch -c my-feature-branch
```

Please ensure you include a [news fragment](https://salt-extensions.github.io/salt-extension-copier/topics/documenting/changelog.html#procedure)
describing your changes. This is a requirement for all user-facing changes (bug fixes, new features),
with the exception of documentation changes.

To [submit a Pull Request][submitting-pr], you'll need a fork of this repository in
your own GitHub account. If you followed the instructions above,
set your fork as the `origin` remote now:

```bash
git remote add origin git@github.com:<your_fork>.git
```

Ensure you followed the [first steps][first-steps] and commit your changes, fixing any
failing `pre-commit` hooks. Then push the feature branch to your fork and submit a PR.

### Ways to contribute

Contributions come in many forms, and they’re all valuable! Here are some ways you can help
without writing code:

* **Documentation**: Especially examples showing how to use this project
  to solve specific problems.
* **Triaging issues**: Help manage [issues][issues] and participate in [discussions][discussions].
* **Reviewing [Pull Requests][PRs]**: We especially appreciate reviews using [Conventional Comments][comments].

You can also contribute by:

* Writing blog posts
* Sharing your experiences using Salt + SQL databases
  on social media
* Giving talks at conferences
* Publishing videos
* Engaging in IRC, Discord or email groups

Any of these things are super valuable to our community, and we sincerely
appreciate every contribution!

[security]: https://github.com/saltstack/salt/blob/master/SECURITY.md
[salt-extensions-guide]: https://salt-extensions.github.io/salt-extension-copier/
[writing-tests]: https://salt-extensions.github.io/salt-extension-copier/topics/testing/writing.html
[running-tests]: https://salt-extensions.github.io/salt-extension-copier/topics/testing/running.html
[writing-docs]: https://salt-extensions.github.io/salt-extension-copier/topics/documenting/writing.html
[rendering-docs]: https://salt-extensions.github.io/salt-extension-copier/topics/documenting/building.html
[first-steps]: https://salt-extensions.github.io/salt-extension-copier/topics/creation.html#initialize-the-python-virtual-environment
[submitting-pr]: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork
[direnv]: https://direnv.net
[issues]: https://github.com/yogzblr/saltext-sqlx/issues
[PRs]: https://github.com/yogzblr/saltext-sqlx/pulls
[discussions]: https://github.com/yogzblr/saltext-sqlx/discussions
[comments]: https://conventionalcomments.org/
