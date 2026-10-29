#!/usr/bin/env bash
# Put the expired (or the good) server certificate in place and make the TLS test servers use it.
# Usage: swap_server_cert.sh expired|good [postgresql|mysql|mariadb]...   (default: all three)
# PostgreSQL: pg_ctl reload. MySQL: ALTER INSTANCE RELOAD TLS. MariaDB has no runtime reload: it is restarted.
set -euo pipefail
. "$(dirname "$0")/../env.sh"
here="$(cd "$(dirname "$0")" && pwd)"
R="${SQLX_TLS_ROOT:-/var/tmp/sqlx-tls-dbs}"; PKI="$R/pki"
which="${1:?expired|good}"; shift || true
targets=("$@"); [ ${#targets[@]} -eq 0 ] && targets=(postgresql mysql mariadb)
case "$which" in expired) src="$PKI/expired-server";; good) src="$PKI/server-good";; *) echo "expired|good" >&2; exit 2;; esac
cp "$src/server.pem" "$PKI/server/server.pem"; cp "$src/server.key" "$PKI/server/server.key"
for t in "${targets[@]}"; do
  case "$t" in
    postgresql) runuser -u sqlxdb -- /usr/lib/postgresql/16/bin/pg_ctl -D "$R/pg/data" reload >/dev/null;;
    mysql) "$SQLX_OPT/mysql8/usr/bin/mysql" --no-defaults -uroot -S "$R/mysql_tls/sock" -e 'ALTER INSTANCE RELOAD TLS';;
    mariadb)
      kill "$(cat "$R/mariadb_tls/pid")" 2>/dev/null || true
      for _ in $(seq 1 30); do kill -0 "$(cat "$R/mariadb_tls/pid" 2>/dev/null)" 2>/dev/null || break; sleep 1; done
      bash "$here/up.sh" >/dev/null;;
  esac
done
sleep 1
