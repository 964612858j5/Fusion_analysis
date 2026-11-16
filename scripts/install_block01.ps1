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
  -NoSpatialData         no SpatialData environment (Step 4's SpatialData export
                         then asks for a Python with spatialdata)
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
    [switch]$NoSpatialData,
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
$script:SdPrefix = ""
$SdSpec = Join-Path $Repo "envs\block01_spatialdata"
# the conda part of the SpatialData environment: Linux's lock (conda-linux-64.lock)
# has these on top of Python; the pip part is the same list on every system
$SdConda = @("python=3.10", "pip", "packaging=26.3", "wheel=0.48.0")

# ── output ────────────────────────────────────────────────────────────────
# Backlog 23 (the user's review, 2026-11-15): only what matters reaches the
# screen -- one line per step, the licences on one card, a table for the
# self-test; everything the tools print (pip, micromamba, curl, the helpers)
# goes to the log, .block01\logs\install-<time>.log, shown only on failure.
# Symbols only where they render (Windows Terminal); plain ASCII elsewhere.
$script:Live = -not [Console]::IsOutputRedirected        # a console: progress lines redrawn
$script:Fancy = $script:Live -and [bool]$env:WT_SESSION
if ($script:Fancy) {
    $script:Sym = @{ ok = [string][char]0x2713; bad = [string][char]0x2717; run = [string][char]0x2022; full = [char]0x2588; empty = [char]0x2591
                     h = [char]0x2500; v = [char]0x2502; tl = [char]0x250C; tr = [char]0x2510; bl = [char]0x2514; br = [char]0x2518 }
} else {
    $script:Sym = @{ ok = "[ok]"; bad = "[x]"; run = "-"; full = "#"; empty = "-"; h = "-"; v = "|"; tl = "+"; tr = "+"; bl = "+"; br = "+" }
}
$script:LogFile = ""
$script:Label = ""
# the running step's "* label ..." line (a console only): it stays the last
# line and is updated in place (the user's review, 2026-11-15) -- a line
# printed meanwhile goes above it, the download's progress takes its place,
# its result replaces it
$script:Pending = ""
function Width { try { return [math]::Max(40, [Console]::WindowWidth - 1) } catch { return 79 } }
function Unpend { if ($script:Pending) { Write-Host ("`r" + "".PadRight((Width)) + "`r") -NoNewline } }
function Repend { if ($script:Pending) { Write-Host $script:Pending -ForegroundColor DarkGray -NoNewline } }

function Log([string]$text) {
    if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $text -Encoding UTF8 }
}
function Say([string]$text) { Unpend; Write-Host $text; Repend; Log $text }
function Dim([string]$text) { Unpend; Write-Host $text -ForegroundColor DarkGray; Repend; Log $text }
function Warn([string]$text) { Unpend; Write-Host "  ! $text" -ForegroundColor Yellow; Repend; Log "WARNING: $text" }
function Took([datetime]$t0) {
    $s = [int]((Get-Date) - $t0).TotalSeconds
    if ($s -lt 60) { return "$s s" }
    return "$([math]::Floor($s / 60)) min $($s % 60) s"
}
# Begin LABEL: a step that takes a while starts; Done: it finished
function Begin([string]$label, [string]$note = "") {
    $script:Label = $label
    $script:StepT0 = Get-Date
    Log "== $label =="
    $line = "  $($script:Sym.run) $label"
    if ($note) { $line += " ($note)" }
    if ($script:Live) { Unpend; $script:Pending = "$line ..."; Repend }
    else { Write-Host "$line ..." -ForegroundColor DarkGray }
}
function Done([string]$label = "", [string]$detail = "") {
    if (-not $label) { $label = $script:Label }
    if (-not $detail) { $detail = Took $script:StepT0 }
    Unpend; $script:Pending = ""
    Write-Host "  $($script:Sym.ok) " -ForegroundColor Green -NoNewline
    Write-Host $label.PadRight([math]::Max(22, $label.Length + 2)) -NoNewline   # two spaces at least
    Write-Host $detail -ForegroundColor DarkGray
    Log "OK $label -- $detail"
}
function Present([string]$label) { $script:Label = $label; Done $label "already installed" }
function Fail([string]$text, [string]$hint = "", [string]$command = "") {
    Unpend; $script:Pending = ""
    Write-Host ""
    if ($script:Label) { Write-Host "  $($script:Sym.bad) $($script:Label)" -ForegroundColor Red }
    Write-Host "STOPPED at: $($script:Step)" -ForegroundColor Red
    Write-Host $text -ForegroundColor Red
    if ($hint) { Write-Host "what to do: $hint" -ForegroundColor Red }
    $tail = @()
    if ($script:LogFile -and (Test-Path -LiteralPath $script:LogFile)) { $tail = @(Get-Content -LiteralPath $script:LogFile -Tail 20) }
    Log "STOPPED at: $($script:Step)"
    if ($command) { Log "failing command: $command" }
    Log $text
    if ($script:LogFile) {
        if ($tail.Count) {
            Write-Host "last lines of the log:" -ForegroundColor DarkGray
            $tail | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
        Write-Host "log: $($script:LogFile)" -ForegroundColor DarkGray
    }
    exit 1
}
# Blink: the running step's bullet on and off (the user's review,
# 2026-11-15: a long step must look alive)
$script:BlinkOn = $true
function Blink {
    if (-not $script:Pending) { return }
    $script:BlinkOn = -not $script:BlinkOn
    $line = $script:Pending
    if (-not $script:BlinkOn) { $line = $line.Replace([string]$script:Sym.run, "".PadRight(([string]$script:Sym.run).Length)) }
    Write-Host ("`r" + $line) -ForegroundColor DarkGray -NoNewline
}

# Spawn EXE ARGS OUT ERR -> exit code: a native command, started (in the
# current folder, with this environment) and waited for while the bullet
# blinks; its stdout and stderr into the files OUT and ERR
function Spawn([string]$exe, [string[]]$arguments, [string]$out, [string]$err) {
    $quoted = ($arguments | ForEach-Object { '"' + ("$_" -replace '"', '\"') + '"' }) -join " "
    $p = Start-Process -FilePath $exe -ArgumentList $quoted -NoNewWindow -PassThru -WorkingDirectory (Get-Location).Path `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $p.Handle                              # (Windows PowerShell keeps ExitCode only so)
    while (-not $p.WaitForExit(1000)) { Blink }
    $p.WaitForExit()
    if (-not $script:BlinkOn) { Blink }            # the bullet back on
    return $p.ExitCode
}

# Run EXE ARGS...: a native command, its output into the log; a non-zero exit
# stops the install
function Run([string]$exe, [string[]]$arguments, [string]$hint = "read the last lines of the log below, fix the cause and run the installer again (finished steps are kept)") {
    Log "> $exe $($arguments -join ' ')"
    $tmp = [IO.Path]::GetTempFileName()
    $code = Spawn $exe $arguments "$tmp.out" "$tmp.err"
    foreach ($f in @("$tmp.out", "$tmp.err")) {
        if (Test-Path -LiteralPath $f) {
            if ($script:LogFile) { Get-Content -LiteralPath $f | Add-Content -LiteralPath $script:LogFile -Encoding UTF8 }
            Remove-Item -LiteralPath $f -Force
        }
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    if ($code -ne 0) { Fail "a command failed (exit $code): $(Split-Path -Leaf $exe) $($arguments | Select-Object -First 4)" $hint "$exe $($arguments -join ' ')" }
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

# Progress NAME FILE SIZE: one line, redrawn in place, on a console only
function Progress([string]$name, [string]$file, [long]$size) {
    if (-not $script:Live -or -not $name) { return }
    $have = 0
    if (Test-Path -LiteralPath $file) { $have = (Get-Item -LiteralPath $file).Length }
    $mb = [math]::Round($have / 1MB)
    if ($size -gt 0) {
        $f = [math]::Min(1.0, $have / $size)
        $n = [int][math]::Floor(24 * $f)
        $bar = ([string]$script:Sym.full * $n) + ([string]$script:Sym.empty * (24 - $n))
        $line = "    {0}  {1} {2,3}%  {3}/{4} MB" -f $name.PadRight(13), $bar, [int](100 * $f), $mb, [math]::Round($size / 1MB)
    } else { $line = "    {0}  {1} MB" -f $name.PadRight(13), $mb }
    $w = Width
    if ($line.Length -gt $w) { $line = $line.Substring(0, $w) }
    Write-Host ("`r" + $line.PadRight($w)) -NoNewline
}
function ProgressClear([string]$name) {
    if ($script:Live -and $name) { Write-Host ("`r" + "".PadRight((Width)) + "`r") -NoNewline; Repend }
}

# Fetch URL FILE [NAME SIZE] -> "" or why not. curl.exe (Windows 10 1803+
# ships it), silent, its messages into the log; the progress line is ours
# (the 1.2 GB Cellpose model takes minutes). Up to 4 attempts, each going on
# from where the last stopped (-C -); only network failures are retried.
# Invoke-WebRequest, silent, where there is no curl.exe.
function Fetch([string]$url, [string]$file, [string]$name = "", [long]$size = 0) {
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (-not (Test-Path -LiteralPath $curl)) {
        try { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file; return "" }
        catch { return $_.Exception.Message }
    }
    Log "download $url"
    $retry = @(5, 6, 7, 18, 28, 35, 52, 55, 56, 92)
    $waits = @(2, 5, 10)
    $err = "$file.err"
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        # curl itself retries a busy server (HTTP 408/429/5xx), silently
        $a = @("-fL", "-sS", "--connect-timeout", "20", "--retry", "2", "-o", $file)
        if (Test-Path -LiteralPath $file) { $a += @("-C", "-") }
        $a += $url
        $quoted = ($a | ForEach-Object { '"' + $_ + '"' }) -join " "
        $p = Start-Process -FilePath $curl -ArgumentList $quoted -NoNewWindow -PassThru -RedirectStandardError $err
        $null = $p.Handle                          # (Windows PowerShell keeps ExitCode only so)
        while (-not $p.WaitForExit(300)) { Progress $name $file $size }
        $p.WaitForExit()
        $code = $p.ExitCode
        ProgressClear $name
        if (Test-Path -LiteralPath $err) { Get-Content -LiteralPath $err | ForEach-Object { Log "  curl: $_" }; Remove-Item -LiteralPath $err -Force }
        if ($code -eq 0) { return "" }
        if ($code -eq 33 -or $code -eq 36) {        # no resuming here: from the start
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        } elseif ($retry -notcontains $code) { return "curl exit $code" }
        if ($attempt -lt 4) {
            Dim "    network problem (curl $code), retrying $($attempt + 1)/4"
            Start-Sleep -Seconds $waits[$attempt - 1]
        }
    }
    return "curl exit $code, 4 attempts"
}

# Download URL DEST SHA [NAME SIZE]: to DEST.part, checked, then moved;
# nothing left on failure
function Download([string]$url, [string]$dest, [string]$sha, [string]$name = "", [long]$size = 0) {
    $tmp = "$dest.part"
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    $why = Fetch $url $tmp $name $size
    if ($why) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Fail "could not download $url ($why)." "check the network connection (a proxy: set HTTPS_PROXY) and run the installer again; finished steps are kept"
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
function TryDownload([string]$url, [string]$dest, [string]$name = "") {
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Remove-Item -LiteralPath "$dest.part" -Force -ErrorAction SilentlyContinue
    if (Fetch $url "$dest.part" $name) {
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
    $script:Label = "Computer"
    $bad = @()
    $v = [Environment]::OSVersion.Version
    $os = if ($v.Build -ge 22000) { "Windows 11" } else { "Windows 10" }
    if (-not [Environment]::Is64BitOperatingSystem -or $v.Major -lt 10 -or $v.Build -lt 19045) {
        $bad += "Windows $v is not supported (Windows 10 22H2 or 11, 64-bit)"
    }
    Log "system: Windows $v; PowerShell $($PSVersionTable.PSVersion)"
    $free = FreeGb $Root
    if ($free -lt 25) { $bad += "$free GB free under $Root; 25 GB are needed" }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($NoGpu) { $gpuText = "GPU not used (-NoGpu)" }
    elseif ($smi) {
        # all its lines first: `| Select-Object -First 1` stops the pipeline
        # before nvidia-smi exits, so $LASTEXITCODE was never set and a working
        # GPU read as "did not answer" (laptop W3 acceptance, 2026-11-15)
        $lines = @(Native { & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null })
        $code = $LASTEXITCODE
        $name = $lines | Select-Object -First 1
        if ($code -eq 0 -and $name) {
            $script:Gpu = $true
            $parts = "$name" -split ",\s*"
            $gpuText = if ($parts.Count -ge 2) { "$($parts[0]) (driver $($parts[1]))" } else { "$name" }
        } else { $gpuText = "nvidia-smi did not answer: a CPU install" }
    } else { $gpuText = "no NVIDIA GPU: a CPU install" }
    if ($env:CUDA_PATH -and (Test-Path -LiteralPath $env:CUDA_PATH)) { $script:CudaPath = $env:CUDA_PATH }
    if ($BuildCore -or $BuildToolbox) {
        if (HaveMsvc) { Log "C++ build tools: present" }
        else {
            $bad += "the C++ build tools are missing (needed by -BuildCore / -BuildToolbox): install the free Visual Studio Build Tools, https://visualstudio.microsoft.com/visual-cpp-build-tools/, workload 'Desktop development with C++'"
        }
    }
    if ($bad.Count) {
        Write-Host "  $($script:Sym.bad) Computer" -ForegroundColor Red
        $bad | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        $script:Label = ""
        Fail "this computer does not meet the requirements above." "fix what is listed, then run the installer again"
    }
    Done "Computer" "$os, $free GB free, $gpuText"
}

# ── 2. micromamba ─────────────────────────────────────────────────────────
function EnsureMicromamba {
    $script:Step = "2. micromamba (the package installer)"
    $exe = Join-Path $Root "bin\micromamba.exe"
    if ((Test-Path -LiteralPath $exe) -and ((Sha256Of $exe) -eq $MicromambaSha)) { Present "micromamba"; return $exe }
    Begin "micromamba"
    Download $MicromambaUrl $exe $MicromambaSha "micromamba"
    Done
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
    if ($LASTEXITCODE -eq 0 -and "$have".Trim() -eq "0.12.1" -and -not $BuildToolbox) { Log "DeepCell-Toolbox 0.12.1: present"; return }
    $entry = LockEntry $ToolboxLock "0.12.1" "win_amd64"
    if ($entry -and -not $BuildToolbox) {
        $whl = Join-Path $Repo "envs\windows\wheels\$($entry.File)"
        if (-not (Test-Path -LiteralPath $whl)) { Fail "the DeepCell-Toolbox wheel $whl that ships with this checkout is missing." "update the checkout (git pull), or run again with -BuildToolbox" }
        if ((Sha256Of $whl) -ne $entry.Sha) { Fail "$whl is not the expected file (its SHA-256 differs from envs\windows\wheels.lock)." "do not use it; restore it with git (git checkout -- envs/windows/wheels) and run again" }
        Log "DeepCell-Toolbox 0.12.1: installing the wheel that ships with this checkout"
        Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--no-deps", $whl)
    } elseif ($BuildToolbox) {
        Log "building DeepCell-Toolbox 0.12.1 from source (-BuildToolbox; step 1 checked the compiler)"
        Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--no-deps", "DeepCell-Toolbox==0.12.1")
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
    if (Test-Path -LiteralPath $ok) { Present "Python environment"; Log "environment block01-${id}: present"; return }
    $script:Label = "Python environment"
    if (Test-Path -LiteralPath $prefix) {
        if (-not (Test-Path -LiteralPath $owner)) { Fail "$prefix exists and was not created by this installer; it is left untouched." "move it away or use another -Root" }
        Log "environment block01-${id}: an unfinished install, made again"
        Remove-Item -LiteralPath $prefix -Recurse -Force
    }
    Begin "Python environment" "a few GB, 10-30 min"
    Log "environment block01-$id"
    New-Item -ItemType Directory -Force -Path (Split-Path $prefix) | Out-Null
    Set-Content -LiteralPath $owner -Value "created by scripts\install_block01.ps1 $(Get-Date -Format s)"
    $env:MAMBA_ROOT_PREFIX = Join-Path $Root "mamba"
    # conda-forge only: no Anaconda channel (its terms bind larger organisations)
    Run $mamba @("create", "-y", "-q", "-p", $prefix, "--override-channels", "-c", "conda-forge", "python=3.10", "pip")
    $env:PIP_CACHE_DIR = Join-Path $Root "cache\pip"
    Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--upgrade", "pip<27")
    Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "-r", (Join-Path $Repo "envs\ci\requirements-cpu.txt"))
    $index = if ($script:Gpu) { $TorchIndexGpu } else { $TorchIndexCpu }
    Run $script:Py (@("-m", "pip", "install", "-q", "--no-warn-script-location") + $TorchPins + @("--index-url", $index))
    Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "-r", (Join-Path $Repo "envs\windows\requirements.txt"))
    EnsureToolbox
    Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--no-deps", "-r", (Join-Path $Repo "envs\windows\requirements-nodeps.txt"))
    Set-Content -LiteralPath $ok -Value "finished $(Get-Date -Format s)"
    Done
}

# ── 3b. the SpatialData environment (Step 4's SpatialData export) ─────────
# Its own small environment, as on Linux: spatialdata 0.5.0 needs NumPy 2,
# the main environment stays on NumPy 1.26 for TensorFlow. The pip list is
# Linux's (envs\block01_spatialdata\requirements-pip.txt, every package
# pinned, --no-deps); its Python is recorded as BLOCK01_SPATIALDATA_PYTHON.
function SdLockId {
    $text = (Get-Content -Raw -LiteralPath (Join-Path $SdSpec "requirements-pip.txt")) + "`n" + ($SdConda -join " ")
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 8)
}

function EnsureSpatialData([string]$mamba) {
    $script:Step = "3b. the SpatialData environment"
    if ($NoSpatialData) {
        Dim "  $($script:Sym.run) SpatialData environment  skipped (-NoSpatialData): Step 4 asks for a Python with spatialdata"
        return
    }
    $prefix = Join-Path $Root "envs\block01_spatialdata-$(SdLockId)"
    $py = Join-Path $prefix "python.exe"
    $owner = "$prefix.owner"
    $ok = Join-Path $prefix ".block01_env_ok"
    $script:Label = "SpatialData environment"
    if (Test-Path -LiteralPath $ok) {
        Present "SpatialData environment"
    } else {
        if (Test-Path -LiteralPath $prefix) {
            if (-not (Test-Path -LiteralPath $owner)) { Fail "$prefix exists and was not created by this installer; it is left untouched." "move it away, use another -Root, or skip it with -NoSpatialData" }
            Log "SpatialData environment: an unfinished install, made again"
            Remove-Item -LiteralPath $prefix -Recurse -Force
        }
        Begin "SpatialData environment" "for Step 4's export, 2-5 min"
        New-Item -ItemType Directory -Force -Path (Split-Path $prefix) | Out-Null
        Set-Content -LiteralPath $owner -Value "created by scripts\install_block01.ps1 $(Get-Date -Format s)"
        $env:MAMBA_ROOT_PREFIX = Join-Path $Root "mamba"
        Run $mamba (@("create", "-y", "-q", "-p", $prefix, "--override-channels", "-c", "conda-forge") + $SdConda)
        Run $py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--no-deps", "-r", (Join-Path $SdSpec "requirements-pip.txt"))
        Run $py @("-m", "pip", "check") "report the log: the pinned SpatialData packages do not fit together on this computer (or install with -NoSpatialData)"
        Set-Content -LiteralPath $ok -Value "finished $(Get-Date -Format s)"
        Done
    }
    $err = Join-Path $Root "spatialdata.err"
    $v = Native { & $py -I -c "import spatialdata; print(spatialdata.__version__)" 2> $err } | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0 -or -not "$v") {
        Fail "spatialdata does not import in $prefix (its error: $err)." "run the installer again; if it repeats, report that file (or install with -NoSpatialData)"
    }
    Log "spatialdata $v in $prefix"
    $script:SdPrefix = $prefix
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
    if ((InstalledCore) -eq "$want 1") { Present "Viewer core $want"; return }
    Begin "Viewer core $want"
    $entry = LockEntry $WheelsLock $want "windows"
    if ($entry -and -not $BuildCore) {
        $whl = Join-Path $Root "downloads\core-$want\$($entry.File)"
        if (-not (Test-Path -LiteralPath $whl) -or (Sha256Of $whl) -ne $entry.Sha) {
            Download "$ReleaseUrl/core-v$want/$($entry.File)" $whl $entry.Sha "viewer core"
        }
    } elseif ($BuildCore) {
        if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { Fail "-BuildCore needs Rust (cargo) on PATH." "install Rust from https://rustup.rs and run again" }
        Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "maturin==1.15.0")
        $out = Join-Path $Root "downloads\core-$want-build"
        Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue
        Run $script:Py @("-m", "maturin", "build", "--release", "--locked", "-i", $script:Py, "-m", $CargoToml, "-o", $out)
        $whl = (Get-ChildItem -LiteralPath $out -Filter "fusion_viewer_core-$want-*.whl" | Select-Object -First 1).FullName
        if (-not $whl) { Fail "the build made no wheel." "" }
    } else {
        Fail "there is no prebuilt viewer core for version $want (a development checkout)." "run again with -BuildCore (needs Rust and the Visual Studio C++ Build Tools)"
    }
    Run $script:Py @("-m", "pip", "install", "-q", "--no-warn-script-location", "--no-deps", "--force-reinstall", $whl)
    $have = InstalledCore
    if ($have -ne "$want 1") { Fail "the installed viewer core reports '$have', expected '$want 1'." "" }
    Done
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

function ManifestSize([string]$key, [string]$suffix) {
    $entry = (Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json).models.$key
    $f = @($entry.files | Where-Object { $_.path.EndsWith($suffix) }) | Select-Object -First 1
    if ($f) { return [long]$f.size }
    return 0
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

# the model licences, one row each on the card (backlog 23, 2026-11-15: one
# card, one question, before the long steps -- nobody has to wait for it)
$Pretty = [ordered]@{ cellpose_cpsam = "Cellpose-SAM"; stardist_2D_versatile_fluo = "StarDist"; mesmer_multiplex_segmentation = "Mesmer" }
$Licence = @{
    cellpose_cpsam = @{ Size = "1.2 GB"; Text = "BSD-3-Clause; trained on CC-BY-NC data"; Strict = $false
                        Url = "https://huggingface.co/mouseland/cellpose-sam  (and the Cellpose README)" }
    stardist_2D_versatile_fluo = @{ Size = "5 MB"; Text = "BSD-3-Clause"; Strict = $false
                                    Url = "https://github.com/stardist/stardist" }
    mesmer_multiplex_segmentation = @{ Size = "100 MB"; Text = "NON-COMMERCIAL academic use only"; Strict = $true
                                       Url = "https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE`n                  the PyTorch copy made from it has the same licence: never share it" }
}
$script:Accepted = @{}

# NeedsInstall KEY -> $true when a file of this model is missing (read from
# models.json by PowerShell itself: no Python needed yet)
function NeedsInstall([string]$key) {
    $entry = (Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json).models.$key
    foreach ($f in $entry.files) { if (-not (Test-Path -LiteralPath (Join-Path (ModelDir $key) $f.path))) { return $true } }
    return $false
}

# LicenceCard KEYS: the licences of the models about to be installed, on one
# card; one answer for all of them
function LicenceCard([string[]]$keys) {
    $keys = @($keys | Where-Object { $_ })
    if (-not $keys.Count) { return }
    $script:Step = "the model licences"
    $script:Label = "Model licences"
    $rows = @($keys | ForEach-Object { " {0}{1}{2} " -f $Pretty[$_].PadRight(14), $Licence[$_].Size.PadRight(8), $Licence[$_].Text })
    $w = [math]::Max(40, ($rows | Measure-Object -Property Length -Maximum).Maximum)
    $s = $script:Sym
    Write-Host ""
    $title = " Model licences "
    Write-Host ("  " + $s.tl + $title + ([string]$s.h * ($w - $title.Length)) + $s.tr)
    for ($i = 0; $i -lt $keys.Count; $i++) {
        Write-Host ("  " + $s.v) -NoNewline
        if ($Licence[$keys[$i]].Strict) { Write-Host $rows[$i].PadRight($w) -ForegroundColor Yellow -NoNewline }
        else { Write-Host $rows[$i].PadRight($w) -NoNewline }
        Write-Host $s.v
    }
    Write-Host ("  " + $s.bl + ([string]$s.h * $w) + $s.br)
    foreach ($k in $keys) { Write-Host ("    {0}{1}" -f $Pretty[$k].PadRight(14), $Licence[$k].Url) -ForegroundColor DarkGray }
    foreach ($k in $keys) { Log "licence $k : $($Licence[$k].Text) -- $($Licence[$k].Url)" }
    if ($AcceptModelLicences) {
        Done "Model licences" "accepted with -AcceptModelLicences"
    } else {
        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
            Fail "the model licences need your answer, and there is no console to ask." "run the installer in a console, or read the licences above and add -AcceptModelLicences"
        }
        Write-Host ""
        Write-Host "  Accept all and install them? [y/N] " -NoNewline
        $answer = "$(Read-Host)".Trim().ToLowerInvariant()
        Log "licences answered: $answer"
        if ($answer -ne "y" -and $answer -ne "yes") { Fail "the licences were not accepted, so no model was installed." "run the installer again and answer y, or give your own copies with -Models DIR" }
    }
    Write-Host ""
    foreach ($k in $keys) { $script:Accepted[$k] = $true }
}

# AcceptLicence KEY: a model the card did not cover (it went missing since)
function AcceptLicence([string]$key) {
    if ($script:Accepted[$key]) { return }
    LicenceCard @($key)
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
    # `continue sources`: a bare `continue` inside the switch below only
    # leaves the switch, and the empty stage was then checked and reported
    # as "its files do not match" (the laptop's install, 2026-11-16)
    :sources foreach ($src in @("official", "mirror", "zip", "folder")) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        $archive = Join-Path $stage "m.tar.gz"
        switch ($src) {
            "official" {
                Log "Mesmer: trying DeepCell's public archive"
                if (-not (TryDownload $MesmerUrlOfficial $archive "Mesmer")) { Dim "    DeepCell's archive is not available; trying the next source"; continue sources }
                if ((Sha256Of $archive) -ne $MesmerSha) { Dim "    DeepCell's archive is not the expected file; trying the next source"; continue sources }
                if (-not (MesmerFromArchive $archive $stage)) { continue sources }
                Remove-Item -LiteralPath $archive -Force
                $from = "DeepCell's archive"
            }
            "mirror" {
                $mirror = if ($MesmerUrl) { $MesmerUrl } else { $MesmerMirrorDefault }
                Log "Mesmer: trying the mirror $mirror"
                if (-not (TryDownload $mirror $archive "Mesmer")) { Dim "    the mirror is not available"; continue sources }
                if (-not (MesmerFromArchive $archive $stage)) { Dim "    the mirror's file holds no MultiplexSegmentation folder"; continue sources }
                Remove-Item -LiteralPath $archive -Force
                $from = if ($MesmerUrl) { "the mirror" } else { "the lab's mirror" }
            }
            "zip" {
                if (-not $MesmerZip) { continue sources }
                Log "Mesmer: trying the archive $MesmerZip"
                if (-not (Test-Path -LiteralPath $MesmerZip)) { Dim "    $MesmerZip does not exist"; continue sources }
                if (-not (MesmerFromArchive ([IO.Path]::GetFullPath($MesmerZip)) $stage)) { Dim "    $MesmerZip holds no MultiplexSegmentation folder"; continue sources }
                $from = $MesmerZip
            }
            "folder" {
                if (-not $Models) { continue sources }
                $from = Join-Path $Models $key
                if ((ModelState $key $from) -ne "ok") { continue sources }
                Log "Mesmer: copying the verified copy from $from"
                foreach ($f in ManifestFiles $key) {
                    $dst = Join-Path $stage $f
                    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
                    Copy-Item -LiteralPath (Join-Path $from $f) -Destination $dst
                }
            }
        }
        if ((ModelState $key $stage) -eq "ok") {
            VerifyIntoPlace $key $stage $dir
            Done "Mesmer" "from $from, verified ($(Took $script:StepT0))"
            return
        }
        Dim "    its files do not match models.json; not used"
    }
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    $script:MesmerMissing = $true
    Unpend; $script:Pending = ""
    Warn "Mesmer: no source worked; the install goes on without it (add it later with -MesmerOnly)"
}

function FetchModel([string]$key, [string]$dir) {
    $stage = "$dir.stage"
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    switch ($key) {
        "cellpose_cpsam" { Download $CpsamUrl (Join-Path $stage "cpsam") (ManifestSha $key "cpsam") $Pretty[$key] (ManifestSize $key "cpsam") }
        "stardist_2D_versatile_fluo" {
            $zip = Join-Path $stage "2D_versatile_fluo.zip"
            Download $StardistUrl $zip (ManifestSha $key ".zip") $Pretty[$key] (ManifestSize $key ".zip")
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
    Log "models under $Root\models"
    foreach ($key in $OriginalModels) {
        $dir = ModelDir $key
        $script:Label = $Pretty[$key]
        $script:StepT0 = Get-Date
        $st = ModelState $key $dir
        if ($st -eq "ok") { Present $Pretty[$key]; continue }
        if ($st -ne "missing") { Fail "model $key in $dir does not match models.json ($st); it is left untouched." "move that folder away (or use another -Root) and run the installer again" }
        AcceptLicence $key
        $script:Label = $Pretty[$key]
        if ($key -eq "mesmer_multiplex_segmentation") { Begin "Mesmer"; EnsureMesmer $dir; continue }
        if ($Models -and (ModelState $key (Join-Path $Models $key)) -eq "ok") {
            Log "model ${key}: copying the verified copy from $Models\$key"
            $stage = "$dir.stage"
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
            foreach ($f in ManifestFiles $key) {
                $dst = Join-Path $stage $f
                New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
                Copy-Item -LiteralPath (Join-Path (Join-Path $Models $key) $f) -Destination $dst
            }
            VerifyIntoPlace $key $stage $dir
            Done $Pretty[$key] "copied from $Models, verified"
            continue
        }
        Begin $Pretty[$key]
        FetchModel $key $dir
        Done $Pretty[$key] "downloaded and verified ($(Took $script:StepT0))"
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
    $script:Label = "StarDist (PyTorch weights)"
    AppEnv
    Log "StarDist: converting weights_best.h5 for the PyTorch engine"
    Push-Location $Repo
    try {
        $env:PYTHONPATH = $Repo
        Run $script:Py @((Join-Path $Repo "scripts\convert_stardist_to_torch.py"))
    } finally { Pop-Location; Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue }
}

# ── 6. the package link ──────────────────────────────────────────────────
function EnsureLink {
    $script:Step = "6. the package link for this checkout"
    $script:Label = "Package link"
    $pkg = Join-Path $StateDir "pkg"
    $link = Join-Path $pkg "block01"
    New-Item -ItemType Directory -Force -Path $pkg, (Join-Path $StateDir "run"), (Join-Path $StateDir "logs") | Out-Null
    if (Test-Path -LiteralPath $link) {
        $item = Get-Item -LiteralPath $link -Force
        $target = @($item.Target) | Select-Object -First 1
        if ($item.LinkType -ne "Junction" -and $item.LinkType -ne "SymbolicLink") { Fail "$link exists and is not a link; it is left untouched." "move it away and run the installer again" }
        if ([IO.Path]::GetFullPath($target).TrimEnd('\') -ne $Repo.TrimEnd('\')) { Fail "$link points at $target, not this checkout; it is left untouched." "remove that link yourself if it is stale, then run the installer again" }
        Log "package link: present"
        return
    }
    New-Item -ItemType Junction -Path $link -Target $Repo | Out-Null
    Log "package link: $link -> $Repo"
}

# ── 7. self-test ──────────────────────────────────────────────────────────
function InstalledEngines {
    if ($script:MesmerMissing) { return @("cellpose", "stardist") }
    return @("cellpose", "stardist", "mesmer")
}

# CheckSelftest LINES ENGINES -> @{engine = "ok" | "FAILED"}: ok when the
# engine said ok, exit 0 and a device
function CheckSelftest([string[]]$lines, [string[]]$want) {
    $res = [ordered]@{}
    foreach ($e in $want) { $res[$e] = "FAILED" }
    foreach ($l in $lines) {
        $t = "$l".Trim()
        if (-not $t.StartsWith("{")) { continue }
        $r = $t | ConvertFrom-Json
        if ($r.state -eq "ok" -and $r.exit_code -eq 0 -and $r.device) { $res[$r.engine] = "ok" }
        elseif ($res.Contains($r.engine)) { $res[$r.engine] = "FAILED" }
    }
    return $res
}

# SelftestEngines ENGINES -> the results (CheckSelftest); its lines go to the log
function SelftestEngines([string[]]$engines, [string]$label) {
    $run = Join-Path $StateDir "run"
    Push-Location $run
    try {
        $env:PYTHONPATH = $Repo
        $err = Join-Path $run "selftest.err"
        $outFile = Join-Path $run "selftest.out"
        $null = Spawn $script:Py (@("-m", "seg_runner.selftest") + $engines) $outFile $err
        $out = @(Get-Content -LiteralPath $outFile -ErrorAction SilentlyContinue)
    } finally { Pop-Location; Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue }
    Log "self-test $label"
    $out | ForEach-Object { Log "  $_" }
    if (Test-Path -LiteralPath $err) { Get-Content -LiteralPath $err -Tail 30 | ForEach-Object { Log "  stderr: $_" } }
    return (CheckSelftest $out $engines)
}

# the self-test as a table: one row per engine, one column per device
function SelftestTable([string[]]$engines, $gpu, $cpu) {
    Unpend; $script:Pending = ""
    $names = @{ cellpose = "Cellpose"; stardist = "StarDist"; mesmer = "Mesmer" }
    $cols = @()
    if ($gpu) { $cols += , @("GPU", $gpu) }
    $cols += , @("CPU", $cpu)
    Write-Host ("    {0}" -f "".PadRight(12)) -NoNewline
    foreach ($c in $cols) { Write-Host $c[0].PadRight(8) -ForegroundColor DarkGray -NoNewline }
    Write-Host ""
    foreach ($e in $engines) {
        Write-Host ("    {0}" -f $names[$e].PadRight(12)) -NoNewline
        foreach ($c in $cols) {
            $v = $c[1][$e]
            if ($v -eq "ok") { Write-Host "ok".PadRight(8) -ForegroundColor Green -NoNewline }
            else { Write-Host "$v".PadRight(8) -ForegroundColor Red -NoNewline }
        }
        Write-Host ""
        Log "self-test $e : $(($cols | ForEach-Object { "$($_[0]) $($_[1][$e])" }) -join ', ')"
    }
}

function Selftest {
    $script:Step = "7. self-test"
    AppEnv
    $run = Join-Path $StateDir "run"
    if ($SkipModels) {
        Dim "  $($script:Sym.run) Self-test           skipped (-SkipModels)"
    } else {
        Begin "Self-test" "one small task per engine"
        $engines = InstalledEngines
        $gpu = $null
        if ($script:Gpu) {
            $gpu = SelftestEngines $engines "on the GPU"
            $env:CUDA_VISIBLE_DEVICES = "-1"          # Step2's 'Use GPU' off
            try { $cpu = SelftestEngines $engines "with the GPU hidden" } finally { Remove-Item env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue }
        } else { $cpu = SelftestEngines $engines "" }
        $failed = @($engines | Where-Object { $cpu[$_] -ne "ok" -or ($gpu -and $gpu[$_] -ne "ok") })
        if ($failed.Count) {
            SelftestTable $engines $gpu $cpu
            Fail "the self-test failed for: $($failed -join ', ') (its error output is in $run\selftest.err)." "read the error, fix the cause (often the GPU driver) and run the installer again"
        }
        Push-Location $run
        try { Run $script:Py @((Join-Path $Repo "scripts\model_manifest.py"), "--verify", "--installed") } finally { Pop-Location }
        Done
        SelftestTable $engines $gpu $cpu
    }
    $pkg = Join-Path $StateDir "pkg"
    Push-Location $run
    try {
        $env:PYTHONPATH = $pkg
        $gerr = Join-Path $run "gpucheck.err"
        $bg = Native { & $script:Py -c "from block01.core import bg_correction as b; print('ok' if b.GPU_MORPH_AVAILABLE else 'cpu', b.GPU_MORPH_SMOKE_TEST)" 2> $gerr } | Select-Object -Last 1
        if ("$bg".StartsWith("ok")) { Done "GPU background correction" "ok" }
        elseif ($script:Gpu) { Warn "GPU background correction unavailable, the CPU is used ($bg)" }
        else { Log "background correction on the CPU ($bg)" }
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
    $script:Label = "App start"
    if ("$core".EndsWith("accepted")) { Log "app start check: $core"; Done "App starts" "viewer core $(CoreVersion)" }
    else {
        Get-Content -LiteralPath (Join-Path $run "startcheck.err") -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Log "  $_" }
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
    if ($script:SdPrefix) { $lines += "BLOCK01_SPATIALDATA_PYTHON=$(Join-Path $script:SdPrefix 'python.exe')" }
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
    Log "desktop shortcut: $lnk"
    Done "Desktop shortcut" "FusionFlux"
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
    $script:Label = "Mesmer"
    $script:StepT0 = Get-Date
    if ($st -eq "ok") { Present "Mesmer" }
    elseif ($st -eq "missing") {
        AcceptLicence $key
        Begin "Mesmer"
        EnsureMesmer $dir
        if ($script:MesmerMissing) { Fail "no Mesmer source worked (messages above)." "give a file with -MesmerZip FILE, a link with -MesmerUrl URL, or a folder with -Models DIR" }
    } else { Fail "$dir holds files that do not match models.json; they are left untouched." "move that folder away and run this again" }
    $script:Step = "the Mesmer self-test"
    AppEnv
    Begin "Self-test" "Mesmer"
    $res = SelftestEngines @("mesmer") ""
    if ($res["mesmer"] -ne "ok") { Fail "the Mesmer self-test failed (its error output is in $(Join-Path $StateDir 'run\selftest.err'))." "read the error, fix the cause and run this again" }
    Push-Location (Join-Path $StateDir "run")
    try { Run $script:Py @((Join-Path $Repo "scripts\model_manifest.py"), "--verify", "--installed") } finally { Pop-Location }
    Done
    # the record says Mesmer is there now
    $file = Join-Path $StateDir "install.env"
    (Get-Content -LiteralPath $file) -replace '^BLOCK01_MESMER=.*$', 'BLOCK01_MESMER=installed' | Set-Content -LiteralPath $file -Encoding UTF8
    Write-Host ""
    Say "Mesmer added. Start FusionFlux as before (the desktop shortcut or scripts\block01.cmd)."
    Dim "Log: $($script:LogFile)"
}

# ── main ──────────────────────────────────────────────────────────────────
# StartLog: .block01\logs\install-<time>.log, the tools' own output
function StartLog {
    $logs = Join-Path $StateDir "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $script:LogFile = Join-Path $logs "install-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    Set-Content -LiteralPath $script:LogFile -Value "FusionFlux installer $(Get-Date -Format s): checkout $Repo, install root $Root" -Encoding UTF8
}

function Header {
    Write-Host ""
    Write-Host "FusionFlux installer"
    Dim "  checkout    $Repo"
    Dim "  install to  $Root"
    Write-Host ""
}

function Main {
    if ($MesmerOnly) { Header; StartLog; MesmerOnlyInstall; return }
    $t0 = Get-Date
    Header
    if (-not $CheckOnly) { StartLog }
    Preflight
    if ($CheckOnly) { Dim "check only: nothing was written"; return }
    if (-not $SkipModels) { LicenceCard @($OriginalModels | Where-Object { NeedsInstall $_ }) }
    $script:Step = "2. starting"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    # one install into a root at a time
    try { $lock = [IO.File]::Open((Join-Path $Root ".install.lock"), "OpenOrCreate", "ReadWrite", "None") }
    catch { Fail "another install into $Root is running." "wait for it to finish, then run the installer again" }
    try {
        $mamba = EnsureMicromamba
        EnsureEnvironment $mamba
        EnsureSpatialData $mamba
        EnsureCore
        EnsureLink
        if ($SkipModels) { Dim "  $($script:Sym.run) Models              skipped (-SkipModels)" }
        else {
            EnsureModels
            ConvertStardist
        }
        Selftest
        WriteRecord
        WriteShortcut
    } finally { $lock.Close() }
    Write-Host ""
    if ($script:MesmerMissing) {
        Warn "Mesmer is not installed (its three methods are unavailable). Add it later with:"
        Warn "  scripts\install_block01.ps1 -MesmerOnly -MesmerZip FILE   (or -MesmerUrl URL, -Models DIR)"
        Write-Host ""
    }
    Write-Host "Done in $(Took $t0). " -ForegroundColor Green -NoNewline
    Write-Host "Double-click FusionFlux on the desktop, or run:"
    Write-Host "  $Repo\scripts\block01.cmd"
    Dim "Log: $($script:LogFile)"
    Log "done in $(Took $t0)"
}

if (-not $env:BLOCK01_INSTALL_SOURCED) { Main }
