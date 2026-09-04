# Prepare the display, controller, and Big Picture for a couch session.
# Failures before READY restore the office display profile.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'enter'

# Wait for an in-progress teardown before starting.
if (Test-CgTaskRunning 'Exit') {
    Log 'Exit task is running - waiting for teardown to finish'
    if (-not (Wait-For { -not (Test-CgTaskRunning 'Exit') } 45 'Exit finished')) {
        Log 'Exit still running - aborting launch, TV untouched'
        Write-CgEvent 'enter_failed' @{ reason = 'exit_still_running' } 'error'
        Stop-CgTranscript
        throw 'aborted: Exit task still running'
    }
}

try {
    # Start VirtualHere reconnection while the display is prepared.
    Start-Process -WindowStyle Hidden $CG.Vh -ArgumentList '-t','LIST','-r',$CG.VhNudge
    Log ("primary height at start: {0}" -f (Get-PrimaryHeight))

    # 1. TV EDID visible (the K15 just powered it on)
    if (-not (Wait-For { (Get-TvNames) -match $CG.TvEdid } 30 'TV detected')) {
        throw "S90C never appeared over HDMI (Windows lists: $(@(Get-TvNames) -join ', ')) - aborting, office display untouched"
    }

    # 2. Launch the TV-only profile without waiting - it settles during the USB work
    Start-Process $CG.TvGamingLnk
    Log 'TV-GAMING profile launched'

    # 3a. VirtualHere client (re)connects to the K15 hub
    if (-not (Wait-For { Get-PuckAddress } 30 'VirtualHere sees Puck')) {
        throw 'VirtualHere client never re-connected to the K15 hub'
    }

    # 3b. Claim the Puck.
    Request-PuckClaim
    Write-CgEvent 'puck_claimed'

    # 4. Verify the profile took (it had the whole USB phase to settle)
    $retried = $false
    if (-not (Wait-For { Test-TvIsPrimary } 20 'TV is primary (2160p)')) {
        # Restore OFFICE before retrying so DisplayMagician has an active path.
        $retried = $true
        Log 'TV-GAMING did not take - restoring OFFICE, then one retry'
        Write-CgEvent 'profile_retry' @{ profile = 'TV-GAMING' } 'warn'
        Stop-DisplayMagician
        # Keep retries within the K15 READY timeout.
        if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 20 1 'office restored before retry')) {
            Log 'WARNING: OFFICE did not verify - the retry below will probably be a no-op'
        }
        $rescued = Invoke-DisplayProfile $CG.TvGamingLnk { Test-TvIsPrimary } 20 1 'TV is primary (2160p)'
        # Capture diagnostics from both profile attempts.
        Copy-DisplayMagicianLog $(if ($rescued) { 'retry-ok' } else { 'retry-failed' })
        if (-not $rescued) {
            throw 'TV never came up at 2160p - most likely still asleep (Ex-Link power_on is send-only; this machine cannot verify TV power)'
        }
    }
    # retried=True tracks whether the TV's wake is drifting slower.
    Write-CgEvent 'profile_applied' @{ profile = 'TV-GAMING'; retried = $retried }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Stop-DisplayMagician

    # 5. Open and focus Big Picture so it receives controller input.
    Hide-DesktopSteam
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    # Recorded, not acted on.
    $running = Get-RunningAppId
    $wsh = New-Object -ComObject WScript.Shell
    $focused = Wait-For { $wsh.AppActivate($CG.BpmWindow) } 20 'Big Picture focused'
    if (-not $focused) { Log 'WARNING: Big Picture never took focus - session will need a click' }
    if ($running) { Log "note: game $running was already running at Enter" }

    # 6. Write READY only after setup completes.
    $fg = Get-ForegroundTitle
    if ($fg -eq $CG.SteamWindow) {
        $focused = $false
        Log "WARNING: desktop Steam is in the foreground - the controller will not reach the TV"
    }
    Set-ReadyMarker
    Log "READY (foreground: '$fg')"
    # Warn if a game is already running or Big Picture lacks focus.
    Write-CgEvent 'ready' @{ focused = $focused; fg = $fg; running_appid = $running } $(if ($focused -and -not $running) { 'info' } else { 'warn' })
}
catch {
    # Capture the failed display state before restoring OFFICE.
    $height = -1
    try { $height = Get-PrimaryHeight } catch { }
    Write-CgEvent 'enter_failed' @{ err = "$_"; primary_height = $height } 'error'
    Stop-DisplayMagician
    Request-PuckRelease 1 | Out-Null
    if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 20 2 'office restored')) {
        Log 'WARNING: office did not verify during abort - ForceOfficeAtLogon converges at next logon'
    }
    Clear-ReadyMarker
    throw
}
finally { Stop-CgTranscript }
