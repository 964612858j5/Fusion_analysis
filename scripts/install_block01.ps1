<#
Install FusionFlux (Block01) on Windows with one command
(docs/v17_windows_native_plan.md, phase W3).

  powershell -ExecutionPolicy Bypass -File scripts\install_block01.ps1 [options]

  -Root DIR              install root (default %LOCALAPPDATA%\block01): the
                         environment, the models, the tools it downloads
  -CheckOnly             step 1 only; nothing is written
  -NoGpu                 a CPU install even when an NVIDIA GPU is present
  -BuildCore             build the viewer core here instead of the prebuilt wheel
  -BuildToolbox          build DeepCell-Toolbox from source instead of the wheel that
                         ships in envs\windows\wheels (both -Build* need the Visual
                         Studio C++ Build Tools)
  -Models DIR            a folder with verified model copies, one sub-folder per
                         model (e.g. DIR\mesmer_multiplex_segmentation)
  -MesmerUrl URL         a mirror of the Mesmer archive (also BLOCK01_MESMER_URL;
                         default: the lab's mirror)
  -MesmerZip FILE        a local Mesmer archive (.tar.gz or .zip)
  -AcceptModelLicences   accept the model licences shown (for scripted installs)
  -SkipModels            no models and no engine self-test (the CI's install check)
  -MesmerOnly            add Mesmer to this checkout's existing install, nothing else

Mesmer (2026-11-15): DeepCell's public archive (SPACEc's URL) answers 403
since 2026-11-14. It is tried first, then the mirror (-MesmerUrl, else the
lab's), -MesmerZip, -Models;
each is checked file by file against envs\fusion_mesmer\models.json. When
none works the install finishes without Mesmer and says how to add it later.

The rules of scripts/install_block01.sh: nothing it did not create is
changed; every download is pinned and checked; a run that stopped half-way
is redone; every refusal names the step and what to do. No admin rights.
The checkout's record is <repo>\.block01\install.env, read by
scripts\block01.ps1 (scripts\block01.cmd, and the desktop shortcut).
#>
[CmdletBinding()]
param(
    [string]$Root = "",
    [switch]$CheckOnly,
    [switch]$NoGpu,
    [switch]$BuildCore,
    [switch]$BuildToolbox,
    [string]$Models = "",
    [string]$MesmerUrl = "",
    [string]$MesmerZip = "",
    [switch]$AcceptModelLicences,
    [switch]$SkipModels,
    [switch]$MesmerOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is 10x slower with it
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Root) { $Root = Join-Path $env:LOCALAPPDATA "block01" }
$Root = [IO.Path]::GetFullPath($Root)
if (-not $MesmerUrl -and $env:BLOCK01_MESMER_URL) { $MesmerUrl = $env:BLOCK01_MESMER_URL }
$StateDir = Join-Path $Repo ".block01"
$Manifest = Join-Path $Repo "envs\fusion_mesmer\models.json"
$WheelsLock = Join-Path $Repo "native\fusion_viewer_core\wheels.lock"
$ToolboxLock = Join-Path $Repo "envs\windows\wheels.lock"
$CargoToml = Join-Path $Repo "native\fusion_viewer_core\Cargo.toml"
$ReleaseUrl = if ($env:BLOCK01_RELEASE_URL) { $env:BLOCK01_RELEASE_URL } else { "https://github.com/964612858j5/Fusion_analysis/releases/download" }

# pinned downloads, checked before they are used
$MicromambaUrl = "https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-win-64"
$MicromambaSha = "b645a5259cb92b5869b0e60943390dd0d362cae45bc7e2f5ba8c7e4a4b06c7aa"
$TorchPins = @("torch==2.5.1", "torchvision==0.20.1")
$TorchIndexGpu = "https://download.pytorch.org/whl/cu121"
$TorchIndexCpu = "https://download.pytorch.org/whl/cpu"
$CpsamUrl = "https://huggingface.co/mouseland/cellpose-sam/resolve/main/cpsam"
$StardistUrl = "https://github.com/stardist/stardist-models/releases/download/v0.1/python_2D_versatile_fluo.zip"
# DeepCell's public model storage (SPACEc's URL) and its archive's digest
$MesmerUrlOfficial = "https://deepcell-data.s3-us-west-1.amazonaws.com/saved-models/MultiplexSegmentation-9.tar.gz"
$MesmerSha = "25b1babc2ff769a9f9484184073db8bc264d5ab2de8a6f9ce07c289e8b935129"
# the lab's mirror (2026-11-15, the user's Google Drive): the Mac's verified
# copy, repacked -- checked file by file against models.json, not by the
# official archive's digest. -MesmerUrl (or BLOCK01_MESMER_URL) replaces it.
$MesmerMirrorDefault = "https://drive.usercontent.google.com/download?id=1bsRETcNz6ritHEA00Ds9cAoffr48WnQi&export=download&confirm=t"

# the variable each model directory is found through (models.json env_override)
$ModelVar = [ordered]@{
    cellpose_cpsam                       = "CELLPOSE_LOCAL_MODELS_PATH"
    stardist_2D_versatile_fluo           = "BLOCK01_STARDIST_MODEL_PATH"
    stardist_2D_versatile_fluo_torch     = "BLOCK01_STARDIST_TORCH_PATH"
    mesmer_multiplex_segmentation        = "DEEPCELL_MESMER_MODEL_PATH"
    mesmer_multiplex_segmentation_torch  = "BLOCK01_MESMER_TORCH_PATH"
}
$OriginalModels = @("cellpose_cpsam", "stardist_2D_versatile_fluo", "mesmer_multiplex_segmentation")

$script:Step = "start"
$script:Py = ""
$script:EnvPrefix = ""
$script:Gpu = $false
$script:CudaPath = ""
$script:MesmerMissing = $false

# ── output ────────────────────────────────────────────────────────────────
function Say([string]$text) { Write-Host "[install] $text" }
function Warn([string]$text) { Write-Host "[install] WARNING: $text" -ForegroundColor Yellow }
function Fail([string]$text, [string]$hint = "", [string]$command = "") {
    Write-Host ""
    Write-Host "[install] STOPPED at: $($script:Step)" -ForegroundColor Red
    if ($command) { Write-Host "[install] failing command: $command" -ForegroundColor Red }
    Write-Host "[install] $text" -ForegroundColor Red
    if ($hint) { Write-Host "[install] what to do: $hint" -ForegroundColor Red }
    exit 1
}
# Run EXE ARGS...: a native command; a non-zero exit stops the install
function Run([string]$exe, [string[]]$arguments, [string]$hint = "read the messages above; fix the cause and run the installer again") {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail "the command above failed (exit $LASTEXITCODE)." $hint "$exe $($arguments -join ' ')" }
}

# Native { ... }: a native command whose stderr is redirected or captured --
# Windows PowerShell 5.1 turns such stderr lines into errors, which
# $ErrorActionPreference = Stop would make fatal; its exit code is checked
# by the caller instead
function Native([scriptblock]$block) {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $block } finally { $ErrorActionPreference = $eap }
}

function Sha256Of([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() }

# Fetch URL FILE -> "" or why not. curl.exe (Windows 10 1803+ ships it) shows
# a progress bar -- the 1.2 GB Cellpose model takes minutes (backlog 23,
# 2026-11-15) -- without the slowdown PowerShell's own progress costs;
# Invoke-WebRequest, silent, where there is no curl.exe
function Fetch([string]$url, [string]$file) {
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path -LiteralPath $curl) {
        # its bar is written to stderr: harmless, never an error of this script
        Native { & $curl -fL --retry 3 --connect-timeout 20 --progress-bar -o $file $url }
        if ($LASTEXITCODE -eq 0) { return "" }
        return "curl exit $LASTEXITCODE"
    }
    try { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file; return "" }
    catch { return $_.Exception.Message }
}

# Download URL DEST SHA: to DEST.part, checked, then moved; nothing left on failure
function Download([string]$url, [string]$dest, [string]$sha) {
    $tmp = "$dest.part"
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    $why = Fetch $url $tmp
    if ($why) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Fail "could not download $url ($why)." "check the network connection (a proxy: set HTTPS_PROXY) and run the installer again"
    }
    if ($sha) {
        $got = Sha256Of $tmp
        if ($got -ne $sha) {
            Remove-Item -LiteralPath $tmp -Force
            Fail "the file from $url is not the expected one (SHA-256 $got, expected $sha)." "do not use this file; try again later or report it"
        }
    }
    Move-Item -LiteralPath $tmp -Destination $dest -Force
}

# TryDownload URL DEST -> $true/$false (the Mesmer sources are tried in turn)
function TryDownload([string]$url, [string]$dest) {
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    if (Fetch $url "$dest.part") {
        Remove-Item -LiteralPath "$dest.part" -Force -ErrorAction SilentlyContinue
        return $false
    }
    try { Move-Item -LiteralPath "$dest.part" -Destination $dest -Force; return $true }
    catch { Remove-Item -LiteralPath "$dest.part" -Force -ErrorAction SilentlyContinue; return $false }
}

function FreeGb([string]$path) {
    $p = $path
    while (-not (Test-Path -LiteralPath $p)) { $p = Split-Path $p; if (-not $p) { return 0 } }
    $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $p).Path))
    return [math]::Floor($drive.AvailableFreeSpace / 1GB)
}

function CoreVersion {
    $line = Get-Content -LiteralPath $CargoToml | Where-Object { $_ -match '^version\s*=\s*"([^"]+)"' } | Select-Object -First 1
    if ($line -match '"([^"]+)"') { return $Matches[1] }
    return ""
}

# a "version platform file sha256" line of a lock file
function LockEntry([string]$lock, [string]$version, [string]$platform) {
    if (-not (Test-Path -LiteralPath $lock)) { return $null }
    foreach ($l in Get-Content -LiteralPath $lock) {
        if ($l -match '^\s*#' -or -not $l.Trim()) { continue }
        $f = $l.Trim() -split '\s+'
        if ($f.Count -ge 4 -and $f[0] -eq $version -and $f[1] -eq $platform) { return @{ File = $f[2]; Sha = $f[3] } }
    }
    return $null
}

function HaveMsvc {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere)) { return $false }
    $found = Native { & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null }
    return [bool]$found
}

# ── 1. checking this computer (writes nothing) ───────────────────────────
function Preflight {
    $script:Step = "1. checking this computer"
    Say "== 1. Checking this computer (nothing is written) =="
    $hard = $false
    $v = [Environment]::OSVersion.Version
    if (-not [Environment]::Is64BitOperatingSystem -or $v.Major -lt 10 -or $v.Build -lt 19045) {
        Say "  system:   Windows $v -- NOT SUPPORTED (Windows 10 22H2 or 11, 64-bit)"; $hard = $true
    } else { Say "  system:   Windows $v, 64-bit" }
    Say "  PowerShell $($PSVersionTable.PSVersion)"
    $free = FreeGb $Root
    if ($free -lt 25) { Say "  space:    $free GB free under $Root -- 25 GB needed"; $hard = $true }
    else { Say "  space:    $free GB free under $Root" }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($NoGpu) { Say "  GPU:      not used (-NoGpu): a CPU install" }
    elseif ($smi) {
        # all its lines first: `| Select-Object -First 1` stops the pipeline
        # before nvidia-smi exits, so $LASTEXITCODE was never set and a working
        # GPU read as "did not answer" (laptop W3 acceptance, 2026-11-15)
        $lines = @(Native { & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null })
        $code = $LASTEXITCODE
        $name = $lines | Select-Object -First 1
        if ($code -eq 0 -and $name) { $script:Gpu = $true; Say "  GPU:      $name" }
        else { Say "  GPU:      nvidia-smi did not answer: a CPU install" }
    } else { Say "  GPU:      no NVIDIA driver found: a CPU install (slower segmentation)" }
    if ($env:CUDA_PATH -and (Test-Path -LiteralPath $env:CUDA_PATH)) { $script:CudaPath = $env:CUDA_PATH }
    if ($BuildCore -or $BuildToolbox) {
        if (HaveMsvc) { Say "  C++ build tools: present" }
        else {
            Say "  C++ build tools: MISSING (needed by -BuildCore / -BuildToolbox)"
            Say "            install the free Visual Studio Build Tools:"
            Say "            https://visualstudio.microsoft.com/visual-cpp-build-tools/"
            Say "            and tick the workload 'Desktop development with C++'"
            $hard = $true
        }
    }
    if ($hard) { Fail "this computer does not meet the requirements above." "fix what is marked, then run the installer again" }
}

# ── 2. micromamba ─────────────────────────────────────────────────────────
function EnsureMicromamba {
    $script:Step = "2. micromamba (the package installer)"
    $exe = Join-Path $Root "bin\micromamba.exe"
    if ((Test-Path -LiteralPath $exe) -and ((Sha256Of $exe) -eq $MicromambaSha)) { Say "micromamba: present"; return $exe }
    Say "== 2. micromamba: downloading the pinned release =="
    Download $MicromambaUrl $exe $MicromambaSha
    return $exe
}

# ── 3. the environment ────────────────────────────────────────────────────
function LockId {
    $files = @("envs\ci\requirements-cpu.txt", "envs\windows\requirements.txt", "envs\windows\requirements-nodeps.txt", "envs\windows\wheels.lock")
    $text = ($files | ForEach-Object { $p = Join-Path $Repo $_; if (Test-Path -LiteralPath $p) { Get-Content -Raw -LiteralPath $p } }) -join "`n"
    $text += "`n" + ($TorchPins -join " ") + "`n" + $(if ($script:Gpu) { "cu121" } else { "cpu" })
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 8)
}

# DeepCell-Toolbox 0.12.1 (W3-D5, user ruling 2026-11-15): skipped when the
# environment already has it working; else the wheel that ships with this
# checkout (envs\windows\wheels\, its digest in envs\windows\wheels.lock),
# so no compiler is needed; else (or -BuildToolbox) built from source
function EnsureToolbox {
    $have = Native { & $script:Py -c "import importlib.metadata as m; from deepcell_toolbox.compute_overlap import compute_overlap; print(m.version('DeepCell-Toolbox'))" 2>$null } | Select-Object -Last 1
    if ($LASTEXITCODE -eq 0 -and "$have".Trim() -eq "0.12.1" -and -not $BuildToolbox) { Say "DeepCell-Toolbox 0.12.1: present"; return }
    $entry = LockEntry $ToolboxLock "0.12.1" "win_amd64"
    if ($entry -and -not $BuildToolbox) {
        $whl = Join-Path $Repo "envs\windows\wheels\$($entry.File)"
        if (-not (Test-Path -LiteralPath $whl)) { Fail "the DeepCell-Toolbox wheel $whl that ships with this checkout is missing." "update the checkout (git pull), or run again with -BuildToolbox" }
        if ((Sha256Of $whl) -ne $entry.Sha) { Fail "$whl is not the expected file (its SHA-256 differs from envs\windows\wheels.lock)." "do not use it; restore it with git (git checkout -- envs/windows/wheels) and run again" }
        Say "DeepCell-Toolbox 0.12.1: installing the wheel that ships with this checkout"
        Run $script:Py @("-m", "pip", "install", "-q", "--no-deps", $whl)
    } elseif ($BuildToolbox) {
        Say "building DeepCell-Toolbox 0.12.1 from source (-BuildToolbox; step 1 checked the compiler)"
        Run $script:Py @("-m", "pip", "install", "-q", "--no-deps", "DeepCell-Toolbox==0.12.1")
    } else {
        Fail "envs\windows\wheels.lock names no DeepCell-Toolbox wheel for this checkout." "update the checkout (git pull), or run again with -BuildToolbox (needs the Visual Studio C++ Build Tools)"
    }
}

function EnsureEnvironment([string]$mamba) {
    $script:Step = "3. the Python environment"
    $id = LockId
    $prefix = Join-Path $Root "envs\block01-$id"
    $script:EnvPrefix = $prefix
    $script:Py = Join-Path $prefix "python.exe"
    $owner = "$prefix.owner"
    $ok = Join-Path $prefix ".block01_env_ok"
    if (Test-Path -LiteralPath $ok) { Say "environment block01-${id}: present"; return }
    if (Test-Path -LiteralPath $prefix) {
        if (-not (Test-Path -LiteralPath $owner)) { Fail "$prefix exists and was not created by this installer; it is left untouched." "move it away or use another -Root" }
        Say "environment block01-${id}: an unfinished install, made again"
        Remove-Item -LiteralPath $prefix -Recurse -Force
    }
    Say "== 3. The Python environment block01-$id (a few GB; 10-30 min) =="
    New-Item -ItemType Directory -Force -Path (Split-Path $prefix) | Out-Null
    Set-Content -LiteralPath $owner -Value "created by scripts\install_block01.ps1 $(Get-Date -Format s)"
    $env:MAMBA_ROOT_PREFIX = Join-Path $Root "mamba"
    Run $mamba @("create", "-y", "-q", "-p", $prefix, "-c", "conda-forge", "python=3.10", "pip")
    $env:PIP_CACHE_DIR = Join-Path $Root "cache\pip"
    Run $script:Py @("-m", "pip", "install", "-q", "--upgrade", "pip<27")
    Run $script:Py @("-m", "pip", "install", "-q", "-r", (Join-Path $Repo "envs\ci\requirements-cpu.txt"))
    $index = if ($script:Gpu) { $TorchIndexGpu } else { $TorchIndexCpu }
    Run $script:Py (@("-m", "pip", "install", "-q") + $TorchPins + @("--index-url", $index))
    Run $script:Py @("-m", "pip", "install", "-q", "-r", (Join-Path $Repo "envs\windows\requirements.txt"))
    EnsureToolbox
    Run $script:Py @("-m", "pip", "install", "-q", "--no-deps", "-r", (Join-Path $Repo "envs\windows\requirements-nodeps.txt"))
    Set-Content -LiteralPath $ok -Value "finished $(Get-Date -Format s)"
}

# ── 4. the viewer core ────────────────────────────────────────────────────
function InstalledCore {
    $out = Native { & $script:Py -c "import fusion_viewer_core as c; print(c.__version__, getattr(c, 'CORE_API', 'none'))" 2>$null }
    if ($LASTEXITCODE -ne 0) { return "" }
    return ($out | Select-Object -Last 1)
}

function EnsureCore {
    $script:Step = "4. the viewer core (fusion_viewer_core)"
    $want = CoreVersion
    if ((InstalledCore) -eq "$want 1") { Say "viewer core ${want}: present"; return }
    $entry = LockEntry $WheelsLock $want "windows"
    if ($entry -and -not $BuildCore) {
        $whl = Join-Path $Root "downloads\core-$want\$($entry.File)"
        if (-not (Test-Path -LiteralPath $whl) -or (Sha256Of $whl) -ne $entry.Sha) {
            Say "downloading the prebuilt viewer core $want"
            Download "$ReleaseUrl/core-v$want/$($entry.File)" $whl $entry.Sha
        }
    } elseif ($BuildCore) {
        if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { Fail "-BuildCore needs Rust (cargo) on PATH." "install Rust from https://rustup.rs and run again" }
        Run $script:Py @("-m", "pip", "install", "-q", "maturin==1.15.0")
        $out = Join-Path $Root "downloads\core-$want-build"
        Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue
        Run $script:Py @("-m", "maturin", "build", "--release", "--locked", "-i", $script:Py, "-m", $CargoToml, "-o", $out)
        $whl = (Get-ChildItem -LiteralPath $out -Filter "fusion_viewer_core-$want-*.whl" | Select-Object -First 1).FullName
        if (-not $whl) { Fail "the build made no wheel." "" }
    } else {
        Fail "there is no prebuilt viewer core for version $want (a development checkout)." "run again with -BuildCore (needs Rust and the Visual Studio C++ Build Tools)"
    }
    Run $script:Py @("-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", $whl)
    $have = InstalledCore
    if ($have -ne "$want 1") { Fail "the installed viewer core reports '$have', expected '$want 1'." "" }
    Say "viewer core $want installed"
}

# ── 5. models ─────────────────────────────────────────────────────────────
function ModelDir([string]$key) { return (Join-Path $Root "models\$key") }

# ModelState KEY DIR -> ok | missing | partial | different (a file with other
# bytes is never overwritten)
function ModelState([string]$key, [string]$dir) {
    $code = @'
import hashlib, json, os, sys
manifest, key, root = sys.argv[1:]
entry = json.load(open(manifest, encoding="utf-8"))["models"][key]
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
'@
    $out = $code | & $script:Py - $Manifest $key $dir
    return ($out | Select-Object -Last 1)
}

function ManifestFiles([string]$key) {
    return (& $script:Py -c "import json,sys; [print(f['path']) for f in json.load(open(sys.argv[1], encoding='utf-8'))['models'][sys.argv[2]]['files']]" $Manifest $key)
}

function ManifestSha([string]$key, [string]$suffix) {
    return (& $script:Py -c "import json,sys; e=json.load(open(sys.argv[1], encoding='utf-8'))['models'][sys.argv[2]]; print([f['sha256'] for f in e['files'] if f['path'].endswith(sys.argv[3])][0])" $Manifest $key $suffix)
}

# stage -> final dir after verification: only the manifest's files, each to a
# path that does not exist yet; the stage is removed
function VerifyIntoPlace([string]$key, [string]$stage, [string]$dir) {
    $st = ModelState $key $stage
    if ($st -ne "ok") { Remove-Item -LiteralPath $stage -Recurse -Force; Fail "the $key files do not match models.json ($st)." "try again later or report it" }
    foreach ($f in ManifestFiles $key) {
        $dst = Join-Path $dir $f
        if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $stage -Recurse -Force; Fail "$dst appeared during the install; it is left untouched." "run the installer again" }
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Move-Item -LiteralPath (Join-Path $stage $f) -Destination $dst
    }
    Remove-Item -LiteralPath $stage -Recurse -Force
}

function LicenceText([string]$key) {
    switch ($key) {
        "cellpose_cpsam" { return @"
  Cellpose-SAM model "cpsam" (1.2 GB) from huggingface.co/mouseland/cellpose-sam
  Licence of the model (its model card): BSD-3-Clause
    https://huggingface.co/mouseland/cellpose-sam
  Separate statement in Cellpose's README: Cellpose-SAM was trained on data
  under CC-BY-NC licences.  https://github.com/MouseLand/cellpose
"@ }
        "stardist_2D_versatile_fluo" { return @"
  StarDist model "2D_versatile_fluo" (5 MB) from github.com/stardist/stardist-models
  Licence: BSD-3-Clause  https://github.com/stardist/stardist
"@ }
        "mesmer_multiplex_segmentation" { return @"
  Mesmer model "MultiplexSegmentation" (100 MB) from DeepCell's public model storage
    (deepcell-data.s3-us-west-1.amazonaws.com), a mirror or a copy you give
  Licence: non-commercial academic use only (DeepCell ASSET_LICENSE)
    https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE
  The PyTorch copy Block01 makes from it keeps the same licence; never share it.
"@ }
    }
}

function AcceptLicence([string]$key) {
    Say "Licence for the model about to be installed:"
    Write-Host (LicenceText $key)
    if ($AcceptModelLicences) { Say "  accepted with -AcceptModelLicences"; return }
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        Fail "a model licence needs your answer, and there is no console to ask." "run the installer in a console, or read the licences above and add -AcceptModelLicences"
    }
    $answer = Read-Host "[install] Type yes to accept this licence and install the model"
    if ($answer -ne "yes") { Fail "the licence was not accepted, so the model was not installed." "run the installer again and type yes, or give your own copy with -Models DIR" }
}

# MesmerFromArchive ARCHIVE STAGE -> $true when STAGE holds the contents of
# the archive's MultiplexSegmentation folder
function MesmerFromArchive([string]$archive, [string]$stage) {
    $x = Join-Path $stage "x"
    New-Item -ItemType Directory -Force -Path $x | Out-Null
    if ($archive -like "*.zip") {
        try { Expand-Archive -LiteralPath $archive -DestinationPath $x -Force } catch { return $false }
    } else {
        & tar.exe -xzf $archive -C $x                 # Windows 10 1803+ ships bsdtar
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    $src = Get-ChildItem -LiteralPath $x -Directory -Recurse -Filter "MultiplexSegmentation" | Select-Object -First 1
    if (-not $src) { return $false }
    Get-ChildItem -LiteralPath $src.FullName -Force | Move-Item -Destination $stage
    Remove-Item -LiteralPath $x -Recurse -Force
    return $true
}

# EnsureMesmer DIR: DeepCell's archive, the mirror, the local archive, the
# -Models folder -- the first that yields models.json's files is moved in;
# none: MesmerMissing, said, the install goes on
function EnsureMesmer([string]$dir) {
    $key = "mesmer_multiplex_segmentation"
    $stage = "$dir.stage"
    foreach ($src in @("official", "mirror", "zip", "folder")) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        $archive = Join-Path $stage "m.tar.gz"
        switch ($src) {
            "official" {
                Say "model ${key}: trying DeepCell's public archive"
                if (-not (TryDownload $MesmerUrlOfficial $archive)) { Say "  not available"; continue }
                if ((Sha256Of $archive) -ne $MesmerSha) { Say "  not the expected archive"; continue }
                if (-not (MesmerFromArchive $archive $stage)) { continue }
                Remove-Item -LiteralPath $archive -Force
            }
            "mirror" {
                $mirror = if ($MesmerUrl) { $MesmerUrl } else { $MesmerMirrorDefault }
                Say "model ${key}: trying the mirror $mirror"
                if (-not (TryDownload $mirror $archive)) { Say "  not available"; continue }
                if (-not (MesmerFromArchive $archive $stage)) { Say "  no MultiplexSegmentation folder in it"; continue }
                Remove-Item -LiteralPath $archive -Force
            }
            "zip" {
                if (-not $MesmerZip) { continue }
                Say "model ${key}: trying the archive $MesmerZip"
                if (-not (Test-Path -LiteralPath $MesmerZip)) { Say "  no such file"; continue }
                if (-not (MesmerFromArchive ([IO.Path]::GetFullPath($MesmerZip)) $stage)) { Say "  no MultiplexSegmentation folder in it"; continue }
            }
            "folder" {
                if (-not $Models) { continue }
                $from = Join-Path $Models $key
                if ((ModelState $key $from) -ne "ok") { continue }
                Say "model ${key}: copying the verified copy from $from"
                foreach ($f in ManifestFiles $key) {
                    $dst = Join-Path $stage $f
                    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
                    Copy-Item -LiteralPath (Join-Path $from $f) -Destination $dst
                }
            }
        }
        if ((ModelState $key $stage) -eq "ok") {
            VerifyIntoPlace $key $stage $dir
            Say "model ${key}: installed and verified"
            return
        }
        Say "  its files do not match models.json; not used"
    }
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    $script:MesmerMissing = $true
    Warn "Mesmer is NOT installed: no source worked. Everything else is. To add it later:"
    Warn "  scripts\install_block01.ps1 -MesmerOnly -MesmerZip FILE   (or -MesmerUrl URL, -Models DIR)"
}

function FetchModel([string]$key, [string]$dir) {
    $stage = "$dir.stage"
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    switch ($key) {
        "cellpose_cpsam" { Download $CpsamUrl (Join-Path $stage "cpsam") (ManifestSha $key "cpsam") }
        "stardist_2D_versatile_fluo" {
            $zip = Join-Path $stage "2D_versatile_fluo.zip"
            Download $StardistUrl $zip (ManifestSha $key ".zip")
            $x = Join-Path $stage "x"
            Expand-Archive -LiteralPath $zip -DestinationPath $x -Force
            # the three files, wherever the zip put them
            foreach ($n in @("config.json", "thresholds.json", "weights_best.h5")) {
                $f = Get-ChildItem -LiteralPath $x -Recurse -File -Filter $n | Select-Object -First 1
                if ($f) { Move-Item -LiteralPath $f.FullName -Destination (Join-Path $stage $n) }
            }
            Remove-Item -LiteralPath $x -Recurse -Force
        }
    }
    VerifyIntoPlace $key $stage $dir
}

function EnsureModels {
    $script:Step = "5. the segmentation models"
    Say "== 5. The segmentation models (under $Root\models) =="
    foreach ($key in $OriginalModels) {
        $dir = ModelDir $key
        $st = ModelState $key $dir
        if ($st -eq "ok") { Say "model ${key}: present and verified"; continue }
        if ($st -ne "missing") { Fail "model $key in $dir does not match models.json ($st); it is left untouched." "move that folder away (or use another -Root) and run the installer again" }
        AcceptLicence $key
        if ($key -eq "mesmer_multiplex_segmentation") { EnsureMesmer $dir; continue }
        if ($Models -and (ModelState $key (Join-Path $Models $key)) -eq "ok") {
            Say "model ${key}: copying the verified copy from $Models\$key"
            $stage = "$dir.stage"
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
            foreach ($f in ManifestFiles $key) {
                $dst = Join-Path $stage $f
                New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
                Copy-Item -LiteralPath (Join-Path (Join-Path $Models $key) $f) -Destination $dst
            }
            VerifyIntoPlace $key $stage $dir
            continue
        }
        Say "model ${key}: downloading"
        FetchModel $key $dir
        Say "model ${key}: downloaded and verified"
    }
}

# the variables every later step and the launcher run with
function AppEnv {
    $env:KERAS_BACKEND = "tensorflow"
    $env:KERAS_HOME = Join-Path $Root "keras"
    $env:CUPY_CACHE_DIR = Join-Path $Root "cache\cupy"
    $env:CUDA_CACHE_PATH = Join-Path $Root "cache\nv"
    $env:MPLCONFIGDIR = Join-Path $Root "cache\matplotlib"
    $env:PYTHONIOENCODING = "utf-8"
    foreach ($key in $ModelVar.Keys) { Set-Item -Path "env:$($ModelVar[$key])" -Value (ModelDir $key) }
    if ($script:CudaPath) { $env:CUDA_PATH = $script:CudaPath }
    New-Item -ItemType Directory -Force -Path $env:KERAS_HOME | Out-Null
}

function ConvertStardist {
    $script:Step = "5b. the StarDist torch weights"
    AppEnv
    Say "== 5b. StarDist: converting weights_best.h5 for the PyTorch engine =="
    Push-Location $Repo
    try {
        $env:PYTHONPATH = $Repo
        Run $script:Py @((Join-Path $Repo "scripts\convert_stardist_to_torch.py"))
    } finally { Pop-Location; Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue }
}

# ── 6. the package link ──────────────────────────────────────────────────
function EnsureLink {
    $script:Step = "6. the package link for this checkout"
    $pkg = Join-Path $StateDir "pkg"
    $link = Join-Path $pkg "block01"
    New-Item -ItemType Directory -Force -Path $pkg, (Join-Path $StateDir "run"), (Join-Path $StateDir "logs") | Out-Null
    if (Test-Path -LiteralPath $link) {
        $item = Get-Item -LiteralPath $link -Force
        $target = @($item.Target) | Select-Object -First 1
        if ($item.LinkType -ne "Junction" -and $item.LinkType -ne "SymbolicLink") { Fail "$link exists and is not a link; it is left untouched." "move it away and run the installer again" }
        if ([IO.Path]::GetFullPath($target).TrimEnd('\') -ne $Repo.TrimEnd('\')) { Fail "$link points at $target, not this checkout; it is left untouched." "remove that link yourself if it is stale, then run the installer again" }
        Say "package link: present"
        return
    }
    New-Item -ItemType Junction -Path $link -Target $Repo | Out-Null
    Say "package link: $link -> $Repo"
}

# ── 7. self-test ──────────────────────────────────────────────────────────
function InstalledEngines {
    if ($script:MesmerMissing) { return @("cellpose", "stardist") }
    return @("cellpose", "stardist", "mesmer")
}

# CheckSelftest LINES ENGINES -> $true when every engine said ok, exit 0, a device
function CheckSelftest([string[]]$lines, [string[]]$want) {
    $seen = @{}
    $bad = @()
    foreach ($l in $lines) {
        $t = "$l".Trim()
        if (-not $t.StartsWith("{")) { continue }
        $r = $t | ConvertFrom-Json
        $seen[$r.engine] = $true
        if ($r.state -ne "ok" -or $r.exit_code -ne 0 -or -not $r.device) { $bad += $r.engine }
        else { Say "  $($r.engine): ok on $($r.device)" }
    }
    foreach ($e in $want) { if (-not $seen.ContainsKey($e)) { $bad += $e } }
    if ($bad.Count) { Say "  failed: $($bad -join ', ')"; return $false }
    return $true
}

function SelftestEngines([string[]]$engines, [string]$label) {
    $run = Join-Path $StateDir "run"
    Push-Location $run
    try {
        $env:PYTHONPATH = $Repo
        $err = Join-Path $run "selftest.err"
        $out = Native { & $script:Py -m seg_runner.selftest @engines 2> $err }
    } finally { Pop-Location; Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue }
    $out | ForEach-Object { Write-Host $_ }
    if (-not (CheckSelftest $out $engines)) {
        Fail "an engine failed its self-test $label (details above and in $run\selftest.err)." "read the error, fix the cause (often the GPU driver) and run the installer again"
    }
}

function Selftest {
    $script:Step = "7. self-test"
    AppEnv
    $run = Join-Path $StateDir "run"
    if ($SkipModels) {
        Say "== 7. Self-test: the engines are not tested (-SkipModels) =="
    } else {
        Say "== 7. Self-test: one small task per installed segmentation engine =="
        $engines = InstalledEngines
        SelftestEngines $engines ""
        if ($script:Gpu) {
            Say "  ...and with the GPU hidden (Step2's 'Use GPU' off)"
            $env:CUDA_VISIBLE_DEVICES = "-1"
            try { SelftestEngines $engines "with the GPU hidden" } finally { Remove-Item env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue }
        }
        Push-Location $run
        try { Run $script:Py @((Join-Path $Repo "scripts\model_manifest.py"), "--verify", "--installed") } finally { Pop-Location }
    }
    $pkg = Join-Path $StateDir "pkg"
    Push-Location $run
    try {
        $env:PYTHONPATH = $pkg
        $gerr = Join-Path $run "gpucheck.err"
        $gpu = Native { & $script:Py -c "from block01.core import bg_correction as b; print('ok' if b.GPU_MORPH_AVAILABLE else 'cpu', b.GPU_MORPH_SMOKE_TEST)" 2> $gerr } | Select-Object -Last 1
        if ("$gpu".StartsWith("ok")) { Say "  GPU background correction: ok" }
        else { Warn "GPU background correction unavailable, the CPU is used ($gpu)" }
        $env:QT_QPA_PLATFORM = "offscreen"
        $code = @'
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
'@
        $serr = Join-Path $run "startcheck.err"
        $core = Native { $code | & $script:Py - 2> $serr } | Select-Object -Last 1
    } finally {
        Pop-Location
        Remove-Item env:PYTHONPATH, env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    if ("$core".EndsWith("accepted")) { Say "  app start check: $core" }
    else {
        Get-Content -LiteralPath (Join-Path $run "startcheck.err") -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        Fail "the app does not start or refuses its viewer core: $core (all of its error output is in $run\startcheck.err)." "read the error above, fix the cause and run the installer again"
    }
}

# ── 8. the record, the shortcut ───────────────────────────────────────────
function WriteRecord {
    $script:Step = "8. the install record"
    $rec = Join-Path $StateDir "install.env"
    $lines = @(
        "# written by scripts\install_block01.ps1 $(Get-Date -Format s); read by scripts\block01.ps1",
        "BLOCK01_CHECKOUT=$Repo",
        "BLOCK01_ROOT=$Root",
        "BLOCK01_PREFIX=$($script:EnvPrefix)",
        "BLOCK01_CORE_VERSION=$(CoreVersion)",
        "BLOCK01_MESMER=$(if ($script:MesmerMissing) { 'missing' } else { 'installed' })",
        "KERAS_BACKEND=tensorflow",
        "KERAS_HOME=$(Join-Path $Root 'keras')",
        "CUPY_CACHE_DIR=$(Join-Path $Root 'cache\cupy')",
        "CUDA_CACHE_PATH=$(Join-Path $Root 'cache\nv')",
        "MPLCONFIGDIR=$(Join-Path $Root 'cache\matplotlib')",
        "PYTHONIOENCODING=utf-8"
    )
    foreach ($key in $ModelVar.Keys) { $lines += "$($ModelVar[$key])=$(ModelDir $key)" }
    if ($script:CudaPath) { $lines += "CUDA_PATH=$($script:CudaPath)" }
    Set-Content -LiteralPath "$rec.tmp" -Value $lines -Encoding UTF8
    Move-Item -LiteralPath "$rec.tmp" -Destination $rec -Force
    Set-Content -LiteralPath (Join-Path $script:EnvPrefix ".block01_install_ok") -Value "core $(CoreVersion)`ntime $(Get-Date -Format s)"
}

function WriteShortcut {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) { return }
    $lnk = Join-Path $desktop "FusionFlux.lnk"
    $target = Join-Path $Repo "scripts\block01.cmd"
    $shell = New-Object -ComObject WScript.Shell
    if (Test-Path -LiteralPath $lnk) {
        $old = $shell.CreateShortcut($lnk)
        if ($old.TargetPath -and $old.TargetPath -ne $target -and $old.Description -ne "FusionFlux (Block01)") {
            Warn "$lnk exists and is not ours; it is left untouched (start with $target)"
            return
        }
    }
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $target
    $s.WorkingDirectory = $Repo
    $s.Description = "FusionFlux (Block01)"
    $s.Save()
    Say "desktop shortcut: $lnk"
}

# ── -MesmerOnly ───────────────────────────────────────────────────────────
function ReadRecord {
    $rec = Join-Path $StateDir "install.env"
    if (-not (Test-Path -LiteralPath $rec)) { Fail "this checkout has no install yet ($rec is missing)." "run the installer without -MesmerOnly first" }
    $vals = @{}
    foreach ($l in Get-Content -LiteralPath $rec) {
        if ($l -match '^\s*#' -or $l -notmatch '=') { continue }
        $i = $l.IndexOf('='); $vals[$l.Substring(0, $i)] = $l.Substring($i + 1)
    }
    return $vals
}

function MesmerOnlyInstall {
    $script:Step = "adding Mesmer to the existing install"
    $rec = ReadRecord
    if ($rec["BLOCK01_ROOT"]) { $script:Root = $rec["BLOCK01_ROOT"] }     # the install's own root, not the default
    $script:EnvPrefix = $rec["BLOCK01_PREFIX"]
    $script:Py = Join-Path $script:EnvPrefix "python.exe"
    if ($rec["CUDA_PATH"]) { $script:CudaPath = $rec["CUDA_PATH"] }
    if (-not (Test-Path -LiteralPath $script:Py)) { Fail "the environment $($script:EnvPrefix) is gone." "run the installer without -MesmerOnly" }
    $key = "mesmer_multiplex_segmentation"
    $dir = ModelDir $key
    $st = ModelState $key $dir
    if ($st -eq "ok") { Say "Mesmer is already installed and verified ($dir)" }
    elseif ($st -eq "missing") {
        AcceptLicence $key
        EnsureMesmer $dir
        if ($script:MesmerMissing) { Fail "no Mesmer source worked (messages above)." "give a file with -MesmerZip FILE, a link with -MesmerUrl URL, or a folder with -Models DIR" }
    } else { Fail "$dir holds files that do not match models.json; they are left untouched." "move that folder away and run this again" }
    $script:Step = "the Mesmer self-test"
    AppEnv
    SelftestEngines @("mesmer") ""
    Push-Location (Join-Path $StateDir "run")
    try { Run $script:Py @((Join-Path $Repo "scripts\model_manifest.py"), "--verify", "--installed") } finally { Pop-Location }
    # the record says Mesmer is there now
    $file = Join-Path $StateDir "install.env"
    (Get-Content -LiteralPath $file) -replace '^BLOCK01_MESMER=.*$', 'BLOCK01_MESMER=installed' | Set-Content -LiteralPath $file -Encoding UTF8
    Say "== Mesmer added. Start FusionFlux as before (the desktop shortcut or scripts\block01.cmd) =="
}

# ── main ──────────────────────────────────────────────────────────────────
function Main {
    if ($MesmerOnly) { MesmerOnlyInstall; return }
    $t0 = Get-Date
    Say "FusionFlux install: checkout $Repo, install root $Root"
    Preflight
    if ($CheckOnly) { Say "check only: nothing was written"; return }
    $script:Step = "2. starting"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    # one install into a root at a time
    try { $lock = [IO.File]::Open((Join-Path $Root ".install.lock"), "OpenOrCreate", "ReadWrite", "None") }
    catch { Fail "another install into $Root is running." "wait for it to finish, then run the installer again" }
    try {
        $mamba = EnsureMicromamba
        EnsureEnvironment $mamba
        EnsureCore
        EnsureLink
        if ($SkipModels) { Say "== 5. models: skipped (-SkipModels) ==" }
        else {
            EnsureModels
            ConvertStardist
        }
        Selftest
        WriteRecord
        WriteShortcut
    } finally { $lock.Close() }
    Say "== Done in $([int]((Get-Date) - $t0).TotalSeconds) s. Start FusionFlux with the desktop shortcut or: =="
    Say "   $Repo\scripts\block01.cmd"
    if ($script:MesmerMissing) {
        Warn "Mesmer is not installed (its three methods are unavailable). Add it later with:"
        Warn "   scripts\install_block01.ps1 -MesmerOnly -MesmerZip FILE   (or -MesmerUrl URL, -Models DIR)"
    }
}

if (-not $env:BLOCK01_INSTALL_SOURCED) { Main }
