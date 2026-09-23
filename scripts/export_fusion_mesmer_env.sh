#!/usr/bin/env bash
# Regenerate envs/fusion_mesmer/ from the live micromamba environment.
#
# Read-only on the environment: it only runs `micromamba env export` and
# `pip freeze`. The conda part is written as an explicit linux-64 lock
# (exact package URLs + md5), the pip part as exact pins installed with
# --no-deps, so a rebuild reproduces this package set and nothing else.
#
#   scripts/export_fusion_mesmer_env.sh [env_name]
set -euo pipefail

ENV_NAME="${1:-fusion_mesmer}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/envs/fusion_mesmer"
mkdir -p "$OUT"

PREFIX="$(micromamba env list 2>/dev/null | awk -v n="$ENV_NAME" '$1==n{print $NF}')"
[ -n "$PREFIX" ] || { echo "environment $ENV_NAME not found" >&2; exit 1; }
PY="$PREFIX/bin/python"

# Conda part: explicit lock (platform-specific, exact builds).
micromamba env export -n "$ENV_NAME" --explicit --md5 > "$OUT/conda-linux-64.lock"

# Human-readable spec (conda + pip), for review and diffing only.
micromamba env export -n "$ENV_NAME" > "$OUT/environment.yml"

# Pip part: every distribution pip itself installed (INSTALLER == pip),
# pinned to the version pip reports (local versions like +cu121 kept).
# Selecting by installer, not by name, matters: a conda package and a pip
# distribution can share a name (conda `tzdata` is the zoneinfo database,
# pip `tzdata` the Python package).
{
  echo "# pip part of $ENV_NAME; install with: pip install --no-deps -r requirements-pip.txt"
  echo "--extra-index-url https://download.pytorch.org/whl/cu121"
  "$PY" -m pip inspect 2>/dev/null | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
pins = sorted((x["metadata"]["name"].lower().replace("_", "-"), x["metadata"]["version"])
              for x in d["installed"] if x.get("installer") == "pip")
for name, version in pins:
    print(name + "==" + version)
'
} > "$OUT/requirements-pip.txt"

echo "conda packages: $(grep -c '^https://' "$OUT/conda-linux-64.lock")"
echo "pip packages:   $(grep -vc '^[#-]' "$OUT/requirements-pip.txt")"
