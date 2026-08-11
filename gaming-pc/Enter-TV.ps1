# Session setup: TV-GAMING profile, Puck claim, Big Picture, READY marker.
# Runs as Scheduled Task \CouchGaming\Enter, dispatched by the K15 over SSH.
# Any failure before READY restores the office and leaves the TV input alone
# (the K15 only switches input after seeing the marker).
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'enter'

# Conflict rule: teardown wins, launch queues. If an Exit is mid-flight, wait
# briefly for it to finish; if it won't, abort - nothing has been touched yet
# and the K15 leaves the TV alone when READY never appears.
if (Test-CgTaskRunning 'Exit') {
    Log 'Exit task is running - waiting for teardown to finish'
    if (-not (Wait-For { -not (Test-CgTaskRunning 'Exit') } 45 'Exit finished')) {
        Log 'Exit still running - aborting launch, TV untouched'
        Write-CgEvent 'enter_failed' @{ reason = 'exit_still_running' } 'error'
        Stop-Transcript
        throw 'aborted: Exit task still running'
    }
}

try {
    # Kick the VirtualHere client immediately so dead-socket detection + reconnect
    # start now and overlap everything below
    Start-Process -WindowStyle Hidden $CG.Vh -ArgumentList '-t','LIST','-r',$CG.VhNudge
    Log ("primary height at start: {0}" -f (Get-PrimaryHeight))

    # 1. TV EDID visible (in the real flow the K15 just powered it on)
    if (-not (Wait-For { (Get-TvNames) -match $CG.TvEdid } 30 'TV detected')) {
        throw 'S90C never appeared over HDMI - aborting, office display untouched'
    }

    # 2. Launch the TV-only profile and DON'T wait for it - it settles while we do USB work
    Start-Process $CG.TvGamingLnk
    Log 'TV-GAMING profile launched'

    # 3a. Wait for the VirtualHere client to (re)connect to the K15 hub
    if (-not (Wait-For { (Get-VhList) -match [regex]::Escape($CG.Puck) } 30 'VirtualHere sees Puck')) {
        throw 'VirtualHere client never re-connected to the K15 hub'
    }

    # 3b. Claim the Puck (stale-claim recycle + enumeration-verified, see lib)
    Request-PuckClaim
    Write-CgEvent 'puck_claimed'

    # 4. NOW verify the profile actually took (it had the whole USB phase to settle)
    if (-not (Wait-For { Test-TvIsPrimary } 20 'TV is primary (2160p)')) {
        throw 'TV-GAMING profile did not take'
    }
    Write-CgEvent 'profile_applied' @{ profile = 'TV-GAMING' }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Stop-DisplayMagician

    # 5. Big Picture, forced to the foreground
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    Start-Sleep 1
    $wsh = New-Object -ComObject WScript.Shell
    $focused = $false
    for ($i = 0; -not $focused -and $i -lt 5; $i++) {
        foreach ($t in 'Steam Big Picture Mode','Steam') {
            if ($wsh.AppActivate($t)) { $focused = $true; Log "focused '$t'"; break }
        }
        if (-not $focused) { Start-Sleep 1 }
    }

    # 6. Ready marker - the K15 switches the TV input only after seeing this
    Set-ReadyMarker
    Log 'READY'
    # The milestone the whole system is gated on: dur_ms here IS time-to-READY,
    # which is the distribution the launch-health dashboard is built from.
    Write-CgEvent 'ready' @{ focused = $focused }
}
catch {
    # The failure path obeys the same rules as the success path: kill
    # DisplayMagician first (a hung instance is the likeliest reason we're
    # here), release best-effort, then a VERIFIED office apply. The TV input
    # was never switched - the K15 gates on READY.
    Write-CgEvent 'enter_failed' @{ err = "$_" } 'error'
    Stop-DisplayMagician
    Request-PuckRelease 1 | Out-Null
    if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 20 2 'office restored')) {
        Log 'WARNING: office did not verify during abort - ForceOfficeAtLogon converges at next logon'
    }
    Clear-ReadyMarker
    throw
}
finally { Stop-Transcript }
