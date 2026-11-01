#!/usr/bin/env bash
# docs/v17_ci_plan.md 2.2a: run the listed test modules, one pytest process
# each, JOBS at a time, under a hard time limit; fail when any module did not
# pass or ran nothing. The same script runs on GitHub and on this machine.
#
#   ci_run_modules.sh PYTHON LIST TESTS_DIR LOG_DIR [JOBS=4]
#
# LIST: one module file name per line ("#" comments and blank lines ignored).
# Exit 0 all passed; 1 a module failed / timed out / ran nothing; 2 bad input.
set -u
PY=${1:?python}; LIST=${2:?list}; TESTS=${3:?tests dir}; LOGS=${4:?log dir}; JOBS=${5:-4}
LIMIT=${CI_MODULE_TIMEOUT:-600}

[ -f "$LIST" ] || { echo "ci_run_modules: no list $LIST" >&2; exit 2; }
mapfile -t MODS < <(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$LIST" | grep -v '^$')
[ "${#MODS[@]}" -gt 0 ] || { echo "ci_run_modules: $LIST names no module" >&2; exit 2; }
missing=0
for m in "${MODS[@]}"; do
    [ -f "$TESTS/$m" ] || { echo "ci_run_modules: listed module not found: $m" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || exit 2

mkdir -p "$LOGS"
: > "$LOGS/status.txt"
export PY TESTS LOGS LIMIT
export QT_QPA_PLATFORM=offscreen CUDA_VISIBLE_DEVICES= BLOCK01_REQUIRE_STEP1_GPU=0
unset DISPLAY

one() {
    m=$1
    t0=$(date +%s)
    timeout -k 30 "$LIMIT" "$PY" -m pytest -q -p no:cacheprovider --confcutdir=/ --rootdir="$TESTS" \
        --junitxml="$LOGS/$m.xml" "$TESTS/$m" > "$LOGS/$m.log" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        # a listed module must run something: all cases skipped is a failure
        ran=$("$PY" - "$LOGS/$m.xml" <<'EOF'
import sys, xml.etree.ElementTree as ET
try:
    r = ET.parse(sys.argv[1]).getroot()
    s = r if r.tag == "testsuite" else r.find("testsuite")
    print(int(s.get("tests", 0)) - int(s.get("skipped", 0)))
except Exception:
    print(0)
EOF
)
        [ "$ran" -gt 0 ] 2>/dev/null || rc=105
    fi
    echo "$m $rc $(( $(date +%s) - t0 ))" >> "$LOGS/status.txt"
}
export -f one

printf '%s\n' "${MODS[@]}" | xargs -P "$JOBS" -I{} bash -c 'one "$@"' _ {}

bad=$(awk '$2 != 0' "$LOGS/status.txt")
done_n=$(wc -l < "$LOGS/status.txt")
if [ "$done_n" -ne "${#MODS[@]}" ]; then
    echo "ci_run_modules: $done_n of ${#MODS[@]} modules reported" >&2
    exit 1
fi
if [ -n "$bad" ]; then
    echo "ci_run_modules: modules that did not pass (rc 124/137 = time limit, 105 = ran nothing, 5 = nothing collected):"
    while read -r m rc secs; do
        echo "=== $m rc=$rc ${secs}s"
        tail -n 25 "$LOGS/$m.log"
    done <<< "$bad"
    exit 1
fi
echo "ci_run_modules: all ${#MODS[@]} modules passed"
