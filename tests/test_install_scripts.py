"""docs/v17_install_plan.md §2-3: the installer's and the launcher's decisions,
without network or a real environment. Each case sources the script
(`BLOCK01_INSTALL_SOURCED=1` / `BLOCK01_LAUNCH_SOURCED=1`: functions only) in
a bash process and calls one function against files under tmp_path.

* prefix states: absent / ours complete / ours interrupted / foreign;
* an interrupted prefix is removed and redone, a foreign one is refused and
  left byte for byte as it was;
* model states: ok / missing / partial / different, and a different model
  is refused, never overwritten;
* the wheels.lock lookup;
* the package link: made, kept, a foreign one refused;
* the launcher refuses a missing record, another checkout's record, a wrong
  link, an unfinished install and a refused core, before Python starts.
"""

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install_block01.sh"
LAUNCH = REPO / "scripts" / "block01"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt",
                                reason="the Linux / Mac bash installers (Windows: phase W3)")


def _bash(script, body, env=None, cwd=None):
    flag = "BLOCK01_INSTALL_SOURCED" if script == INSTALL else "BLOCK01_LAUNCH_SOURCED"
    full = dict(os.environ, **{flag: "1"}, **(env or {}))
    return subprocess.run(["bash", "-c", f'. "{script}"\n{body}'], capture_output=True, text=True,
                          env=full, cwd=cwd, timeout=60)


# ── the environment prefix ─────────────────────────────────────────────────
def _state(path):
    r = _bash(INSTALL, f'prefix_state "{path}"')
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_prefix_states(tmp_path):
    p = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    assert _state(p) == "absent"
    p.mkdir(parents=True)
    assert _state(p) == "foreign"                       # no owner file: not ours
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("x")
    assert _state(p) == "interrupted"                   # ours, never finished
    (p / ".block01_env_ok").write_text("t")
    assert _state(p) == "built"                         # env done, a later step stopped
    (p / ".block01_install_ok").write_text("core 0.2.0")
    assert _state(p) == "complete"


def _ensure_env(root, extra=""):
    """`ensure_environment` with micromamba and pip replaced by stubs that
    record their calls and make the prefix."""
    body = f'''
ROOT="{root}"
MAMBA=stub_mamba
stub_mamba() {{ echo "mamba $*" >> "{root}/calls"; mkdir -p "$ENV_PREFIX/bin"; }}
lock_id() {{ echo abcdef12; }}
run() {{ echo "run $*" >> "{root}/calls"; [ "$1" = stub_mamba ] && stub_mamba; return 0; }}
{extra}
ensure_environment
echo "PREFIX=$ENV_PREFIX"
'''
    return _bash(INSTALL, body)


def test_an_absent_prefix_is_created_with_its_owner_file_first(tmp_path):
    r = _ensure_env(tmp_path)
    assert r.returncode == 0, r.stderr
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    assert (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").read_text(encoding="utf-8").startswith("created by")
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "create -y -q -p " + str(prefix) in calls and "pip install -q --no-deps" in calls
    assert "pip check" in calls
    assert (prefix / ".block01_env_ok").exists()


def test_an_interrupted_prefix_is_removed_and_redone(tmp_path):
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    (prefix / "half").mkdir(parents=True)
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("old")
    r = _ensure_env(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "stopped half-way" in r.stdout
    assert not (prefix / "half").exists()
    assert "create -y -q" in (tmp_path / "calls").read_text(encoding="utf-8")


def _tree(path):
    out = {}
    for p in sorted(path.rglob("*")):
        out[str(p.relative_to(path))] = (p.is_dir(), p.stat().st_mtime_ns,
                                         None if p.is_dir() else p.read_bytes())
    return out


def test_a_foreign_prefix_is_refused_and_left_untouched(tmp_path):
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "lib" / "x").write_text("someone else's")
    before = _tree(tmp_path / "envs")
    r = _ensure_env(tmp_path)
    assert r.returncode == 1
    assert "was not created by this installer" in r.stderr and "--root NEW_DIR" in r.stderr
    assert _tree(tmp_path / "envs") == before
    assert not (tmp_path / "calls").exists()


def test_a_built_prefix_is_kept_and_the_run_continues(tmp_path):
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    prefix.mkdir(parents=True)
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("ours")
    (prefix / ".block01_env_ok").write_text("t")
    r = _ensure_env(tmp_path)
    assert r.returncode == 0 and "continuing where the last run stopped" in r.stdout
    assert not (tmp_path / "calls").exists() and prefix.exists()


def test_a_complete_prefix_is_not_reinstalled(tmp_path):
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    prefix.mkdir(parents=True)
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("ours")
    (prefix / ".block01_install_ok").write_text("core 0.2.0")
    r = _ensure_env(tmp_path)
    assert r.returncode == 0 and "already installed" in r.stdout
    assert not (tmp_path / "calls").exists()


# ── models ────────────────────────────────────────────────────────────────
def _manifest(tmp_path, files, key="k"):
    entry = {"files": [{"path": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                       for n, b in files.items()]}
    m = tmp_path / "models.json"
    m.write_text(json.dumps({"models": {key: entry}}))
    return m


PY = shutil.which("python3") or "python3"


def _model_state(manifest, d):
    r = _bash(INSTALL, f'PY={PY}; MANIFEST="{manifest}"; model_state k "{d}"')
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_model_states(tmp_path):
    files = {"a.bin": b"alpha", "sub/b.bin": b"beta"}
    m = _manifest(tmp_path, files)
    d = tmp_path / "model"
    assert _model_state(m, d) == "missing"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"alpha")
    assert _model_state(m, d) == "partial"
    (d / "sub" / "b.bin").write_bytes(b"beta")
    assert _model_state(m, d) == "ok"
    (d / "sub" / "b.bin").write_bytes(b"BETA")
    assert _model_state(m, d) == "different"


def test_a_different_model_is_refused_and_never_overwritten(tmp_path):
    m = _manifest(tmp_path, {"cpsam": b"right"})
    root = tmp_path / "root"
    d = root / "models" / "cellpose_cpsam"
    d.mkdir(parents=True)
    (d / "cpsam").write_bytes(b"wrong")
    body = f'''PY={PY}; MANIFEST="{m}"; ROOT="{root}"
ORIGINAL_MODELS=(cellpose_cpsam)
model_state() {{ echo different; }}
fetch_model() {{ echo FETCHED; }}
ensure_models'''
    r = _bash(INSTALL, body)
    assert r.returncode == 1 and "left untouched" in r.stderr
    assert "FETCHED" not in r.stdout
    assert (d / "cpsam").read_bytes() == b"wrong"


_MESMER_STUBS = '''
model_state() {{ if [ -f "$2/ok" ]; then echo ok; else echo missing; fi; }}
try_download() {{ echo "TRY $1"; case "$1" in {working}) mkdir -p "$(dirname "$2")"; : > "$2"; return 0 ;; esac; return 1; }}
sha256_of() {{ echo 25b1babc2ff769a9f9484184073db8bc264d5ab2de8a6f9ce07c289e8b935129; }}
mesmer_from_archive() {{ : > "$2/ok"; }}
verify_into_place() {{ echo "VERIFY $1"; }}
'''


def _mesmer(tmp_path, working="none", extra=""):
    body = (f'PY={PY}; ROOT="{tmp_path}"; unset DEEPCELL_ACCESS_TOKEN; ACCEPT_LICENCES=1\n'
            "ORIGINAL_MODELS=(mesmer_multiplex_segmentation)\n" + extra
            + _MESMER_STUBS.format(working=working) + "ensure_models; echo MISSING=$MESMER_MISSING")
    return _bash(INSTALL, body)


def test_mesmer_comes_first_from_the_public_address_with_no_token(tmp_path):
    """DeepCell's public archive (SPACEc's URL) is tried first, its fixed
    SHA-256 checked; no token is asked for; the licence is shown first."""
    r = _mesmer(tmp_path, working="https://deepcell-data.*")
    assert r.returncode == 0, r.stderr
    assert "NON-COMMERCIAL" in r.stdout and "accepted (--accept-model-licences)" in r.stdout
    assert ("TRY https://deepcell-data.s3-us-west-1.amazonaws.com/saved-models/"
            "MultiplexSegmentation-9.tar.gz") in r.stdout
    assert "VERIFY mesmer_multiplex_segmentation" in r.stdout and "MISSING=0" in r.stdout
    assert "DEEPCELL_ACCESS_TOKEN" not in r.stdout + r.stderr
    src = INSTALL.read_text(encoding="utf-8")
    assert "users.deepcell.org" not in src and "DEEPCELL_ACCESS_TOKEN" not in src


def test_mesmer_from_the_mirror_when_deepcell_is_gone(tmp_path):
    """2026-11-15: DeepCell's archive answers 403; the lab's mirror is used,
    checked file by file against models.json."""
    r = _mesmer(tmp_path, working="https://mirror.example/*",
                extra='MESMER_MIRROR="https://mirror.example/m.tar.gz"\n')
    assert r.returncode == 0, r.stderr
    assert r.stdout.index("TRY https://deepcell-data") < r.stdout.index("TRY https://mirror.example")
    assert "VERIFY mesmer_multiplex_segmentation" in r.stdout and "MISSING=0" in r.stdout


def test_mesmer_from_a_local_archive(tmp_path):
    archive = tmp_path / "m.tar.gz"
    archive.write_bytes(b"x")
    r = _mesmer(tmp_path, extra=f'MESMER_ZIP="{archive}"\n')
    assert r.returncode == 0, r.stderr
    assert "from the archive" in r.stdout and "MISSING=0" in r.stdout


def test_without_any_mesmer_source_the_install_goes_on_and_says_how_to_add_it(tmp_path):
    r = _mesmer(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "MISSING=1" in r.stdout and "VERIFY" not in r.stdout
    assert "Mesmer is NOT installed" in r.stderr and "--mesmer-only" in r.stderr


def test_the_self_test_and_the_manifest_check_skip_a_missing_mesmer():
    r = subprocess.run(["bash", "-c", f'. "{INSTALL}"\nMESMER_MISSING=1; installed_engines; '
                        f'MESMER_MISSING=0; installed_engines'],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, BLOCK01_INSTALL_SOURCED="1"))
    assert r.stdout.splitlines() == ["cellpose stardist", "cellpose stardist mesmer"]
    assert "model_manifest.py\" --verify --installed" in INSTALL.read_text(encoding="utf-8")


def test_mesmer_licence_answered_no_downloads_nothing(tmp_path):
    body = f'''PY={PY}; ROOT="{tmp_path}"; ACCEPT_LICENCES=0
ORIGINAL_MODELS=(mesmer_multiplex_segmentation)
model_state() {{ echo missing; }}
fetch_model() {{ echo FETCHED; }}
ensure_models'''
    if shutil.which("script") is None:
        pytest.skip("needs script(1) for a terminal")
    cmd = f'. "{INSTALL}"\n{body}'
    import sys
    argv = (["script", "-q", "/dev/null", "bash", "-c", cmd] if sys.platform == "darwin"
            else ["script", "-qec", f"bash -c {shlex.quote(cmd)}", "/dev/null"])
    r = subprocess.run(argv, capture_output=True, text=True, input="no\n",
                       env=dict(os.environ, BLOCK01_INSTALL_SOURCED="1"), timeout=60)
    out = r.stdout + r.stderr
    assert "NON-COMMERCIAL" in out and "FETCHED" not in out
    assert "the licences were not accepted" in out


def test_a_licence_needs_an_answer(tmp_path):
    body = f'''PY={PY}; ROOT="{tmp_path}"; ACCEPT_LICENCES=0
ORIGINAL_MODELS=(stardist_2D_versatile_fluo)
model_state() {{ echo missing; }}
fetch_model() {{ echo FETCHED; }}
ensure_models'''
    r = subprocess.run(["setsid", "bash", "-c", f'. "{INSTALL}"\n{body}'], capture_output=True, text=True,
                       env=dict(os.environ, BLOCK01_INSTALL_SOURCED="1"), timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "no terminal to ask" in r.stderr
    assert "BSD-3-Clause" in r.stdout and "FETCHED" not in r.stdout
    r = _bash(INSTALL, body.replace("ACCEPT_LICENCES=0", "ACCEPT_LICENCES=1"))
    assert r.returncode == 0 and "FETCHED" in r.stdout and "accepted (--accept-model-licences)" in r.stdout


def test_models_from_a_folder_are_verified_and_copied_not_moved(tmp_path):
    m = _manifest(tmp_path, {"cpsam": b"weights"}, key="cellpose_cpsam")
    src = tmp_path / "given" / "cellpose_cpsam"
    src.mkdir(parents=True)
    (src / "cpsam").write_bytes(b"weights")
    root = tmp_path / "root"
    body = f'''PY={PY}; MANIFEST="{m}"; ROOT="{root}"; MODELS_IN="{tmp_path / 'given'}"
ORIGINAL_MODELS=(cellpose_cpsam)
ensure_models'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stderr
    assert "copied from the verified copy" in r.stdout
    assert (root / "models" / "cellpose_cpsam" / "cpsam").read_bytes() == b"weights"
    assert (src / "cpsam").read_bytes() == b"weights"


# ── system libraries ──────────────────────────────────────────────────────
def test_missing_qt_libraries_are_named_with_their_packages(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    listing = "\n".join(f"\t{lib} (libc6,x86-64) => /usr/lib/{lib}" for lib in
                        ("libGL.so.1", "libEGL.so.1", "libglib-2.0.so.0"))
    (fake / "ldconfig").write_text(f"#!/bin/sh\nprintf '%s\\n' '{listing}'\n")
    (fake / "ldconfig").chmod(0o755)
    r = _bash(INSTALL, "check_system_libs", env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 1
    assert "libgthread-2.0.so.0" in r.stdout and "libGL.so.1" not in r.stdout.split("MISSING")[1].split("\n")[0]
    apt = [l for l in r.stdout.splitlines() if "sudo apt install" in l][0]
    assert apt.count("libglib2.0-0") == 1 and "libxcb-xinerama0" in apt and " libgl1" not in apt
    full = "\n".join(f"\t{pair.split(':')[0]} (libc6,x86-64) => /x" for pair in
                     _bash(INSTALL, 'echo $QT_SYSTEM_LIBS').stdout.split())
    (fake / "ldconfig").write_text(f"#!/bin/sh\nprintf '%s\\n' '{full}'\n")
    r = _bash(INSTALL, "check_system_libs", env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 0 and "Qt libs:  present" in r.stdout


# ── self-test result ──────────────────────────────────────────────────────
def _selftest(lines):
    r = subprocess.run(["bash", "-c", f'. "{INSTALL}"\nPY={PY}; check_selftest'], input="\n".join(lines),
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, BLOCK01_INSTALL_SOURCED="1"))
    return r.returncode, r.stdout


def _engine(name, **kw):
    rec = {"engine": name, "state": "ok", "device": "cpu", "exit_code": 0}
    rec.update(kw)
    return json.dumps(rec)


def test_the_self_test_needs_every_engine_ok_exit_0_and_a_device():
    noise = "a warning line"
    rc, out = _selftest([noise, _engine("cellpose", device="cuda:0"), _engine("stardist"), _engine("mesmer", device="gpu")])
    assert rc == 0 and "cellpose\tok\tcuda:0\n" in out and "mesmer\tok\tgpu\n" in out
    assert _selftest([_engine("cellpose"), _engine("stardist")])[1].endswith("mesmer\tFAILED\t\n")
    assert _selftest([_engine("cellpose"), _engine("stardist", exit_code=1), _engine("mesmer")])[0] == 1
    assert _selftest([_engine("cellpose"), _engine("stardist", state="error"), _engine("mesmer")])[0] == 1
    assert _selftest([_engine("cellpose", device=""), _engine("stardist"), _engine("mesmer")])[0] == 1


def test_a_model_import_copies_only_listed_files_as_real_files_and_keeps_user_files(tmp_path):
    m = _manifest(tmp_path, {"cpsam": b"weights"}, key="cellpose_cpsam")
    real = tmp_path / "elsewhere" / "cpsam"
    real.parent.mkdir()
    real.write_bytes(b"weights")
    src = tmp_path / "given" / "cellpose_cpsam"
    src.mkdir(parents=True)
    (src / "cpsam").symlink_to(real)                      # a link in the given folder
    (src / "README").write_text("theirs")
    root = tmp_path / "root"
    dest = root / "models" / "cellpose_cpsam"
    dest.mkdir(parents=True)
    (dest / "README").write_text("mine")                  # the user's own file
    body = f'''PY={PY}; MANIFEST="{m}"; ROOT="{root}"; MODELS_IN="{tmp_path / 'given'}"
ORIGINAL_MODELS=(cellpose_cpsam)
ensure_models'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stderr
    assert (dest / "README").read_text(encoding="utf-8") == "mine"
    assert not (dest / "cpsam").is_symlink() and (dest / "cpsam").read_bytes() == b"weights"
    real.unlink()                                          # the install does not depend on it
    assert (dest / "cpsam").read_bytes() == b"weights"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["cellpose_cpsam"]   # no stage left


def _preflight(tmp_path, free, extra="", after=""):
    body = f'''ROOT="{tmp_path}"; BUILD_CORE=0
curl() {{ return 0; }}
check_system_libs() {{ return 0; }}
free_gb() {{ echo {free}; }}
lock_id() {{ echo abcdef12; }}
{extra}
preflight
{after}'''
    return _bash(INSTALL, body)


def test_the_space_check_counts_only_the_work_left(tmp_path):
    r = _preflight(tmp_path, 5)                    # 14 env + 2 models + 2 SpatialData env
    assert r.returncode == 1 and "NOT ENOUGH" in r.stdout and "about 18 GB" in r.stdout
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    prefix.mkdir(parents=True)
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("ours")
    (prefix / ".block01_env_ok").write_text("t")
    (tmp_path / "models" / "cellpose_cpsam").mkdir(parents=True)
    (tmp_path / "models" / "cellpose_cpsam" / "cpsam").write_bytes(b"x")
    r = _preflight(tmp_path, 5)
    assert r.returncode == 0 and "about 3 GB" in r.stdout and "NOT ENOUGH" not in r.stdout
    r = _preflight(tmp_path, 5, "NO_SPATIALDATA=1")
    assert r.returncode == 0 and "about 1 GB" in r.stdout
    # an interrupted (ours, unfinished) environment is removed first: its size counts as free
    (prefix / ".block01_env_ok").unlink()
    stub_du = 'du() { echo "12582912 $2"; }'                       # 12 GB in KiB
    r = _preflight(tmp_path, 5, stub_du)
    assert r.returncode == 0 and "about 4 GB" in r.stdout and "NOT ENOUGH" not in r.stdout
    r = _preflight(tmp_path, 1, stub_du)
    assert r.returncode == 1 and "NOT ENOUGH" in r.stdout


def test_a_second_install_into_the_same_root_is_refused(tmp_path):
    import fcntl
    tmp_path.joinpath(".install.lock").touch()
    with open(tmp_path / ".install.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        body = f'''preflight() {{ :; }}; models_to_license() {{ :; }}; STATE_DIR="{tmp_path}/state"
ensure_micromamba() {{ echo GOT_PAST_THE_LOCK; }}
main --root "{tmp_path}"'''
        r = _bash(INSTALL, body)
    assert r.returncode == 1 and "another install" in r.stderr
    assert "GOT_PAST_THE_LOCK" not in r.stdout


def test_install_caches_stay_under_the_root(tmp_path):
    body = f'''preflight() {{ :; }}; models_to_license() {{ :; }}; STATE_DIR="{tmp_path}/state"
ensure_micromamba() {{ echo "PIP=$PIP_CACHE_DIR"; exit 0; }}
main --root "{tmp_path}"'''
    assert f"PIP={tmp_path}/cache/pip" in _bash(INSTALL, body).stdout
    r = _bash(INSTALL, f'ROOT="{tmp_path}"; CUDA_DIR=""; app_env; '
                       'echo "CUPY=$CUPY_CACHE_DIR NV=$CUDA_CACHE_PATH MPL=$MPLCONFIGDIR KERAS=$KERAS_HOME"')
    assert (f"CUPY={tmp_path}/cache/cupy NV={tmp_path}/cache/nv MPL={tmp_path}/cache/matplotlib "
            f"KERAS={tmp_path}/keras") in r.stdout


def test_a_manual_release_run_never_publishes():
    wf = (REPO / ".github" / "workflows" / "fusion_viewer_core_release.yml").read_text(encoding="utf-8")
    assert "if: github.event_name == 'push' && github.ref_type == 'tag'" in wf


# ── viewer core ───────────────────────────────────────────────────────────
def test_wheels_lock_lookup(tmp_path):
    lock = tmp_path / "wheels.lock"
    lock.write_text("# version platform file sha256\n"
                    "0.2.0 linux fusion_viewer_core-0.2.0-cp310-abi3-manylinux_2_28_x86_64.whl aaa\n"
                    "0.2.0 windows fusion_viewer_core-0.2.0-cp310-abi3-win_amd64.whl bbb\n")
    r = _bash(INSTALL, f'WHEELS_LOCK="{lock}"; wheels_lock_entry 0.2.0 linux; echo "|"; wheels_lock_entry 0.3.0 linux')
    assert r.stdout.split("|")[0].strip() == "fusion_viewer_core-0.2.0-cp310-abi3-manylinux_2_28_x86_64.whl aaa"
    assert r.stdout.split("|")[1].strip() == ""


def test_a_development_version_needs_build_core(tmp_path):
    body = f'''WHEELS_LOCK="{tmp_path}/none"; BUILD_CORE=0; ENV_PREFIX="{tmp_path}"; ROOT="{tmp_path}"
installed_core() {{ echo ""; }}
ensure_core'''
    r = _bash(INSTALL, body)
    assert r.returncode == 1 and "--build-core" in r.stderr


# ── the package link ──────────────────────────────────────────────────────
def test_the_package_link_is_made_kept_and_a_foreign_one_refused(tmp_path):
    state = tmp_path / "state"
    body = f'STATE_DIR="{state}"; ensure_alias'
    assert _bash(INSTALL, body).returncode == 0
    link = state / "pkg" / "block01"
    assert link.is_symlink() and link.resolve() == REPO
    assert _bash(INSTALL, body).returncode == 0                 # kept
    link.unlink()
    link.symlink_to(tmp_path)
    r = _bash(INSTALL, body)
    assert r.returncode == 1 and "left untouched" in r.stderr
    assert link.resolve() == tmp_path


# ── the launcher ──────────────────────────────────────────────────────────
def _launcher_checkout(tmp_path):
    """A stand-in checkout: the launcher script and a state dir."""
    co = tmp_path / "co"
    (co / "scripts").mkdir(parents=True)
    shutil.copy2(LAUNCH, co / "scripts" / "block01")
    state = co / ".block01"
    (state / "pkg").mkdir(parents=True)
    (state / "run").mkdir()
    return co, state


def _fake_prefix(tmp_path, answer):
    prefix = tmp_path / "env"
    (prefix / "bin").mkdir(parents=True)
    py = prefix / "bin" / "python"
    py.write_text(f"#!/bin/sh\necho '{answer}'\n")
    py.chmod(0o755)
    (prefix / ".block01_install_ok").write_text("core 0.2.0")
    return prefix


def _record(state, checkout, prefix):
    (state / "install.env").write_text(f"BLOCK01_CHECKOUT={checkout}\nBLOCK01_PREFIX={prefix}\n")


def _check(co):
    return _bash(co / "scripts" / "block01", "check_install && echo PASSED")


def test_the_launcher_refusals(tmp_path):
    co, state = _launcher_checkout(tmp_path)
    r = _check(co)
    assert r.returncode == 1 and "not installed for this checkout" in r.stderr
    prefix = _fake_prefix(tmp_path, "")
    _record(state, tmp_path / "other", prefix)
    r = _check(co)
    assert r.returncode == 1 and "another checkout" in r.stderr
    _record(state, co, prefix)
    r = _check(co)
    assert r.returncode == 1 and "does not point at this checkout" in r.stderr
    (state / "pkg" / "block01").symlink_to(co)
    assert "PASSED" in _check(co).stdout
    (prefix / ".block01_install_ok").unlink()
    r = _check(co)
    assert r.returncode == 1 and "never finished" in r.stderr


def test_the_launcher_refuses_an_incompatible_core(tmp_path):
    co, state = _launcher_checkout(tmp_path)
    (state / "pkg" / "block01").symlink_to(co)
    prefix = _fake_prefix(tmp_path, "fusion_viewer_core 0.1.0 has no CORE_API, this code needs 1")
    _record(state, co, prefix)
    r = _check(co)
    assert r.returncode == 1 and "does not fit this code" in r.stderr and "has no CORE_API" in r.stderr


# ── the SpatialData environment (docs/v17_spatialdata_env_install_plan.md) ──
def _ensure_sd(root, extra="", python_ok=True):
    """`ensure_spatialdata_environment` with micromamba / pip stubbed; the
    created prefix gets a stand-in python answering the import check."""
    answer = 'echo 0.5.0' if python_ok else 'echo "No module named spatialdata" >&2; exit 1'
    body = f'''
ROOT="{root}"
STATE_DIR="{root}/state"; mkdir -p "$STATE_DIR/run"
MAMBA=stub_mamba
sd_lock_id() {{ echo 5d5d5d5d; }}
make_python() {{ mkdir -p "$1/bin"; printf '#!/bin/sh\\n{answer}\\n' > "$1/bin/python"; chmod +x "$1/bin/python"; }}
run() {{ echo "run $*" >> "{root}/calls"; [ "$1" = stub_mamba ] && make_python "$(sd_env_prefix)"; return 0; }}
{extra}
ensure_spatialdata_environment
echo "SD=$SD_PREFIX"
'''
    return _bash(INSTALL, body)


def test_the_spatialdata_environment_is_created_owned_and_checked(tmp_path):
    r = _ensure_sd(tmp_path)
    assert r.returncode == 0, r.stderr
    prefix = tmp_path / "envs" / "block01_spatialdata-5d5d5d5d"
    assert (tmp_path / "envs" / "block01_spatialdata-5d5d5d5d.owner").read_text(encoding="utf-8").startswith("created by")
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "block01_spatialdata/conda-linux-64.lock" in calls
    assert "pip install -q --no-deps -r" in calls and "block01_spatialdata/requirements-pip.txt" in calls
    assert "pip check" in calls
    assert (prefix / ".block01_env_ok").exists() and (prefix / ".block01_install_ok").exists()
    assert "spatialdata 0.5.0 ready" in r.stdout and f"SD={prefix}" in r.stdout


def test_the_spatialdata_environment_states(tmp_path):
    envs = tmp_path / "envs"
    prefix = envs / "block01_spatialdata-5d5d5d5d"
    # interrupted: removed and redone
    (prefix / "half").mkdir(parents=True)
    (envs / "block01_spatialdata-5d5d5d5d.owner").write_text("old")
    r = _ensure_sd(tmp_path)
    assert r.returncode == 0 and "stopped half-way" in r.stdout and not (prefix / "half").exists()
    # built (environment done, a later step stopped): kept, checked, completed
    (prefix / ".block01_install_ok").unlink()
    (tmp_path / "calls").unlink()
    r = _ensure_sd(tmp_path)
    assert r.returncode == 0 and "already installed" in r.stdout and not (tmp_path / "calls").exists()
    assert (prefix / ".block01_install_ok").exists()
    # complete: kept, only checked again
    r = _ensure_sd(tmp_path)
    assert r.returncode == 0 and "already installed" in r.stdout and not (tmp_path / "calls").exists()
    # foreign: refused, untouched
    shutil.rmtree(envs)
    (prefix / "lib").mkdir(parents=True)
    before = _tree(envs)
    r = _ensure_sd(tmp_path)
    assert r.returncode == 1 and "was not created by this installer" in r.stderr and "--no-spatialdata" in r.stderr
    assert _tree(envs) == before


def test_a_spatialdata_environment_that_does_not_import_stops_the_install(tmp_path):
    r = _ensure_sd(tmp_path, python_ok=False)
    assert r.returncode == 1 and "spatialdata does not import" in r.stderr
    prefix = tmp_path / "envs" / "block01_spatialdata-5d5d5d5d"
    assert not (prefix / ".block01_install_ok").exists()


def test_no_spatialdata_skips_it(tmp_path):
    r = _ensure_sd(tmp_path, "NO_SPATIALDATA=1")
    assert r.returncode == 0 and "skipped (--no-spatialdata)" in r.stdout and "SD=\n" in r.stdout + "\n"
    assert not (tmp_path / "envs").exists() and not (tmp_path / "calls").exists()


@pytest.mark.parametrize("sd", ["", "/x/envs/block01_spatialdata-5d5d5d5d"])
def test_the_record_names_the_spatialdata_python_only_when_installed(tmp_path, sd):
    state = tmp_path / "state"
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    prefix.mkdir(parents=True)
    body = f'''
ROOT="{tmp_path}"; STATE_DIR="{state}"; mkdir -p "$STATE_DIR"
ENV_PREFIX="{prefix}"; CUDA_DIR=""; SD_PREFIX="{sd}"
model_dir() {{ echo "{tmp_path}/models/$1"; }}
write_record
'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stderr
    rec = (state / "install.env").read_text(encoding="utf-8")
    if sd:
        assert f"export BLOCK01_SPATIALDATA_PYTHON={sd}/bin/python" in rec
    else:
        assert "BLOCK01_SPATIALDATA_PYTHON" not in rec


def test_the_space_check_counts_the_spatialdata_environment_left(tmp_path):
    prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    prefix.mkdir(parents=True)
    (tmp_path / "envs" / "fusion_mesmer-abcdef12.owner").write_text("ours")
    (prefix / ".block01_env_ok").write_text("t")
    (tmp_path / "models" / "cellpose_cpsam").mkdir(parents=True)
    (tmp_path / "models" / "cellpose_cpsam" / "cpsam").write_bytes(b"x")
    stub = "sd_lock_id() { echo 5d5d5d5d; }"
    r = _preflight(tmp_path, 5, stub)
    assert r.returncode == 0 and "about 3 GB" in r.stdout                  # absent: 2
    sd = tmp_path / "envs" / "block01_spatialdata-5d5d5d5d"
    sd.mkdir()
    (tmp_path / "envs" / "block01_spatialdata-5d5d5d5d.owner").write_text("ours")
    r = _preflight(tmp_path, 5, stub + '\ndu() { echo "1048576 $2"; }')   # interrupted, 1 GB back
    assert r.returncode == 0 and "about 2 GB" in r.stdout
    (sd / ".block01_env_ok").write_text("t")
    r = _preflight(tmp_path, 5, stub)
    assert r.returncode == 0 and "about 1 GB" in r.stdout                  # built: 0


_LINUX = '''uname() { case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac; }
flock() { :; }
getconf() { echo "glibc %s"; }'''


@pytest.mark.parametrize("version,route", [
    ("2.27", "refused"),
    ("2.28", "6.9.3"), ("2.31", "6.9.3"), ("2.33", "6.9.3"),
    ("2.34", "6.11.3"), ("2.35", "6.11.3"),
])
def test_the_glibc_decides_the_qt_route(tmp_path, version, route):
    """docs/v17_older_linux_qt_plan.md §3.2: PySide6 6.10+ has Linux wheels
    only for glibc >= 2.34; 2.28-2.33 get 6.9.3 (the last release built for
    2.28) in an environment of their own; older is refused at the pre-check,
    not half-way through pip."""
    r = _preflight(tmp_path, 100, _LINUX % version, after='echo "ROUTE=$OLDER_QT $(env_prefix)"')
    # the Computer line names the glibc; a note under it names an older Qt route
    assert f"glibc {version}" in r.stdout or f"glibc: {version}" in r.stdout, r.stdout
    line = next((l for l in r.stdout.splitlines() if "glibc: " in l), "")
    if route == "refused":
        assert "too old (2.28 or newer needed" in line and r.returncode == 1, r.stdout
        return
    assert "too old" not in r.stdout and r.returncode == 0, r.stdout + r.stderr
    if route == "6.9.3":
        assert "uses PySide6 6.9.3 (the last release built for this glibc)" in line
        assert f"ROUTE=1 {tmp_path}/envs/fusion_mesmer-qt69-abcdef12" in r.stdout
    else:
        assert "PySide6" not in line
        assert f"ROUTE=0 {tmp_path}/envs/fusion_mesmer-abcdef12" in r.stdout


def test_older_qt_picks_the_6_9_route_on_a_newer_glibc(tmp_path):
    body = f'''{_LINUX % "2.35"}
curl() {{ return 0; }}
check_system_libs() {{ return 0; }}
free_gb() {{ echo 100; }}
lock_id() {{ echo abcdef12; }}
main --root "{tmp_path}" --check-only --older-qt'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "uses PySide6 6.9.3 (--older-qt)" in r.stdout and "check only: nothing was written" in r.stdout
    assert not (tmp_path / "envs").exists()


def _ids():
    spec = REPO / "envs" / "fusion_mesmer"
    main = (spec / "conda-linux-64.lock").read_bytes() + (spec / "requirements-pip.txt").read_bytes()
    older = main + (spec / "requirements-pip-older-linux.txt").read_bytes()
    return hashlib.sha256(main).hexdigest()[:8], hashlib.sha256(older).hexdigest()[:8]


def test_each_route_has_its_own_prefix_and_the_6_11_one_is_unchanged(tmp_path):
    main_id, older_id = _ids()
    r = _bash(INSTALL, f'ROOT="{tmp_path}"; env_prefix; OLDER_QT=1; env_prefix; qt_version; OLDER_QT=0; qt_version')
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [f"{tmp_path}/envs/fusion_mesmer-{main_id}",          # as before this block
                                f"{tmp_path}/envs/fusion_mesmer-qt69-{older_id}", "6.9.3", "6.11.3"]
    assert main_id != older_id


def test_the_older_pin_list_is_exactly_the_two_qt_pins():
    lines = [l for l in (REPO / "envs/fusion_mesmer/requirements-pip-older-linux.txt")
             .read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert lines == ["pyside6-essentials==6.9.3", "shiboken6==6.9.3"]


def _spec(tmp_path, pip_lines):
    spec = tmp_path / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "requirements-pip.txt").write_text("\n".join(pip_lines) + "\n")
    (spec / "requirements-pip-older-linux.txt").write_text(
        "# why\npyside6-essentials==6.9.3\nshiboken6==6.9.3\n")
    (spec / "conda-linux-64.lock").write_text("@EXPLICIT\n")
    return spec


def test_the_6_9_pip_list_replaces_exactly_the_two_qt_lines(tmp_path):
    main = ["# pip part", "--extra-index-url https://x", "numpy==1.26.4", "pyside6-essentials==6.11.3",
            "scipy==1.15.3", "shiboken6==6.11.3", "zarr==2.18.3"]
    spec = _spec(tmp_path, main)
    out = tmp_path / "out.txt"
    r = _bash(INSTALL, f'SPEC="{spec}"; OLDER_PINS="{spec}/requirements-pip-older-linux.txt"; write_older_pip_list "{out}"')
    assert r.returncode == 0, r.stderr
    got = out.read_text(encoding="utf-8").splitlines()
    assert got == [l for l in main if "6.11.3" not in l] + ["pyside6-essentials==6.9.3", "shiboken6==6.9.3"]
    # a main list without one of them (or with two) stops: the pins no longer fit it
    for broken in ([l for l in main if not l.startswith("shiboken6")], main + ["shiboken6==6.11.2"]):
        spec = _spec(tmp_path, broken)
        r = _bash(INSTALL, f'SPEC="{spec}"; OLDER_PINS="{spec}/requirements-pip-older-linux.txt"; write_older_pip_list "{out}"')
        assert r.returncode == 1 and "the pip list changed" in r.stderr and "shiboken6" in r.stderr


@pytest.mark.parametrize("name", ["pyside6-essentials", "shiboken6"])
@pytest.mark.parametrize("count", [0, 2])
def test_a_changed_main_list_stops_the_6_9_route_before_any_environment(tmp_path, name, count):
    main = [l for l in (REPO / "envs/fusion_mesmer/requirements-pip.txt").read_text(encoding="utf-8").splitlines()
            if not l.lower().startswith(name + "==")] + [f"{name}==6.11.{i}" for i in range(count)]
    spec = _spec(tmp_path, main)
    r = _ensure_env(tmp_path, f'OLDER_QT=1; SPEC="{spec}"; OLDER_PINS="{spec}/requirements-pip-older-linux.txt"')
    assert r.returncode == 1 and "the pip list changed" in r.stderr and name in r.stderr
    assert not (tmp_path / "calls").exists()                       # no micromamba, no pip
    assert not (tmp_path / "envs" / "fusion_mesmer-qt69-abcdef12").exists()


@pytest.mark.parametrize("older", [0, 1])
def test_ensure_environment_gives_pip_the_routes_list(tmp_path, older):
    r = _ensure_env(tmp_path, f"OLDER_QT={older}")
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    if older:
        prefix = tmp_path / "envs" / "fusion_mesmer-qt69-abcdef12"
        listing = pathlib.Path(f"{prefix}.requirements-pip.txt")
        assert f"pip install -q --no-deps -r {listing}" in calls
        text = listing.read_text(encoding="utf-8")
        assert "pyside6-essentials==6.9.3" in text and "6.11.3" not in text
        assert "torch==2.5.1+cu121" in text
        assert "PySide6 6.9.3" in r.stdout
    else:
        prefix = tmp_path / "envs" / "fusion_mesmer-abcdef12"
        assert f"pip install -q --no-deps -r {REPO}/envs/fusion_mesmer/requirements-pip.txt" in calls
        assert not list((tmp_path / "envs").glob("*.requirements-pip.txt"))
    assert f"PREFIX={prefix}" in r.stdout and (prefix / ".block01_env_ok").exists()
    assert "pip check" in calls


def _complete(path):
    path.mkdir(parents=True)
    path.with_name(path.name + ".owner").write_text("ours")
    (path / ".block01_install_ok").write_text("core 0.2.1")


@pytest.mark.parametrize("state", ["absent", "interrupted", "foreign", "built", "complete"])
def test_the_6_9_route_keeps_to_its_own_prefix(tmp_path, state):
    """With a complete 6.11.3 environment in the same root, a 6.9.3-route
    run creates, redoes, refuses or keeps only its own `qt69` prefix."""
    other = tmp_path / "envs" / "fusion_mesmer-abcdef12"
    _complete(other)
    before = _tree(other)
    mine = tmp_path / "envs" / "fusion_mesmer-qt69-abcdef12"
    owner = tmp_path / "envs" / "fusion_mesmer-qt69-abcdef12.owner"
    if state != "absent":
        (mine / "half").mkdir(parents=True)
        if state != "foreign":
            owner.write_text("ours")
        if state == "built":
            (mine / ".block01_env_ok").write_text("t")
        if state == "complete":
            (mine / ".block01_install_ok").write_text("core 0.2.1")
    r = _ensure_env(tmp_path, "OLDER_QT=1")
    assert _tree(other) == before and other.with_name(other.name + ".owner").read_text() == "ours"
    calls = (tmp_path / "calls").read_text(encoding="utf-8") if (tmp_path / "calls").exists() else ""
    if state in ("absent", "interrupted"):
        assert r.returncode == 0, r.stderr
        assert f"create -y -q -p {mine}" in calls and not (mine / "half").exists()
        assert (mine / ".block01_env_ok").exists()
    elif state == "foreign":
        assert r.returncode == 1 and "was not created by this installer" in r.stderr
        assert (mine / "half").exists() and calls == ""
    else:
        assert r.returncode == 0 and calls == "" and (mine / "half").exists()


def test_the_removal_guard_takes_only_the_two_names(tmp_path):
    odd = tmp_path / "envs" / "fusion_mesmer-qt70-abcdef12"
    (odd / "half").mkdir(parents=True)
    odd.with_name(odd.name + ".owner").write_text("ours")
    r = _ensure_env(tmp_path, f'env_prefix() {{ echo "{odd}"; }}')
    assert r.returncode == 1 and "refusing to remove" in r.stderr
    assert (odd / "half").exists()


def test_the_space_check_counts_the_6_9_environment_left(tmp_path):
    _complete(tmp_path / "envs" / "fusion_mesmer-abcdef12")          # the 6.11.3 one does not count
    (tmp_path / "models" / "cellpose_cpsam").mkdir(parents=True)
    (tmp_path / "models" / "cellpose_cpsam" / "cpsam").write_bytes(b"x")
    stub = _LINUX % "2.31" + "\nsd_lock_id() { echo 5d5d5d5d; }\nNO_SPATIALDATA=1"
    r = _preflight(tmp_path, 100, stub)
    assert r.returncode == 0 and "about 14 GB" in r.stdout, r.stdout
    mine = tmp_path / "envs" / "fusion_mesmer-qt69-abcdef12"
    mine.mkdir()
    mine.with_name(mine.name + ".owner").write_text("ours")
    r = _preflight(tmp_path, 100, stub + '\ndu() { echo "12582912 $2"; }')   # interrupted: 12 GB back
    assert r.returncode == 0 and "about 2 GB" in r.stdout, r.stdout
    (mine / ".block01_env_ok").write_text("t")
    r = _preflight(tmp_path, 100, stub)
    assert r.returncode == 0 and "about 1 GB" in r.stdout, r.stdout
    (mine / ".block01_install_ok").write_text("core 0.2.1")
    r = _preflight(tmp_path, 100, stub)
    assert r.returncode == 0 and "about 1 GB" in r.stdout, r.stdout
    (mine / ".block01_install_ok").unlink()
    (mine / ".block01_env_ok").unlink()
    r = _preflight(tmp_path, 1, stub + '\ndu() { echo "0 $2"; }')     # interrupted, nothing back, 1 GB free
    assert r.returncode == 1 and "NOT ENOUGH" in r.stdout, r.stdout


@pytest.mark.parametrize("older,version", [(0, "6.11.3"), (1, "6.9.3")])
def test_the_record_names_the_qt_version(tmp_path, older, version):
    state = tmp_path / "state"
    prefix = tmp_path / "envs" / "p"
    prefix.mkdir(parents=True)
    body = f'''
ROOT="{tmp_path}"; STATE_DIR="{state}"; mkdir -p "$STATE_DIR"; OLDER_QT={older}
ENV_PREFIX="{prefix}"; CUDA_DIR=""; SD_PREFIX=""
model_dir() {{ echo "{tmp_path}/models/$1"; }}
write_record
'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stderr
    assert f"BLOCK01_QT_VERSION={version}\n" in (state / "install.env").read_text(encoding="utf-8")


def test_the_missing_library_hint_names_epel_for_rhel(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ldconfig").write_text("#!/bin/sh\necho\n")
    (fake / "ldconfig").chmod(0o755)
    r = _bash(INSTALL, "check_system_libs", env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 1 and "libxcb-cursor.so.0" in r.stdout
    assert "RHEL/Rocky: sudo dnf install" in r.stdout and "xcb-util-cursor, from EPEL" in r.stdout


# ── the terminal output (backlog 23, user ruling 2026-11-15) ────────────────
SH = INSTALL.read_text(encoding="utf-8")


def test_tools_write_to_the_log_not_the_terminal(tmp_path):
    """Every tool's own output (micromamba, pip, the helpers) goes to the
    log; the terminal shows one line per step."""
    log = tmp_path / "install.log"
    r = _bash(INSTALL, f'LOG="{log}"; run sh -c "echo TOOL-OUT; echo TOOL-ERR >&2"; '
                       'step_begin Thing; step_done "3 s"')
    assert r.returncode == 0, r.stderr
    assert "TOOL-" not in r.stdout + r.stderr
    assert "TOOL-OUT" in log.read_text() and "TOOL-ERR" in log.read_text()
    assert "[ok] Thing  3 s" in r.stdout                   # not a terminal: ASCII, no colour
    assert "\033[" not in r.stdout and "✓" not in r.stdout
    # a failure names the step, says what to do and shows the end of the log
    r = _bash(INSTALL, f'LOG="{log}"; STEP="4. a step"; step_begin Thing; run sh -c "echo WHY-IT-BROKE; exit 3"')
    assert r.returncode == 1
    assert "[x] Thing" in r.stderr and "STOPPED at: 4. a step" in r.stderr and "what to do:" in r.stderr
    assert "WHY-IT-BROKE" in r.stderr and f"log: {log}" in r.stderr


def test_the_conda_part_is_conda_forge_only_and_pip_says_nothing_of_path():
    creates = [l for l in SH.splitlines() if " create -y -q -p " in l]
    assert len(creates) == 2 and all("--override-channels" in l for l in creates)
    pips = [l for l in SH.splitlines() if "-m pip install" in l]
    assert pips and all("--no-warn-script-location" in l for l in pips), pips


def test_one_licence_card_for_every_model_to_install(tmp_path):
    """One card, one answer, before anything is installed; a model already
    there (or given with --models) is not on it; a model on the card is not
    asked again."""
    body = f'''ROOT="{tmp_path}"; ACCEPT_LICENCES=1
echo "TODO=$(models_to_license | tr '\\n' ' ')"
mkdir -p "$ROOT/models/cellpose_cpsam"; : > "$ROOT/models/cellpose_cpsam/cpsam"
echo "TODO=$(models_to_license | tr '\\n' ' ')"
confirm_licences $(models_to_license)
echo AFTER; accept_licence mesmer_multiplex_segmentation; echo END'''
    r = _bash(INSTALL, body)
    assert r.returncode == 0, r.stderr
    todo = [l for l in r.stdout.splitlines() if l.startswith("TODO=")]
    assert todo == ["TODO=cellpose_cpsam stardist_2D_versatile_fluo mesmer_multiplex_segmentation ",
                    "TODO=stardist_2D_versatile_fluo mesmer_multiplex_segmentation "]
    assert r.stdout.count("Model licences") == 2                         # the card's title, the answer
    card = r.stdout.split("AFTER")[0]
    assert "StarDist" in card and "NON-COMMERCIAL" in card and "Cellpose-SAM" not in card
    assert "accepted (--accept-model-licences)" in card
    assert r.stdout.split("AFTER")[1].strip() == "END"                   # not asked again
    assert "confirm_licences $(models_to_license)" in SH
    assert SH.index("confirm_licences $(models_to_license)") < SH.index("    ensure_micromamba\n")


def _fetch(tmp_path, codes, extra=""):
    """`fetch` with curl replaced: it answers the exit codes in turn and
    records its arguments; the waits are 0."""
    body = f'''LOG="{tmp_path}/log"; FETCH_WAITS=(0 0 0)
curl() {{ n=$(cat "{tmp_path}/n" 2>/dev/null || echo 0); echo $((n + 1)) > "{tmp_path}/n"
         echo "ARGS $*" >> "{tmp_path}/args"; echo "curl: (99) its own words" >&2
         codes=({" ".join(map(str, codes))}); rc=${{codes[$n]}}
         [ "$rc" = 0 ] && echo data >> "${{10}}"; return "$rc"; }}
{extra}
fetch https://example.org/f "{tmp_path}/f.part" F && echo "RC=0" || echo "RC=$?"'''
    r = _bash(INSTALL, body, cwd=tmp_path)
    attempts = int((tmp_path / "n").read_text()) if (tmp_path / "n").exists() else 0
    return r, attempts


def test_downloads_retry_resume_and_keep_curl_quiet(tmp_path):
    r, n = _fetch(tmp_path, [28, 56, 0])
    assert "RC=0" in r.stdout and n == 3
    assert "network problem (curl 28), retrying 2/4" in r.stdout
    assert "network problem (curl 56), retrying 3/4" in r.stdout
    assert "its own words" not in r.stdout + r.stderr                  # curl's words: the log only
    assert "its own words" in (tmp_path / "log").read_text()
    args = (tmp_path / "args").read_text().splitlines()
    assert all("-C -" in a and "-sS" in a and "--retry 2" in a for a in args)   # resumed, silent
    # an answer such as 404 / 403 is not retried; 4 network failures give up
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    r, n = _fetch(tmp_path / "a", [22, 0])
    assert "RC=22" in r.stdout and n == 1
    r, n = _fetch(tmp_path / "b", [28, 28, 28, 28, 0])
    assert "RC=28" in r.stdout and n == 4
    # no range support (33): the part is dropped and fetched from 0
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "f.part").write_text("half")
    r, n = _fetch(tmp_path / "c", [33, 0])
    assert "RC=0" in r.stdout and n == 2 and (tmp_path / "c" / "f.part").read_text() == "data\n"
    # no curl progress bar of its own anywhere
    assert "--progress-bar" not in SH and '"-#"' not in SH and "progress_args" not in SH


def test_the_self_test_is_a_table():
    r = _bash(INSTALL, "selftest_table $'cellpose\\tok\\tcuda:0\\nstardist\\tok\\tgpu\\nmesmer\\tFAILED\\t'")
    lines = [l.split() for l in r.stdout.splitlines()]
    assert lines == [["Self-test", "GPU"], ["Cellpose", "ok"], ["StarDist", "ok"], ["Mesmer", "FAILED"]]
    r = _bash(INSTALL, "selftest_table $'cellpose\\tok\\tcpu'")
    assert r.stdout.split()[:2] == ["Self-test", "CPU"]
    assert '"$PY" -m seg_runner.selftest' in SH and "printf '%s\\n' \"$out\"\n" not in SH   # no JSON on screen


def test_a_running_step_is_updated_in_place():
    """The user's review (2026-11-15): a running step is one line, updated in
    place; a note, a warning or a download's progress never ends it early."""
    assert "out() { unpend; printf '%s\\n' \"$*\"; repend; }" in SH
    assert "warn() { unpend;" in SH and "repend; log \"WARNING" in SH
    fetch = SH[SH.index("fetch() {"):SH.index("# download URL DEST")]
    assert "flush" not in fetch and "repend" in fetch
    fail = SH[SH.index("fail() {"):SH.index("run() {")]
    assert "unpend; PENDING=0" in fail


def test_the_running_bullet_blinks(tmp_path):
    """The user's review (2026-11-15): while a step's command runs, its bullet
    blinks; the command's output still goes only to the log, its failure
    still stops the install."""
    run = SH[SH.index("run() {"):SH.index("sha256_of()")]
    assert 'blink "$pid"' in run and 'wait "$pid" || rc=$?' in run and "fail" in run
    blink = SH[SH.index("blink() {"):SH.index("run() {")]
    assert "sleep 1\n" in blink and 'kill -0 "$pid"' in blink
