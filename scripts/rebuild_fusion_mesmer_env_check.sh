#!/usr/bin/env bash
# Rebuild an environment from envs/fusion_mesmer/ into a TEMPORARY prefix and
# check that its package set equals the recorded manifest.
#
# Never touches an existing named environment. The temporary prefix is
# deleted at the end unless --keep is given (a later step may want to run
# checks inside it; it must then be deleted by hand).
#
#   scripts/rebuild_fusion_mesmer_env_check.sh <temp_prefix> [--keep]
set -euo pipefail

PREFIX="${1:?usage: $0 <temp_prefix> [--keep]}"
KEEP="${2:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="$HERE/envs/fusion_mesmer"
[ ! -e "$PREFIX" ] || { echo "refusing: $PREFIX already exists" >&2; exit 1; }
case "$PREFIX" in /root/micromamba/envs/*) echo "refusing: prefix is a named env dir" >&2; exit 1;; esac

cleanup() { [ "$KEEP" = "--keep" ] || rm -rf -- "$PREFIX"; }
trap cleanup EXIT

t0=$(date +%s)
micromamba create -y -q -p "$PREFIX" --file "$SPEC/conda-linux-64.lock"
t1=$(date +%s)
"$PREFIX/bin/python" -m pip install -q --no-deps -r "$SPEC/requirements-pip.txt"
t2=$(date +%s)

# Package sets: conda part by explicit URL, pip part by exact pin.
micromamba env export -p "$PREFIX" --explicit --md5 | grep '^https://' | sort > "$PREFIX.conda.txt"
grep '^https://' "$SPEC/conda-linux-64.lock" | sort > "$PREFIX.conda.want"
"$PREFIX/bin/python" -m pip freeze --all 2>/dev/null \
  | awk -F'==' 'NF==2{n=tolower($1); gsub("_","-",n); print n"=="$2}' | sort > "$PREFIX.pip.txt"
grep -v '^[#-]' "$SPEC/requirements-pip.txt" | sort > "$PREFIX.pip.want"

status=0
if diff -q "$PREFIX.conda.want" "$PREFIX.conda.txt" >/dev/null; then
  echo "conda: identical ($(wc -l < "$PREFIX.conda.txt") packages)"
else
  echo "conda: DIFFERENT"; diff "$PREFIX.conda.want" "$PREFIX.conda.txt" | head -20; status=1
fi
# Every pinned pip package must be present at its pin; report extras separately.
missing=$(comm -23 "$PREFIX.pip.want" "$PREFIX.pip.txt")
if [ -z "$missing" ]; then
  echo "pip: all $(wc -l < "$PREFIX.pip.want") pins present"
else
  echo "pip: MISSING or different:"; echo "$missing" | head -20; status=1
fi
"$PREFIX/bin/python" -m pip check || status=1
echo "timing_s: conda=$((t1-t0)) pip=$((t2-t1))"
echo "size: $(du -sh "$PREFIX" | cut -f1)"
rm -f "$PREFIX".conda.txt "$PREFIX".conda.want "$PREFIX".pip.txt "$PREFIX".pip.want
exit $status
