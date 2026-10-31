<#
.SYNOPSIS
  Build the LeapMotor Mate Server .msi.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File packaging\windows\build_msi.ps1 [-Version 0.1.0]

  Needs only the .NET runtime (6 or later) and an internet connection; no administrator rights, no
  .NET SDK, no WiX install. Every download is pinned, and the binaries that end up inside the package
  (uv, WinSW) are checked against SHA-256 values written here.

  WiX is 5.0.2 on purpose: 6 and later refuse to build until a maintenance-fee licence is accepted
  (WIX7015) - the same reason MateDesktop stays on 5. It is run straight from its NuGet package
  (`dotnet wix.dll`), which is why no SDK is needed.

  Output: dist\LeapMotor-Mate-Server-<version>-x64.msi
#>
[CmdletBinding()]
param([string]$Version = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$WixVersion = "5.0.2"
$UvVersion = "0.12.19"
$UvSha256 = "6dbb02d79e419522f1c500f0adb1cddcff0cda7d59b0d66ea7f5e3b4a1b2f5f0"      # uv-x86_64-pc-windows-msvc.zip
$WinSwUrl = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$WinSwSha256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
$PythonVersion = "3.12"     # Mate needs 3.12 exactly: 3.13+ verifies TLS strictly and Leapmotor's certs fail it

$Here = $PSScriptRoot
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$Build = Join-Path $Repo "build"
$Tools = Join-Path $Build "tools"
$Root = Join-Path $Build "root"
$Dist = Join-Path $Repo "dist"

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$([IO.Path]::GetFileName($Exe)) $($Arguments[0]) failed (exit $LASTEXITCODE)" }
}
function Get-Verified($url, $dest, $sha256) {
    if ((Test-Path $dest) -and (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower() -eq $sha256) { return }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    $got = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
    if ($got -ne $sha256) { Remove-Item $dest -Force; Fail "$url failed its checksum (got $got)" }
}

if (-not $Version) {
    $line = Select-String -Path (Join-Path $Repo "mate_server\__init__.py") -Pattern '^SERVER_VERSION\s*=\s*"([^"]+)"'
    if (-not $line) { Fail "no SERVER_VERSION in mate_server\__init__.py" }
    $Version = $line.Matches[0].Groups[1].Value
}
$dotnet = (Get-Command dotnet -ErrorAction SilentlyContinue).Source
if (-not $dotnet) { Fail "the .NET runtime (6 or later) is required: https://dotnet.microsoft.com/download" }
Say "building LeapMotor Mate Server $Version"

New-Item -ItemType Directory -Force -Path $Tools, $Dist | Out-Null
if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
New-Item -ItemType Directory -Force -Path "$Root\bin", "$Root\lib" | Out-Null

# ── tools: WiX (from NuGet) and uv ───────────────────────────────────────────────────────────
foreach ($pkg in "wix", "WixToolset.UI.wixext", "WixToolset.Util.wixext", "WixToolset.Firewall.wixext") {
    $dir = Join-Path $Tools "$pkg.$WixVersion"
    if (-not (Test-Path $dir)) {
        Say "fetching $pkg $WixVersion"
        $nupkg = "$dir.zip"
        Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/$pkg/$WixVersion" -OutFile $nupkg -UseBasicParsing
        Expand-Archive -Path $nupkg -DestinationPath $dir -Force
        Remove-Item $nupkg
    }
}
$wixDll = Join-Path $Tools "wix.$WixVersion\tools\net6.0\any\wix.dll"
$ext = @("UI", "Util", "Firewall") | ForEach-Object { Join-Path $Tools "WixToolset.$_.wixext.$WixVersion\wixext5\WixToolset.$_.wixext.dll" }

$uvZip = Join-Path $Tools "uv-$UvVersion.zip"
Get-Verified "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $uvZip $UvSha256
$uvDir = Join-Path $Tools "uv-$UvVersion"
if (-not (Test-Path "$uvDir\uv.exe")) { Expand-Archive -Path $uvZip -DestinationPath $uvDir -Force }
$uv = "$uvDir\uv.exe"

# ── Python 3.12, bundled whole ───────────────────────────────────────────────────────────────
# uv's CPython builds are relocatable: the folder works wherever the installer puts it.
Say "staging Python $PythonVersion"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR = Join-Path $Tools "uv-cache"
Invoke-Checked $uv @("python", "install", "--quiet", $PythonVersion)
$pyDir = Get-ChildItem -Directory $env:UV_PYTHON_INSTALL_DIR -Filter "cpython-$PythonVersion.*-windows-x86_64-none" |
    Sort-Object { [version]($_.Name -replace '^cpython-([\d.]+)-.*$', '$1') } | Select-Object -Last 1
if (-not $pyDir) { Fail "uv did not install Python $PythonVersion" }
Copy-Item -Recurse $pyDir.FullName "$Root\python"
# Nothing Mate uses, and a third of the size: the test suite, IDLE, Tk.
foreach ($drop in "Lib\test", "Lib\idlelib", "Lib\tkinter", "Lib\turtledemo", "tcl", "Lib\ensurepip\_bundled") {
    if (Test-Path "$Root\python\$drop") { Remove-Item -Recurse -Force "$Root\python\$drop" }
}
Get-ChildItem -Path "$Root\python" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$py = "$Root\python\python.exe"
$pyVer = (& $py -c "import sys;print('%d.%d.%d'%sys.version_info[:3])")
Say "Python $pyVer ($($pyDir.Name))"

# ── the shell and what it imports ────────────────────────────────────────────────────────────
Say "staging the mate-server shell"
Invoke-Checked $uv @("pip", "install", "--quiet", "--python", $py, "--target", "$Root\lib", "certifi")
Copy-Item -Recurse "$Repo\mate_server" "$Root\lib\mate_server"
Get-ChildItem -Path "$Root\lib" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Copy-Item "$Here\mate-server.py", "$Here\LeapMotorMate.xml", "$Here\Mate.ico" $Root
Copy-Item $uv "$Root\bin\uv.exe"
Set-Content -Path "$Root\mate-server.cmd" -Encoding ASCII -Value @'
@echo off
rem Command line for the installed server, e.g.:  mate-server status
"%~dp0python\python.exe" -I "%~dp0mate-server.py" %*
'@

$winsw = Join-Path $Tools "WinSW-x64-2.12.0.exe"
Get-Verified $WinSwUrl $winsw $WinSwSha256
Copy-Item $winsw "$Root\LeapMotorMate.exe"

# A last look before packaging: the bundled interpreter must import the shell in isolated mode.
$smoke = & $py -I -B "$Root\mate-server.py" --version
if ($LASTEXITCODE -ne 0) { Fail "the staged shell does not start" }
Say "staged: $smoke"

# Precompile. The service account cannot write __pycache__ into Program Files, so without this every
# start would recompile the standard library in memory. unchecked-hash: the .pyc is trusted as is,
# whatever modification times Windows Installer gives the files - they only change with a new .msi.
Say "precompiling"
& $py -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$Root\python\Lib" "$Root\lib" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "compileall failed" }

# ── package ──────────────────────────────────────────────────────────────────────────────────
$msi = Join-Path $Dist "LeapMotor-Mate-Server-$Version-x64.msi"
Say "building $msi"
$wixArgs = @($wixDll, "build", "-arch", "x64", "-d", "Version=$Version", "-d", "RootDir=$Root",
             "-b", $Here, "-o", $msi, "-nologo")
foreach ($e in $ext) { $wixArgs += @("-ext", $e) }
$wixArgs += (Join-Path $Here "Product.wxs")
$env:DOTNET_ROLL_FORWARD = "Major"     # wix 5 targets net6.0; let a newer runtime run it
Invoke-Checked $dotnet $wixArgs

$hash = (Get-FileHash -Algorithm SHA256 $msi).Hash.ToLower()
"$hash  $([IO.Path]::GetFileName($msi))" | Set-Content -Encoding ASCII "$msi.sha256"
Say ("done: {0} ({1:N1} MB)" -f $msi, ((Get-Item $msi).Length / 1MB))
Write-Host "    sha256 $hash"
