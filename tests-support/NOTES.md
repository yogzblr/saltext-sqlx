# tests-support notes (T1)

Test databases for `saltext-sqlx`. One command starts every database that can run in the current environment:

```bash
tests-support/up.sh            # auto: Docker if the daemon answers, otherwise native (PostgreSQL, MySQL, MariaDB)
tests-support/up.sh docker     # all five, via docker-compose.yml
tests-support/up.sh native     # PostgreSQL 16, MySQL 8.0, MariaDB 10.11 from the Ubuntu archive (needs root)
tests-support/native/down.sh [--purge]
. tests-support/env.sh; sqlx_url postgresql rw     # prints the SQLAlchemy URL for an account
```

Ports and credentials are in `env.sh`. Accounts on every database: `ro` (SELECT on the scratch schema) and `rw`
(SELECT/INSERT/UPDATE/DELETE on it, no DDL); passwords are `ro_pw` / `rw_pw` (SQL Server: `Ro_pw_12345!` / `Rw_pw_12345!`).
Admin: `postgres`/`root`/`sa` with `admin_pw` (SQL Server `Sa_pw_12345!`).
Seed (`seed/*.sql`): schema `scratch` with `orders` (text, numeric, timestamp, UUID/GUID, binary, JSON and NULL columns,
two rows: one fully populated with non-ASCII text, one mostly NULL), `uniq` (primary key, for unique violations) and
`t_probe` (empty write target); schema `private` with `secrets` that neither account may read.

## What was actually run (T1, 2026-10-29)

| Database | How | Result |
|---|---|---|
| PostgreSQL 16.15 | native (`apt`), `initdb -E UTF8 --locale=C.UTF-8`, database created `ENCODING 'UTF8'` | RAN |
| MySQL 8.0.46 | native, `.deb` from the Ubuntu archive unpacked with `dpkg -x` | RAN |
| MariaDB 10.11.14 | native, same method | RAN |
| Oracle | Docker only | NOT RUN |
| SQL Server | Docker only | NOT RUN |

Why NOT RUN: the sandbox allows outbound HTTPS only to PyPI, the Ubuntu archive and GitHub. `docker pull` of
`hello-world` failed with `Forbidden` from `registry-1.docker.io`; the proxy status page showed `403 to CONNECT` for
Docker Hub, and `mcr.microsoft.com`, `container-registry.oracle.com`, `ghcr.io`, `quay.io`, `repo.mysql.com`,
`dlm.mariadb.com`, `packages.microsoft.com`, `download.oracle.com` and `apt.postgresql.org` were all unreachable.
Neither product has an apt package. The block is a policy decision; nothing was routed around it.
`dockerd` itself starts here (`docker info` reports Server 29.8.2 once `dockerd` is launched), so on a machine with
registry access `tests-support/up.sh docker` is expected to work, but that is **not verified**.

## Changes to the suggestions in TEST_PLAN.md section 2

| TEST_PLAN suggestion | Change | Verified? |
|---|---|---|
| `postgres:16`, `POSTGRES_INITDB_ARGS="-E UTF8"` | Kept the image; compose sets `POSTGRES_DB=sqlx_test` and `-E UTF8`. Native path uses `initdb -E UTF8 --locale=C.UTF-8` and `CREATE DATABASE ... ENCODING 'UTF8'` | Native: yes (`show server_encoding` = UTF8). Compose: **not run** |
| `mysql:8` | `mysql:8.0` (floating `8` would move to a new major) | Image **not pulled**. Native server is 8.0.46 |
| `mariadb:11` | `mariadb:10.11` because the server that was actually observed is 10.11.14; `11` would be an unobserved major | Image **not pulled** |
| `gvenzl/oracle-free` | `gvenzl/oracle-free:23-slim`, port 1521 mapped to 15211, service `FREEPDB1` | **Not run.** The assumption that init scripts run as SYS inside the PDB is unverified |
| `mcr.microsoft.com/mssql/server` | `...:2022-latest`, `ACCEPT_EULA=Y` is set explicitly in compose (operator can remove it); no init directory exists so a `mssql-seed` one-shot service runs `sqlcmd` | **Not run** |

`docker compose config -q` accepts the file (syntax only).

## Problems found while making the native path work (all fixed in the scripts)

1. `mysqld` refuses to start without `--secure-file-priv` pointing at an existing directory (`/var/lib/mysql-files`
   does not exist in an unpacked install); the script passes `--secure-file-priv=NULL`.
2. The MySQL and MariaDB apt packages conflict, so they cannot be installed together. Each is unpacked into its own
   prefix (`/opt/mysql8`, `/opt/mariadb`). Runtime libraries needed: `libevent-pthreads-2.1-7t64`,
   `libprotobuf-lite32t64`, `libaio1t64`, `liburing2`.
3. `mariadb-install-db` creates anonymous accounts `''@localhost` and `''@<hostname>`. They shadow `ro@%`/`rw@%` for
   TCP connections from 127.0.0.1, so `rw` failed with `ERROR 1045 Access denied` until the anonymous users were dropped.
   The official MariaDB Docker image removes them itself.
4. The servers refuse to run as root; the scripts run them as user `sqlxdb` (created by `install.sh`).
5. The host time zone was an invalid abbreviation (`ASIA0`), which made psycopg warn
   `unknown PostgreSQL timezone: 'ASIA0'`. The script starts PostgreSQL with `timezone=UTC`.
6. The readiness check must work both before and after the root password is set; the first version only worked on
   the first run and failed on every restart.
7. MySQL 8 enables TLS with an auto-generated certificate by default, and PyMySQL 1.2.3 uses it without being asked
   (see `docs/WIRE_SHAPES.md`, authentication matrix). MariaDB has no TLS configured here.
8. MariaDB `json` columns are `LONGTEXT` with a JSON check, MySQL has a real `JSON` type; the seed is identical.
