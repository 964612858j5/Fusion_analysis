#!/usr/bin/env bash
# Install Block01 with one command (docs/v17_install_plan.md §2).
#
#   bash scripts/install_block01.sh [--root DIR] [--check-only] [--build-core]
#                                   [--models DIR] [--accept-model-licences]
#                                   [--no-spatialdata]
#
# Linux x86_64 + NVIDIA. Everything goes under the install root (default
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
DEEPCELL_API="https://users.deepcell.org/api/getData/"
DEEPCELL_KEY="models/MultiplexSegmentation-9.tar.gz"

# environment variable each original model entry is found through (§2.5)
declare -A MODEL_VAR=(
    [cellpose_cpsam]=CELLPOSE_LOCAL_MODELS_PATH
    [stardist_2D_versatile_fluo]=BLOCK01_STARDIST_MODEL_PATH
    [mesmer_multiplex_segmentation]=DEEPCELL_MESMER_MODEL_PATH
    [mesmer_multiplex_segmentation_torch]=BLOCK01_MESMER_TORCH_PATH
)
ORIGINAL_MODELS=(cellpose_cpsam stardist_2D_versatile_fluo mesmer_multiplex_segmentation)

ROOT="${HOME}/.local/share/block01"
CHECK_ONLY=0
BUILD_CORE=0
NO_SPATIALDATA=0
MODELS_IN=""
ACCEPT_LICENCES=0
STEP="start"
CUDA_DIR=""

# ── output ────────────────────────────────────────────────────────────────
say()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; }
# fail STEP-TEXT HINT [COMMAND]: the step, what to do, the failing command
fail() {
    printf '\n[install] STOPPED at: %s\n' "$STEP" >&2
    [ -n "${3:-}" ] && printf '[install] failing command: %s\n' "$3" >&2
    printf '[install] %s\n' "$1" >&2
    [ -n "${2:-}" ] && printf '[install] what to do: %s\n' "$2" >&2
    exit 1
}
run() {  # run CMD... ; on failure stop with the command line
    "$@" || fail "the command above failed (exit $?)." "${RUN_HINT:-read the messages above; fix the cause and run the installer again}" "$*"
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# download URL DEST SHA256 [extra curl args...]: to a temp name beside DEST,
# verified, then moved into place; nothing is left on failure
download() {
    local url=$1 dest=$2 want=$3; shift 3
    local tmp="$dest.part.$$"
    mkdir -p "$(dirname "$dest")"
    rm -f -- "$dest".part.*                    # left by a run that was killed
    if ! curl -fL --retry 3 --connect-timeout 20 -sS -o "$tmp" "$@" "$url"; then
        rm -f -- "$tmp"
        fail "could not download $url." "check the network connection (a proxy needs https_proxy set) and run the installer again" "curl $url"
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

# the system libraries pip's Qt 5 needs for its window (xcb) and offscreen
# platforms, with the Debian/Ubuntu package that has each (found by the
# clean ubuntu:22.04 test: a bare system has none of them)
QT_SYSTEM_LIBS="libGL.so.1:libgl1 libEGL.so.1:libegl1 libX11-xcb.so.1:libx11-xcb1 libXext.so.6:libxext6
libdbus-1.so.3:libdbus-1-3 libglib-2.0.so.0:libglib2.0-0 libgthread-2.0.so.0:libglib2.0-0
libfontconfig.so.1:libfontconfig1 libxkbcommon.so.0:libxkbcommon0 libxkbcommon-x11.so.0:libxkbcommon-x11-0
libxcb-icccm.so.4:libxcb-icccm4 libxcb-image.so.0:libxcb-image0 libxcb-keysyms.so.1:libxcb-keysyms1
libxcb-randr.so.0:libxcb-randr0 libxcb-render-util.so.0:libxcb-render-util0 libxcb-render.so.0:libxcb-render0
libxcb-shape.so.0:libxcb-shape0 libxcb-shm.so.0:libxcb-shm0 libxcb-sync.so.1:libxcb-sync1
libxcb-xfixes.so.0:libxcb-xfixes0 libxcb-xinerama.so.0:libxcb-xinerama0 libxcb-xkb.so.1:libxcb-xkb1"

check_system_libs() {
    local have missing="" pkgs="" pair lib pkg
    have=$(ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null || true)
    for pair in $QT_SYSTEM_LIBS; do
        lib=${pair%%:*}; pkg=${pair#*:}
        case "$have" in *"$lib "*) ;; *) missing="$missing $lib"; case " $pkgs " in *" $pkg "*) ;; *) pkgs="$pkgs $pkg" ;; esac ;; esac
    done
    if [ -z "$missing" ]; then
        say "  Qt libs:  present"; return 0
    fi
    say "  Qt libs:  MISSING$missing"
    say "            Ubuntu/Debian: sudo apt install$pkgs"
    say "            (other systems: the packages that provide these files)"
    return 1
}

preflight() {
    STEP="1. checking this computer"
    local hard=0
    say "== 1. Checking this computer (nothing is written) =="
    if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
        say "  system:   $(uname -s) $(uname -m) -- NOT SUPPORTED (Linux x86_64 only)"; hard=1
    else
        say "  system:   Linux x86_64"
    fi
    # (no `cmd | head` here: under pipefail the writer's SIGPIPE fails the pipe)
    local glibc; glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null) || glibc="glibc 0"
    glibc=${glibc##* }
    if [ "$(printf '%s\n2.28\n' "$glibc" | sort -V | sed -n 1p)" != 2.28 ]; then
        say "  glibc:    $glibc -- too old (2.28 or newer needed)"; hard=1
    else
        say "  glibc:    $glibc"
    fi
    for tool in curl tar bzip2 sha256sum df flock; do
        command -v "$tool" >/dev/null || { say "  tool:     $tool MISSING (install it with the system package manager)"; hard=1; }
    done
    # deepcell-toolbox (Mesmer) has no wheel: pip compiles it, so a C compiler
    # is needed even without --build-core (found by the clean-container test)
    if command -v cc >/dev/null || command -v gcc >/dev/null; then
        say "  compiler: $(command -v cc || command -v gcc)"
    else
        say "  compiler: MISSING (one package is compiled during the install: install gcc;"
        say "            Ubuntu/Debian: sudo apt install build-essential, RHEL/Fedora: sudo dnf install gcc)"
        hard=1
    fi
    check_system_libs || hard=1
    if command -v nvidia-smi >/dev/null; then
        local drv; drv=$(nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>/dev/null) || drv=""
        drv=${drv%%$'\n'*}
        say "  NVIDIA:   ${drv:-nvidia-smi present, no GPU answered}"
        local major=${drv%%.*}
        if [ -n "$drv" ] && [ "${major:-0}" -lt 535 ] 2>/dev/null; then
            warn "driver older than the verified 535: GPU engines are untested on it"
        fi
    else
        warn "no nvidia-smi: segmentation engines and background correction will run on the CPU (slow)"
    fi
    CUDA_DIR=$(resolve_cuda)
    if [ -n "$CUDA_DIR" ]; then
        local cv; cv=$(grep -m1 -oE '"version" *: *"[0-9.]+"' "$CUDA_DIR/version.json" 2>/dev/null) || cv=""
        cv=$(printf '%s' "$cv" | grep -oE '[0-9][0-9.]*' || true)
        say "  CUDA:     $CUDA_DIR ${cv:+(version $cv)}  (what GPU background correction compiles against)"
        case "$cv" in 12.2*|"") ;; *) warn "verified with CUDA 12.2 headers; others are checked by the GPU test in step 6" ;; esac
    else
        warn "no CUDA toolkit found (CUDA_PATH, nvcc, /usr/local/cuda): background correction will use the CPU"
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
    say "  space:    ${g} GB free at $ROOT (about $need GB needed for what is left to do)"
    [ "$g" -ge "$need" ] || { say "  space:    NOT ENOUGH"; hard=1; }
    local url
    command -v curl >/dev/null || { say "  network:  not checked (needs curl)"; [ "$hard" = 0 ] || fail "this computer does not meet the requirements listed above." "fix the items marked above and run the installer again"; }
    for url in https://conda.anaconda.org https://pypi.org https://download.pytorch.org/whl/cu121/ \
               https://github.com https://huggingface.co https://users.deepcell.org; do
        if curl -sS -o /dev/null --connect-timeout 10 --max-time 20 -I "$url" 2>/dev/null; then
            say "  network:  $url ok"
        else
            warn "cannot reach $url (a step that needs it will stop)"
        fi
    done
    if [ "$BUILD_CORE" = 1 ]; then
        if curl -sS -o /dev/null --connect-timeout 10 -I https://static.rust-lang.org 2>/dev/null; then
            say "  network:  https://static.rust-lang.org ok"
        else
            warn "cannot reach static.rust-lang.org (--build-core needs it)"
        fi
    fi
    [ "$hard" = 0 ] || fail "this computer does not meet the requirements listed above." "fix the items marked above and run the installer again"
}

# ── 2. micromamba ─────────────────────────────────────────────────────────
MAMBA=""
ensure_micromamba() {
    STEP="2. micromamba (the package installer)"
    if command -v micromamba >/dev/null; then
        MAMBA=$(command -v micromamba); say "using micromamba at $MAMBA"; return
    fi
    MAMBA="$ROOT/bin/micromamba"
    if [ -x "$MAMBA" ] && [ "$(sha256_of "$MAMBA")" = "$MICROMAMBA_SHA256" ]; then
        say "using micromamba at $MAMBA"; return
    fi
    say "downloading micromamba 2.8.1 into $ROOT/bin"
    download "$MICROMAMBA_URL" "$MAMBA" "$MICROMAMBA_SHA256"
    chmod +x "$MAMBA"
}

# ── 3. the environment ────────────────────────────────────────────────────
lock_id() { cat "$SPEC/conda-linux-64.lock" "$SPEC/requirements-pip.txt" | sha256sum | cut -c1-8; }
env_prefix() { echo "$ROOT/envs/fusion_mesmer-$(lock_id)"; }
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
    case "$state" in
        complete)
            say "environment already installed: $ENV_PREFIX"
            return ;;
        built)
            say "environment already built: $ENV_PREFIX (continuing where the last run stopped)"
            return ;;
        foreign)
            fail "$ENV_PREFIX exists but was not created by this installer; it is left untouched." \
                 "install into a new location: bash scripts/install_block01.sh --root NEW_DIR" ;;
        interrupted)
            say "an earlier install into $ENV_PREFIX stopped half-way: removing it and starting again"
            case "$ENV_PREFIX" in "$ROOT"/envs/fusion_mesmer-????????) ;; *) fail "refusing to remove $ENV_PREFIX (unexpected path)" "" ;; esac
            rm -rf -- "$ENV_PREFIX" ;;
    esac
    mkdir -p "$ROOT/envs" "$ROOT/mamba"
    # ownership first, so an interruption at any later point is recognised
    printf 'created by %s\npid %s\ntime %s\ncheckout %s\n' "$0" "$$" "$(date -Is)" "$REPO" > "$ENV_PREFIX.owner"
    say "creating the environment (conda part, about 160 packages): $ENV_PREFIX"
    # micromamba gets a HOME of its own: it would add the prefix to
    # ~/.conda/environments.txt and make ~/.mamba
    mkdir -p "$ROOT/mamba/home"
    HOME="$ROOT/mamba/home" MAMBA_ROOT_PREFIX="$ROOT/mamba" CONDA_PKGS_DIRS="$ROOT/mamba/pkgs" \
        run "$MAMBA" create -y -q -p "$ENV_PREFIX" --file "$SPEC/conda-linux-64.lock"
    say "installing the pip part (about 220 packages, PyTorch included)"
    run "$PY" -m pip install -q --no-deps -r "$SPEC/requirements-pip.txt"
    RUN_HINT="report this output: the pinned packages do not fit together on this machine" run "$PY" -m pip check
    date -Is > "$ENV_PREFIX/.block01_env_ok"
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
        say "SpatialData environment skipped (--no-spatialdata): Step4 asks for a Python with spatialdata if its export is ticked"
        return
    fi
    local prefix; prefix=$(sd_env_prefix)
    local state; state=$(prefix_state "$prefix")
    case "$state" in
        complete|built)
            say "SpatialData environment already installed: $prefix" ;;
        foreign)
            fail "$prefix exists but was not created by this installer; it is left untouched." \
                 "install into a new location (--root NEW_DIR), or skip it with --no-spatialdata" ;;
        absent|interrupted)
            if [ "$state" = interrupted ]; then
                say "an earlier install into $prefix stopped half-way: removing it and starting again"
                case "$prefix" in "$ROOT"/envs/block01_spatialdata-????????) ;; *) fail "refusing to remove $prefix (unexpected path)" "" ;; esac
                rm -rf -- "$prefix"
            fi
            mkdir -p "$ROOT/envs" "$ROOT/mamba/home"
            printf 'created by %s\npid %s\ntime %s\ncheckout %s\n' "$0" "$$" "$(date -Is)" "$REPO" > "$prefix.owner"
            say "creating the SpatialData environment (Step4's SpatialData export, about 1.2 GB): $prefix"
            HOME="$ROOT/mamba/home" MAMBA_ROOT_PREFIX="$ROOT/mamba" CONDA_PKGS_DIRS="$ROOT/mamba/pkgs" \
                run "$MAMBA" create -y -q -p "$prefix" --file "$SD_SPEC/conda-linux-64.lock"
            run "$prefix/bin/python" -m pip install -q --no-deps -r "$SD_SPEC/requirements-pip.txt"
            RUN_HINT="report this output: the pinned SpatialData packages do not fit together on this machine" \
                run "$prefix/bin/python" -m pip check
            date -Is > "$prefix/.block01_env_ok" ;;
    esac
    local version
    version=$(cd "$STATE_DIR/run" 2>/dev/null || cd /; "$prefix/bin/python" -I -c 'import spatialdata; print(spatialdata.__version__)' 2>"$STATE_DIR/run/spatialdata.err" | tail -n1) \
        || fail "spatialdata does not import in $prefix (its error: $STATE_DIR/run/spatialdata.err)." \
                "run the installer again; if it repeats, report that file (or install with --no-spatialdata)"
    say "  spatialdata $version ready"
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
        say "installing the Rust toolchain $RUST_TOOLCHAIN under $rhome (your own Rust setup is not changed)"
        download "$RUSTUP_URL" "$rhome/rustup-init" "$RUSTUP_SHA256"
        chmod +x "$rhome/rustup-init"
        run "$rhome/rustup-init" -y -q --no-modify-path --default-toolchain none --profile minimal
    fi
    run "$CARGO_HOME/bin/rustup" toolchain install "$RUST_TOOLCHAIN" --profile minimal
    [ -x "$venv/bin/maturin" ] || { run "$PY" -m venv "$venv"; run "$venv/bin/python" -m pip install -q "$MATURIN_PIN"; }
    say "building the viewer core from this checkout"
    PATH="$CARGO_HOME/bin:$PATH" RUSTUP_TOOLCHAIN="$RUST_TOOLCHAIN" run "$venv/bin/maturin" build --release --locked \
        -i "$PY" -m "$REPO/native/fusion_viewer_core/Cargo.toml" -o "$out"
}

ensure_core() {
    STEP="4. the viewer core (fusion_viewer_core)"
    local want; want=$(core_version)
    local have; have=$(installed_core "$ENV_PREFIX")
    if [ "$have" = "$want 1" ]; then
        say "viewer core $want already installed"; return
    fi
    local dist="$ROOT/downloads/core-$want"
    local entry; entry=$(wheels_lock_entry "$want" linux)
    local wheel
    if [ -n "$entry" ]; then
        local file=${entry% *} sha=${entry#* }
        wheel="$dist/$file"
        if [ ! -f "$wheel" ] || [ "$(sha256_of "$wheel")" != "$sha" ]; then
            say "downloading the prebuilt viewer core $want"
            download "$RELEASE_URL/core-v$want/$file" "$wheel" "$sha"
        fi
    elif [ "$BUILD_CORE" = 1 ]; then
        build_core_wheel "$dist"
        wheel=$(find "$dist" -maxdepth 1 -name "fusion_viewer_core-$want-*.whl" -print -quit)
        [ -n "$wheel" ] || fail "the build made no wheel" ""
    else
        fail "there is no prebuilt viewer core for version $want (a development checkout)." \
             "run again with --build-core to build it here (needs a C compiler and about 2 minutes)"
    fi
    run "$PY" -m pip install -q --no-deps --force-reinstall "$wheel"
    have=$(installed_core "$ENV_PREFIX")
    [ "$have" = "$want 1" ] || fail "the installed viewer core reports '$have', expected '$want 1'." ""
    say "viewer core $want installed"
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

licence_text() {
    case "$1" in
        cellpose_cpsam) cat <<'EOF'
  Cellpose-SAM model "cpsam" (1.2 GB) from huggingface.co/mouseland/cellpose-sam
  Licence of the model (its model card): BSD-3-Clause
    https://huggingface.co/mouseland/cellpose-sam
  Separate statement in Cellpose's README: Cellpose-SAM was trained on data
  under CC-BY-NC licences.  https://github.com/MouseLand/cellpose
EOF
        ;;
        stardist_2D_versatile_fluo) cat <<'EOF'
  StarDist model "2D_versatile_fluo" (5 MB) from github.com/stardist/stardist-models
  Licence: BSD-3-Clause  https://github.com/stardist/stardist
EOF
        ;;
        mesmer_multiplex_segmentation) cat <<'EOF'
  Mesmer model "MultiplexSegmentation" (100 MB) from DeepCell (users.deepcell.org)
  Licence: non-commercial academic use only (DeepCell ASSET_LICENSE)
    https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE
  The PyTorch copy Block01 makes from it keeps the same licence; never share it.
EOF
        ;;
    esac
}

accept_licence() {
    say "Licence for the model about to be downloaded:"
    licence_text "$1"
    if [ "$ACCEPT_LICENCES" = 1 ]; then
        say "  accepted with --accept-model-licences"; return 0
    fi
    if ! { : < /dev/tty; } 2>/dev/null; then              # no controlling terminal
        fail "a model licence needs your answer, and there is no terminal to ask." \
             "run the installer in a terminal, or read the licences above and add --accept-model-licences"
    fi
    local answer
    printf '[install] Type yes to accept this licence and download: ' > /dev/tty
    read -r answer < /dev/tty || answer=""
    [ "$answer" = yes ] || fail "the licence was not accepted, so the model was not downloaded." \
        "run the installer again and type yes, or put your own copy in a folder and pass --models FOLDER"
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

fetch_model() {
    local key=$1 dir=$2
    local stage="$dir.stage.$$"
    rm -rf -- "$stage"; mkdir -p "$stage"
    case "$key" in
        cellpose_cpsam)
            local sha; sha=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['models']['cellpose_cpsam']['files'][0]['sha256'])" "$MANIFEST")
            download "$CPSAM_URL" "$stage/cpsam" "$sha" ;;
        stardist_2D_versatile_fluo)
            local zsha; zsha=$("$PY" -c "import json,sys; e=json.load(open(sys.argv[1]))['models']['stardist_2D_versatile_fluo']; print([f['sha256'] for f in e['files'] if f['path'].endswith('.zip')][0])" "$MANIFEST")
            download "$STARDIST_URL" "$stage/2D_versatile_fluo.zip" "$zsha"
            run "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$stage/2D_versatile_fluo.zip" "$stage" ;;
        mesmer_multiplex_segmentation)
            local reply url
            reply=$(curl -fsS --retry 3 -X POST -H "X-Api-Key: $DEEPCELL_ACCESS_TOKEN" \
                    --data-urlencode "s3_key=$DEEPCELL_KEY" "$DEEPCELL_API") \
                || { rm -rf -- "$stage"; fail "DeepCell refused the download (a wrong or expired token, or no network)." \
                     "check DEEPCELL_ACCESS_TOKEN at https://users.deepcell.org and run the installer again"; }
            url=$(printf '%s' "$reply" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['url'])") \
                || fail "DeepCell's answer had no download link." "try again later"
            download "$url" "$stage/mesmer.tar.gz" ""
            mkdir -p "$stage/x"
            run tar -xzf "$stage/mesmer.tar.gz" -C "$stage/x"
            [ -d "$stage/x/MultiplexSegmentation" ] || fail "the DeepCell archive has no MultiplexSegmentation folder." "report it"
            # the CONTENTS of MultiplexSegmentation/ are the manifest entry's root
            cp -a "$stage/x/MultiplexSegmentation"/. "$stage"/
            rm -rf -- "$stage/x" "$stage/mesmer.tar.gz" ;;
    esac
    verify_into_place "$key" "$stage" "$dir"
}

ensure_models() {
    STEP="5. the segmentation models"
    local key dir st
    for key in "${ORIGINAL_MODELS[@]}"; do
        dir=$(model_dir "$key")
        st=$(model_state "$key" "$dir")
        case "$st" in
            ok) say "model $key: present and verified"; continue ;;
            different|partial)
                fail "model $key in $dir does not match models.json ($st); it is left untouched." \
                     "move that folder away (or use another --root) and run the installer again" ;;
        esac
        if [ -n "$MODELS_IN" ] && [ "$(model_state "$key" "$MODELS_IN/$key")" = ok ]; then
            say "model $key: copying the verified copy from $MODELS_IN/$key"
            local stage="$dir.stage.$$" f
            rm -rf -- "$stage"; mkdir -p "$stage"
            # the listed files only, as real files (-L: a link in the given
            # folder must not leave the install depending on that folder)
            while IFS= read -r f; do
                mkdir -p "$(dirname "$stage/$f")"
                cp -L -- "$MODELS_IN/$key/$f" "$stage/$f"
            done < <(manifest_files "$key")
            verify_into_place "$key" "$stage" "$dir"
            continue
        fi
        if [ "$key" = mesmer_multiplex_segmentation ] && [ -z "${DEEPCELL_ACCESS_TOKEN:-}" ]; then
            licence_text "$key"
            fail "the Mesmer model needs a free DeepCell account token, and DEEPCELL_ACCESS_TOKEN is not set." \
                 "get a token at https://users.deepcell.org, then: DEEPCELL_ACCESS_TOKEN=... bash scripts/install_block01.sh ; or put an existing copy in FOLDER/$key and pass --models FOLDER"
        fi
        accept_licence "$key"
        say "model $key: downloading"
        fetch_model "$key" "$dir"
        say "model $key: downloaded and verified"
    done
}

# ── 6. alias, self-test ───────────────────────────────────────────────────
STATE_DIR="$REPO/.block01"

ensure_alias() {
    STEP="6. the package link for this checkout"
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

# check_selftest: seg_runner.selftest's lines on stdin; every engine must be
# ok, exit 0 and name its device
check_selftest() {
    "$PY" -c '
import json, sys
want = {"cellpose", "stardist", "mesmer"}
seen, bad = set(), []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    r = json.loads(line)
    seen.add(r.get("engine"))
    if r.get("state") != "ok" or r.get("exit_code") != 0 or not r.get("device"):
        bad.append(r.get("engine"))
    else:
        print("[install]   %s: ok on %s" % (r["engine"], r["device"]))
bad += sorted(want - seen)
if bad:
    print("[install]   failed: " + ", ".join(bad)); sys.exit(1)
'
}

selftest() {
    STEP="6. self-test"
    app_env
    say "== 6. Self-test: one small task per segmentation engine =="
    local out
    out=$(cd "$STATE_DIR/run" && PYTHONPATH="$REPO" "$PY" -m seg_runner.selftest 2>"$STATE_DIR/run/selftest.err") || true
    printf '%s\n' "$out"
    printf '%s\n' "$out" | check_selftest || fail "an engine failed its self-test (details above and in $STATE_DIR/run/selftest.err)." \
          "read the error, fix the cause (often the GPU driver or CUDA) and run the installer again"
    (cd "$STATE_DIR/run" && run "$PY" "$REPO/scripts/model_manifest.py" --verify)
    local gpu
    gpu=$(cd "$STATE_DIR/run" && PYTHONPATH="$STATE_DIR/pkg" "$PY" -c \
        'from block01.core import bg_correction as b; print("ok" if b.GPU_MORPH_AVAILABLE else "cpu", b.GPU_MORPH_SMOKE_TEST)' 2>"$STATE_DIR/run/gpucheck.err" | tail -n1 || echo "cpu (check failed: $STATE_DIR/run/gpucheck.err)")
    case "$gpu" in ok*) say "  GPU background correction: ok" ;;
                   *) warn "GPU background correction unavailable, the CPU is used ($gpu)" ;; esac
    local core
    core=$(cd "$STATE_DIR/run" && QT_QPA_PLATFORM=offscreen PYTHONPATH="$STATE_DIR/pkg" "$PY" -c '
from PyQt5.QtWidgets import QApplication
app = QApplication([])
import block01.main  # noqa: F401 -- the app imports
from block01.ui.step1_core_adapter import core_unavailable_reason
import fusion_viewer_core as c
why = core_unavailable_reason()
print(f"fusion_viewer_core {c.__version__} API {c.CORE_API} accepted" if not why else "REFUSED: " + why)
' 2>"$STATE_DIR/run/startcheck.err" | tail -n1 || true)
    case "$core" in
        *accepted) say "  app start check: $core" ;;
        *) tail -n 15 "$STATE_DIR/run/startcheck.err" >&2 || true
           fail "the app does not start or refuses its viewer core: ${core:-no answer} (its error output is above, all of it in $STATE_DIR/run/startcheck.err)." \
                "a missing system library is named there: install it with the system package manager and run the installer again" ;;
    esac
}

# ── 7. the record for this checkout ───────────────────────────────────────
write_record() {
    STEP="7. the install record"
    local rec="$STATE_DIR/install.env" key
    {
        echo "# written by scripts/install_block01.sh $(date -Is); read by scripts/block01"
        printf 'BLOCK01_CHECKOUT=%q\n' "$REPO"
        printf 'BLOCK01_ROOT=%q\n' "$ROOT"
        printf 'BLOCK01_PREFIX=%q\n' "$ENV_PREFIX"
        printf 'BLOCK01_CORE_VERSION=%q\n' "$(core_version)"
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

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --root) ROOT=$(realpath -m "$2"); shift 2 ;;
            --check-only) CHECK_ONLY=1; shift ;;
            --build-core) BUILD_CORE=1; shift ;;
            --models) MODELS_IN=$(realpath -m "$2"); shift 2 ;;
            --accept-model-licences) ACCEPT_LICENCES=1; shift ;;
            --no-spatialdata) NO_SPATIALDATA=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 2 ;;
        esac
    done
    local t0; t0=$(date +%s)
    say "Block01 install: checkout $REPO, install root $ROOT"
    preflight
    [ "$CHECK_ONLY" = 0 ] || { say "check only: nothing was written"; exit 0; }
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
    selftest
    write_record
    say "== Done in $(( $(date +%s) - t0 )) s. Start Block01 with: =="
    say "   $REPO/scripts/block01"
}

if [ -z "${BLOCK01_INSTALL_SOURCED:-}" ]; then
    main "$@"
fi
