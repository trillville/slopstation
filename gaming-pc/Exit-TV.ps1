# Session teardown: close Big Picture, restore OFFICE, release the Puck.
# Runs as Scheduled Task \CouchGaming\Exit (Big Picture tile, Ctrl+Alt+E, or
# Wake-Safety).
# Teardown policy: plow through - every step is best-effort and the logon
# failsafe converges anything this misses. Hence no $ErrorActionPreference = 'Stop'.
$ErrorActionPreference = 'Continue'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'exit'

# Conflict rule: teardown wins. If a launch is mid-flight, stop it - this
# script is the reconciler for whatever half-state the kill leaves (office,
# Puck release, ready marker are all handled below).
if (Test-CgTaskRunning 'Enter') {
    Log 'Enter task is running - stopping it (teardown wins)'
    Stop-CgTask 'Enter'
    Start-Sleep 1
}

# Leave Big Picture FIRST, while still on the TV - Steam's window never gets
# resolution-yanked mid-render (prevents a garbled desktop-Steam window)
Start-Process 'steam://close/bigpicture'
Log 'closing Big Picture'
Start-Sleep 2   # blind wait by design: Big Picture exposes no reliable closed signal

# Office first: controller stays live during teardown
if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 15 2 'ultrawide restored')) {
    Log 'WARNING: office did not verify - ForceOfficeAtLogon will converge at next logon'
}

if (Request-PuckRelease 3) { Log 'Puck released' }
else { Log 'WARNING: Puck may still be claimed - check VirtualHere client' }

# Repaint guard: minimize desktop Steam so it re-lays-out fresh (at the ultrawide's
# resolution) the next time it's opened - prevents the stale-4K garbled window
Add-Type -Namespace P2 -Name W -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
$sp = Get-Process steam -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($sp) { [void][P2.W]::ShowWindow($sp.MainWindowHandle, 6); Log 'Steam minimized' }   # 6 = SW_MINIMIZE

Clear-ReadyMarker
Log 'done'
Stop-Transcript
