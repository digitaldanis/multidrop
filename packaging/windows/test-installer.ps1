# Installs MultiDrop-Setup.exe silently for the current user, checks the
# install (files, shortcuts, autostart, self-test) and that uninstalling
# removes all of it. Used by CI; leaves the profile as it was.
param([string]$Setup = "dist\MultiDrop-Setup.exe")
$ErrorActionPreference = "Stop"

$app = "$env:LOCALAPPDATA\Programs\MultiDrop"
$exe = "$app\MultiDrop.exe"
$sendTo = "$env:APPDATA\Microsoft\Windows\SendTo\MultiDrop.lnk"
$startMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\MultiDrop.lnk"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Check($ok, $what) {
    if (-not $ok) { throw "FAIL: $what" }
    Write-Host "ok  $what"
}
function RunValue { (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).MultiDrop }

if ((Test-Path $app) -or (RunValue)) { throw "MultiDrop is already installed here; not touching it" }

$p = Start-Process $Setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=sendto,autostart" -Wait -PassThru
Check ($p.ExitCode -eq 0) "setup exit code 0"
Check (Test-Path $exe) "MultiDrop.exe installed"
Check (Test-Path "$app\mirror\doubletake.exe") "screen mirroring engine installed"
Check (Test-Path "$app\mirror\gstreamer\lib\gstreamer-1.0\gstd3d11.dll") "its GStreamer with d3d11 capture"
Check (Test-Path $startMenu) "Start menu shortcut"
Check (Test-Path $sendTo) "Send to shortcut"
Check ((RunValue) -eq "`"$exe`" --no-browser") "start with Windows"

$p = Start-Process $exe -ArgumentList "--selftest" -PassThru
if (-not $p.WaitForExit(90000)) { $p.Kill(); throw "FAIL: self-test timed out" }
Check ($p.ExitCode -eq 0) "installed app passes its self-test"

Start-Process "$app\unins000.exe" -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
# The uninstaller runs a copy of itself from %TEMP%: wait for it to finish.
for ($i = 0; $i -lt 60 -and (Test-Path $exe); $i++) { Start-Sleep 1 }
Check (-not (Test-Path $exe)) "uninstalled: program removed"
Check (-not (Test-Path "$app\mirror")) "uninstalled: screen mirroring engine removed"
Check (-not (Test-Path $startMenu)) "uninstalled: Start menu shortcut removed"
Check (-not (Test-Path $sendTo)) "uninstalled: Send to shortcut removed"
Check (-not (RunValue)) "uninstalled: autostart removed"
Write-Host "PASS"
