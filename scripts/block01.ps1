<#
Start FusionFlux (Block01) on Windows (docs/v17_windows_native_plan.md, W3).

  scripts\block01.cmd [app arguments]        (or the desktop shortcut)

Runs THIS checkout with the install scripts\install_block01.ps1 recorded for
it in <repo>\.block01\install.env; refuses clearly when that record is
missing, names another checkout, or its environment, package link or viewer
core does not fit. The app runs in <repo>\.block01\run; its console output
also goes to <repo>\.block01\logs\block01-<date>.log.
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AppArgs)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $Repo ".block01"
$InstallHint = "powershell -ExecutionPolicy Bypass -File `"$Repo\scripts\install_block01.ps1`""

function Refuse([string]$why, [string]$todo) {
    Write-Host "[block01] cannot start: $why" -ForegroundColor Red
    Write-Host "[block01] what to do: $todo" -ForegroundColor Red
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

# the record: KEY=VALUE lines
function ReadRecord {
    $rec = Join-Path $StateDir "install.env"
    if (-not (Test-Path -LiteralPath $rec)) { Refuse "FusionFlux is not installed for this checkout ($rec is missing)." $InstallHint }
    $vals = [ordered]@{}
    foreach ($l in Get-Content -LiteralPath $rec -Encoding UTF8) {
        if ($l -match '^\s*#' -or $l -notmatch '=') { continue }
        $i = $l.IndexOf('='); $vals[$l.Substring(0, $i).Trim()] = $l.Substring($i + 1)
    }
    return $vals
}

function CheckInstall($rec) {
    if ("$($rec['BLOCK01_CHECKOUT'])".TrimEnd('\') -ne $Repo.TrimEnd('\')) {
        Refuse "the install record names another checkout ($($rec['BLOCK01_CHECKOUT']))." $InstallHint
    }
    $link = Join-Path $StateDir "pkg\block01"
    $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
    $target = if ($item) { @($item.Target) | Select-Object -First 1 } else { "" }
    if (-not $item -or -not $target -or [IO.Path]::GetFullPath($target).TrimEnd('\') -ne $Repo.TrimEnd('\')) {
        Refuse "$link does not point at this checkout." $InstallHint
    }
    $py = Join-Path $rec['BLOCK01_PREFIX'] "python.exe"
    if (-not (Test-Path -LiteralPath $py)) { Refuse "the environment $($rec['BLOCK01_PREFIX']) is gone." $InstallHint }
    if (-not (Test-Path -LiteralPath (Join-Path $rec['BLOCK01_PREFIX'] ".block01_install_ok"))) {
        Refuse "the install into $($rec['BLOCK01_PREFIX']) never finished." $InstallHint
    }
    return $py
}

$rec = ReadRecord
$py = CheckInstall $rec
foreach ($k in $rec.Keys) {
    if ($k -like "BLOCK01_CHECKOUT" -or $k -like "BLOCK01_PREFIX" -or $k -like "BLOCK01_ROOT" -or $k -like "BLOCK01_CORE_VERSION") { continue }
    Set-Item -Path "env:$k" -Value $rec[$k]
}
$run = Join-Path $StateDir "run"
$logs = Join-Path $StateDir "logs"
New-Item -ItemType Directory -Force -Path $run, $logs | Out-Null
$env:PYTHONPATH = Join-Path $StateDir "pkg"            # this checkout only; nothing written into the environment
Set-Location -LiteralPath $run

# the viewer core must fit this code (as scripts/block01's check_install)
$eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$why = & $py -c "from block01.ui.step1_core_adapter import core_unavailable_reason as r; print(r())" 2>&1 | Select-Object -Last 1
$rc = $LASTEXITCODE
$ErrorActionPreference = $eap
if ($rc -ne 0) { Refuse "the environment cannot load FusionFlux: $why" $InstallHint }
if ("$why".Trim()) { Refuse "its viewer core does not fit this code: $why" $InstallHint }

if ($rec['BLOCK01_MESMER'] -eq "missing") {
    Write-Host "[block01] note: Mesmer is not installed; add it with: scripts\install_block01.ps1 -MesmerOnly -MesmerZip FILE"
}
$log = Join-Path $logs ("block01-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Write-Host "[block01] starting FusionFlux; its output also goes to $log"
$ErrorActionPreference = "Continue"
# The app writes UTF-8 (PYTHONIOENCODING) and PowerShell reads a program's
# output in the console's encoding: GBK on a Chinese Windows garbled every
# dash and ellipsis. Read it as UTF-8 for this run; the console gets its own back.
$consoleEnc = $null
try { $consoleEnc = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
try {
    & $py -m block01.main @AppArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log
} finally {
    if ($consoleEnc) { try { [Console]::OutputEncoding = $consoleEnc } catch { } }
}
exit $LASTEXITCODE
