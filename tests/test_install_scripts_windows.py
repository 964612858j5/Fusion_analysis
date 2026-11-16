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
    nothing on screen; then curl's own bar and retry warnings were noise (the
    user's review). curl runs silent, its messages into the log; the progress
    line is the installer's own, on a console only; up to 4 attempts, each
    resuming the .part (-C -), only network failures retried, one line each."""
    fetch = _fn("Fetch")
    assert '"-sS"' in fetch and "--progress-bar" not in fetch and '"--retry", "2"' in fetch
    assert '"-C", "-"' in fetch and "$attempt -le 4" in fetch and "-RedirectStandardError $err" in fetch
    assert 'Log "  curl: $_"' in fetch and 'Dim "    network problem (curl $code), retrying' in fetch
    assert "Progress $name $file $size" in fetch
    assert "if (-not $script:Live -or -not $name) { return }" in _fn("Progress")
    assert "Fetch $url $tmp $name $size" in _fn("Download") and 'Fetch $url "$dest.part" $name' in _fn("TryDownload")
    assert "ManifestSize $key" in _fn("FetchModel")
    # Linux: the same rule (its own static tests cover the details)
    assert "-C -" in SH


def test_the_tools_print_into_the_log_not_on_screen():
    """The user's review (2026-11-15): pip's PATH warnings, micromamba's
    channel warning, the self-test's JSON and the internal details were noise.
    Every tool's output goes to .block01\\logs\\install-<time>.log; the last
    lines are shown only on failure."""
    run = _fn("Run")
    assert "Add-Content -LiteralPath $script:LogFile" in run and "Spawn $exe $arguments" in run
    assert "Write-Host" not in run
    assert "install-$(Get-Date -Format 'yyyyMMdd-HHmmss').log" in _fn("StartLog")
    fail = _fn("Fail")
    assert "-Tail 20" in fail and "log: $($script:LogFile)" in fail
    assert INSTALL.count('"--no-warn-script-location"') == INSTALL.count('"pip", "install"')
    assert '"--override-channels", "-c", "conda-forge"' in _fn("EnsureEnvironment")
    for name in ("EnsureLink", "EnsureToolbox", "ConvertStardist"):
        assert "Say " not in _fn(name), name
    self_test = _fn("SelftestEngines")
    assert "Write-Host" not in self_test and 'Log "  $_"' in self_test
    assert "SelftestTable $engines $gpu $cpu" in _fn("Selftest")


def test_one_licence_card_before_the_long_steps():
    """The user's review (2026-11-15): one card for every model about to be
    installed, one answer, asked before the environment is built."""
    main = _fn("Main")
    assert main.index("LicenceCard") < main.index("EnsureMicromamba")
    assert "NeedsInstall $_" in main and "-not $SkipModels" in main
    card = _fn("LicenceCard")
    assert "Accept all and install them? [y/N]" in card
    assert '$answer -ne "y" -and $answer -ne "yes"' in card and "Fail" in card
    assert "-AcceptModelLicences" in card and "IsInputRedirected" in card
    assert "-ForegroundColor Yellow" in card                     # the non-commercial row
    assert "if ($script:Accepted[$key]) { return }" in _fn("AcceptLicence")
    assert "ConvertFrom-Json" in _fn("NeedsInstall")             # before Python exists
    assert 'mesmer_multiplex_segmentation = @{ Size = "100 MB"; Text = "NON-COMMERCIAL' in INSTALL


def test_symbols_only_where_they_render():
    """Windows PowerShell's console may lack the glyphs; Windows Terminal has
    them. The script itself stays ASCII (Windows PowerShell reads a file
    without a BOM in the ANSI code page)."""
    assert "$script:Fancy = $script:Live -and [bool]$env:WT_SESSION" in INSTALL
    assert "[char]0x2713" in INSTALL                       # the glyphs made at run time
    code = [l for l in INSTALL.splitlines() if not l.lstrip().startswith("#")]
    bad = [l for l in code if any(ord(ch) > 127 for ch in l)]
    assert not bad, bad


def test_a_native_command_is_never_cut_short_by_select_first():
    """Laptop W3 acceptance (2026-11-15): `& nvidia-smi ... | Select-Object
    -First 1` stopped the pipeline before nvidia-smi exited, $LASTEXITCODE was
    never set, and a working RTX 3060 made a CPU install."""
    for text in (INSTALL, LAUNCH):
        for line in text.splitlines():
            if re.search(r"&\s+[\w$.-]+.*\|\s*Select-Object -First", line):
                raise AssertionError(line)
    preflight = _fn("Preflight")
    assert "$code = $LASTEXITCODE" in preflight and "$code -eq 0" in preflight


def test_a_running_step_is_updated_in_place():
    """The user's review (2026-11-15): "• micromamba ..." and then
    "✓ micromamba 3 s" on two lines; every running step is now one line,
    replaced by its result; a line printed meanwhile goes above it."""
    assert '$script:Pending = "$line ..."' in _fn("Begin")
    assert 'Unpend; $script:Pending = ""' in _fn("Done") and 'Unpend; $script:Pending = ""' in _fn("Fail")
    for name in ("Say", "Dim", "Warn"):
        body = _fn(name)
        assert body.index("Unpend") < body.index("Write-Host") < body.index("Repend"), name
    assert "Repend" in _fn("ProgressClear")
    assert 'Unpend; $script:Pending = ""' in _fn("SelftestTable")
    assert "Begin $Pretty[$key]" in _fn("EnsureModels") and 'Begin "Mesmer"' in _fn("EnsureModels")


def test_the_running_bullet_blinks():
    """The user's review (2026-11-15): the bullet of a running step blinks.
    The tools run in the background (Start-Process), waited for in 1 s
    steps, each one toggling the bullet; their output still only in the log."""
    spawn = _fn("Spawn")
    assert "Start-Process" in spawn and "-RedirectStandardOutput $out -RedirectStandardError $err" in spawn
    assert "while (-not $p.WaitForExit(1000)) { Blink }" in spawn and "-WorkingDirectory" in spawn
    run = _fn("Run")
    assert "Spawn $exe $arguments" in run and "Add-Content -LiteralPath $script:LogFile" in run
    assert "Write-Host" not in run and "$code -ne 0" in run and "Fail" in run
    assert "Spawn $script:Py" in _fn("SelftestEngines")
    assert "if (-not $script:Pending) { return }" in _fn("Blink")


def test_a_failed_mesmer_source_goes_straight_to_the_next():
    """The laptop's install (2026-11-16): a bare `continue` inside PowerShell's
    `switch` only leaves the switch, so a failed source's empty stage was
    checked too and reported a second time. The loop is labelled."""
    body = _fn("EnsureMesmer")
    assert ':sources foreach ($src in @("official", "mirror", "zip", "folder"))' in body
    inner = body.split("switch ($src)", 1)[1]
    assert inner.count("continue sources") == 10
    assert not re.search(r"continue(?! sources)\b", inner)


def test_a_long_step_name_keeps_two_spaces():
    """The laptop's install (2026-11-16): "GPU background correctionok"."""
    assert "$label.PadRight([math]::Max(22, $label.Length + 2))" in _fn("Done")
