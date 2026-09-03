# Registers the K15's two lanes as scheduled tasks. Run once per machine from
# a NORMAL (not administrator) PowerShell, from the checkout root, after the
# venv exists:
#
#   .\Setup-K15-Tasks.ps1
#
# Each task runs `slopstation-lane <name>` at logon, in the logged-on user's
# own session - a service would land in session 0, which reaches neither the
# Puck nor the audio devices. The wrapper restarts a crashed lane itself; the
# scheduler's restart-on-failure is not set because it does not fire on a
# non-zero exit at all (measured). The task, not a shortcut, is what
# brings the lanes up after a reboot; there is nothing to put in shell:startup
# any more. Re-run after moving the checkout.
param([string]$Checkout = $PSScriptRoot)

$lane = Join-Path $Checkout ".venv\Scripts\slopstation-lane.exe"
if (-not (Test-Path $lane)) {
    throw "no $lane - create the venv and pip install -e first"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# No time limit (the default silently ends a task after three days), one
# instance at a time, and a task that missed its logon trigger runs on the
# next chance rather than waiting for the next logon.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
# Limited, never Highest: an elevated lane cannot be stopped from the normal
# window the deployer runs in.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

foreach ($name in "listener", "voice") {
    $action = New-ScheduledTaskAction -Execute $lane -Argument $name `
        -WorkingDirectory $Checkout
    Register-ScheduledTask -TaskPath "\Slopstation\" -TaskName $name `
        -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
    "registered \Slopstation\$name -> $lane $name"
}
"start them now with .\Start-Slopstation.bat"
