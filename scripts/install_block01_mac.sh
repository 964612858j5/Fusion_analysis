#!/bin/bash
# Install FusionFlux (Block01) on a Mac with one command
# (docs/v17_mac_install_plan.md).
#
#   bash scripts/install_block01_mac.sh [--root DIR] [--check-only]
#                                       [--models DIR] [--accept-model-licences]
#                                       [--mesmer-url URL] [--mesmer-zip FILE]
#                                       [--skip-models]
#   bash scripts/install_block01_mac.sh --mesmer-only [--mesmer-url URL |
#                                       --mesmer-zip FILE | --models DIR]
#                                       [--accept-model-licences]
#
# Apple silicon (arm64), macOS 13 or newer; the engines run on the Mac's GPU
# (PyTorch MPS), the CPU when it is switched off. Everything goes under the
# install root (default ~/.local/share/block01, as on Linux): the
# environment, the models, the tools it downloads. It never modifies an
# environment, a model file or a link it did not create; a run that stopped
# half-way is recognised and continued. The checkout gets its record in
# <repo>/.block01/install.env, read by scripts/block01 (and by the app
# ~/Applications/FusionFlux.app this installer makes).
#
# The same rules and the same screen as scripts/install_block01.sh (Linux)
# and scripts/install_block01.ps1 (Windows): one line per step, updated in
# place, its bullet blinking while it runs; one card for the model
# licences; the tools' own output only in .block01/logs/install-<time>.log.
#
# Written for the Mac's own bash 3.2 and its BSD tools. Sourced with
# BLOCK01_INSTALL_SOURCED=1 it only defines its functions
# (tests/test_install_scripts_mac.py).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MANIFEST="$REPO/envs/fusion_mesmer/models.json"
WHEELS_LOCK="$REPO/native/fusion_viewer_core/wheels.lock"
TOOLBOX_LOCK="$REPO/envs/macos/wheels.lock"
CARGO_TOML="$REPO/native/fusion_viewer_core/Cargo.toml"
RELEASE_URL="${BLOCK01_RELEASE_URL:-https://github.com/964612858j5/Fusion_analysis/releases/download}"

# pinned downloads, checked before they are used
MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-osx-arm64"
MICROMAMBA_SHA256="9618a2866a2ffb3d36b55e9520f64d63dcd6dc2e622a351ca3cbe8e2cc90c757"
TORCH_PINS="torch==2.5.1 torchvision==0.20.1"
CPSAM_URL="https://huggingface.co/mouseland/cellpose-sam/resolve/main/cpsam"
STARDIST_URL="https://github.com/stardist/stardist-models/releases/download/v0.1/python_2D_versatile_fluo.zip"
MESMER_URL="https://deepcell-data.s3-us-west-1.amazonaws.com/saved-models/MultiplexSegmentation-9.tar.gz"
MESMER_SHA256="25b1babc2ff769a9f9484184073db8bc264d5ab2de8a6f9ce07c289e8b935129"
# the lab's mirror (2026-11-15): the Mac's verified copy, repacked; checked
# file by file against models.json. --mesmer-url replaces it.
MESMER_MIRROR_DEFAULT="https://drive.usercontent.google.com/download?id=1bsRETcNz6ritHEA00Ds9cAoffr48WnQi&export=download&confirm=t"
MESMER_MIRROR="${BLOCK01_MESMER_URL:-$MESMER_MIRROR_DEFAULT}"

ORIGINAL_MODELS="cellpose_cpsam stardist_2D_versatile_fluo mesmer_multiplex_segmentation"
# every model directory and the variable it is found through (models.json env_override)
MODEL_KEYS="cellpose_cpsam stardist_2D_versatile_fluo stardist_2D_versatile_fluo_torch mesmer_multiplex_segmentation mesmer_multiplex_segmentation_torch"
model_var() {
    case "$1" in
        cellpose_cpsam) echo CELLPOSE_LOCAL_MODELS_PATH ;;
        stardist_2D_versatile_fluo) echo BLOCK01_STARDIST_MODEL_PATH ;;
        stardist_2D_versatile_fluo_torch) echo BLOCK01_STARDIST_TORCH_PATH ;;
        mesmer_multiplex_segmentation) echo DEEPCELL_MESMER_MODEL_PATH ;;
        mesmer_multiplex_segmentation_torch) echo BLOCK01_MESMER_TORCH_PATH ;;
    esac
}
model_name() {
    case "$1" in
        cellpose_cpsam) echo "Cellpose-SAM" ;;
        stardist_2D_versatile_fluo) echo "StarDist" ;;
        mesmer_multiplex_segmentation) echo "Mesmer" ;;
        *) echo "$1" ;;
    esac
}

ROOT="$HOME/.local/share/block01"      # as on Linux; no space in the path (conda needs that)
STATE_DIR="$REPO/.block01"
APP_DIR="$HOME/Applications/FusionFlux.app"
CHECK_ONLY=0
SKIP_MODELS=0
MODELS_IN=""
MESMER_ZIP=""
MESMER_ONLY=0
MESMER_MISSING=0
ACCEPT_LICENCES=0
STEP="start"
GPU_NAME=""

# ── output ────────────────────────────────────────────────────────────────
# One line per step, updated in place; its bullet blinks while a tool runs;
# every tool's own output goes to LOG. Glyphs only on a UTF-8 terminal,
# colours only on a terminal (not TERM=dumb, not NO_COLOR).
LOG=/dev/null
STEP_LABEL=""
STEP_T0=0
PENDING=0
PENDING_LINE=""
RESUMABLE=0
ui_init() {
    TTY=0; [ -t 1 ] && TTY=1
    local loc="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" utf=0
    case "$loc" in *UTF-8*|*utf-8*|*UTF8*|*utf8*) utf=1 ;; esac
    if [ "$TTY" = 1 ] && [ "$utf" = 1 ]; then
        G_OK="✓"; G_BAD="✗"; G_RUN="•"
        B_TL="┌"; B_TR="┐"; B_BL="└"; B_BR="┘"; B_H="─"; B_V="│"; BAR_ON="█"; BAR_OFF="░"
    else
        G_OK="[ok]"; G_BAD="[x]"; G_RUN="-"
        B_TL="+"; B_TR="+"; B_BL="+"; B_BR="+"; B_H="-"; B_V="|"; BAR_ON="#"; BAR_OFF="-"
    fi
    if [ "$TTY" = 1 ] && [ "${TERM:-dumb}" != dumb ] && [ -z "${NO_COLOR:-}" ]; then
        C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'; C_0=$'\033[0m'
    else
        C_OK=""; C_BAD=""; C_WARN=""; C_DIM=""; C_B=""; C_0=""
    fi
}
ui_init

now() { date +%Y-%m-%dT%H:%M:%S; }
log() { printf '%s\n' "$*" >> "$LOG"; }
unpend() { if [ "$PENDING" = 1 ]; then printf '\r\033[K'; fi; }
repend() { if [ "$PENDING" = 1 ]; then printf '%s' "$PENDING_LINE"; fi; }
out() { unpend; printf '%s\n' "$*"; repend; }
fmt_secs() {
    local s=$1
    if [ "$s" -lt 60 ]; then echo "$s s"; else echo "$((s / 60)) min $((s % 60)) s"; fi
}
pad() { printf '%-*s' "$2" "$1"; }    # pad TEXT WIDTH
# a step's result line: "  ✓ LABEL  DETAIL" (two spaces at least)
ok_line() {
    local w=22; [ "${#1}" -lt 20 ] || w=$((${#1} + 2))
    printf '  %s%s%s %s%s%s%s\n' "$C_OK" "$G_OK" "$C_0" "$(pad "$1" "$w")" "$C_DIM" "$2" "$C_0"
}
step_begin() {   # step_begin LABEL [NOTE]
    unpend; PENDING=0
    STEP_LABEL=$1; STEP_T0=$(date +%s)
    log "== $1"
    local line; line=$(printf '  %s %s%s ...' "$G_RUN" "$1" "${2:+ ($2)}")
    if [ "$TTY" = 1 ]; then PENDING_LINE="$C_DIM$line$C_0"; printf '%s' "$PENDING_LINE"; PENDING=1
    else printf '%s\n' "$line"; fi
}
step_done() {    # step_done [DETAIL]: default the time it took
    local detail=${1:-$(fmt_secs $(( $(date +%s) - STEP_T0 )))}
    log "ok: $STEP_LABEL  $detail"
    unpend; PENDING=0
    ok_line "$STEP_LABEL" "$detail"
    STEP_LABEL=""
}
step_ok() { unpend; PENDING=0; STEP_LABEL=$1; step_done "$2"; }
step_quiet() { STEP_LABEL=$1; log "== $1"; }
note() { out "    ${C_DIM}$*${C_0}"; log "$*"; }
warn() { unpend; printf '  %s! %s%s\n' "$C_WARN" "$*" "$C_0"; repend; log "WARNING: $*"; }
fail() {         # fail REASON HINT [COMMAND]
    unpend; PENDING=0
    {
        [ -n "$STEP_LABEL" ] && printf '\n  %s%s %s%s\n' "$C_BAD" "$G_BAD" "$STEP_LABEL" "$C_0"
        printf '%sSTOPPED at: %s%s\n' "$C_BAD" "$STEP" "$C_0"
        printf '%s%s%s\n' "$C_BAD" "$1" "$C_0"
        [ -n "${2:-}" ] && printf '%swhat to do: %s%s\n' "$C_BAD" "$2" "$C_0"
        [ "$RESUMABLE" = 1 ] && printf '%sfinished steps are kept: running the installer again continues from here%s\n' "$C_DIM" "$C_0"
        if [ -f "$LOG" ] && [ -s "$LOG" ]; then
            printf '%slast lines of the log:%s\n' "$C_DIM" "$C_0"
            tail -n 20 "$LOG" | sed "s/^/  ${C_DIM}/; s/\$/${C_0}/"
            printf '%slog: %s%s\n' "$C_DIM" "$LOG" "$C_0"
        fi
    } >&2
    log "STOPPED at: $STEP: $1"
    [ -n "${3:-}" ] && log "failing command: $3"
    exit 1
}
# blink PID: the running step's bullet on and off, once a second
blink() {
    local pid=$1 on=1 off
    off="${PENDING_LINE/"$G_RUN"/${G_RUN//?/ }}"
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if [ "$on" = 1 ]; then printf '\r%s' "$off"; on=0; else printf '\r%s' "$PENDING_LINE"; on=1; fi
    done
    [ "$on" = 1 ] || printf '\r%s' "$PENDING_LINE"
}
# run CMD...: its output into the log, the bullet blinking; a failure stops
run() {
    local rc=0
    log "> $*"
    if [ "$PENDING" = 1 ]; then
        "$@" >> "$LOG" 2>&1 &
        local pid=$!
        blink "$pid"
        wait "$pid" || rc=$?
    else
        "$@" >> "$LOG" 2>&1 || rc=$?
    fi
    [ "$rc" = 0 ] || fail "a command failed (exit $rc): $(basename "$1") ${2:-} ${3:-}" \
        "read the last lines of the log below, fix the cause and run the installer again" "$*"
}

sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
abspath() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$PWD/$1" ;; esac; }
file_size() { stat -f %z "$1" 2>/dev/null || echo 0; }

# draw_progress NAME FILE SIZE: one line, redrawn ("    NAME  ████░░  71%  850/1200 MB")
draw_progress() {
    local name=$1 file=$2 size=${3:-0} have=0 mb
    [ -f "$file" ] && have=$(file_size "$file")
    mb=$((have / 1048576))
    if [ "$size" -gt 0 ]; then
        local pct=$((have * 100 / size)) w=24 on bar="" i=0
        [ "$pct" -le 100 ] || pct=100
        on=$((pct * w / 100))
        while [ "$i" -lt "$w" ]; do
            if [ "$i" -lt "$on" ]; then bar="$bar$BAR_ON"; else bar="$bar$BAR_OFF"; fi
            i=$((i + 1))
        done
        printf '\r\033[K    %s  %s %3d%%  %d/%d MB' "$(pad "$name" 13)" "$bar" "$pct" "$mb" "$((size / 1048576))"
    else
        printf '\r\033[K    %s  %d MB' "$(pad "$name" 13)" "$mb"
    fi
}

# fetch URL PART NAME [SIZE]: curl into PART, silent (its messages into the
# log), our own progress line on a terminal; up to 4 attempts, 2/5/10 s
# apart, each resuming (-C -); only network failures are retried (curl's own
# --retry covers a busy server). 0 = PART is complete.
fetch() {
    local url=$1 part=$2 name=$3 size=${4:-0} attempt=1 rc pid wait_s
    while [ "$attempt" -le 4 ]; do
        log "curl $url (attempt $attempt)"
        curl -fL -sS --retry 2 --connect-timeout 20 -C - -o "$part" "$url" >> "$LOG" 2>&1 &
        pid=$!
        if [ "$TTY" = 1 ]; then
            while kill -0 "$pid" 2>/dev/null; do draw_progress "$name" "$part" "$size"; sleep 0.3; done
        fi
        rc=0; wait "$pid" || rc=$?
        if [ "$TTY" = 1 ]; then printf '\r\033[K'; repend; fi
        [ "$rc" = 0 ] && return 0
        case "$rc" in
            33|36) rm -f -- "$part" ;;                     # no resume there: from 0
            5|6|7|18|28|35|52|55|56|92) ;;                 # the network: worth another try
            *) return "$rc" ;;                             # an answer (404, 403...): no retry
        esac
        [ "$attempt" = 4 ] && return "$rc"
        note "network problem (curl $rc), retrying $((attempt + 1))/4"
        case "$attempt" in 1) wait_s=2 ;; 2) wait_s=5 ;; *) wait_s=10 ;; esac
        sleep "$wait_s"
        attempt=$((attempt + 1))
    done
    return 1
}

# download URL DEST SHA256 [NAME] [SIZE]: to DEST.part, checked, then moved
download() {
    local url=$1 dest=$2 want=$3 name=${4:-$(basename "$2")} size=${5:-0} tmp="$2.part" rc=0
    mkdir -p "$(dirname "$dest")"
    rm -f -- "$tmp"
    fetch "$url" "$tmp" "$name" "$size" || rc=$?
    if [ "$rc" != 0 ]; then
        rm -f -- "$tmp"
        fail "could not download $url (curl exit $rc)." "check the network connection (a proxy needs https_proxy set) and run the installer again" "curl $url"
    fi
    local got; got=$(sha256_of "$tmp")
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
        rm -f -- "$tmp"
        fail "the file from $url is not the expected one (SHA-256 $got, expected $want)." "do not use this file; try again later or report it"
    fi
    mv -f -- "$tmp" "$dest"
}

# try_download URL DEST NAME: like download, without a digest; a failure is
# returned (the Mesmer sources are tried one after another)
try_download() {
    local tmp="$2.part"
    mkdir -p "$(dirname "$2")"
    rm -f -- "$tmp"
    if fetch "$1" "$tmp" "${3:-download}"; then mv -f -- "$tmp" "$2"; return 0; fi
    rm -f -- "$tmp"; return 1
}

# ── 1. checking this Mac (writes nothing) ─────────────────────────────────
free_gb() {
    local d=$1
    while [ ! -d "$d" ]; do d=$(dirname "$d"); done
    df -Pk "$d" | awk 'NR==2 {printf "%d", $4/1048576}'
}
preflight() {
    STEP="1. checking this Mac"
    STEP_LABEL="Computer"
    local problems="" facts="" v major arch chip g
    arch=$(uname -m)
    v=$(sw_vers -productVersion 2>/dev/null || echo 0)
    major=${v%%.*}
    log "system: macOS $v $arch; bash $BASH_VERSION"
    if [ "$(uname -s)" != Darwin ]; then
        problems="${problems}this is not a Mac ($(uname -s)): use scripts/install_block01.sh (Linux) or scripts\\install_block01.ps1 (Windows)
"
    elif [ "$arch" != arm64 ]; then
        problems="${problems}an Intel Mac ($arch) is not supported: PyTorch 2.5.1 has no macOS x86_64 build (Apple silicon only)
"
    fi
    if [ "${major:-0}" -lt 13 ] 2>/dev/null; then
        problems="${problems}macOS $v is too old (macOS 13 or newer)
"
    fi
    chip=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "")
    GPU_NAME="${chip:+$chip GPU}"
    case "$ROOT" in *" "*) problems="${problems}the install root \"$ROOT\" contains a space (a conda environment cannot live there): choose another with --root
" ;; esac
    g=$(free_gb "$ROOT")
    if [ "$g" -lt 20 ]; then
        problems="${problems}$g GB free at $ROOT; about 20 GB are needed
"
    fi
    facts="macOS $v, $g GB free${GPU_NAME:+, $GPU_NAME (Metal)}"
    if [ -n "$problems" ]; then
        printf '  %s%s Computer%s  %s%s%s\n' "$C_BAD" "$G_BAD" "$C_0" "$C_DIM" "$facts" "$C_0"
        printf '%s' "$problems" | while IFS= read -r p; do [ -n "$p" ] && printf '    %s%s%s\n' "$C_BAD" "$p" "$C_0"; log "problem: $p"; done
        STEP_LABEL=""
        fail "this Mac does not meet the requirements above." "fix what is listed, then run the installer again"
    fi
    step_ok Computer "$facts"
}

# ── the model licences: one card, one answer, before the long steps ───────
licence_row() {   # KEY -> "size|licence|its page"
    case "$1" in
        cellpose_cpsam) echo "1.2 GB|BSD-3-Clause; trained on CC-BY-NC data|https://huggingface.co/mouseland/cellpose-sam  (and the Cellpose README)" ;;
        stardist_2D_versatile_fluo) echo "5 MB|BSD-3-Clause|https://github.com/stardist/stardist" ;;
        mesmer_multiplex_segmentation) echo "100 MB|NON-COMMERCIAL academic use only|https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE" ;;
    esac
}
repeat() { local i=0 s=""; while [ "$i" -lt "$2" ]; do s="$s$1"; i=$((i + 1)); done; printf '%s' "$s"; }
licence_card() {
    local key size lic url rows="" w=40 row
    for key in "$@"; do
        IFS='|' read -r size lic url <<EOF
$(licence_row "$key")
EOF
        row=" $(pad "$(model_name "$key")" 14)$(pad "$size" 8)$lic "
        [ "${#row}" -le "$w" ] || w=${#row}
    done
    local title=" Model licences "
    out ""
    out "  $B_TL$title$(repeat "$B_H" $((w - ${#title})))$B_TR"
    for key in "$@"; do
        IFS='|' read -r size lic url <<EOF
$(licence_row "$key")
EOF
        row=" $(pad "$(model_name "$key")" 14)$(pad "$size" 8)$lic "
        if [ "$key" = mesmer_multiplex_segmentation ]; then out "  $B_V$C_WARN$(pad "$row" "$w")$C_0$B_V"
        else out "  $B_V$(pad "$row" "$w")$B_V"; fi
    done
    out "  $B_BL$(repeat "$B_H" "$w")$B_BR"
    for key in "$@"; do
        IFS='|' read -r size lic url <<EOF
$(licence_row "$key")
EOF
        out "    ${C_DIM}$(pad "$(model_name "$key")" 14)$url${C_0}"
        [ "$key" != mesmer_multiplex_segmentation ] || out "    ${C_DIM}$(pad "" 14)the PyTorch copy made from it has the same licence: never share it${C_0}"
        log "licence $key: $lic -- $url"
    done
}
LICENCES_ACCEPTED=" "
confirm_licences() {     # confirm_licences KEY...: the card, one answer for all
    [ $# -gt 0 ] || return 0
    local step_was=$STEP
    STEP="the model licences"
    STEP_LABEL="Model licences"
    licence_card "$@"
    if [ "$ACCEPT_LICENCES" = 1 ]; then
        out ""
        step_ok "Model licences" "accepted with --accept-model-licences"
    else
        if ! { : < /dev/tty; } 2>/dev/null; then
            fail "the model licences need your answer, and there is no terminal to ask." \
                 "run the installer in a terminal, or read the licences above and add --accept-model-licences"
        fi
        local answer what="all and install them"
        [ $# -gt 1 ] || what="and install it"
        printf '\n  Accept %s? [y/N] ' "$what" > /dev/tty
        read -r answer < /dev/tty || answer=""
        log "licences ($*): answered '$answer'"
        case "$answer" in
            y|Y|yes|Yes|YES) ;;
            *) STEP_LABEL=""
               fail "the licences were not accepted, so no model was installed." \
                    "run the installer again and answer y, or give your own copies with --models DIR" ;;
        esac
    fi
    out ""
    STEP_LABEL=""
    LICENCES_ACCEPTED="$LICENCES_ACCEPTED$* "
    STEP=$step_was
}
accept_licence() {       # a model the card did not cover
    case "$LICENCES_ACCEPTED" in *" $1 "*) return 0 ;; esac
    local label=$STEP_LABEL
    confirm_licences "$1"
    STEP_LABEL=$label
}
# manifest_paths KEY: the files models.json lists, read without Python (the
# card comes before the environment exists)
manifest_paths() {
    awk -v k="$1" '
        /^    "[^"]+": *\{/ { match($0, /"[^"]+"/); cur = substr($0, RSTART + 1, RLENGTH - 2) }
        cur == k && /"path":/ { sub(/.*"path": *"/, ""); sub(/".*/, ""); print }' "$MANIFEST"
}
models_to_license() {    # the models this run will install (a file missing)
    local key f missing
    for key in $ORIGINAL_MODELS; do
        missing=0
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            [ -f "$(model_dir "$key")/$f" ] || missing=1
        done <<EOF
$(manifest_paths "$key")
EOF
        [ "$missing" = 0 ] || echo "$key"
    done
}

# ── 2. micromamba ─────────────────────────────────────────────────────────
MAMBA=""
ensure_micromamba() {
    STEP="2. micromamba (the package installer)"
    MAMBA="$ROOT/bin/micromamba"
    if [ -x "$MAMBA" ] && [ "$(sha256_of "$MAMBA")" = "$MICROMAMBA_SHA256" ]; then
        step_ok micromamba "already installed"; return
    fi
    step_begin micromamba
    download "$MICROMAMBA_URL" "$MAMBA" "$MICROMAMBA_SHA256" micromamba
    chmod +x "$MAMBA"
    step_done
}

# ── 3. the environment ────────────────────────────────────────────────────
lock_id() {
    { cat "$REPO/envs/ci/requirements-cpu.txt" "$REPO/envs/macos/requirements.txt" \
          "$REPO/envs/macos/requirements-nodeps.txt" "$TOOLBOX_LOCK"; echo "$TORCH_PINS"; } \
        | shasum -a 256 | cut -c1-8
}
ENV_PREFIX=""
PY=""
# prefix_state PREFIX -> absent | complete | built | interrupted | foreign
prefix_state() {
    local p=$1
    if [ ! -e "$p" ]; then echo absent
    elif [ ! -f "$p.owner" ]; then echo foreign
    elif [ -f "$p/.block01_install_ok" ]; then echo complete
    elif [ -f "$p/.block01_env_ok" ]; then echo built
    else echo interrupted
    fi
}
# lock_entry LOCK VERSION PLATFORM -> "file sha256" or nothing
lock_entry() {
    [ -f "$1" ] || return 0
    awk -v v="$2" -v p="$3" '!/^[[:space:]]*#/ && $1 == v && $2 == p { print $3, $4; exit }' "$1"
}
ensure_toolbox() {
    local have entry file sha whl
    have=$("$PY" -c "import importlib.metadata as m; from deepcell_toolbox.compute_overlap import compute_overlap; print(m.version('DeepCell-Toolbox'))" 2>/dev/null | tail -n1 || true)
    if [ "$have" = "0.12.1" ]; then log "DeepCell-Toolbox 0.12.1: present"; return; fi
    entry=$(lock_entry "$TOOLBOX_LOCK" 0.12.1 macos_arm64)
    [ -n "$entry" ] || fail "envs/macos/wheels.lock names no DeepCell-Toolbox wheel for this checkout." "update the checkout (git pull)"
    file=${entry% *}; sha=${entry#* }
    whl="$REPO/envs/macos/wheels/$file"
    [ -f "$whl" ] || fail "the DeepCell-Toolbox wheel $whl that ships with this checkout is missing." "update the checkout (git pull)"
    [ "$(sha256_of "$whl")" = "$sha" ] || fail "$whl is not the expected file (its SHA-256 differs from envs/macos/wheels.lock)." "restore it with git (git checkout -- envs/macos/wheels) and run again"
    log "DeepCell-Toolbox 0.12.1: installing the wheel that ships with this checkout"
    run "$PY" -m pip install -q --no-warn-script-location --no-deps "$whl"
}
ensure_environment() {
    STEP="3. the Python environment"
    ENV_PREFIX="$ROOT/envs/block01-$(lock_id)"
    PY="$ENV_PREFIX/bin/python"
    local state; state=$(prefix_state "$ENV_PREFIX")
    log "environment: $ENV_PREFIX ($state)"
    case "$state" in
        complete|built) step_ok "Python environment" "already installed"; return ;;
        foreign) STEP_LABEL="Python environment"
                 fail "$ENV_PREFIX exists but was not created by this installer; it is left untouched." \
                      "install into a new location: bash scripts/install_block01_mac.sh --root NEW_DIR" ;;
        interrupted)
            case "$ENV_PREFIX" in "$ROOT"/envs/block01-????????) ;; *) fail "refusing to remove $ENV_PREFIX (unexpected path)" "" ;; esac
            log "an earlier install of the environment stopped half-way: it is made again"
            rm -rf -- "$ENV_PREFIX" ;;
    esac
    step_begin "Python environment" "a few GB, 10-30 min"
    mkdir -p "$ROOT/envs" "$ROOT/mamba"
    printf 'created by %s\npid %s\ntime %s\ncheckout %s\n' "$0" "$$" "$(now)" "$REPO" > "$ENV_PREFIX.owner"
    # conda-forge only: no Anaconda channel (its terms bind larger organisations)
    run env MAMBA_ROOT_PREFIX="$ROOT/mamba" "$MAMBA" create -y -q -p "$ENV_PREFIX" --override-channels -c conda-forge python=3.10 pip
    run "$PY" -m pip install -q --no-warn-script-location --upgrade "pip<27"
    run "$PY" -m pip install -q --no-warn-script-location -r "$REPO/envs/ci/requirements-cpu.txt"
    # shellcheck disable=SC2086  # the pins are words
    run "$PY" -m pip install -q --no-warn-script-location $TORCH_PINS
    run "$PY" -m pip install -q --no-warn-script-location -r "$REPO/envs/macos/requirements.txt"
    ensure_toolbox
    run "$PY" -m pip install -q --no-warn-script-location --no-deps -r "$REPO/envs/macos/requirements-nodeps.txt"
    printf 'finished %s\n' "$(now)" > "$ENV_PREFIX/.block01_env_ok"
    step_done
}

# ── 4. the viewer core ────────────────────────────────────────────────────
core_version() { awk -F'"' '/^version = /{print $2; exit}' "$CARGO_TOML"; }
installed_core() {
    "$PY" -c 'import fusion_viewer_core as c; print(c.__version__, getattr(c, "CORE_API", "none"))' 2>/dev/null || true
}
ensure_core() {
    STEP="4. the viewer core (fusion_viewer_core)"
    local want; want=$(core_version)
    if [ "$(installed_core)" = "$want 1" ]; then step_ok "Viewer core $want" "already installed"; return; fi
    step_begin "Viewer core $want"
    local entry; entry=$(lock_entry "$WHEELS_LOCK" "$want" macos)
    [ -n "$entry" ] || fail "there is no prebuilt macOS viewer core for version $want (a development checkout)." \
        "build it with Rust: pip install maturin, then maturin build --release -m native/fusion_viewer_core/Cargo.toml, and install the wheel into $ENV_PREFIX"
    local file=${entry% *} sha=${entry#* } wheel
    wheel="$ROOT/downloads/core-$want/$file"
    if [ ! -f "$wheel" ] || [ "$(sha256_of "$wheel")" != "$sha" ]; then
        download "$RELEASE_URL/core-v$want/$file" "$wheel" "$sha" "Viewer core"
    fi
    run "$PY" -m pip install -q --no-warn-script-location --no-deps --force-reinstall "$wheel"
    local have; have=$(installed_core)
    [ "$have" = "$want 1" ] || fail "the installed viewer core reports '$have', expected '$want 1'." ""
    step_done
}

# ── 5. models ─────────────────────────────────────────────────────────────
model_dir() { echo "$ROOT/models/$1"; }
# model_state KEY DIR -> ok | missing | partial | different (a file with
# other bytes is never overwritten)
model_state() {
    "$PY" - "$MANIFEST" "$1" "$2" <<'PYEOF'
import hashlib, json, os, sys
manifest, key, root = sys.argv[1:]
entry = json.load(open(manifest))["models"][key]
have = miss = 0
for f in entry["files"]:
    p = os.path.join(root, f["path"])
    if not os.path.isfile(p):
        miss += 1
        continue
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    if os.path.getsize(p) != f["size"] or h.hexdigest() != f["sha256"]:
        print("different"); sys.exit(0)
    have += 1
print("ok" if not miss else ("missing" if not have else "partial"))
PYEOF
}
manifest_files() {
    "$PY" -c 'import json, sys; [print(f["path"]) for f in json.load(open(sys.argv[1]))["models"][sys.argv[2]]["files"]]' "$MANIFEST" "$1"
}
# manifest_file KEY SUFFIX -> "sha256 size" of the file whose path ends so
manifest_file() {
    "$PY" -c 'import json, sys; e = json.load(open(sys.argv[1]))["models"][sys.argv[2]]; f = [f for f in e["files"] if f["path"].endswith(sys.argv[3])][0]; print(f["sha256"], f["size"])' "$MANIFEST" "$1" "$2"
}
# the stage's listed files moved into DIR, each to a path that does not
# exist yet; the stage removed
verify_into_place() {
    local key=$1 stage=$2 dir=$3 f st
    st=$(model_state "$key" "$stage")
    [ "$st" = ok ] || { rm -rf -- "$stage"; fail "the $key files do not match models.json ($st)." "try again later or report it"; }
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        [ ! -e "$dir/$f" ] || { rm -rf -- "$stage"; fail "$dir/$f appeared during the install; it is left untouched." "run the installer again"; }
        mkdir -p "$(dirname "$dir/$f")"
        mv -- "$stage/$f" "$dir/$f"
    done <<EOF
$(manifest_files "$key")
EOF
    rm -rf -- "$stage"
}
copy_listed() {          # copy_listed KEY FROM STAGE: the listed files, as real files
    local f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        mkdir -p "$(dirname "$3/$f")"
        cp -L -- "$2/$f" "$3/$f"
    done <<EOF
$(manifest_files "$1")
EOF
}
mesmer_from_archive() {  # ARCHIVE STAGE: the CONTENTS of its MultiplexSegmentation/
    local archive=$1 stage=$2 src
    mkdir -p "$stage/x"
    case "$archive" in
        *.zip) "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$archive" "$stage/x" || return 1 ;;
        *)     tar -xzf "$archive" -C "$stage/x" || return 1 ;;
    esac
    src=$(find "$stage/x" -type d -name MultiplexSegmentation | head -n1)
    [ -n "$src" ] || return 1
    cp -a "$src"/. "$stage"/
    rm -rf -- "$stage/x"
}
# ensure_mesmer DIR: DeepCell's archive, the mirror, the local archive, the
# --models folder -- the first whose files match models.json; none: the
# install goes on without Mesmer
ensure_mesmer() {
    local key=mesmer_multiplex_segmentation dir=$1 stage="$1.stage" src from
    for src in official mirror zip folder; do
        rm -rf -- "$stage"; mkdir -p "$stage"
        case "$src" in
            official)
                from="DeepCell's archive"
                log "Mesmer: trying $MESMER_URL"
                try_download "$MESMER_URL" "$stage/m.tar.gz" Mesmer || { note "DeepCell's archive is not available; trying the next source"; continue; }
                [ "$(sha256_of "$stage/m.tar.gz")" = "$MESMER_SHA256" ] || { note "DeepCell's archive is not the expected file; trying the next source"; continue; }
                mesmer_from_archive "$stage/m.tar.gz" "$stage" >> "$LOG" 2>&1 || continue
                rm -f -- "$stage/m.tar.gz" ;;
            mirror)
                [ -n "$MESMER_MIRROR" ] || continue
                if [ "$MESMER_MIRROR" = "$MESMER_MIRROR_DEFAULT" ]; then from="the lab's mirror"; else from="the mirror"; fi
                log "Mesmer: trying the mirror $MESMER_MIRROR"
                try_download "$MESMER_MIRROR" "$stage/m.tar.gz" Mesmer || { note "the mirror is not available"; continue; }
                mesmer_from_archive "$stage/m.tar.gz" "$stage" >> "$LOG" 2>&1 || { note "the mirror's file holds no MultiplexSegmentation folder"; continue; }
                rm -f -- "$stage/m.tar.gz" ;;
            zip)
                [ -n "$MESMER_ZIP" ] || continue
                from=$MESMER_ZIP
                [ -f "$MESMER_ZIP" ] || { note "$MESMER_ZIP does not exist"; continue; }
                mesmer_from_archive "$MESMER_ZIP" "$stage" >> "$LOG" 2>&1 || { note "$MESMER_ZIP holds no MultiplexSegmentation folder"; continue; } ;;
            folder)
                [ -n "$MODELS_IN" ] || continue
                [ "$(model_state "$key" "$MODELS_IN/$key")" = ok ] || continue
                from="$MODELS_IN/$key"
                copy_listed "$key" "$MODELS_IN/$key" "$stage" ;;
        esac
        if [ "$(model_state "$key" "$stage")" = ok ]; then
            verify_into_place "$key" "$stage" "$dir"
            step_done "from $from, verified ($(fmt_secs $(( $(date +%s) - STEP_T0 ))))"
            return 0
        fi
        note "its files do not match models.json; not used"
    done
    rm -rf -- "$stage"
    MESMER_MISSING=1
    unpend; PENDING=0; STEP_LABEL=""
    warn "Mesmer: no source worked; the install goes on without it (add it later with --mesmer-only)"
}
fetch_model() {
    local key=$1 dir=$2 stage="$2.stage" sha size
    rm -rf -- "$stage"; mkdir -p "$stage"
    case "$key" in
        cellpose_cpsam)
            read -r sha size <<EOF
$(manifest_file "$key" cpsam)
EOF
            download "$CPSAM_URL" "$stage/cpsam" "$sha" Cellpose-SAM "$size" ;;
        stardist_2D_versatile_fluo)
            read -r sha size <<EOF
$(manifest_file "$key" .zip)
EOF
            download "$STARDIST_URL" "$stage/2D_versatile_fluo.zip" "$sha" StarDist "$size"
            run "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$stage/2D_versatile_fluo.zip" "$stage" ;;
    esac
    verify_into_place "$key" "$stage" "$dir"
}
ensure_models() {
    STEP="5. the segmentation models"
    local key dir st name
    for key in $ORIGINAL_MODELS; do
        dir=$(model_dir "$key"); name=$(model_name "$key")
        st=$(model_state "$key" "$dir")
        log "model $key: $st"
        case "$st" in
            ok) step_ok "$name" "already installed"; continue ;;
            different|partial)
                STEP_LABEL=$name
                fail "model $key in $dir does not match models.json ($st); it is left untouched." \
                     "move that folder away (or use another --root) and run the installer again" ;;
        esac
        accept_licence "$key"
        step_begin "$name"
        if [ "$key" = mesmer_multiplex_segmentation ]; then ensure_mesmer "$dir"; continue; fi
        if [ -n "$MODELS_IN" ] && [ "$(model_state "$key" "$MODELS_IN/$key")" = ok ]; then
            local stage="$dir.stage"
            rm -rf -- "$stage"; mkdir -p "$stage"
            copy_listed "$key" "$MODELS_IN/$key" "$stage"
            verify_into_place "$key" "$stage" "$dir"
            step_done "copied from $MODELS_IN, verified"
            continue
        fi
        fetch_model "$key" "$dir"
        step_done "downloaded and verified ($(fmt_secs $(( $(date +%s) - STEP_T0 ))))"
    done
}

# the variables every later step and the launcher run with
app_env() {
    export KERAS_BACKEND=tensorflow
    export KERAS_HOME="$ROOT/keras"
    export MPLCONFIGDIR="$ROOT/cache/matplotlib"
    export PYTORCH_ENABLE_MPS_FALLBACK=1
    local key
    for key in $MODEL_KEYS; do export "$(model_var "$key")=$(model_dir "$key")"; done
    mkdir -p "$KERAS_HOME"
}
convert_stardist_weights() {
    STEP="5b. the StarDist torch weights"
    app_env
    step_quiet "StarDist (PyTorch weights)"
    (cd "$REPO" && run env PYTHONPATH="$REPO" "$PY" "$REPO/scripts/convert_stardist_to_torch.py")
    STEP_LABEL=""
}

# ── 6. the package link, the self-test ────────────────────────────────────
ensure_link() {
    STEP="6. the package link for this checkout"
    step_quiet "Package link"
    local link="$STATE_DIR/pkg/block01"
    mkdir -p "$STATE_DIR/pkg" "$STATE_DIR/run"
    if [ -L "$link" ]; then
        [ "$(readlink -f "$link")" = "$REPO" ] || fail "$link points at $(readlink -f "$link"), not this checkout; it is left untouched." "remove that link yourself if it is stale, then run the installer again"
    elif [ -e "$link" ]; then
        fail "$link exists and is not a link; it is left untouched." "move it away and run the installer again"
    else
        ln -s "$REPO" "$link"
        log "package link: $link -> $REPO"
    fi
    STEP_LABEL=""
}
installed_engines() {
    if [ "$MESMER_MISSING" = 1 ]; then echo "cellpose stardist"; else echo "cellpose stardist mesmer"; fi
}
# check_selftest ENGINE...: seg_runner.selftest's lines on stdin -> one line
# per engine, "engine ok|FAILED device"; 1 unless every engine is ok
check_selftest() {
    "$PY" -c '
import json, sys
want = sys.argv[1:]
got = {}
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    r = json.loads(line)
    ok = r.get("state") == "ok" and r.get("exit_code") == 0 and bool(r.get("device"))
    got[r.get("engine")] = ("ok", r["device"]) if ok else ("FAILED", "-")
bad = 0
for e in want:
    state, device = got.get(e, ("FAILED", "-"))
    bad += state != "ok"
    print("%s %s %s" % (e, state, device))
sys.exit(1 if bad else 0)
' "$@"
}
# selftest_pass FILE ENGINE...: one pass, its rows into FILE; the bullet blinks
selftest_pass() {
    local rows=$1 rc=0; shift
    log "self-test${BLOCK01_ENGINE_CPU_ONLY:+ (CPU only)}: $*"
    (cd "$STATE_DIR/run" && PYTHONPATH="$REPO" "$PY" -m seg_runner.selftest "$@" > "$STATE_DIR/run/selftest.out" 2>"$STATE_DIR/run/selftest.err") &
    local pid=$!
    if [ "$PENDING" = 1 ]; then blink "$pid"; fi
    wait "$pid" || true
    cat "$STATE_DIR/run/selftest.out" >> "$LOG"
    check_selftest "$@" < "$STATE_DIR/run/selftest.out" > "$rows" || rc=$?
    cat "$rows" >> "$LOG"
    return "$rc"
}
# shellcheck disable=SC2094  # both files are only read
selftest_table() {       # selftest_table GPU_ROWS CPU_ROWS
    local e st _d name g c
    unpend; PENDING=0
    printf '    %s%s%s%s\n' "$(pad "" 12)" "$C_DIM" "$(pad GPU 8)CPU" "$C_0"
    while read -r e st _d; do
        [ -n "$e" ] || continue
        case "$e" in cellpose) name=Cellpose ;; stardist) name=StarDist ;; mesmer) name=Mesmer ;; *) name=$e ;; esac
        g=$(awk -v e="$e" '$1 == e { print $2 }' "$1"); c=$(awk -v e="$e" '$1 == e { print $2 }' "$2")
        printf '    %s' "$(pad "$name" 12)"
        if [ "$g" = ok ]; then printf '%s%s%s' "$C_OK" "$(pad ok 8)" "$C_0"; else printf '%s%s%s' "$C_BAD" "$(pad "${g:-FAILED}" 8)" "$C_0"; fi
        if [ "$c" = ok ]; then printf '%s%s%s\n' "$C_OK" ok "$C_0"; else printf '%s%s%s\n' "$C_BAD" "${c:-FAILED}" "$C_0"; fi
    done < "$1"
}
selftest() {
    STEP="7. self-test"
    app_env
    if [ "$SKIP_MODELS" = 1 ]; then
        out "  ${C_DIM}$G_RUN $(pad Self-test 20)skipped (--skip-models)${C_0}"
    else
        local engines gpu_rows cpu_rows bad=0
        engines=$(installed_engines)
        gpu_rows="$STATE_DIR/run/selftest.gpu"; cpu_rows="$STATE_DIR/run/selftest.cpu"
        step_begin Self-test "one small task per engine"
        # shellcheck disable=SC2086  # the engine names are words
        selftest_pass "$gpu_rows" $engines || bad=1
        # the GPU hidden, as Step2's "Use GPU" off does it (seg_runner/engines.py)
        # shellcheck disable=SC2086
        (export BLOCK01_ENGINE_CPU_ONLY=1; selftest_pass "$cpu_rows" $engines) || bad=1
        if [ "$bad" = 1 ]; then
            selftest_table "$gpu_rows" "$cpu_rows"
            fail "the self-test failed (its error output: $STATE_DIR/run/selftest.err)." "read the error, fix the cause and run the installer again"
        fi
        (cd "$STATE_DIR/run" && run "$PY" "$REPO/scripts/model_manifest.py" --verify --installed)
        step_done
        selftest_table "$gpu_rows" "$cpu_rows"
    fi
    local bg core
    bg=$(cd "$STATE_DIR/run" && PYTHONPATH="$STATE_DIR/pkg" "$PY" -c \
        'from block01.core import mps_kernels as m; m.ensure_ready(); print("ok" if m.ready() else "cpu", m.error())' \
        2>"$STATE_DIR/run/gpucheck.err" | tail -n1 || echo "cpu (check failed: $STATE_DIR/run/gpucheck.err)")
    log "GPU background correction: $bg"
    case "$bg" in ok*) step_ok "GPU background correction" "ok" ;;
                  *) warn "GPU background correction unavailable, the CPU is used ($bg)" ;; esac
    STEP_LABEL="App starts"
    core=$(cd "$STATE_DIR/run" && QT_QPA_PLATFORM=offscreen PYTHONPATH="$STATE_DIR/pkg" "$PY" -c '
from PySide6.QtWidgets import QApplication
app = QApplication([])
import block01.main  # noqa: F401 -- the app imports
from block01.ui.main_window import MainWindow
w = MainWindow()          # the window is built and shown once, offscreen
w.show()
app.processEvents()
w.close()
app.processEvents()
from block01.ui.step1_core_adapter import core_unavailable_reason
import fusion_viewer_core as c
why = core_unavailable_reason()
print(f"fusion_viewer_core {c.__version__} API {c.CORE_API} accepted" if not why else "REFUSED: " + why)
' 2>"$STATE_DIR/run/startcheck.err" | tail -n1 || true)
    log "app start check: $core"
    case "$core" in
        *accepted) step_ok "App starts" "viewer core $(core_version)" ;;
        *) tail -n 15 "$STATE_DIR/run/startcheck.err" >> "$LOG" 2>/dev/null || true
           fail "the app does not start or refuses its viewer core: ${core:-no answer} (all of its error output: $STATE_DIR/run/startcheck.err)." \
                "read the error, fix the cause and run the installer again" ;;
    esac
}

# ── 8. the record, the app ────────────────────────────────────────────────
write_record() {
    STEP="8. the install record"
    local rec="$STATE_DIR/install.env" key
    {
        echo "# written by scripts/install_block01_mac.sh $(now); read by scripts/block01"
        printf 'BLOCK01_CHECKOUT=%q\n' "$REPO"
        printf 'BLOCK01_ROOT=%q\n' "$ROOT"
        printf 'BLOCK01_PREFIX=%q\n' "$ENV_PREFIX"
        printf 'BLOCK01_CORE_VERSION=%q\n' "$(core_version)"
        if [ "$MESMER_MISSING" = 1 ]; then echo "BLOCK01_MESMER=missing"; else echo "BLOCK01_MESMER=installed"; fi
        printf 'export KERAS_BACKEND=tensorflow\n'
        printf 'export KERAS_HOME=%q\n' "$ROOT/keras"
        printf 'export MPLCONFIGDIR=%q\n' "$ROOT/cache/matplotlib"
        printf 'export PYTORCH_ENABLE_MPS_FALLBACK=1\n'
        for key in $MODEL_KEYS; do printf 'export %s=%q\n' "$(model_var "$key")" "$(model_dir "$key")"; done
    } > "$rec.tmp"
    mv -f "$rec.tmp" "$rec"
    printf 'core %s\ntime %s\n' "$(core_version)" "$(now)" > "$ENV_PREFIX/.block01_install_ok"
}
# write_app: ~/Applications/FusionFlux.app, which runs scripts/block01 of
# this checkout (no Terminal window; Launchpad and Spotlight find it). An
# app there that is not ours is left untouched.
write_app() {
    STEP="8. the app"
    local plist="$APP_DIR/Contents/Info.plist"
    if [ -e "$APP_DIR" ] && ! grep -q "org.block01.fusionflux" "$plist" 2>/dev/null; then
        warn "$APP_DIR exists and is not ours; it is left untouched (start with $REPO/scripts/block01)"
        return
    fi
    mkdir -p "$APP_DIR/Contents/MacOS"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FusionFlux</string>
  <key>CFBundleDisplayName</key><string>FusionFlux</string>
  <key>CFBundleIdentifier</key><string>org.block01.fusionflux</string>
  <key>CFBundleExecutable</key><string>FusionFlux</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$(core_version)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
    cat > "$APP_DIR/Contents/MacOS/FusionFlux" <<EOF
#!/bin/bash
# made by scripts/install_block01_mac.sh: starts FusionFlux from this checkout
mkdir -p $(printf '%q' "$STATE_DIR/logs")
exec $(printf '%q' "$REPO/scripts/block01") "\$@" >> $(printf '%q' "$STATE_DIR/logs/app.log") 2>&1
EOF
    chmod +x "$APP_DIR/Contents/MacOS/FusionFlux"
    log "app: $APP_DIR"
    step_ok "App" "FusionFlux in ~/Applications"
}

# --mesmer-only: add Mesmer to the install this checkout already has
mesmer_only() {
    STEP="adding Mesmer to the existing install"
    local rec="$STATE_DIR/install.env"
    [ -f "$rec" ] || fail "this checkout has no install yet ($rec is missing)." "run the installer without --mesmer-only first"
    # shellcheck disable=SC1090
    . "$rec"
    ROOT=$BLOCK01_ROOT; ENV_PREFIX=$BLOCK01_PREFIX; PY="$ENV_PREFIX/bin/python"
    [ -x "$PY" ] || fail "the environment $ENV_PREFIX is gone." "run the installer without --mesmer-only"
    take_lock                                   # the same root as a full install
    local key=mesmer_multiplex_segmentation dir done_text="Mesmer added."; dir=$(model_dir "$key")
    case "$(model_state "$key" "$dir")" in
        ok) step_ok Mesmer "already installed"; done_text="Mesmer was already installed." ;;
        missing)
            accept_licence "$key"
            step_begin Mesmer
            ensure_mesmer "$dir"
            [ "$MESMER_MISSING" = 0 ] || fail "no Mesmer source worked (messages above)." \
                "give a file with --mesmer-zip FILE, a link with --mesmer-url URL, or a folder with --models DIR" ;;
        *) fail "$dir holds files that do not match models.json; they are left untouched." "move that folder away and run this again" ;;
    esac
    STEP="the Mesmer self-test"
    app_env
    step_begin Self-test Mesmer
    local rows="$STATE_DIR/run/selftest.mesmer"
    selftest_pass "$rows" mesmer || fail "the Mesmer self-test failed (its error output: $STATE_DIR/run/selftest.err)." "read the error, fix the cause and run this again"
    (cd "$STATE_DIR/run" && run "$PY" "$REPO/scripts/model_manifest.py" --verify --installed)
    step_done
    sed -i '' 's/^BLOCK01_MESMER=.*$/BLOCK01_MESMER=installed/' "$rec"
    out ""
    out "${C_B}$done_text${C_0} Start FusionFlux as before (FusionFlux in ~/Applications, or $REPO/scripts/block01)."
    out "${C_DIM}Log: $LOG${C_0}"
}

# one install into a root at a time (no flock on a Mac: a lock folder that
# names its owner; an owner no longer running leaves a stale lock, taken over)
LOCK_DIR=""
take_lock() {
    LOCK_DIR="$ROOT/.install.lock"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        local pid; pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
        # no owner named yet: another install may have just made the folder
        [ -n "$pid" ] || { sleep 2; pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo ""); }
        # ps, not kill -0: that fails for another user's process too
        if [ -z "$pid" ] || ps -p "$pid" > /dev/null 2>&1; then
            local dir=$LOCK_DIR; LOCK_DIR=""
            fail "another install into $ROOT is running${pid:+ (process $pid)}." \
                 "wait for it to finish, then run the installer again (if none is running, remove $dir)"
        fi
        # its owner is gone: a stale lock, taken over in one step (a rename)
        mv -- "$LOCK_DIR" "$LOCK_DIR.stale.$$" 2>/dev/null && rm -rf -- "$LOCK_DIR.stale.$$"
        mkdir "$LOCK_DIR" 2>/dev/null || { LOCK_DIR=""; fail "another install into $ROOT has just started." "wait for it to finish, then run the installer again"; }
    fi
    echo $$ > "$LOCK_DIR/pid"
    trap 'rm -rf -- "$LOCK_DIR"' EXIT
}

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --root) ROOT=$(abspath "$2"); shift 2 ;;
            --check-only) CHECK_ONLY=1; shift ;;
            --models) MODELS_IN=$(abspath "$2"); shift 2 ;;
            --accept-model-licences) ACCEPT_LICENCES=1; shift ;;
            --mesmer-url) MESMER_MIRROR=$2; shift 2 ;;
            --mesmer-zip) MESMER_ZIP=$(abspath "$2"); shift 2 ;;
            --mesmer-only) MESMER_ONLY=1; shift ;;
            --skip-models) SKIP_MODELS=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 2 ;;
        esac
    done
    local t0; t0=$(date +%s)
    if [ "$CHECK_ONLY" = 0 ]; then
        mkdir -p "$STATE_DIR/logs"
        LOG="$STATE_DIR/logs/install-$(date +%Y%m%d-%H%M%S).log"
        { echo "# scripts/install_block01_mac.sh $*"; echo "# $(now) on $(uname -srm)"; } > "$LOG"
    fi
    out ""
    out "${C_B}FusionFlux installer${C_0}"
    out "${C_DIM}  checkout    $REPO${C_0}"
    [ "$MESMER_ONLY" = 1 ] || out "${C_DIM}  install to  $ROOT${C_0}"
    out ""
    if [ "$MESMER_ONLY" = 1 ]; then mesmer_only; exit 0; fi
    preflight
    [ "$CHECK_ONLY" = 0 ] || { out "${C_DIM}check only: nothing was written${C_0}"; exit 0; }
    # every model licence, answered once, before the long steps
    # shellcheck disable=SC2046  # model keys are words
    [ "$SKIP_MODELS" = 1 ] || confirm_licences $(models_to_license)
    RESUMABLE=1
    STEP="2. starting"
    mkdir -p "$ROOT"
    take_lock
    export PIP_CACHE_DIR="$ROOT/cache/pip"
    ensure_micromamba
    ensure_environment
    mkdir -p "$STATE_DIR/run"
    ensure_core
    if [ "$SKIP_MODELS" = 1 ]; then
        out "  ${C_DIM}$G_RUN $(pad Models 20)skipped (--skip-models)${C_0}"
    else
        ensure_models
        convert_stardist_weights
    fi
    ensure_link
    selftest
    write_record
    write_app
    out ""
    if [ "$MESMER_MISSING" = 1 ]; then
        warn "Mesmer is not installed (its three methods are unavailable). Add it later with:"
        warn "  bash $REPO/scripts/install_block01_mac.sh --mesmer-only --mesmer-zip FILE   (or --mesmer-url URL, --models DIR)"
        out ""
    fi
    out "${C_OK}${C_B}Done in $(fmt_secs $(( $(date +%s) - t0 ))).${C_0} Open FusionFlux in ~/Applications (or Launchpad), or run:"
    out "  $REPO/scripts/block01"
    out "${C_DIM}Log: $LOG${C_0}"
}

if [ -z "${BLOCK01_INSTALL_SOURCED:-}" ]; then
    main "$@"
fi
