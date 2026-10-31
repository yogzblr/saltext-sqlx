#!/usr/bin/env bash
# Bring the end-to-end stack up: PKI, databases (one at a time, each waited for), account setup, Salt master + minions.
# Usage: tests-e2e/up.sh [service ...]     (default: every service; e.g. `up.sh pg mysql` for a subset of databases;
#        `up.sh local` adds the same-host minions for the running databases)
# Logs: $SQLX_E2E_LOGS (default ~/e2e-logs). Data: $SQLX_E2E_DATA (default ~/e2e-data; must be on the Linux filesystem).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export SQLX_E2E_DATA="${SQLX_E2E_DATA:-$HOME/e2e-data}"
logs="${SQLX_E2E_LOGS:-$HOME/e2e-logs}"
case "$SQLX_E2E_DATA" in /mnt/*) echo "SQLX_E2E_DATA must not be under /mnt (Windows filesystem)" >&2; exit 2;; esac
mkdir -p "$SQLX_E2E_DATA" "$logs"
dc() { docker compose -f "$here/docker-compose.yml" "$@"; }

[ -f "$SQLX_E2E_DATA/pki/e2e-server/server.pem" ] || bash "$here/tls/mk_certs.sh" "$SQLX_E2E_DATA/pki"

all_dbs=(pg mysql mariadb mssql oracle pg-tls mysql-tls mariadb-tls)
dbs=("${@:-${all_dbs[@]}}")

wait_healthy() {  # service timeout_s
  local id status deadline=$((SECONDS + $2))
  id="$(dc ps -q "$1")"
  while :; do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)"
    [ "$status" = healthy ] && return 0
    [ "$status" = exited ] || [ "$status" = missing ] && { echo "$1: container $status" >&2; return 1; }
    [ $SECONDS -ge $deadline ] && { echo "$1: not healthy after $2 s (status $status)" >&2; return 1; }
    sleep 3
  done
}

for svc in "${dbs[@]}"; do
  case " ${all_dbs[*]} " in *" $svc "*) ;; *) continue;; esac
  echo "== $svc: starting"
  dc up -d "$svc" >>"$logs/up.log" 2>&1
  if wait_healthy "$svc" 600; then
    if [[ " pg mysql mariadb mssql oracle " == *" $svc "* ]]; then
      "$here/db/setup.sh" "$svc" >>"$logs/setup-$svc.log" 2>&1 || { echo "$svc: setup FAILED, see $logs/setup-$svc.log" >&2; exit 1; }
    fi
    echo "== $svc: healthy"
  else
    dc logs --tail 50 "$svc" >"$logs/$svc-failed.log" 2>&1 || true
    echo "== $svc: NOT HEALTHY, last log lines in $logs/$svc-failed.log" >&2
    exit 1
  fi
done

if [[ " $* " == *" local "* ]]; then
  # same-host minions for every database container that is running (see docker-compose.yml)
  for db in pg mysql mariadb oracle mssql; do
    [ -n "$(dc ps -q "$db" 2>/dev/null)" ] || continue
    dc --profile local up -d "minion-local-$db" >>"$logs/up.log" 2>&1
    wait_healthy "minion-local-$db" 600 && echo "== minion-local-$db: healthy"
  done
  exit 0
fi

if [ $# -eq 0 ] || [[ " $* " == *" salt "* ]]; then
  echo "== salt: building and starting master, minion, minion-policy, minion-adhoc"
  dc build master >>"$logs/up.log" 2>&1
  dc up -d master minion minion-policy minion-adhoc >>"$logs/up.log" 2>&1
  for s in master minion minion-policy minion-adhoc; do wait_healthy "$s" 600; echo "== $s: healthy"; done
  # The minions authenticate to the master (auto_accept) shortly after start; wait until all three answer.
  for _ in $(seq 1 40); do
    if [ "$(dc exec -T master salt --out=txt -t 10 -L minion,minion-policy,minion-adhoc test.ping 2>/dev/null | grep -c True)" -eq 3 ]; then
      echo "== salt: all three minions answer"; exit 0
    fi
    sleep 3
  done
  echo "salt: minions did not answer test.ping" >&2; exit 1
fi
