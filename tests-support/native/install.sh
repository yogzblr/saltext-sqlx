#!/usr/bin/env bash
# Native (no Docker) binaries for PostgreSQL 16, MySQL 8.0 and MariaDB 10.11 from the Ubuntu 24.04 archive.
# MySQL and MariaDB packages conflict in apt, so each is unpacked with dpkg -x into its own prefix.
# Needs root. Oracle and SQL Server have no apt package: they are Docker-only.
set -euo pipefail
. "$(dirname "$0")/../env.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
apt-get update -qq || true   # third-party sources failing is not fatal
apt-get install -y postgresql-16 postgresql-client-16 libevent-pthreads-2.1-7t64 libprotobuf-lite32t64 libaio1t64 liburing2
id sqlxdb >/dev/null 2>&1 || useradd -m -s /bin/bash sqlxdb
cd "$tmp"
apt-get download mysql-server-core-8.0 mysql-client-core-8.0 mariadb-server-core mariadb-client-core
mkdir -p "$SQLX_OPT/mysql8" "$SQLX_OPT/mariadb"
for f in mysql-*.deb;   do dpkg -x "$f" "$SQLX_OPT/mysql8"; done
for f in mariadb-*.deb; do dpkg -x "$f" "$SQLX_OPT/mariadb"; done
# the PG packaging creates a default cluster on 5432; the test cluster is separate, so stop the default one
pg_ctlcluster 16 main stop 2>/dev/null || true
echo "installed: $(ls "$SQLX_OPT/mysql8/usr/sbin" "$SQLX_OPT/mariadb/usr/sbin" | tr '\n' ' ')"
