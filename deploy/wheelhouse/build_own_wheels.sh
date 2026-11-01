#!/usr/bin/env bash
# Build OUR OWN pure-Python wheels into $OWN. Platform independent, so run once.
#   SQLX_DIR / KAFKA_DIR      source checkouts (or SQLX_REPO+SQLX_REF / KAFKA_REPO+KAFKA_REF to clone)
#   SQLX_VERSION / KAFKA_VERSION   version to stamp (setuptools_scm needs git tags; without a tag the version is a dev
#                                  string that changes on every commit and cannot be pinned, so we stamp it explicitly)
# The Kafka package is a build argument ON PURPOSE: the upstream salt-extensions/saltext-kafka is only a TEST STAND-IN.
set -euo pipefail
OWN="${OWN:-/own}"
PIP="${PIP:-python -m pip}"
mkdir -p "$OWN"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

build_one() {  # name dir repo ref version
  local name="$1" dir="$2" repo="$3" ref="$4" ver="$5"
  if [ -z "$dir" ]; then
    [ -n "$repo" ] || { echo "build_own_wheels: $name: set ${name}_DIR or ${name}_REPO" >&2; return 1; }
    dir="$tmp/$name"; git clone --quiet "$repo" "$dir"; git -C "$dir" checkout --quiet "$ref"
  fi
  [ -n "$ver" ] || ver="$(git -C "$dir" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')" || true
  [ -n "$ver" ] || { echo "build_own_wheels: $name: no git tag and no ${name}_VERSION given; refusing to build an unpinnable dev version" >&2; return 1; }
  echo "== building $name $ver from $dir"
  SETUPTOOLS_SCM_PRETEND_VERSION="$ver" $PIP wheel --no-deps --quiet -w "$OWN" "$dir"
}

[ -z "${SQLX_DIR:-}${SQLX_REPO:-}" ]   || build_one SQLX  "${SQLX_DIR:-}"  "${SQLX_REPO:-}"  "${SQLX_REF:-main}"  "${SQLX_VERSION:-}"
[ -z "${KAFKA_DIR:-}${KAFKA_REPO:-}" ] || build_one KAFKA "${KAFKA_DIR:-}" "${KAFKA_REPO:-}" "${KAFKA_REF:-main}" "${KAFKA_VERSION:-}"

# every one of our wheels must be pure Python: py3-none-any or py2.py3-none-any
for w in "$OWN"/*.whl; do
  case "$w" in *-none-any.whl) ;; *) echo "build_own_wheels: not a pure-Python wheel: $w" >&2; exit 1;; esac
done
ls -1 "$OWN"
