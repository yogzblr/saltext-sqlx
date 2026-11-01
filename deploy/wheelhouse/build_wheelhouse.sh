#!/usr/bin/env bash
# Build the offline wheelhouse for every supported target. Cross-platform: runs on any Linux host with network access to PyPI.
#
#   OWN=<dir with our wheels>  OUT=<output dir>  PYVER=3.14  ./build_wheelhouse.sh
#
# Environment:
#   PYVER        Python of the minions' Salt onedir: Salt 3008.x ships 3.14, 3006.27+/3007.15+ ship 3.11 (3006.<=26, 3007.<=14: 3.10)
#   OWN, OUT, REQ  our wheels / output / requirements dir     TARGETS  subset, e.g. "linux-x86_64 windows-arm64"
#   PIP          pip command (default: python -m pip; pip >= 24 needed)
#   SALT_TAG     Salt release whose package lock is honoured (default: v3008.5 for 3.14, v3007.15 for 3.11); the lock files are vendored in
#                requirements/salt-locks/<tag>/ (refresh_salt_locks.sh). Everything in them is PROVIDED by the minion's Salt, so it is
#                resolved against metadata-only stubs, never downloaded, never shipped, and a pin that disagrees with it fails the build.
#   LOCK_UPDATE=1  regenerate requirements/constraints.py<PYV>.<target>.txt (the frozen transitive dependency set) instead of using it
#   SKIP_LICENCES=1  skip the licence gate (never in CI)
# Exit status is non-zero if ANY target fails; every target is attempted and a summary names package + target.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYVER="${PYVER:-3.14}"; PYV="${PYVER/./}"
OWN="${OWN:-/own}"; OUT="${OUT:-/out}"; REQ="${REQ:-$HERE/requirements}"
PIP="${PIP:-python -m pip}"
TOOL=(python "$HERE/wheelhouse_tool.py")
TARGETS="${TARGETS:-linux-x86_64 linux-arm64 windows-x86_64 windows-arm64}"

declare -A PLAT=(
  [linux-x86_64]="manylinux2014_x86_64 manylinux_2_17_x86_64 manylinux_2_28_x86_64"
  [linux-arm64]="manylinux2014_aarch64 manylinux_2_17_aarch64 manylinux_2_28_aarch64"
  [windows-x86_64]="win_amd64"
  [windows-arm64]="win_arm64"
)

ls "$OWN"/*.whl >/dev/null 2>&1 || { echo "build_wheelhouse: no wheels in OWN=$OWN (run build_own_wheels.sh first)" >&2; exit 2; }
mkdir -p "$OUT"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# Which Salt lock files apply (Salt's onedir ships these packages; see the header comment)
declare -A DEFAULT_TAG=([3.14]=v3008.5 [3.11]=v3007.15)
SALT_TAG="${SALT_TAG:-${DEFAULT_TAG[$PYVER]:-}}"
[ -n "$SALT_TAG" ] || { echo "build_wheelhouse: no Salt lock vendored for python $PYVER; run refresh_salt_locks.sh <tag>:$PYVER and set SALT_TAG" >&2; exit 2; }
[ -d "$REQ/salt-locks/$SALT_TAG" ] || { echo "build_wheelhouse: $REQ/salt-locks/$SALT_TAG is missing; run refresh_salt_locks.sh $SALT_TAG:$PYVER" >&2; exit 2; }
# Packages of the Windows lock that cannot exist on a target: pymssql 2.3.11 (Salt's pin) has no win_arm64 wheel for any CPython, so
# a native Windows ARM64 Salt cannot ship it and we do not pretend it does. The mssql extra is not offered on that target.
declare -A SALT_SKIP=([windows-arm64]="pymssql")

declare -A OWNVER=()
while IFS= read -r line; do OWNVER[${line%%==*}]="${line##*==}"; done < <("${TOOL[@]}" names "$OWN")

# effective requirements: common + target file; our own packages pinned to the version of the wheel we built
effective() {  # target -> path
  local t="$1" f="$WORK/req.$1.txt" line name tag
  : > "$f"
  while IFS= read -r line; do
    tag=""; case "$line" in *salt-wins*) tag=" # salt-wins";; esac
    line="${line%%#*}"; line="$(echo "$line" | xargs || true)"; [ -n "$line" ] || continue
    name="$(echo "$line" | sed -E 's/[[<>=!~ ;].*//' | tr 'A-Z_.' 'a-z--')"
    if [ -n "${OWNVER[$name]:-}" ]; then line="$line==${OWNVER[$name]}"; fi
    echo "$line$tag" >> "$f"
  done < <(cat "$REQ/requirements.common.txt" "$REQ/requirements.$t.txt")
  echo "$f"
}

FAILED=()
for T in $(echo $TARGETS | tr ' ' '\n' | sort); do
  [ -n "${PLAT[$T]:-}" ] || { echo "unknown target $T" >&2; exit 2; }
  echo "=================== $T (python $PYVER) ==================="
  P=(); for p in ${PLAT[$T]}; do P+=(--platform "$p"); done
  COMMON=("${P[@]}" --python-version "$PYVER" --implementation cp --abi "cp$PYV" --abi abi3 --abi none --only-binary=:all:)
  DEST="$OUT/$T"; rm -rf "$DEST"; mkdir -p "$DEST"
  EFF="$(effective "$T")"
  SALTLOCK="$REQ/salt-locks/$SALT_TAG/py$PYVER.${T%%-*}.lock"
  [ -f "$SALTLOCK" ] || { echo "FAILED $T: $SALTLOCK is missing"; FAILED+=("$T: no Salt lock file"); continue; }
  SKIP=(); for n in ${SALT_SKIP[$T]:-}; do SKIP+=(--skip "$n"); done
  # 0. a pin that disagrees with what Salt ships is a build error, before anything is downloaded
  if ! "${TOOL[@]}" check-pins --lock "$SALTLOCK" --out "$EFF.checked" "$EFF" 2>"$WORK/pins.$T.err"; then
    cat "$WORK/pins.$T.err" >&2; FAILED+=("$T: a pin conflicts with a package Salt ships"); continue
  fi
  cat "$WORK/pins.$T.err"; mv "$EFF.checked" "$EFF"
  "${TOOL[@]}" stub "$WORK/stub.$T" --lock "$SALTLOCK" "${SKIP[@]}" >/dev/null
  "${TOOL[@]}" constraints "$SALTLOCK" > "$WORK/salt.$T.constraints.txt"
  CONS=(-c "$WORK/salt.$T.constraints.txt")
  LOCKF="$REQ/constraints.py$PYV.$T.txt"
  [ "$SALT_TAG" = "${DEFAULT_TAG[$PYVER]:-}" ] || LOCKF="$REQ/constraints.py$PYV.$SALT_TAG.$T.txt"
  if [ -f "$LOCKF" ] && [ -z "${LOCK_UPDATE:-}" ]; then CONS+=(-c "$LOCKF"); fi
  LOG="$OUT/build-$T.log"
  # 1. download everything (the resolver fails if ANY package has no wheel for this target)
  if ! $PIP download -r "$EFF" -d "$DEST" "${COMMON[@]}" "${CONS[@]}" --find-links "$OWN" --find-links "$WORK/stub.$T" >"$LOG" 2>&1; then
    echo "FAILED $T: pip download. Last lines of $LOG:"; tail -n 12 "$LOG"
    # name the package: pip states which requirement had no wheel for cp$PYV / this platform
    why="$(grep -m1 'Could not find a version that satisfies the requirement\|No matching distribution' "$LOG" || true)"
    pkg="$(echo "$why" | sed -nE 's/.*requirement ([A-Za-z0-9_.-]+).*/\1/p')"
    echo "  -> PACKAGE '${pkg:-?}' has no installable wheel for $T (python $PYVER): ${why:-see $LOG}"
    FAILED+=("$T: ${pkg:-unknown package} (no wheel for cp$PYV on $T; see $LOG)")
    rm -rf "$WORK/probe"; continue
  fi
  rm -f "$DEST"/salt-9999*.whl                       # the build-time stub must never ship
  "${TOOL[@]}" strip "$DEST" --lock "$SALTLOCK" >>"$LOG"   # the stubs of the packages Salt ships (and any real copy that slipped in)
  "${TOOL[@]}" strip "$DEST" --lock "$SALTLOCK" --check || { FAILED+=("$T: a Salt-owned package is in the wheelhouse"); continue; }
  for w in "$OWN"/*.whl; do [ -e "$DEST/$(basename "$w")" ] || cp "$w" "$DEST/"; done
  # 2. freeze (or compare with) the transitive set
  "${TOOL[@]}" names "$DEST" --exclude-own "$OWN" > "$WORK/now.$T.txt"
  if [ -n "${LOCK_UPDATE:-}" ]; then cp "$WORK/now.$T.txt" "$LOCKF"; echo "lock updated: $LOCKF"; fi
  # 3. prove the set installs with NO network (stub stands in for the Salt the minions already have)
  rm -rf "$WORK/chk-$T"
  if ! $PIP install --dry-run --no-index --find-links "$DEST" --find-links "$WORK/stub.$T" --target "$WORK/chk-$T" \
        "${COMMON[@]}" -r "$EFF" >>"$LOG" 2>&1; then
    echo "FAILED $T: offline dry-run install. Last lines of $LOG:"; tail -n 12 "$LOG"; FAILED+=("$T: offline install check"); continue
  fi
  "${TOOL[@]}" salt-owned "$SALTLOCK" "${SKIP[@]}" --out "$DEST/SALT_OWNED.txt" >/dev/null
  (cd "$DEST" && sha256sum ./*.whl SALT_OWNED.txt | sed 's# \./# #' > SHA256SUMS)
  echo "$T: $(ls "$DEST"/*.whl | wc -l) wheels, $(du -sh "$DEST" | cut -f1)"
done

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo; echo "=================== FAILURES ==================="; printf '  %s\n' "${FAILED[@]}"
  echo "No VERSIONS.json / SHA256SUMS written for an incomplete wheelhouse."; exit 1
fi

"${TOOL[@]}" versions --out "$OUT/VERSIONS.json" --pyver "$PYVER" --own "$OWN" --root "$OUT" --salt-tag "$SALT_TAG"
(cd "$OUT" && find . -name '*.whl' -o -name SHA256SUMS -o -name SALT_OWNED.txt -o -name VERSIONS.json | sort | xargs sha256sum | sed 's# \./# #' > SHA256SUMS.all)
if [ -z "${SKIP_LICENCES:-}" ]; then
  "${TOOL[@]}" licences --root "$OUT" --allow "$HERE/licence_exceptions.txt" --markdown "$OUT/LICENCES.md"
  # second opinion with pip-licenses on the installed set, where the target matches this host (same OS family, CPU, Python minor)
  if [ "$(python -c 'import sys;print("%d.%d"%sys.version_info[:2])')" = "$PYVER" ] && [ "$(uname -s)" = Linux ]; then
    case "$(uname -m)" in x86_64) HT=linux-x86_64;; aarch64) HT=linux-arm64;; *) HT=;; esac
    if [ -n "$HT" ] && [ -d "$OUT/$HT" ]; then
      python -m venv "$WORK/lic"
      "$WORK/lic/bin/python" -m pip install -q --disable-pip-version-check pip-licenses
      "$WORK/lic/bin/python" -m pip install -q --disable-pip-version-check --no-index --no-deps "$OUT/$HT"/*.whl
      PL=("$WORK/lic/bin/pip-licenses" --ignore-packages pip-licenses prettytable wcwidth tomli pip setuptools)
      # exact SPDX ids on the metadata first (a partial match on "GPL-3.0" would also hit "LGPL-3.0-only"), then the trove classifiers
      # (not the free-text License field: pymssql pastes the whole LGPL text there, which contains the words "General Public License")
      "${PL[@]}" --fail-on="GPL-2.0-only;GPL-2.0-or-later;GPL-3.0-only;GPL-3.0-or-later;AGPL-3.0-only;AGPL-3.0-or-later;GPLv2;GPLv3" >/dev/null \
        || { echo "ERROR: pip-licenses found a GPL/AGPL package" >&2; exit 1; }
      "${PL[@]}" --from=classifier --partial-match --fail-on="GNU General Public License;GNU Affero General Public License" >/dev/null \
        || { echo "ERROR: pip-licenses found a GPL/AGPL package" >&2; exit 1; }
      echo "pip-licenses ($HT, installed set): no GPL/AGPL"
    fi
  else
    echo "pip-licenses cross-check skipped (needs Linux with python $PYVER on this host)"
  fi
fi
echo "wheelhouse built: $(for d in "$OUT"/*/; do basename "$d"; done | tr '\n' ' ')"
