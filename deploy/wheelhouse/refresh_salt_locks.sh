#!/usr/bin/env bash
# Refresh the vendored copies of Salt's own package lock files (requirements/static/pkg/py<X.Y>/{linux,windows}.lock).
#
#   ./refresh_salt_locks.sh                       the documented set below
#   ./refresh_salt_locks.sh v3008.5:3.14 ...      explicit TAG:PYVER pairs
#
# Why: the Salt onedir already contains every package in these files, at exactly these versions. The wheelhouse must not ship
# them (they would be installed next to / over the ones Salt runs on) and must not require a different version of them.
# build_wheelhouse.sh uses them as pip constraints and strips them from the output (see deploy/README.md, "Respect what Salt ships").
#
# Which tag for which Python (deploy/README.md, "Which Python?"): 3008.x -> 3.14; 3007.15+ and 3006.27+ -> 3.11. The 3.11 lines
# differ (cryptography 47 / 48 / 50), so each gets its own lock; a 3.11 wheelhouse is built against ONE of them (SALT_TAG).
# Needs only curl and network access to raw.githubusercontent.com. The files are written with a header naming tag, date and URL.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/requirements/salt-locks"
PAIRS=("$@"); [ ${#PAIRS[@]} -gt 0 ] || PAIRS=(v3008.5:3.14 v3007.15:3.11 v3006.27:3.11)
TODAY="${LOCK_DATE:-$(date -u +%F)}"
for pair in "${PAIRS[@]}"; do
  tag="${pair%%:*}"; py="${pair##*:}"
  mkdir -p "$OUT/$tag"
  for os in linux windows; do
    url="https://raw.githubusercontent.com/saltstack/salt/$tag/requirements/static/pkg/py$py/$os.lock"
    dest="$OUT/$tag/py$py.$os.lock"
    tmp="$(mktemp)"
    curl -fsSL "$url" -o "$tmp" || { echo "refresh_salt_locks: cannot fetch $url" >&2; rm -f "$tmp"; exit 1; }
    grep -q '^[A-Za-z0-9_.-]*==' "$tmp" || { echo "refresh_salt_locks: $url has no pinned packages" >&2; rm -f "$tmp"; exit 1; }
    {
      echo "# VENDORED COPY, do not edit. Refresh with deploy/wheelhouse/refresh_salt_locks.sh $tag:$py"
      echo "# salt-tag: $tag"
      echo "# python: $py"
      echo "# platform: $os"
      echo "# fetched: $TODAY"
      echo "# source: $url"
      cat "$tmp"
    } > "$dest"
    rm -f "$tmp"
    echo "wrote $dest ($(grep -c '^[A-Za-z0-9_.-]*==' "$dest") packages)"
  done
done
