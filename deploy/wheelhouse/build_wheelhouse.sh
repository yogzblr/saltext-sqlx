#!/usr/bin/env bash
# Build the offline wheelhouse for every supported target (cross-platform, run on any Linux host).
# Needs: pip >= 24, our own pure wheels in $OWN. SKETCH: untested.
set -euo pipefail
PYVER="${PYVER:-3.11}"; PYV="${PYVER/./}"
OWN="${OWN:-/own}"; OUT="${OUT:-/out}"; REQ="${REQ:-$(dirname "$0")/requirements}"
declare -A PLAT=(
  [linux-x86_64]="manylinux2014_x86_64 manylinux_2_17_x86_64 manylinux_2_28_x86_64"
  [linux-arm64]="manylinux2014_aarch64 manylinux_2_17_aarch64 manylinux_2_28_aarch64"
  [windows-x86_64]="win_amd64"
  [windows-arm64]="win_arm64"
)
for T in "${!PLAT[@]}"; do
  P=(); for p in ${PLAT[$T]}; do P+=(--platform "$p"); done
  COMMON=("${P[@]}" --python-version "$PYVER" --implementation cp --abi "cp$PYV" --abi abi3 --abi none --only-binary=:all:)
  mkdir -p "$OUT/$T"
  # fails if ANY package has no wheel for this target (this protects the fleet from a half-built wheelhouse)
  pip download -r "$REQ/requirements.$T.txt" -d "$OUT/$T" "${COMMON[@]}" --find-links "$OWN"
  cp "$OWN"/*.whl "$OUT/$T/"
  # prove the set is complete and installable with no network
  rm -rf "/tmp/chk-$T"
  pip install --dry-run --no-index --find-links "$OUT/$T" --target "/tmp/chk-$T" "${COMMON[@]}" -r "$REQ/requirements.$T.txt"
  (cd "$OUT/$T" && sha256sum ./*.whl > SHA256SUMS)
done
echo "wheelhouse built: $(ls "$OUT")"
