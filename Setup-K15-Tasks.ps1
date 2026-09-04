# Register both K15 lanes from a non-administrator PowerShell after creating
# the virtual environment:
#
#   .\Setup-K15-Tasks.ps1
#
# The tasks run in the logged-in session so they can access audio and controller
# devices. Re-run this script after moving the checkout.
param([string]$Checkout = $PSScriptRoot)

$lane = Join-Path $Checkout ".venv\Scripts\slopstation-lane.exe"
if (-not (Test-Path $lane)) {
    throw "no $lane - create the venv and pip install -e first"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Allow one unlimited instance and start it after a missed logon trigger.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
# Keep tasks non-elevated so the deployer can stop them.
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
