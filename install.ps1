# lamellar one-line installer for Windows 10/11.
#
#   irm https://lamellar.org/install.ps1 | iex
#
# lamellar's agent runs shell commands and needs a POSIX system, so on Windows it runs inside WSL2
# (a small Ubuntu that Windows manages for you). This script:
#   1. turns on WSL2 and installs Ubuntu if needed (one reboot the first time; it continues by itself after)
#   2. runs the Linux installer inside Ubuntu (Python, Ollama with your NVIDIA GPU, model, config, doctor)
#   3. adds a "lamellar" command, starts lamellar at login, and opens http://localhost:8102/ in your browser
#
# Options (environment variables): LAMELLAR_UI, LAMELLAR_MODEL, LAMELLAR_CTX, LAMELLAR_PORT, LAMELLAR_SRC, LAMELLAR_YES
# (same meaning as in install.sh). LAMELLAR_DISTRO picks the WSL distro (default: Ubuntu).
$ErrorActionPreference = 'Stop'
$Distro  = if ($env:LAMELLAR_DISTRO) { $env:LAMELLAR_DISTRO } else { 'Ubuntu' }
$Port    = if ($env:LAMELLAR_PORT)   { $env:LAMELLAR_PORT }   else { '8102' }
$ShUrl   = if ($env:LAMELLAR_SH_URL) { $env:LAMELLAR_SH_URL } else { 'https://lamellar.org/install.sh' }
$Home_   = Join-Path $env:LOCALAPPDATA 'lamellar'
$BinDir  = Join-Path $Home_ 'bin'
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "warning: $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; exit 1 }
function IsAdmin  { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

# Keep a copy of this script so it can resume after the reboot.
$Self = Join-Path $Home_ 'install.ps1'
if ($PSCommandPath -and (Test-Path $PSCommandPath)) { Copy-Item $PSCommandPath $Self -Force }
elseif ($MyInvocation.MyCommand.ScriptBlock) { Set-Content -Path $Self -Value $MyInvocation.MyCommand.ScriptBlock.ToString() -Encoding UTF8 }

if ([Environment]::OSVersion.Version.Build -lt 19041) { Die 'Windows 10 version 2004 (build 19041) or newer is required for WSL2.' }

# ---------- 1. WSL2 + Ubuntu ----------
function DistroReady {
  $list = (wsl.exe -l -q 2>$null) -replace "`0", '' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  return ($list -contains $Distro)
}

$wslOk = $false
try { wsl.exe --status *> $null; $wslOk = ($LASTEXITCODE -eq 0) } catch { $wslOk = $false }

if (-not $wslOk -or -not (DistroReady)) {
  if (-not (IsAdmin)) {
    Say 'Administrator rights are needed once to turn on WSL2. Asking now...'
    Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$Self`""
    exit 0
  }
  Say "Installing WSL2 and $Distro (this downloads about 500 MB)"
  wsl.exe --install -d $Distro --no-launch
  if ($LASTEXITCODE -ne 0) { wsl.exe --install --no-launch; wsl.exe --install -d $Distro --no-launch }
  if (-not (DistroReady)) {
    # WSL was just switched on and needs a reboot before the distro can be registered.
    Say 'WSL needs a restart. The installer will continue automatically after you log back in.'
    New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'lamellar-install' -Force `
      -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Self`"" | Out-Null
    $r = if ($env:LAMELLAR_YES -eq '1') { 'y' } else { Read-Host 'Restart now? (y/n)' }
    if ($r -match '^[yY]') { Restart-Computer -Force }
    exit 0
  }
}

# First start: register the distro with root as the default user, so no username prompt appears.
$launcher = Get-Command ("$($Distro.ToLower()).exe") -ErrorAction SilentlyContinue
if ($launcher) { & $launcher.Source install --root *> $null }
wsl.exe -d $Distro -u root -- true
if ($LASTEXITCODE -ne 0) { Die "Could not start $Distro in WSL. Open 'Ubuntu' from the Start menu once, then run this again." }
wsl.exe --set-default-version 2 *> $null
Say "$Distro is ready"

# NVIDIA: the Windows driver provides CUDA inside WSL; no Linux driver is needed.
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) { Warn 'No NVIDIA driver found on Windows; lamellar will run on CPU (slow). Install the driver from https://www.nvidia.com/drivers and run this again.' }

# ---------- 2. Linux installer inside WSL ----------
$pass = @()
foreach ($k in 'LAMELLAR_UI','LAMELLAR_MODEL','LAMELLAR_CTX','LAMELLAR_PORT','LAMELLAR_SRC','LAMELLAR_YES') {
  $v = [Environment]::GetEnvironmentVariable($k)
  if ($v) { $pass += "$k='$($v -replace "'", '')'" }
}
$pass += 'LAMELLAR_NO_BROWSER=1'
Say 'Running the Linux installer inside WSL'
$cmd = "apt-get update -qq >/dev/null 2>&1; command -v curl >/dev/null || apt-get install -y -qq curl ca-certificates >/dev/null; " +
       "curl -fsSL '$ShUrl' -o /tmp/lamellar-install.sh && env $($pass -join ' ') sh /tmp/lamellar-install.sh"
wsl.exe -d $Distro -u root -- sh -c $cmd
if ($LASTEXITCODE -ne 0) { Die "The Linux installer failed (exit $LASTEXITCODE). Fix the error above and run this again." }

# ---------- 3. Windows launcher, autostart, browser ----------
$cmdFile = Join-Path $BinDir 'lamellar.cmd'
@"
@echo off
rem lamellar launcher (written by install.ps1). Runs the Linux launcher inside WSL.
if /I "%1"=="start" goto start
if /I "%1"=="open" goto open
wsl.exe -d $Distro -u root -- /root/.local/bin/lamellar %*
exit /b %errorlevel%
:start
rem WSL stops a distro when nothing is attached, so keep one hidden session alive.
powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Process wsl.exe -WindowStyle Hidden -ArgumentList '-d $Distro -u root -- sh -c ""/root/.local/bin/lamellar start; exec sleep infinity""'"
timeout /t 8 /nobreak >nul
wsl.exe -d $Distro -u root -- /root/.local/bin/lamellar status
exit /b %errorlevel%
:open
start "" http://localhost:$Port/
"@ | Set-Content -Path $cmdFile -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath -split ';' | Where-Object { $_ -eq $BinDir })) {
  [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$BinDir"), 'User')
  Say "Added $BinDir to your PATH (open a new terminal to use the 'lamellar' command)"
}

# Start at login, hidden.
$startup = [Environment]::GetFolderPath('Startup')
Set-Content -Path (Join-Path $startup 'lamellar.cmd') -Encoding ASCII -Value "@echo off`r`ncall `"$cmdFile`" start >nul 2>&1"

& $cmdFile start
Start-Process "http://localhost:$Port/"

Say 'Running lamellar doctor'
& $cmdFile doctor
if ($LASTEXITCODE -ne 0) { Warn "doctor found problems; the lines marked FAIL say how to fix them." }
Say "Done. Open http://localhost:$Port/"
Say 'Commands (new terminal): lamellar start | stop | status | logs | doctor | open'
