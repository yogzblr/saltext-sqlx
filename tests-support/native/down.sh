#!/usr/bin/env bash
# Stop natively started databases. Add --purge to delete the data directories.
set -uo pipefail
. "$(dirname "$0")/../env.sh"
R="$SQLX_DB_ROOT"
runuser -u sqlxdb -- /usr/lib/postgresql/16/bin/pg_ctl -D "$R/pg/data" -m fast stop 2>/dev/null
for n in mysql8 mariadb; do [ -f "$R/$n/pid" ] && kill "$(cat "$R/$n/pid")" 2>/dev/null; done
sleep 2
[ "${1:-}" = "--purge" ] && rm -rf "$R"
exit 0
