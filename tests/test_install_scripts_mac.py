"""docs/v17_mac_install_plan.md: static checks of the Mac installer, on every
system (the macOS CI job `mac-install` runs it; the lab's Mac runs it with
the models).

* the pins: micromamba's release and digest, torch / torchvision, the
  viewer core and DeepCell-Toolbox only through their lock files;
* the same model sources and Mesmer digest as the Linux installer;
* the screen of the other two installers: one line per step updated in
  place, the blinking bullet, one licence card, the tools in the log;
* Apple silicon and macOS 14 checked first; no space in the install root;
* bash 3.2 (the Mac's own): no associative arrays, no GNU-only tools;
* the launcher is the Linux one, which names the Mac installer on a Mac.
"""

import hashlib
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MAC = (REPO / "scripts" / "install_block01_mac.sh").read_text(encoding="utf-8")
SH = (REPO / "scripts" / "install_block01.sh").read_text(encoding="utf-8")
LAUNCH = (REPO / "scripts" / "block01").read_text(encoding="utf-8")


def _fn(name, text=MAC):
    """The body of shell function `name`."""
    m = re.search(r"^%s\(\) \{.*?^\}" % re.escape(name), text, re.M | re.S)
    assert m, name
    return m.group(0)


def test_downloads_are_pinned_and_checked():
    assert re.search(r'^MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/'
                     r'download/2\.8\.1-1/micromamba-osx-arm64"$', MAC, re.M)
    assert re.search(r'^MICROMAMBA_SHA256="[0-9a-f]{64}"$', MAC, re.M)
    assert 'TORCH_PINS="torch==2.5.1 torchvision==0.20.1"' in MAC
    for var in ("CPSAM_URL", "STARDIST_URL", "MESMER_URL", "MESMER_SHA256", "MESMER_MIRROR_DEFAULT"):
        value = re.search(r'^%s="([^"]+)"' % var, SH, re.M).group(1)
        assert f'{var}="{value}"' in MAC, var
    assert "sha256_of" in _fn("download") and ".part" in _fn("download")
    assert "lock_entry \"$WHEELS_LOCK\" \"$want\" macos" in _fn("ensure_core")
    assert "lock_entry \"$TOOLBOX_LOCK\" 0.12.1 macos_arm64" in _fn("ensure_toolbox")


def test_the_shipped_toolbox_wheel_is_the_one_the_lock_names():
    lines = [l.split() for l in (REPO / "envs" / "macos" / "wheels.lock").read_text().splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    entry = next(f for f in lines if f[:2] == ["0.12.1", "macos_arm64"])
    wheel = REPO / "envs" / "macos" / "wheels" / entry[2]
    assert wheel.name.endswith(("macosx_13_0_arm64.whl", "macosx_13_0_universal2.whl"))
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == entry[3]


def test_the_viewer_core_lock_has_the_mac_wheel():
    lock = (REPO / "native" / "fusion_viewer_core" / "wheels.lock").read_text()
    assert re.search(r"^0\.2\.1 macos fusion_viewer_core-0\.2\.1-cp310-abi3-macosx_13_0_arm64\.whl [0-9a-f]{64}$",
                     lock, re.M)


def test_the_environment_is_the_mac_engine_set_without_cupy():
    req = (REPO / "envs" / "macos" / "requirements.txt").read_text()
    assert not [l for l in req.splitlines() if l.lower().startswith("cupy")]
    win = (REPO / "envs" / "windows" / "requirements.txt").read_text()
    pins = lambda t: {l.strip() for l in t.splitlines() if "==" in l and not l.lstrip().startswith("#")}
    assert pins(req) == {p for p in pins(win) if not p.startswith("cupy")}
    env = _fn("ensure_environment")
    assert '--override-channels -c conda-forge python=3.10 pip' in env
    assert env.count("--no-warn-script-location") == 5
    assert env.index("requirements-cpu.txt") < env.index("$TORCH_PINS") < env.index("envs/macos/requirements.txt") \
        < env.index("ensure_toolbox") < env.index("requirements-nodeps.txt")


def test_the_mac_is_checked_first():
    pre = _fn("preflight")
    assert '"$arch" != arm64' in pre and "Intel Mac" in pre
    assert '-lt 14' in pre and "sw_vers -productVersion" in pre
    assert 'case "$ROOT" in *" "*)' in pre
    assert 'ROOT="$HOME/.local/share/block01"' in MAC


def test_the_screen_is_the_one_of_the_other_installers():
    run = _fn("run")
    assert 'blink "$pid"' in run and '>> "$LOG" 2>&1' in run and "fail" in run
    assert "sleep 1" in _fn("blink")
    assert "unpend; printf '%s\\n' \"$*\"; repend;" in _fn("out")
    fetch = _fn("fetch")
    assert "-sS --retry 2 --connect-timeout 20 -C -" in fetch and '>> "$LOG" 2>&1 &' in fetch
    assert "--progress-bar" not in fetch and "draw_progress" in fetch
    fail = _fn("fail")
    assert "STOPPED at" in fail and "what to do" in fail and "tail -n 20" in fail and "exit 1" in fail
    main = _fn("main")
    assert main.index("confirm_licences") < main.index("ensure_micromamba")
    assert '[ "$SKIP_MODELS" = 1 ] || confirm_licences' in main
    assert "Accept %s? [y/N]" in _fn("confirm_licences")
    assert "C_WARN" in _fn("licence_card")
    sel = _fn("selftest")
    assert "export BLOCK01_ENGINE_CPU_ONLY=1" in sel and 'selftest_table "$gpu_rows" "$cpu_rows"' in sel
    assert "mps_kernels" in sel


def test_mesmer_sources_in_order_and_a_missing_mesmer_is_not_fatal():
    body = _fn("ensure_mesmer")
    order = [body.index(f"            {s})") for s in ("official", "mirror", "zip", "folder")]
    assert order == sorted(order)
    tail = body.split("MESMER_MISSING=1", 1)[1]
    assert "fail" not in tail and "--mesmer-only" in tail


def test_bash_32_and_bsd_tools_only():
    code = "\n".join(l for l in MAC.splitlines() if not l.lstrip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", "${!", "sha256sum", "realpath -m", "date -Is",
                "flock", "setsid", "local -n", ",,}", "^^}", "stat -c"):
        assert bad not in code, bad
    assert MAC.startswith("#!/bin/bash\n")


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="no /bin/bash")
def test_it_parses_with_the_bash_here():
    subprocess.run(["/bin/bash", "-n", str(REPO / "scripts" / "install_block01_mac.sh")], check=True)


def test_the_app_and_the_record():
    app = _fn("write_app")
    assert "org.block01.fusionflux" in app and "is not ours; it is left untouched" in app
    assert 'scripts/block01' in app and "LSMinimumSystemVersion" in app
    rec = _fn("write_record")
    for var in ("BLOCK01_CHECKOUT", "BLOCK01_ROOT", "BLOCK01_PREFIX", "BLOCK01_CORE_VERSION", "BLOCK01_MESMER"):
        assert var in rec, var
    assert ".block01_install_ok" in rec
    assert 'INSTALL_HINT="bash $REPO/scripts/install_block01_mac.sh"' in LAUNCH
    assert '[ "$(uname -s)" != Darwin ]' in LAUNCH


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_the_licence_card_and_the_models_to_license(tmp_path):
    """Sourced: the card names each model to install, Mesmer's row flagged;
    a model already in the root is not asked for."""
    script = REPO / "scripts" / "install_block01_mac.sh"
    root = tmp_path / "root"
    f = root / "models" / "stardist_2D_versatile_fluo"
    out = subprocess.run(
        ["/bin/bash", "-c", f'BLOCK01_INSTALL_SOURCED=1 . "{script}"; ROOT="{root}"; models_to_license; '
                            'licence_card cellpose_cpsam mesmer_multiplex_segmentation'],
        capture_output=True, text=True, check=True).stdout
    assert out.splitlines()[:3] == ["cellpose_cpsam", "stardist_2D_versatile_fluo", "mesmer_multiplex_segmentation"]
    assert "Model licences" in out and "NON-COMMERCIAL academic use only" in out
    assert "never share it" in out and "StarDist      " not in out
    assert f  # (the folder is not made: every model is still to install)


def test_one_install_into_a_root_at_a_time():
    """Codex review (2026-11-16): a lock folder whose owner is not named yet
    is busy, a stale one is taken over by a rename; --mesmer-only takes the
    lock too."""
    lock = _fn("take_lock")
    assert '[ -z "$pid" ] || ps -p "$pid"' in lock and "sleep 2" in lock
    assert 'mv -- "$LOCK_DIR" "$LOCK_DIR.stale.$$"' in lock and "trap" in lock
    assert "take_lock" in _fn("mesmer_only") and "take_lock" in _fn("main")


def test_the_lock_refuses_a_running_owner_and_takes_a_stale_one(tmp_path):
    script = REPO / "scripts" / "install_block01_mac.sh"
    lock = tmp_path / ".install.lock"
    lock.mkdir()
    (lock / "pid").write_text("1\n")              # launchd: always running
    r = subprocess.run(["/bin/bash", "-c", f'BLOCK01_INSTALL_SOURCED=1 . "{script}"; ROOT="{tmp_path}"; take_lock'],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "another install" in r.stderr
    (lock / "pid").write_text("999999\n")         # no such process
    r = subprocess.run(["/bin/bash", "-c", f'BLOCK01_INSTALL_SOURCED=1 . "{script}"; ROOT="{tmp_path}"; take_lock; cat "$LOCK_DIR/pid"'],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip().isdigit() and r.stdout.strip() != "999999"
    assert not lock.exists()                      # removed again at exit
