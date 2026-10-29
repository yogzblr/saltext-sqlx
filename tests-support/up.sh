#!/usr/bin/env bash
# One command to start every runnable test database.
#   tests-support/up.sh            auto: try Docker (all five); if the daemon is down or the images cannot be pulled,
#                                  fall back to native (PostgreSQL, MySQL, MariaDB only)
#   tests-support/up.sh native     force native;  tests-support/up.sh docker  force Docker, no fallback
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-auto}"
run_docker() { docker compose -f "$here/docker-compose.yml" up -d --wait; }
run_native() {
  [ -x "${SQLX_OPT:-/opt}/mysql8/usr/sbin/mysqld" ] || "$here/native/install.sh" || return 1
  "$here/native/up.sh" || return 1
  echo "NOT RUN (no native package, Docker unavailable): oracle, mssql" >&2
}
case "$mode" in
  docker) run_docker;;
  native) run_native;;
  auto)
    if docker info >/dev/null 2>&1 && run_docker; then exit 0; fi
    echo "Docker unavailable or images not pullable; falling back to native" >&2
    run_native;;
  *) echo "usage: $0 [auto|docker|native]" >&2; exit 2;;
esac
