# Registers the CleverSwitch logon task: windowless, logging to ~/cleverswitch.log,
# restart-on-failure, no 3-day execution limit. Safe to re-run any time, including
# after reinstalling cleverswitch with uv (restores the hidapi.dll backup).
# Run in an elevated PowerShell with cleverswitch_logged.py next to this script.
$ErrorActionPreference = "Stop"

$toolDir = (& uv tool dir).Trim()
$scripts = Join-Path $toolDir "cleverswitch\Scripts"
$pythonw = Join-Path $scripts "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "cleverswitch not installed via uv (missing $pythonw)" }

# hidapi.dll: uv reinstalls wipe Scripts, so keep a backup in the profile and restore it.
$dll = Join-Path $scripts "hidapi.dll"
$backup = Join-Path $env:USERPROFILE "hidapi.dll"
if (Test-Path $dll) { Copy-Item $dll $backup -Force }
elseif (Test-Path $backup) { Copy-Item $backup $dll -Force }
else { Write-Warning "hidapi.dll missing from $scripts and no backup at $backup" }

$wrapper = Join-Path $env:USERPROFILE "cleverswitch_logged.py"
Copy-Item (Join-Path $PSScriptRoot "cleverswitch_logged.py") $wrapper -Force

Get-Process pythonw, cleverswitch -ErrorAction SilentlyContinue | Stop-Process -Force

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$wrapper`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "CleverSwitch" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "CleverSwitch"
Write-Host "CleverSwitch task registered and started. Log: $env:USERPROFILE\cleverswitch.log"
