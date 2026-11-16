#!/usr/bin/env bash
# Install Block01 with one command (docs/v17_install_plan.md §2).
#
#   bash scripts/install_block01.sh [--root DIR] [--check-only] [--build-core]
#                                   [--models DIR] [--accept-model-licences]
#                                   [--mesmer-url URL] [--mesmer-zip FILE]
#                                   [--no-spatialdata] [--older-qt]
#   bash scripts/install_block01.sh --mesmer-only [--mesmer-url URL | --mesmer-zip FILE
#                                   | --models DIR] [--accept-model-licences]
#
# Mesmer (backlog 23, 2026-11-15): DeepCell's public archive (SPACEc's URL)
# has answered 403 since 2026-11-14. It is tried first; then a mirror
# (--mesmer-url or BLOCK01_MESMER_URL, else the lab's), a local archive (--mesmer-zip) or
# a folder (--models DIR/mesmer_multiplex_segmentation), each checked file
# by file against models.json. None works: the install finishes without
# Mesmer and says how to add it later (--mesmer-only).
#
# Linux x86_64 + NVIDIA, glibc 2.28 or newer. Below glibc 2.34 the interface
# gets PySide6 6.9.3 (the last release built for it) in an environment of its
# own; --older-qt picks that route on a newer system too
# (docs/v17_older_linux_qt_plan.md). Everything goes under the install root (default
# ~/.local/share/block01): the environment, the models, the tools it
# downloads. It never modifies an environment, a model file or a link it did
# not create; a run that stopped half-way is recognised and redone. The
# checkout gets its own record in <repo>/.block01/ (gitignored), which
# scripts/block01 reads to start the app.
#
# Sourced with BLOCK01_INSTALL_SOURCED=1 it only defines its functions
# (tests/test_install_scripts.py).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SPEC="$REPO/envs/fusion_mesmer"
SD_SPEC="$REPO/envs/block01_spatialdata"      # Step4's SpatialData export (its own NumPy 2 stack)
MANIFEST="$SPEC/models.json"
OLDER_PINS="$SPEC/requirements-pip-older-linux.txt"   # the Qt pins below glibc 2.34
WHEELS_LOCK="$REPO/native/fusion_viewer_core/wheels.lock"
CARGO_TOML="$REPO/native/fusion_viewer_core/Cargo.toml"
RELEASE_URL="${BLOCK01_RELEASE_URL:-https://github.com/964612858j5/Fusion_analysis/releases/download}"

# pinned downloads, verified before they are run
MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-linux-64"
MICROMAMBA_SHA256="77b7790ec97f64581118f103585b175df4306f95829b0fa6bfe4a19cc88a1182"
RUSTUP_URL="https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-linux-gnu/rustup-init"
RUSTUP_SHA256="20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c"
RUST_TOOLCHAIN="1.99.0"
MATURIN_PIN="maturin==1.15.0"

CPSAM_URL="https://huggingface.co/mouseland/cellpose-sam/resolve/main/cpsam"
STARDIST_URL="https://github.com/stardist/stardist-models/releases/download/v0.1/python_2D_versatile_fluo.zip"
# DeepCell's public model storage -- the same archive DeepCell's own client
# and SPACEc fetch, no account token (docs/v17_mesmer_public_download_plan.md)
MESMER_URL="https://deepcell-data.s3-us-west-1.amazonaws.com/saved-models/MultiplexSegmentation-9.tar.gz"
MESMER_SHA256="25b1babc2ff769a9f9484184073db8bc264d5ab2de8a6f9ce07c289e8b935129"

# environment variable each original model entry is found through (§2.5)
declare -A MODEL_VAR=(
    [cellpose_cpsam]=CELLPOSE_LOCAL_MODELS_PATH
    [stardist_2D_versatile_fluo]=BLOCK01_STARDIST_MODEL_PATH
    [stardist_2D_versatile_fluo_torch]=BLOCK01_STARDIST_TORCH_PATH
    [mesmer_multiplex_segmentation]=DEEPCELL_MESMER_MODEL_PATH
    [mesmer_multiplex_segmentation_torch]=BLOCK01_MESMER_TORCH_PATH
)
ORIGINAL_MODELS=(cellpose_cpsam stardist_2D_versatile_fluo mesmer_multiplex_segmentation)

ROOT="${HOME}/.local/share/block01"
CHECK_ONLY=0
BUILD_CORE=0
NO_SPATIALDATA=0
OLDER_QT=0          # 1: PySide6 6.9.3 (glibc 2.28-2.33, or --older-qt)
MODELS_IN=""
# the lab's mirror (2026-11-15, the user's Google Drive): the Mac's verified copy,
# repacked; checked file by file against models.json. --mesmer-url replaces it.
MESMER_MIRROR_DEFAULT="https://drive.usercontent.google.com/download?id=1bsRETcNz6ritHEA00Ds9cAoffr48WnQi&export=download&confirm=t"
MESMER_MIRROR="${BLOCK01_MESMER_URL:-$MESMER_MIRROR_DEFAULT}"   # a mirror of the Mesmer archive
MESMER_ZIP=""                              # a local Mesmer archive (.tar.gz or .zip)
MESMER_ONLY=0                              # 1: add Mesmer to an existing install, nothing else
MESMER_MISSING=0                           # 1: no source worked; the install goes on without it
ACCEPT_LICENCES=0
STEP="start"
CUDA_DIR=""

# ── output ────────────────────────────────────────────────────────────────
# Backlog 23 (user ruling 2026-11-15): the terminal shows one line per step
# and only what matters; every tool's own output (micromamba, pip, curl,
# the Python helpers) goes to the log, LOG, opened by main in
# <repo>/.block01/logs/. Glyphs and box lines only on a UTF-8 terminal,
# colours only on a terminal (not TERM=dumb, not NO_COLOR).
LOG=/dev/null
STEP_LABEL=""          # the step on screen, for the ✗ line of a failure
STEP_T0=0
PENDING=0              # a "• step ..." line waiting for its result (terminal)
PENDING_LINE=""        # its text: redrawn under anything printed meanwhile
RESUMABLE=0            # 1 once past the checks: a rerun continues
ui_init() {
    TTY=0; [ -t 1 ] && TTY=1
    local loc="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" utf=0
    case "$loc" in *UTF-8*|*utf-8*|*UTF8*|*utf8*) utf=1 ;; esac
    if [ "$TTY" = 1 ] && [ "$utf" = 1 ]; then
        G_OK="✓"; G_BAD="✗"; G_RUN="•"; G_SEP=" · "
        B_TL="┌"; B_TR="┐"; B_BL="└"; B_BR="┘"; B_H="─"; B_V="│"; BAR_ON="█"; BAR_OFF="░"
    else
        G_OK="[ok]"; G_BAD="[x]"; G_RUN="-"; G_SEP=", "
        B_TL="+"; B_TR="+"; B_BL="+"; B_BR="+"; B_H="-"; B_V="|"; BAR_ON="#"; BAR_OFF="-"
    fi
    if [ "$TTY" = 1 ] && [ "${TERM:-dumb}" != dumb ] && [ -z "${NO_COLOR:-}" ]; then
        C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'; C_0=$'\033[0m'
    else
        C_OK=""; C_BAD=""; C_WARN=""; C_DIM=""; C_B=""; C_0=""
    fi
}
ui_init

log() { printf '%s\n' "$*" >> "$LOG"; }
# the pending "• step ..." line stays the last line on a terminal and is
# updated in place (backlog 23, the user's review 2026-11-15): a line printed
# meanwhile goes above it (interject), its result replaces it (step_done);
# flush ends it only when another step starts without one
flush() { if [ "$PENDING" = 1 ]; then printf '\n'; PENDING=0; fi; }
unpend() { if [ "$PENDING" = 1 ]; then printf '\r\033[K'; fi; }
repend() { if [ "$PENDING" = 1 ]; then printf '%s' "$PENDING_LINE"; fi; }
out() { unpend; printf '%s\n' "$*"; repend; }
fmt_secs() {
    local s=$1
    if [ "$s" -lt 60 ]; then echo "$s s"; else echo "$((s / 60)) min $((s % 60)) s"; fi
}
# step_begin LABEL [NOTE]: "  • LABEL NOTE ..." until step_done
step_begin() {
    flush
    STEP_LABEL=$1; STEP_T0=$(date +%s)
    log "== $1"
    if [ "$TTY" = 1 ]; then PENDING_LINE=$(printf '  %s %s%s%s ...' "$G_RUN" "$1" "${2:+ }" "${2:-}"); printf '%s' "$PENDING_LINE"; PENDING=1
    else printf '  %s %s%s%s ...\n' "$G_RUN" "$1" "${2:+ }" "${2:-}"; fi
}
# step_done [DETAIL]: "  ✓ LABEL  DETAIL" (default: the time it took)
step_done() {
    local detail=${1:-$(fmt_secs $(( $(date +%s) - STEP_T0 )))}
    log "ok: $STEP_LABEL  $detail"
    if [ "$PENDING" = 1 ]; then printf '\r\033[K'; PENDING=0; fi
    printf '  %s%s%s %s  %s%s%s\n' "$C_OK" "$G_OK" "$C_0" "$STEP_LABEL" "$C_DIM" "$detail" "$C_0"
    STEP_LABEL=""
}
# step_ok LABEL DETAIL: a step with nothing to do
step_ok() { flush; STEP_LABEL=$1; step_done "$2"; }
# step_quiet LABEL: a step with no line of its own (named only if it fails)
step_quiet() { flush; STEP_LABEL=$1; log "== $1"; }
step_skip() { out "  - $1  ${C_DIM}$2${C_0}"; log "skipped: $1  $2"; }
sub_ok() { out "    ${C_OK}${G_OK}${C_0} $1  ${C_DIM}$2${C_0}"; log "ok: $1  $2"; }
note() { out "    ${C_DIM}$*${C_0}"; log "$*"; }
warn() { unpend; printf '  %s! %s%s\n' "$C_WARN" "$*" "$C_0" >&2; repend; log "WARNING: $*"; }
# fail REASON HINT [COMMAND]: the step, what to do, the end of the log
fail() {
    unpend; PENDING=0
    {
        [ -n "$STEP_LABEL" ] && printf '  %s%s %s%s\n' "$C_BAD" "$G_BAD" "$STEP_LABEL" "$C_0"
        printf '%sSTOPPED at: %s%s\n' "$C_BAD" "$STEP" "$C_0"
        [ -n "${3:-}" ] && printf 'failing command: %s\n' "$3"
        printf '%s\n' "$1"
        [ -n "${2:-}" ] && printf 'what to do: %s\n' "$2"
        [ "$RESUMABLE" = 1 ] && printf '%sfinished steps are kept: running the installer again continues from here%s\n' "$C_DIM" "$C_0"
        if [ -f "$LOG" ] && [ -s "$LOG" ]; then
            printf 'last lines of the log:\n'
            tail -n 20 "$LOG" | sed "s/^/  ${C_DIM}/; s/\$/${C_0}/"
            printf 'log: %s\n' "$LOG"
        fi
    } >&2
    log "STOPPED at: $STEP: $1"
    exit 1
}
# blink PID: the running step's bullet on and off while PID runs (the
# user's review, 2026-11-15: a long step must look alive)
blink() {
    local pid=$1 on=1 off="${PENDING_LINE/"$G_RUN"/${G_RUN//?/ }}"
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if [ "$on" = 1 ]; then printf '\r%s' "$off"; on=0; else printf '\r%s' "$PENDING_LINE"; on=1; fi
    done
    [ "$on" = 1 ] || printf '\r%s' "$PENDING_LINE"
}
run() {  # run CMD... ; its output to the log; on failure stop with the command line
    local rc=0
    if [ "$PENDING" = 1 ]; then
        "$@" >> "$LOG" 2>&1 &
        local pid=$!
        blink "$pid"
        wait "$pid" || rc=$?
    else
        "$@" >> "$LOG" 2>&1 || rc=$?
    fi
    [ "$rc" = 0 ] || fail "the command failed (exit $rc)." "${RUN_HINT:-read the end of the log below; fix the cause and run the installer again}" "$*"
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# draw_progress NAME FILE SIZE: one line, redrawn ("    NAME  ████░░  71%  850/1200 MB")
draw_progress() {
    local name=$1 file=$2 size=${3:-0} have mb
    have=0; [ -f "$file" ] && have=$(wc -c 2>/dev/null < "$file"); have=$((have + 0))
    mb=$((have / 1048576))
    if [ "$size" -gt 0 ]; then
        local pct=$((have * 100 / size)) w=24 on bar="" i
        [ "$pct" -le 100 ] || pct=100
        on=$((pct * w / 100))
        for ((i = 0; i < w; i++)); do if [ "$i" -lt "$on" ]; then bar+="$BAR_ON"; else bar+="$BAR_OFF"; fi; done
        printf '\r\033[K    %s  %s %3d%%  %d/%d MB' "$name" "$bar" "$pct" "$mb" "$((size / 1048576))"
    else
        printf '\r\033[K    %s  %d MB' "$name" "$mb"
    fi
}

# fetch URL PART NAME [SIZE]: curl into PART, 4 attempts (2, 5, 10 s apart),
# each resuming what the last one got (-C -; a server without ranges: from
# 0 again). curl's own messages go to the log; on a terminal we draw the
# progress. 0 = PART is complete.
FETCH_WAITS=(2 5 10)
fetch() {
    local url=$1 part=$2 name=$3 size=${4:-0} attempt rc pid
    for attempt in 1 2 3 4; do
        log "curl $url (attempt $attempt)"
        # curl's own --retry covers what -f makes exit 22 (408, 429, 5xx)
        curl -fL -sS --retry 2 --connect-timeout 20 -C - -o "$part" "$url" >> "$LOG" 2>&1 &
        pid=$!
        if [ "$TTY" = 1 ]; then
            while kill -0 "$pid" 2>/dev/null; do draw_progress "$name" "$part" "$size"; sleep 0.3; done
        fi
        rc=0; wait "$pid" || rc=$?
        [ "$TTY" = 1 ] && { printf '\r\033[K'; repend; }
        [ "$rc" = 0 ] && return 0
        case "$rc" in
            33) rm -f -- "$part" ;;                        # no resume there: from 0
            6|7|18|28|35|52|55|56|92) ;;                   # the network: worth another try
            *) return "$rc" ;;                             # an answer (404, 403...): no retry
        esac
        [ "$attempt" = 4 ] && return "$rc"
        note "network problem (curl $rc), retrying $((attempt + 1))/4"
        sleep "${FETCH_WAITS[$((attempt - 1))]}"
    done
}

# download URL DEST SHA256 [NAME] [SIZE]: to DEST.part, verified, then moved
# into place; nothing is left on failure
download() {
    local url=$1 dest=$2 want=$3 name=${4:-$(basename "$2")} size=${5:-0}
    local tmp="$dest.part"
    mkdir -p "$(dirname "$dest")"
    rm -f -- "$tmp" "$dest".part.*             # left by a run that was killed
    local rc=0
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

# ── 1. preflight (writes nothing) ─────────────────────────────────────────
nearest_dir() { local d=$1; while [ ! -d "$d" ]; do d=$(dirname "$d"); done; echo "$d"; }
free_gb() { df -Pk "$(nearest_dir "$1")" | awk 'NR==2 {printf "%d", $4/1048576}'; }

# the CUDA toolkit CuPy would use: CUDA_PATH, then nvcc on PATH, then /usr/local/cuda
resolve_cuda() {
    if [ -n "${CUDA_PATH:-}" ] && [ -d "$CUDA_PATH" ]; then echo "$CUDA_PATH"; return; fi
    local nvcc; nvcc=$(command -v nvcc 2>/dev/null || true)
    if [ -n "$nvcc" ]; then dirname "$(dirname "$(readlink -f "$nvcc")")"; return; fi
    [ -d /usr/local/cuda ] && { readlink -f /usr/local/cuda; return; }
    echo ""
}

# the system libraries the Qt binding needs for its window (xcb) and offscreen
# platforms, with the Debian/Ubuntu package that has each (found by the
# clean ubuntu:22.04 test: a bare system has none of them)
QT_SYSTEM_LIBS="libGL.so.1:libgl1 libEGL.so.1:libegl1 libX11-xcb.so.1:libx11-xcb1 libXext.so.6:libxext6
libdbus-1.so.3:libdbus-1-3 libglib-2.0.so.0:libglib2.0-0 libgthread-2.0.so.0:libglib2.0-0
libfontconfig.so.1:libfontconfig1 libxkbcommon.so.0:libxkbcommon0 libxkbcommon-x11.so.0:libxkbcommon-x11-0
libxcb-icccm.so.4:libxcb-icccm4 libxcb-image.so.0:libxcb-image0 libxcb-keysyms.so.1:libxcb-keysyms1
libxcb-randr.so.0:libxcb-randr0 libxcb-render-util.so.0:libxcb-render-util0 libxcb-render.so.0:libxcb-render0
libxcb-cursor.so.0:libxcb-cursor0
libxcb-shape.so.0:libxcb-shape0 libxcb-shm.so.0:libxcb-shm0 libxcb-sync.so.1:libxcb-sync1
libxcb-xfixes.so.0:libxcb-xfixes0 libxcb-xinerama.so.0:libxcb-xinerama0 libxcb-xkb.so.1:libxcb-xkb1"

# check_system_libs: 0 when every library is there; else 1 and the missing
# ones with their packages, one finding per line
check_system_libs() {
    local have missing="" pkgs="" pair lib pkg
    have=$(ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null || true)
    for pair in $QT_SYSTEM_LIBS; do
        lib=${pair%%:*}; pkg=${pair#*:}
        case "$have" in *"$lib "*) ;; *) missing="$missing $lib"; case " $pkgs " in *" $pkg "*) ;; *) pkgs="$pkgs $pkg" ;; esac ;; esac
    done
    if [ -z "$missing" ]; then
        echo "Qt libs:  present"; return 0
    fi
    echo "Qt libs:  MISSING$missing"
    echo "  Ubuntu/Debian: sudo apt install$pkgs"
    echo "  RHEL/Rocky: sudo dnf install the packages that provide these files"
    echo "  (libxcb-cursor.so.0 is xcb-util-cursor, from EPEL)"
    echo "  (other systems: the packages that provide these files)"
    return 1
}

glibc_at_least() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | sed -n 1p)" = "$2" ]; }

# preflight: one line for this computer ("✓ Computer  Linux x86_64 · glibc
# 2.35 · ..."); below it what stops the install (✗) and what only warns (!)
preflight() {
    STEP="1. checking this computer"
    STEP_LABEL="Computer"
    local hard=0 facts=() problems=() warnings=() notes=()
    if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
        problems+=("system: $(uname -s) $(uname -m) -- NOT SUPPORTED (Linux x86_64 only)"); hard=1
    else
        facts+=("Linux x86_64")
    fi
    # (no `cmd | head` here: under pipefail the writer's SIGPIPE fails the pipe)
    local glibc; glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null) || glibc="glibc 0"
    glibc=${glibc##* }
    # PySide6 6.10+ ships Linux wheels only as manylinux_2_34; 6.9.3 is the
    # last one built for 2.28, the floor of the viewer core, h5py and pyarrow
    if glibc_at_least "$glibc" 2.34; then
        facts+=("glibc $glibc")
        if [ "$OLDER_QT" = 1 ]; then
            notes+=("glibc: $glibc -- the interface uses PySide6 $(qt_version) (--older-qt)")
        fi
    elif glibc_at_least "$glibc" 2.28; then
        OLDER_QT=1
        facts+=("glibc $glibc")
        notes+=("glibc: $glibc -- the interface uses PySide6 $(qt_version) (the last release built for this glibc)")
    else
        problems+=("glibc: $glibc -- too old (2.28 or newer needed: PySide6, the viewer core, h5py and pyarrow packages are built for it)"); hard=1
    fi
    for tool in curl tar bzip2 sha256sum df flock; do
        command -v "$tool" >/dev/null || { problems+=("tool: $tool MISSING (install it with the system package manager)"); hard=1; }
    done
    # deepcell-toolbox (Mesmer) has no wheel: pip compiles it, so a C compiler
    # is needed even without --build-core (found by the clean-container test)
    if command -v cc >/dev/null || command -v gcc >/dev/null; then
        log "compiler: $(command -v cc || command -v gcc)"
    else
        problems+=("compiler: MISSING (one package is compiled during the install: install gcc;"
                   "  Ubuntu/Debian: sudo apt install build-essential, RHEL/Fedora: sudo dnf install gcc)")
        hard=1
    fi
    local libs l
    if libs=$(check_system_libs); then log "$libs"
    else while IFS= read -r l; do problems+=("$l"); done <<< "$libs"; hard=1; fi
    if command -v nvidia-smi >/dev/null; then
        local drv; drv=$(nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>/dev/null) || drv=""
        drv=${drv%%$'\n'*}
        if [ -n "$drv" ]; then
            facts+=("${drv#*, } (driver ${drv%%,*})")
            local major=${drv%%.*}
            if [ "${major:-0}" -lt 535 ] 2>/dev/null; then
                warnings+=("NVIDIA driver older than the verified 535: GPU engines are untested on it")
            fi
        else
            warnings+=("nvidia-smi present, no GPU answered: the engines may run on the CPU (slow)")
        fi
    else
        warnings+=("no nvidia-smi: segmentation engines and background correction will run on the CPU (slow)")
    fi
    CUDA_DIR=$(resolve_cuda)
    if [ -n "$CUDA_DIR" ]; then
        local cv; cv=$(grep -m1 -oE '"version" *: *"[0-9.]+"' "$CUDA_DIR/version.json" 2>/dev/null) || cv=""
        cv=$(printf '%s' "$cv" | grep -oE '[0-9][0-9.]*' || true)
        facts+=("CUDA ${cv:-(version unknown)}")
        log "CUDA: $CUDA_DIR ${cv:+(version $cv)} (what GPU background correction compiles against)"
        case "$cv" in 12.2*|"") ;; *) warnings+=("CUDA $cv: verified with CUDA 12.2 headers; others are checked by the GPU test of the self-test") ;; esac
    else
        warnings+=("no CUDA toolkit found (CUDA_PATH, nvcc, /usr/local/cuda): background correction will use the CPU")
    fi
    # only the work still to do: no environment space for a finished or
    # built one, no model space once cpsam (1.2 of the 1.4 GB) is in place
    local need_env=14 need_models=2 g
    case "$(prefix_state "$(env_prefix)")" in
        complete|built) need_env=1 ;;
        interrupted)    # its half-made environment is removed first: that space comes back
            local reclaim; reclaim=$(du -sk "$(env_prefix)" 2>/dev/null | awk '{print int($1 / 1048576)}')
            need_env=$(( need_env - ${reclaim:-0} )); [ "$need_env" -ge 1 ] || need_env=1 ;;
    esac
    if [ -f "$(model_dir cellpose_cpsam)/cpsam" ]; then need_models=0; fi
    # the SpatialData environment: 1.2 GB + caches (docs/v17_spatialdata_env_install_plan.md)
    local need_sd=0
    if [ "$NO_SPATIALDATA" = 0 ]; then
        case "$(prefix_state "$(sd_env_prefix)")" in
            absent) need_sd=2 ;;
            interrupted)
                local sd_reclaim; sd_reclaim=$(du -sk "$(sd_env_prefix)" 2>/dev/null | awk '{print int($1 / 1048576)}')
                need_sd=$(( 2 - ${sd_reclaim:-0} )); [ "$need_sd" -ge 0 ] || need_sd=0 ;;
        esac
    fi
    local need=$((need_env + need_models + need_sd))
    g=$(free_gb "$ROOT")
    if [ "$g" -ge "$need" ]; then
        facts+=("$g GB free (about $need GB needed)")
    else
        problems+=("space: $g GB free at $ROOT, about $need GB needed for what is left to do -- NOT ENOUGH"); hard=1
    fi
    local url
    if command -v curl >/dev/null; then
        for url in https://conda.anaconda.org https://pypi.org https://download.pytorch.org/whl/cu121/ \
                   https://github.com https://huggingface.co https://deepcell-data.s3-us-west-1.amazonaws.com; do
            if curl -sS -o /dev/null --connect-timeout 10 --max-time 20 -I "$url" 2>/dev/null; then
                log "network: $url ok"
            else
                warnings+=("cannot reach $url (a step that needs it will stop)")
            fi
        done
        if [ "$BUILD_CORE" = 1 ]; then
            if curl -sS -o /dev/null --connect-timeout 10 -I https://static.rust-lang.org 2>/dev/null; then
                log "network: https://static.rust-lang.org ok"
            else
                warnings+=("cannot reach static.rust-lang.org (--build-core needs it)")
            fi
        fi
    fi
    local joined="" f
    for f in "${facts[@]}"; do joined="${joined:+$joined$G_SEP}$f"; done
    if [ "$hard" = 0 ]; then
        step_done "$joined"
        STEP_LABEL=""
    else
        out "  ${C_BAD}${G_BAD} Computer${C_0}  ${C_DIM}$joined${C_0}"
    fi
    for f in "${problems[@]}"; do out "    ${C_BAD}$f${C_0}"; log "problem: $f"; done
    for f in "${notes[@]}"; do note "$f"; done
    for f in "${warnings[@]}"; do warn "$f"; done
    [ "$hard" = 0 ] || { STEP_LABEL=""; fail "this computer does not meet the requirements listed above." "fix the items marked above and run the installer again"; }
}

# ── 2. micromamba ─────────────────────────────────────────────────────────
MAMBA=""
ensure_micromamba() {
    STEP="2. micromamba (the package installer)"
    if command -v micromamba >/dev/null; then
        MAMBA=$(command -v micromamba); step_ok micromamba "found at $MAMBA"; return
    fi
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
# the 6.9.3 route hashes its Qt pins too and has a name of its own, so the
# 6.11.3 environment's id and name are what they were
lock_id() {
    if [ "$OLDER_QT" = 1 ]; then
        cat "$SPEC/conda-linux-64.lock" "$SPEC/requirements-pip.txt" "$OLDER_PINS" | sha256sum | cut -c1-8
    else
        cat "$SPEC/conda-linux-64.lock" "$SPEC/requirements-pip.txt" | sha256sum | cut -c1-8
    fi
}
env_prefix() {
    if [ "$OLDER_QT" = 1 ]; then echo "$ROOT/envs/fusion_mesmer-qt69-$(lock_id)"
    else echo "$ROOT/envs/fusion_mesmer-$(lock_id)"; fi
}
# the pip list this route installs from
pip_list() {
    if [ "$OLDER_QT" = 1 ]; then echo "$(env_prefix).requirements-pip.txt"
    else echo "$SPEC/requirements-pip.txt"; fi
}
# the PySide6 version this route installs
qt_version() {
    local f="$SPEC/requirements-pip.txt"; [ "$OLDER_QT" = 1 ] && f=$OLDER_PINS
    awk -F'==' 'tolower($1) == "pyside6-essentials" {print $2; exit}' "$f"
}
# write_older_pip_list OUT: the main pip list with its two Qt lines replaced
# by the older-Linux pins; stops when the main list no longer has exactly one
# line for each
write_older_pip_list() {
    local out=$1 name n
    for name in pyside6-essentials shiboken6; do
        n=$(grep -ciE "^${name}==" "$SPEC/requirements-pip.txt" || true)
        [ "$n" = 1 ] || fail "the pip list changed: $n lines for $name in envs/fusion_mesmer/requirements-pip.txt (exactly 1 expected)." \
                             "update envs/fusion_mesmer/requirements-pip-older-linux.txt to match, or report it"
    done
    {
        grep -viE '^(pyside6-essentials|shiboken6)==' "$SPEC/requirements-pip.txt"
        grep -vE '^[[:space:]]*(#|$)' "$OLDER_PINS"
    } > "$out"
}
sd_lock_id() { cat "$SD_SPEC/conda-linux-64.lock" "$SD_SPEC/requirements-pip.txt" | sha256sum | cut -c1-8; }
sd_env_prefix() { echo "$ROOT/envs/block01_spatialdata-$(sd_lock_id)"; }
core_version() { awk -F'"' '/^version = /{print $2; exit}' "$CARGO_TOML"; }

# prefix_state PREFIX -> absent | complete | built | interrupted | foreign
#   complete:    ours (owner file) and the whole install finished (ok marker)
#   built:       ours, the environment itself finished (env marker); a later
#                step stopped -- the environment is kept, the run continues
#   interrupted: ours, the environment never finished -- removed and redone
#   foreign:     not created by this installer -- never touched
prefix_state() {
    local p=$1
    if [ ! -e "$p" ]; then echo absent
    elif [ ! -f "$p.owner" ]; then echo foreign
    elif [ -f "$p/.block01_install_ok" ]; then echo complete
    elif [ -f "$p/.block01_env_ok" ]; then echo built
    else echo interrupted
    fi
}

# the installed core's "version api", or "" when it does not import
installed_core() {
    "$1/bin/python" -c 'import fusion_viewer_core as c; print(c.__version__, getattr(c, "CORE_API", "none"))' 2>/dev/null || true
}

ENV_PREFIX=""
PY=""
ensure_environment() {
    STEP="3. the Python environment"
    ENV_PREFIX=$(env_prefix)
    PY="$ENV_PREFIX/bin/python"
    local state; state=$(prefix_state "$ENV_PREFIX")
    log "environment: $ENV_PREFIX ($state)"
    case "$state" in
        complete)
            step_ok "Python environment" "already installed"
            return ;;
        built)
            step_ok "Python environment" "already built; continuing where the last run stopped"
            return ;;
        foreign)
            fail "$ENV_PREFIX exists but was not created by this installer; it is left untouched." \
                 "install into a new location: bash scripts/install_block01.sh --root NEW_DIR" ;;
        interrupted)
            note "an earlier install of the Python environment stopped half-way: it is made again"
            case "$ENV_PREFIX" in "$ROOT"/envs/fusion_mesmer-????????|"$ROOT"/envs/fusion_mesmer-qt69-????????) ;; *) fail "refusing to remove $ENV_PREFIX (unexpected path)" "" ;; esac
            rm -rf -- "$ENV_PREFIX" ;;
    esac
    mkdir -p "$ROOT/envs" "$ROOT/mamba"
    # ownership first, so an interruption at any later point is recognised
    printf 'created by %s\npid %s\ntime %s\ncheckout %s\n' "$0" "$$" "$(date -Is)" "$REPO" > "$ENV_PREFIX.owner"
    # the 6.9.3 route's pip list, kept beside the prefix for the record
    if [ "$OLDER_QT" = 1 ]; then write_older_pip_list "$(pip_list)"; fi
    step_begin "Python environment" "(10-30 min)"
    # micromamba gets a HOME of its own: it would add the prefix to
    # ~/.conda/environments.txt and make ~/.mamba; conda-forge only
    # (--override-channels: no default channel, no repo.anaconda.com)
    mkdir -p "$ROOT/mamba/home"
    HOME="$ROOT/mamba/home" MAMBA_ROOT_PREFIX="$ROOT/mamba" CONDA_PKGS_DIRS="$ROOT/mamba/pkgs" \
        run "$MAMBA" create -y -q -p "$ENV_PREFIX" --override-channels --file "$SPEC/conda-linux-64.lock"
    run "$PY" -m pip install -q --no-deps -r "$(pip_list)" --no-warn-script-location
    RUN_HINT="report the log: the pinned packages do not fit together on this machine" run "$PY" -m pip check
    date -Is > "$ENV_PREFIX/.block01_env_ok"
    step_done "$(fmt_secs $(( $(date +%s) - STEP_T0 ))) (PySide6 $(qt_version))"
}

# ── 3b. the SpatialData environment ───────────────────────────────────────
# Step4's SpatialData export runs in a Python of its own: spatialdata needs
# NumPy 2, the main environment stays on 1.26 for TensorFlow
# (docs/v17_spatialdata_env_install_plan.md). Same states as step 3.
SD_PREFIX=""
ensure_spatialdata_environment() {
    STEP="3b. the SpatialData environment"
    SD_PREFIX=""
    if [ "$NO_SPATIALDATA" = 1 ]; then
        step_skip "SpatialData environment" "skipped (--no-spatialdata): Step4 asks for a Python with spatialdata if its export is ticked"
        return
    fi
    local prefix; prefix=$(sd_env_prefix)
    local state done_as=""; state=$(prefix_state "$prefix")
    log "SpatialData environment: $prefix ($state)"
    case "$state" in
        complete|built)
            step_quiet "SpatialData environment"; done_as="already installed" ;;
        foreign)
            fail "$prefix exists but was not created by this installer; it is left untouched." \
                 "install into a new location (--root NEW_DIR), or skip it with --no-spatialdata" ;;
        absent|interrupted)
            if [ "$state" = interrupted ]; then
                note "an earlier install of the SpatialData environment stopped half-way: it is made again"
                case "$prefix" in "$ROOT"/envs/block01_spatialdata-????????) ;; *) fail "refusing to remove $prefix (unexpected path)" "" ;; esac
                rm -rf -- "$prefix"
            fi
            mkdir -p "$ROOT/envs" "$ROOT/mamba/home"
            printf 'created by %s\npid %s\ntime %s\ncheckout %s\n' "$0" "$$" "$(date -Is)" "$REPO" > "$prefix.owner"
            step_begin "SpatialData environment" "(for Step4's export; 2-5 min)"
            HOME="$ROOT/mamba/home" MAMBA_ROOT_PREFIX="$ROOT/mamba" CONDA_PKGS_DIRS="$ROOT/mamba/pkgs" \
                run "$MAMBA" create -y -q -p "$prefix" --override-channels --file "$SD_SPEC/conda-linux-64.lock"
            run "$prefix/bin/python" -m pip install -q --no-deps -r "$SD_SPEC/requirements-pip.txt" --no-warn-script-location
            RUN_HINT="report the log: the pinned SpatialData packages do not fit together on this machine" \
                run "$prefix/bin/python" -m pip check
            date -Is > "$prefix/.block01_env_ok"
            done_as=$(fmt_secs $(( $(date +%s) - STEP_T0 ))) ;;
    esac
    local version
    version=$(cd "$STATE_DIR/run" 2>/dev/null || cd /; "$prefix/bin/python" -I -c 'import spatialdata; print(spatialdata.__version__)' 2>"$STATE_DIR/run/spatialdata.err" | tail -n1) \
        || fail "spatialdata does not import in $prefix (its error: $STATE_DIR/run/spatialdata.err)." \
                "run the installer again; if it repeats, report that file (or install with --no-spatialdata)"
    step_done "$done_as, spatialdata $version ready"
    date -Is > "$prefix/.block01_install_ok"
    SD_PREFIX="$prefix"
}

# ── 4. the viewer core ────────────────────────────────────────────────────
# wheels_lock_entry VERSION PLATFORM -> "file sha256" or ""
wheels_lock_entry() {
    [ -f "$WHEELS_LOCK" ] || return 0
    awk -v v="$1" -v p="$2" '$1 == v && $2 == p {print $3, $4; exit}' "$WHEELS_LOCK"
}

build_core_wheel() {
    local out=$1
    local rhome="$ROOT/rust" venv="$ROOT/build-venv"
    export RUSTUP_HOME="$rhome/rustup" CARGO_HOME="$rhome/cargo"
    if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
        note "installing the Rust toolchain $RUST_TOOLCHAIN under $rhome (your own Rust setup is not changed)"
        download "$RUSTUP_URL" "$rhome/rustup-init" "$RUSTUP_SHA256" rustup
        chmod +x "$rhome/rustup-init"
        run "$rhome/rustup-init" -y -q --no-modify-path --default-toolchain none --profile minimal
    fi
    run "$CARGO_HOME/bin/rustup" toolchain install "$RUST_TOOLCHAIN" --profile minimal
    [ -x "$venv/bin/maturin" ] || { run "$PY" -m venv "$venv"; run "$venv/bin/python" -m pip install -q "$MATURIN_PIN" --no-warn-script-location; }
    note "building the viewer core from this checkout (about 2 min)"
    PATH="$CARGO_HOME/bin:$PATH" RUSTUP_TOOLCHAIN="$RUST_TOOLCHAIN" run "$venv/bin/maturin" build --release --locked \
        -i "$PY" -m "$REPO/native/fusion_viewer_core/Cargo.toml" -o "$out"
}

ensure_core() {
    STEP="4. the viewer core (fusion_viewer_core)"
    local want; want=$(core_version)
    local have; have=$(installed_core "$ENV_PREFIX")
    if [ "$have" = "$want 1" ]; then
        step_ok "Viewer core $want" "already installed"; return
    fi
    step_begin "Viewer core $want"
    local dist="$ROOT/downloads/core-$want"
    local entry; entry=$(wheels_lock_entry "$want" linux)
    local wheel
    if [ -n "$entry" ]; then
        local file=${entry% *} sha=${entry#* }
        wheel="$dist/$file"
        if [ ! -f "$wheel" ] || [ "$(sha256_of "$wheel")" != "$sha" ]; then
            download "$RELEASE_URL/core-v$want/$file" "$wheel" "$sha" "Viewer core $want"
        fi
    elif [ "$BUILD_CORE" = 1 ]; then
        build_core_wheel "$dist"
        wheel=$(find "$dist" -maxdepth 1 -name "fusion_viewer_core-$want-*.whl" -print -quit)
        [ -n "$wheel" ] || fail "the build made no wheel" ""
    else
        fail "there is no prebuilt viewer core for version $want (a development checkout)." \
             "run again with --build-core to build it here (needs a C compiler and about 2 minutes)"
    fi
    run "$PY" -m pip install -q --no-deps --force-reinstall "$wheel" --no-warn-script-location
    have=$(installed_core "$ENV_PREFIX")
    [ "$have" = "$want 1" ] || fail "the installed viewer core reports '$have', expected '$want 1'." ""
    step_done
}

# ── 5. models ─────────────────────────────────────────────────────────────
model_dir() { echo "$ROOT/models/$1"; }

# model_state ENTRY DIR -> ok | missing | partial | different
#   (partial = some files there, others missing; different = a file is
#    there with other bytes: never overwritten)
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

# ── the model licences: one card, one answer, before anything is installed
# (user ruling 2026-11-15) ──
declare -A MODEL_NAME=(
    [cellpose_cpsam]="Cellpose-SAM"
    [stardist_2D_versatile_fluo]="StarDist"
    [mesmer_multiplex_segmentation]="Mesmer"
)
# licence_row KEY -> "size|licence|its pages"
licence_row() {
    case "$1" in
        cellpose_cpsam) echo "1.2 GB|BSD-3-Clause (trained on CC-BY-NC data)|https://huggingface.co/mouseland/cellpose-sam https://github.com/MouseLand/cellpose" ;;
        stardist_2D_versatile_fluo) echo "5 MB|BSD-3-Clause|https://github.com/stardist/stardist" ;;
        mesmer_multiplex_segmentation) echo "100 MB|NON-COMMERCIAL academic use only|https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE" ;;
    esac
}
CARD_W=70
card_rule() {   # card_rule LEFT RIGHT [TITLE]
    local title=${3:-} line="" i n
    n=$((CARD_W + 2 - ${#title}))
    for ((i = 0; i < n; i++)); do line+="$B_H"; done
    out "$1$title$line$2"
}
card_row() {    # card_row TEXT [COLOUR]
    local text
    text=$(printf '%-*s' "$CARD_W" "$1")
    out "$B_V ${2:-}$text${2:+$C_0} $B_V"
}
# licence_card KEY...: what is installed and under which licence
licence_card() {
    local key size lic urls u
    flush
    card_rule "$B_TL" "$B_TR" " Model licences "
    for key in "$@"; do
        IFS='|' read -r size lic urls <<< "$(licence_row "$key")"
        if [ "$key" = mesmer_multiplex_segmentation ]; then
            card_row "$(printf '%-14s %-7s %s' "${MODEL_NAME[$key]}" "$size" "$lic")" "$C_WARN"
            card_row "  its PyTorch copy, made here, keeps this licence: never share it" "$C_WARN"
        else
            card_row "$(printf '%-14s %-7s %s' "${MODEL_NAME[$key]}" "$size" "$lic")"
        fi
    done
    card_row "Details:" "$C_DIM"
    for key in "$@"; do
        IFS='|' read -r size lic urls <<< "$(licence_row "$key")"
        for u in $urls; do card_row "  $u" "$C_DIM"; done
    done
    card_rule "$B_BL" "$B_BR"
}

LICENCES_ACCEPTED=" "
# confirm_licences KEY...: the card, then one answer for all of them
confirm_licences() {
    [ $# -gt 0 ] || return 0
    local step_was=$STEP label_was=$STEP_LABEL t0_was=$STEP_T0
    STEP="the model licences"
    licence_card "$@"
    if [ "$ACCEPT_LICENCES" = 1 ]; then
        step_ok "Model licences" "accepted (--accept-model-licences)"
    else
        STEP_LABEL="Model licences"
        if ! { : < /dev/tty; } 2>/dev/null; then              # no controlling terminal
            fail "a model licence needs your answer, and there is no terminal to ask." \
                 "run the installer in a terminal, or read the licences above and add --accept-model-licences"
        fi
        local answer what="all and install them"
        [ $# -gt 1 ] || what="and install it"
        printf '%sAccept %s? [y/N] %s' "$C_B" "$what" "$C_0" > /dev/tty
        read -r answer < /dev/tty || answer=""
        log "licences ($*): answered '$answer'"
        case "$answer" in
            y|Y|yes|Yes|YES) step_ok "Model licences" "accepted" ;;
            *) fail "the licences were not accepted, so no model was installed." \
                    "run the installer again and answer y, or put your own copies in a folder and pass --models FOLDER" ;;
        esac
    fi
    LICENCES_ACCEPTED+="$* "
    STEP=$step_was; STEP_LABEL=$label_was; STEP_T0=$t0_was
}
# accept_licence KEY: already accepted on the card, else asked now (a model
# the card could not foresee)
accept_licence() {
    case "$LICENCES_ACCEPTED" in *" $1 "*) return 0 ;; esac
    confirm_licences "$1"
}

# manifest_paths KEY: the files models.json lists for KEY, read without
# Python (the card comes before the environment exists)
manifest_paths() {
    awk -v k="$1" '
        /^    "[^"]+": *\{/ { match($0, /"[^"]+"/); cur = substr($0, RSTART + 1, RLENGTH - 2) }
        cur == k && /"path":/ { sub(/.*"path": *"/, ""); sub(/".*/, ""); print }' "$MANIFEST"
}
# models_to_license: the models this run will install (a file missing),
# except those copied from --models (no licence was asked for them before)
models_to_license() {
    local key f paths missing given
    for key in "${ORIGINAL_MODELS[@]}"; do
        paths=$(manifest_paths "$key")
        missing=0; given=1
        [ -n "$paths" ] || missing=1
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            [ -f "$(model_dir "$key")/$f" ] || missing=1
            { [ -n "$MODELS_IN" ] && [ -f "$MODELS_IN/$key/$f" ]; } || given=0
        done <<< "$paths"
        [ "$missing" = 1 ] || continue
        if [ "$key" != mesmer_multiplex_segmentation ] && [ -n "$MODELS_IN" ] && [ "$given" = 1 ]; then continue; fi
        echo "$key"
    done
}

# the files manifest entry KEY lists, one relative path per line
manifest_files() {
    "$PY" -c 'import json, sys; [print(f["path"]) for f in json.load(open(sys.argv[1]))["models"][sys.argv[2]]["files"]]' "$MANIFEST" "$1"
}

# staging dir -> final dir, after verification: ONLY the manifest's files
# are moved, each to a path that does not exist yet (model_state said
# "missing"), so nothing already in DIR is replaced; the stage is removed
verify_into_place() {
    local key=$1 stage=$2 dir=$3 f
    local st; st=$(model_state "$key" "$stage")
    [ "$st" = ok ] || { rm -rf -- "$stage"; fail "the $key files do not match models.json ($st)." "try again later or report it"; }
    while IFS= read -r f; do
        [ ! -e "$dir/$f" ] || { rm -rf -- "$stage"; fail "$dir/$f appeared during the install; it is left untouched." "run the installer again"; }
        mkdir -p "$(dirname "$dir/$f")"
        mv -- "$stage/$f" "$dir/$f"
    done < <(manifest_files "$key")
    rm -rf -- "$stage"
}

# try_download URL DEST: like `download` without a digest, but a failure is
# returned, not fatal (the Mesmer sources are tried one after another)
try_download() {
    local url=$1 dest=$2 name=${3:-download} tmp="$2.part"
    mkdir -p "$(dirname "$dest")"
    rm -f -- "$tmp"
    if fetch "$url" "$tmp" "$name"; then
        mv -f -- "$tmp" "$dest"; return 0
    fi
    rm -f -- "$tmp"; return 1
}

# mesmer_from_archive ARCHIVE STAGE: unpack ARCHIVE (.tar.gz or .zip) and put
# the CONTENTS of its MultiplexSegmentation/ folder into STAGE; 1 if it has none
mesmer_from_archive() {
    local archive=$1 stage=$2
    mkdir -p "$stage/x"
    case "$archive" in
        *.zip) "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$archive" "$stage/x" || return 1 ;;
        *)     tar -xzf "$archive" -C "$stage/x" || return 1 ;;
    esac
    local src; src=$(find "$stage/x" -type d -name MultiplexSegmentation | head -n1)
    [ -n "$src" ] || return 1
    cp -a "$src"/. "$stage"/
    rm -rf -- "$stage/x"
}

# ensure_mesmer DIR: DeepCell's archive, the mirror, the local archive, the
# --models folder -- the first that yields the four files of models.json is
# moved into DIR; none: MESMER_MISSING=1, said, and the install goes on
ensure_mesmer() {
    local key=mesmer_multiplex_segmentation dir=$1 stage="$1.stage.$$" src from
    for src in official mirror zip folder; do
        rm -rf -- "$stage"; mkdir -p "$stage"
        case "$src" in
            official)
                from="DeepCell's archive"
                log "Mesmer: trying DeepCell's public archive $MESMER_URL"
                try_download "$MESMER_URL" "$stage/m.tar.gz" Mesmer || { note "Mesmer: $from not available"; continue; }
                [ "$(sha256_of "$stage/m.tar.gz")" = "$MESMER_SHA256" ] || { note "Mesmer: $from is not the expected file"; continue; }
                mesmer_from_archive "$stage/m.tar.gz" "$stage" >> "$LOG" 2>&1 || continue
                rm -f -- "$stage/m.tar.gz" ;;
            mirror)
                [ -n "$MESMER_MIRROR" ] || continue
                from="the mirror"
                log "Mesmer: trying the mirror $MESMER_MIRROR"
                try_download "$MESMER_MIRROR" "$stage/m.tar.gz" Mesmer || { note "Mesmer: $from not available"; continue; }
                mesmer_from_archive "$stage/m.tar.gz" "$stage" >> "$LOG" 2>&1 || { note "Mesmer: no MultiplexSegmentation folder in $from"; continue; }
                rm -f -- "$stage/m.tar.gz" ;;
            zip)
                [ -n "$MESMER_ZIP" ] || continue
                from="the archive $MESMER_ZIP"
                [ -f "$MESMER_ZIP" ] || { note "Mesmer: $from: no such file"; continue; }
                mesmer_from_archive "$MESMER_ZIP" "$stage" >> "$LOG" 2>&1 || { note "Mesmer: no MultiplexSegmentation folder in $from"; continue; } ;;
            folder)
                [ -n "$MODELS_IN" ] || continue
                [ "$(model_state "$key" "$MODELS_IN/$key")" = ok ] || continue
                from="the verified copy in $MODELS_IN/$key"
                local f
                while IFS= read -r f; do
                    mkdir -p "$(dirname "$stage/$f")"
                    cp -L -- "$MODELS_IN/$key/$f" "$stage/$f"
                done < <(manifest_files "$key") ;;
        esac
        if [ "$(model_state "$key" "$stage")" = ok ]; then
            verify_into_place "$key" "$stage" "$dir"
            sub_ok Mesmer "installed and verified (from $from)"
            return 0
        fi
        note "Mesmer: the files from $from do not match models.json; not used"
    done
    rm -rf -- "$stage"
    MESMER_MISSING=1
    warn "Mesmer is NOT installed: no source worked. Everything else is. To add it later:"
    warn "  bash $REPO/scripts/install_block01.sh --mesmer-only --mesmer-zip FILE   (or --mesmer-url URL, --models DIR)"
}

fetch_model() {
    local key=$1 dir=$2
    local stage="$dir.stage.$$"
    rm -rf -- "$stage"; mkdir -p "$stage"
    case "$key" in
        cellpose_cpsam)
            local sha size; read -r sha size < <("$PY" -c "import json,sys; f=json.load(open(sys.argv[1]))['models']['cellpose_cpsam']['files'][0]; print(f['sha256'], f['size'])" "$MANIFEST")
            download "$CPSAM_URL" "$stage/cpsam" "$sha" Cellpose-SAM "$size" ;;
        stardist_2D_versatile_fluo)
            local zsha zsize; read -r zsha zsize < <("$PY" -c "import json,sys; e=json.load(open(sys.argv[1]))['models']['stardist_2D_versatile_fluo']; f=[f for f in e['files'] if f['path'].endswith('.zip')][0]; print(f['sha256'], f['size'])" "$MANIFEST")
            download "$STARDIST_URL" "$stage/2D_versatile_fluo.zip" "$zsha" StarDist "$zsize"
            run "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$stage/2D_versatile_fluo.zip" "$stage" ;;
    esac
    verify_into_place "$key" "$stage" "$dir"
}

ensure_models() {
    STEP="5. the segmentation models"
    local key dir st began=0
    for key in "${ORIGINAL_MODELS[@]}"; do
        dir=$(model_dir "$key")
        st=$(model_state "$key" "$dir")
        log "model $key: $st"
        [ "$st" = ok ] || [ "$began" = 1 ] || { step_begin Models; began=1; }
        case "$st" in
            ok) continue ;;
            different|partial)
                fail "model $key in $dir does not match models.json ($st); it is left untouched." \
                     "move that folder away (or use another --root) and run the installer again" ;;
        esac
        if [ "$key" = mesmer_multiplex_segmentation ]; then
            accept_licence "$key"
            ensure_mesmer "$dir"
            continue
        fi
        if [ -n "$MODELS_IN" ] && [ "$(model_state "$key" "$MODELS_IN/$key")" = ok ]; then
            local stage="$dir.stage.$$" f
            rm -rf -- "$stage"; mkdir -p "$stage"
            # the listed files only, as real files (-L: a link in the given
            # folder must not leave the install depending on that folder)
            while IFS= read -r f; do
                mkdir -p "$(dirname "$stage/$f")"
                cp -L -- "$MODELS_IN/$key/$f" "$stage/$f"
            done < <(manifest_files "$key")
            verify_into_place "$key" "$stage" "$dir"
            sub_ok "${MODEL_NAME[$key]}" "copied from the verified copy in $MODELS_IN/$key"
            continue
        fi
        accept_licence "$key"
        fetch_model "$key" "$dir"
        sub_ok "${MODEL_NAME[$key]}" "downloaded and verified"
    done
    if [ "$began" = 1 ]; then step_done
    else step_ok Models "already installed"; fi
}

# ── 6. alias, self-test ───────────────────────────────────────────────────
STATE_DIR="$REPO/.block01"

ensure_alias() {
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
    fi
}

# the variables every later step and the launcher run with
app_env() {
    export KERAS_BACKEND=tensorflow
    export KERAS_HOME="$ROOT/keras"
    # the runtime caches go under the root too, not into HOME
    export CUPY_CACHE_DIR="$ROOT/cache/cupy"         # CuPy's compiled kernels (~/.cupy)
    export CUDA_CACHE_PATH="$ROOT/cache/nv"          # the CUDA driver's JIT cache (~/.nv)
    export MPLCONFIGDIR="$ROOT/cache/matplotlib"     # matplotlib's config + font cache
    local key
    for key in "${!MODEL_VAR[@]}"; do export "${MODEL_VAR[$key]}=$(model_dir "$key")"; done
    if [ -n "$CUDA_DIR" ]; then export CUDA_PATH="$CUDA_DIR"; fi
    mkdir -p "$KERAS_HOME"
}

# convert_stardist_weights: the StarDist engine loads a torch conversion of
# weights_best.h5 (docs/stardist_torch_plan.md §4.5) and converts nothing
# itself. Make it here: after the original models are verified (step 5) and
# BEFORE the self-test (step 6) starts any engine -- the manifest check inside
# the self-test would otherwise fail for a missing file on a first install.
convert_stardist_weights() {
    STEP="5b. the StarDist torch weights"
    app_env                                   # both directories come from MODEL_VAR
    step_quiet "StarDist weights for PyTorch"         # a few seconds; its output to the log
    (cd "$REPO" && run env PYTHONPATH="$REPO" "$PY" "$REPO/scripts/convert_stardist_to_torch.py")
}

# check_selftest [ENGINE...]: seg_runner.selftest's lines on stdin -> one
# line per engine, "engine<TAB>ok<TAB>device" or "engine<TAB>FAILED<TAB>";
# 1 unless every engine is ok, exit 0 and names its device
check_selftest() {
    "$PY" -c '
import json, sys
want = sys.argv[1:] or ["cellpose", "stardist", "mesmer"]
got = {}
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    r = json.loads(line)
    ok = r.get("state") == "ok" and r.get("exit_code") == 0 and bool(r.get("device"))
    got[r.get("engine")] = ("ok", r["device"]) if ok else ("FAILED", "")
bad = 0
for e in want:
    state, device = got.get(e, ("FAILED", ""))
    bad += state != "ok"
    print("%s\t%s\t%s" % (e, state, device))
sys.exit(1 if bad else 0)
' "$@"
}

# the engines whose models are installed (Mesmer may be missing, 2026-11-15)
installed_engines() {
    if [ "$MESMER_MISSING" = 1 ]; then echo "cellpose stardist"; else echo "cellpose stardist mesmer"; fi
}

# selftest_table ROWS: "  Self-test   GPU" and a line per engine
selftest_table() {
    local rows=$1 e st col="CPU" name
    case "$rows" in *$'\t'ok$'\t'cuda*|*$'\t'ok$'\t'gpu*) col="GPU" ;; esac
    if [ "$PENDING" = 1 ]; then printf '\r\033[K'; PENDING=0; fi    # the "• Self-test ..." line
    printf '  %-14s %s\n' "Self-test" "$col"
    while IFS=$'\t' read -r e st _; do
        [ -n "$e" ] || continue
        case "$e" in cellpose) name=Cellpose ;; stardist) name=StarDist ;; mesmer) name=Mesmer ;; *) name=$e ;; esac
        if [ "$st" = ok ]; then printf '  %-14s %s%s%s\n' "$name" "$C_OK" ok "$C_0"
        else printf '  %-14s %s%s%s\n' "$name" "$C_BAD" FAILED "$C_0"; fi
    done <<< "$rows"
}

# run_selftest ENGINE...: one small task per engine; the table; stops on a failure
run_selftest() {
    local out rows rc=0
    out=$(cd "$STATE_DIR/run" && PYTHONPATH="$REPO" "$PY" -m seg_runner.selftest "$@" 2>"$STATE_DIR/run/selftest.err") || true
    log "$out"
    rows=$(printf '%s\n' "$out" | check_selftest "$@") || rc=$?
    log "$rows"
    selftest_table "$rows"
    STEP_LABEL=""
    [ "$rc" = 0 ] || fail "an engine failed its self-test (its error output: $STATE_DIR/run/selftest.err)." \
        "read the error, fix the cause (often the GPU driver or CUDA) and run the installer again"
}

selftest() {
    STEP="6. self-test"
    app_env
    step_begin Self-test "(about a minute)"
    # shellcheck disable=SC2046  # the engine names are words
    run_selftest $(installed_engines)
    step_quiet "Model files"
    (cd "$STATE_DIR/run" && run "$PY" "$REPO/scripts/model_manifest.py" --verify --installed)
    local gpu
    gpu=$(cd "$STATE_DIR/run" && PYTHONPATH="$STATE_DIR/pkg" "$PY" -c \
        'from block01.core import bg_correction as b; print("ok" if b.GPU_MORPH_AVAILABLE else "cpu", b.GPU_MORPH_SMOKE_TEST)' 2>"$STATE_DIR/run/gpucheck.err" | tail -n1 || echo "cpu (check failed: $STATE_DIR/run/gpucheck.err)")
    log "GPU background correction: $gpu"
    case "$gpu" in ok*) step_ok "GPU background correction" "ok" ;;
                   *) warn "GPU background correction unavailable, the CPU is used ($gpu)" ;; esac
    step_quiet "App starts"
    local core
    core=$(cd "$STATE_DIR/run" && QT_QPA_PLATFORM=offscreen PYTHONPATH="$STATE_DIR/pkg" "$PY" -c '
from PySide6.QtWidgets import QApplication
app = QApplication([])
import block01.main  # noqa: F401 -- the app imports
from block01.ui.step1_core_adapter import core_unavailable_reason
import fusion_viewer_core as c
why = core_unavailable_reason()
print(f"fusion_viewer_core {c.__version__} API {c.CORE_API} accepted" if not why else "REFUSED: " + why)
' 2>"$STATE_DIR/run/startcheck.err" | tail -n1 || true)
    log "app start check: $core"
    case "$core" in
        *accepted) step_done "viewer core $(core_version) accepted" ;;
        *) tail -n 15 "$STATE_DIR/run/startcheck.err" >> "$LOG" 2>/dev/null || true
           fail "the app does not start or refuses its viewer core: ${core:-no answer} (all of its error output: $STATE_DIR/run/startcheck.err)." \
                "a missing system library is named there: install it with the system package manager and run the installer again" ;;
    esac
}

# ── 7. the record for this checkout ───────────────────────────────────────
write_record() {
    STEP="7. the install record"
    step_quiet "Install record"
    local rec="$STATE_DIR/install.env" key
    {
        echo "# written by scripts/install_block01.sh $(date -Is); read by scripts/block01"
        printf 'BLOCK01_CHECKOUT=%q\n' "$REPO"
        printf 'BLOCK01_ROOT=%q\n' "$ROOT"
        printf 'BLOCK01_PREFIX=%q\n' "$ENV_PREFIX"
        printf 'BLOCK01_CORE_VERSION=%q\n' "$(core_version)"
        printf 'BLOCK01_QT_VERSION=%q\n' "$(qt_version)"
        printf 'export KERAS_BACKEND=tensorflow\n'
        printf 'export KERAS_HOME=%q\n' "$ROOT/keras"
        printf 'export CUPY_CACHE_DIR=%q\n' "$ROOT/cache/cupy"
        printf 'export CUDA_CACHE_PATH=%q\n' "$ROOT/cache/nv"
        printf 'export MPLCONFIGDIR=%q\n' "$ROOT/cache/matplotlib"
        for key in "${!MODEL_VAR[@]}"; do printf 'export %s=%q\n' "${MODEL_VAR[$key]}" "$(model_dir "$key")"; done
        [ -n "$CUDA_DIR" ] && printf 'export CUDA_PATH=%q\n' "$CUDA_DIR"
        if [ -n "$SD_PREFIX" ]; then printf 'export BLOCK01_SPATIALDATA_PYTHON=%q\n' "$SD_PREFIX/bin/python"; fi
    } > "$rec.tmp"
    mv -f "$rec.tmp" "$rec"
    printf 'core %s\ntime %s\n' "$(core_version)" "$(date -Is)" > "$ENV_PREFIX/.block01_install_ok"
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
    # the same lock as a full install into this root: both write its models
    # (codex review of the Mac installer, 2026-11-16)
    exec 9>"$ROOT/.install.lock"
    flock -n 9 || fail "another install into $ROOT is running." "wait for it to finish, then run this again"
    local key=mesmer_multiplex_segmentation dir; dir=$(model_dir "$key")
    case "$(model_state "$key" "$dir")" in
        ok) step_ok Mesmer "already installed and verified" ;;
        missing)
            accept_licence "$key"
            ensure_mesmer "$dir"
            [ "$MESMER_MISSING" = 0 ] || fail "no Mesmer source worked (messages above)." \
                "give a file with --mesmer-zip FILE, a link with --mesmer-url URL, or a folder with --models DIR" ;;
        *) fail "$dir holds files that do not match models.json; they are left untouched." "move that folder away and run this again" ;;
    esac
    STEP="the Mesmer self-test"
    app_env
    step_begin Self-test
    run_selftest mesmer
    step_quiet "Model files"
    (cd "$STATE_DIR/run" && run "$PY" "$REPO/scripts/model_manifest.py" --verify --installed)
    out ""
    out "${C_B}Mesmer added.${C_0} Start FusionFlux as before: $REPO/scripts/block01"
    out "${C_DIM}Log: $LOG${C_0}"
}

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --root) ROOT=$(realpath -m "$2"); shift 2 ;;
            --check-only) CHECK_ONLY=1; shift ;;
            --build-core) BUILD_CORE=1; shift ;;
            --models) MODELS_IN=$(realpath -m "$2"); shift 2 ;;
            --accept-model-licences) ACCEPT_LICENCES=1; shift ;;
            --mesmer-url) MESMER_MIRROR=$2; shift 2 ;;
            --mesmer-zip) MESMER_ZIP=$(realpath -m "$2"); shift 2 ;;
            --mesmer-only) MESMER_ONLY=1; shift ;;
            --no-spatialdata) NO_SPATIALDATA=1; shift ;;
            --older-qt) OLDER_QT=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 2 ;;
        esac
    done
    local t0; t0=$(date +%s)
    # the log: every tool's own output (nothing is written by --check-only)
    if [ "$CHECK_ONLY" = 0 ]; then
        mkdir -p "$STATE_DIR/logs"
        LOG="$STATE_DIR/logs/install-$(date +%Y%m%d-%H%M%S).log"
        { echo "# scripts/install_block01.sh $*"; echo "# $(date -Is) on $(uname -srm)"; } > "$LOG"
    fi
    out "${C_B}FusionFlux installer${C_0}"
    out "${C_DIM}  checkout   $REPO${C_0}"
    [ "$MESMER_ONLY" = 1 ] || out "${C_DIM}  install to $ROOT${C_0}"
    out ""
    if [ "$MESMER_ONLY" = 1 ]; then mesmer_only; exit 0; fi
    preflight
    [ "$CHECK_ONLY" = 0 ] || { out "check only: nothing was written"; exit 0; }
    # every model licence, answered once, before anything is installed
    # shellcheck disable=SC2046  # model keys are words
    confirm_licences $(models_to_license)
    RESUMABLE=1
    STEP="2. starting"
    mkdir -p "$ROOT"
    # one install into a root at a time: a second one would take the first
    # one's unfinished environment for an interrupted one and remove it
    exec 9>"$ROOT/.install.lock"
    flock -n 9 || fail "another install into $ROOT is running." "wait for it to finish, then run the installer again"
    export PIP_CACHE_DIR="$ROOT/cache/pip"           # pip's downloads, not ~/.cache/pip
    ensure_micromamba
    ensure_environment
    mkdir -p "$STATE_DIR/run"
    ensure_spatialdata_environment
    ensure_core
    ensure_models
    ensure_alias
    convert_stardist_weights
    selftest
    write_record
    out ""
    out "${C_B}Done in $(fmt_secs $(( $(date +%s) - t0 ))).${C_0} Start FusionFlux with: $REPO/scripts/block01"
    out "${C_DIM}Log: $LOG${C_0}"
    if [ "$MESMER_MISSING" = 1 ]; then
        warn "Mesmer is not installed (its three methods are unavailable). Add it later with:"
        warn "   bash $REPO/scripts/install_block01.sh --mesmer-only --mesmer-zip FILE"
    fi
}

if [ -z "${BLOCK01_INSTALL_SOURCED:-}" ]; then
    main "$@"
fi
