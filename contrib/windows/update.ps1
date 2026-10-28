# One-shot PC updater for this fork's CleverSwitch setup. Run in an elevated
# PowerShell; safe to re-run. Reinstalls from the keyboard-disconnect-relay
# branch, writes the peer and bounce config, fetches the latest task scripts
# and re-registers the logon task.
$ErrorActionPreference = "Stop"
$raw = "https://raw.githubusercontent.com/gavin74/CleverSwitch/pc-setup/contrib/windows"

$env:SETUPTOOLS_SCM_PRETEND_VERSION = "1.5.10"
uv tool install --reinstall "cleverswitch @ https://github.com/gavin74/CleverSwitch/archive/refs/heads/keyboard-disconnect-relay.zip"

mkdir "$env:USERPROFILE\.config\cleverswitch" -Force | Out-Null
Set-Content "$env:USERPROFILE\.config\cleverswitch\peer_host" 1
Set-Content "$env:USERPROFILE\.config\cleverswitch\bounce_followers" 0xB035

$dir = Join-Path $env:TEMP "cleverswitch-setup"
mkdir $dir -Force | Out-Null
Invoke-WebRequest "$raw/setup-task.ps1" -OutFile (Join-Path $dir "setup-task.ps1")
Invoke-WebRequest "$raw/cleverswitch_logged.py" -OutFile (Join-Path $dir "cleverswitch_logged.py")
& powershell -ExecutionPolicy Bypass -File (Join-Path $dir "setup-task.ps1")
