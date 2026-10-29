#!/usr/bin/env bash
# Stop the TLS test servers started by up.sh and delete their data directories and the throwaway PKI.
set -uo pipefail
. "$(dirname "$0")/../env.sh"
R="${SQLX_TLS_ROOT:-/var/tmp/sqlx-tls-dbs}"
# Only ever touch the data directories under $R, never the standard servers.
runuser -u sqlxdb -- /usr/lib/postgresql/16/bin/pg_ctl -D "$R/pg/data" -m fast stop 2>/dev/null
for n in mysql_tls mariadb_tls; do [ -f "$R/$n/pid" ] && kill "$(cat "$R/$n/pid")" 2>/dev/null; done
sleep 3
case "$R" in /var/tmp/sqlx-tls-dbs*) rm -rf "$R";; *) echo "refusing to delete $R" >&2;; esac
exit 0
