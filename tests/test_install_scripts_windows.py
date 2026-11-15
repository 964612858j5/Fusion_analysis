"""docs/v17_windows_native_plan.md W3: static checks of the Windows installer
and launcher, on every system (the Windows CI runs the installer itself,
job `windows-install`).

* the pins: micromamba's release and digest, torch / torchvision, the
  viewer core and DeepCell-Toolbox only through their lock files;
* every refusal names its step (`$script:Step` set in each step function);
* Mesmer: DeepCell's archive first (SPACEc's URL, its digest), then the
  mirror, the local archive, the folder; none: the install goes on and says
  `-MesmerOnly`; the import mode exists;
* the package link is a junction and nothing is written into the
  environment (no .pth); the launcher puts .block01\\pkg on PYTHONPATH,
  refuses another checkout's record and an unfinished install;
* the desktop shortcut; the launcher's .cmd calls the .ps1.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
INSTALL = (REPO / "scripts" / "install_block01.ps1").read_text(encoding="utf-8")
LAUNCH = (REPO / "scripts" / "block01.ps1").read_text(encoding="utf-8")
CMD = (REPO / "scripts" / "block01.cmd").read_text(encoding="utf-8")
SH = (REPO / "scripts" / "install_block01.sh").read_text(encoding="utf-8")


def _fn(name, text=INSTALL):
    """The body of PowerShell function `name`."""
    m = re.search(r"^function %s\b.*?(?=^function |\Z)" % re.escape(name), text, re.M | re.S)
    assert m, name
    return m.group(0)


def test_downloads_are_pinned_and_checked():
    assert re.search(r'\$MicromambaUrl = "https://github.com/mamba-org/micromamba-releases/releases/'
                     r'download/2\.8\.1-1/micromamba-win-64"', INSTALL)
    assert re.search(r'\$MicromambaSha = "[0-9a-f]{64}"', INSTALL)
    assert '"torch==2.5.1", "torchvision==0.20.1"' in INSTALL
    # the same model sources and Mesmer digest as the Linux installer
    for var in ("CPSAM_URL", "STARDIST_URL", "MESMER_URL", "MESMER_SHA256"):
        value = re.search(r'^%s="([^"]+)"' % var, SH, re.M).group(1)
        assert f'"{value}"' in INSTALL, var
    download = _fn("Download")
    assert "Sha256Of" in download and "Move-Item" in download and ".part" in download
    assert "LockEntry $WheelsLock" in _fn("EnsureCore")
    assert "LockEntry $ToolboxLock" in _fn("EnsureToolbox")


def test_every_step_sets_its_name():
    for name in ("Preflight", "EnsureMicromamba", "EnsureEnvironment", "EnsureCore", "EnsureModels",
                 "ConvertStardist", "EnsureLink", "Selftest", "WriteRecord", "MesmerOnlyInstall"):
        assert "$script:Step = " in _fn(name), name
    fail = _fn("Fail")
    assert "STOPPED at" in fail and "what to do" in fail and "exit 1" in fail


def test_mesmer_sources_in_order_and_a_missing_mesmer_is_not_fatal():
    body = _fn("EnsureMesmer")
    order = [body.index(f'"{s}" {{') for s in ("official", "mirror", "zip", "folder")]
    assert order == sorted(order)
    assert "$MesmerSha" in body                       # the official archive's digest
    assert "ModelState $key $stage" in body           # every source file by file
    assert "$script:MesmerMissing = $true" in body and "-MesmerOnly" in body
    assert "Fail" not in body.split("$script:MesmerMissing = $true")[1]
    assert '@("cellpose", "stardist")' in _fn("InstalledEngines")
    assert '"--verify", "--installed"' in _fn("Selftest")
    assert "[switch]$MesmerOnly" in INSTALL and "MesmerOnlyInstall" in _fn("Main")


def test_nothing_is_written_into_the_environment_and_the_link_is_a_junction():
    assert ".pth" not in INSTALL
    link = _fn("EnsureLink")
    assert "-ItemType Junction" in link and "left untouched" in link
    assert '$env:PYTHONPATH = Join-Path $StateDir "pkg"' in LAUNCH
    assert ".pth" not in LAUNCH


def test_the_launcher_refuses_before_python_starts():
    for why in ("is not installed for this checkout", "names another checkout",
                "does not point at this checkout", "is gone", "never finished",
                "viewer core does not fit"):
        assert why in LAUNCH, why
    assert LAUNCH.index("CheckInstall $rec") < LAUNCH.index("-m block01.main")
    assert "Tee-Object -FilePath $log" in LAUNCH
    assert 'block01.ps1" %*' in CMD and "-ExecutionPolicy Bypass" in CMD


def test_the_installer_makes_the_desktop_shortcut_and_never_replaces_a_foreign_one():
    shortcut = _fn("WriteShortcut")
    assert '"FusionFlux.lnk"' in shortcut and "block01.cmd" in shortcut
    assert "is not ours; it is left untouched" in shortcut
    assert "WriteShortcut" in _fn("Main")


def test_the_native_stderr_of_windows_powershell_is_not_fatal():
    """PowerShell 5.1 turns a redirected native stderr line into an error,
    which `$ErrorActionPreference = Stop` would make fatal."""
    for line in INSTALL.splitlines():
        if re.search(r"& \$\S+ .*2>", line) and "Native {" not in line:
            raise AssertionError(line)


def test_the_shipped_toolbox_wheel_is_the_one_the_lock_names():
    """W3-D5 (user ruling 2026-11-15): DeepCell-Toolbox ships with the
    checkout; the installer keeps a working one, else installs this wheel,
    and builds from source only with -BuildToolbox."""
    import hashlib
    lines = [l.split() for l in (REPO / "envs" / "windows" / "wheels.lock").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    entry = next(f for f in lines if f[:2] == ["0.12.1", "win_amd64"])
    wheel = REPO / "envs" / "windows" / "wheels" / entry[2]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == entry[3]
    body = _fn("EnsureToolbox")
    assert body.index("present") < body.index("LockEntry $ToolboxLock")     # a working one is kept
    assert 'elseif ($BuildToolbox)' in body and "Fail" in body


def test_model_downloads_show_their_progress():
    """Backlog 23 (2026-11-15): the 1.2 GB Cellpose model took minutes with
    nothing on screen. Windows: curl.exe's bar (PowerShell's own progress is
    off: it slows the download); Linux: curl's bar on a terminal."""
    fetch = _fn("Fetch")
    assert "--progress-bar" in fetch and "Native {" in fetch
    assert "Fetch $url" in _fn("Download") and "Fetch $url" in _fn("TryDownload")
    assert 'progress_args() { if [ -t 2 ]; then echo "-#"; else echo "-sS"; fi; }' in SH
    assert SH.count('"$(progress_args)"') == 2
