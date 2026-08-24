# Session setup: TV-GAMING profile, Puck claim, Big Picture, READY marker.
# Runs as Scheduled Task \CouchGaming\Enter, dispatched by the K15 over SSH.
# Any failure before READY restores the office and leaves the TV input alone
# (the K15 only switches input after seeing the marker).
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'enter'

# Teardown wins, launch queues: wait out an in-flight Exit, else abort with
# nothing touched.
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
    # Kick VirtualHere first so dead-socket detection + reconnect overlap the rest
    Start-Process -WindowStyle Hidden $CG.Vh -ArgumentList '-t','LIST','-r',$CG.VhNudge
    Log ("primary height at start: {0}" -f (Get-PrimaryHeight))

    # 1. TV EDID visible (the K15 just powered it on)
    if (-not (Wait-For { (Get-TvNames) -match $CG.TvEdid } 30 'TV detected')) {
        throw 'S90C never appeared over HDMI - aborting, office display untouched'
    }

    # 2. Launch the TV-only profile without waiting - it settles during the USB work
    Start-Process $CG.TvGamingLnk
    Log 'TV-GAMING profile launched'

    # 3a. VirtualHere client (re)connects to the K15 hub
    if (-not (Wait-For { Get-PuckAddress } 30 'VirtualHere sees Puck')) {
        throw 'VirtualHere client never re-connected to the K15 hub'
    }

    # 3b. Claim the Puck (stale-claim recycle + enumeration-verified, see lib)
    Request-PuckClaim
    Write-CgEvent 'puck_claimed'

    # 4. Verify the profile took (it had the whole USB phase to settle)
    $retried = $false
    if (-not (Wait-For { Test-TvIsPrimary } 20 'TV is primary (2160p)')) {
        # No gate can catch a TV that never woke: while detached, its EDID and
        # all three WMI monitor classes read identically asleep or awake
        # (2026-08-13, full power cycle). A retry is the only defence.
        # The OFFICE apply between attempts is load-bearing: a failed TV-GAMING
        # apply detaches the desk without activating the TV, QueryDisplayConfig
        # then has no valid paths, and DisplayMagician cannot initialise, so
        # every further apply is a silent no-op (2026-08-13, 7 dead applies).
        $retried = $true
        Log 'TV-GAMING did not take - restoring OFFICE, then one retry'
        Write-CgEvent 'profile_retry' @{ profile = 'TV-GAMING' } 'warn'
        Stop-DisplayMagician
        # One attempt, not the abort path's two: step 4 plus the abort's own
        # attempts must fit inside the K15's 120 s READY wait (couch.py).
        if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 20 1 'office restored before retry')) {
            Log 'WARNING: OFFICE did not verify - the retry below will probably be a no-op'
        }
        $rescued = Invoke-DisplayProfile $CG.TvGamingLnk { Test-TvIsPrimary } 20 1 'TV is primary (2160p)'
        # A missed apply is explained only in DisplayMagician's log. Sampled
        # after the retry so the copy covers both attempts.
        Copy-DisplayMagicianLog $(if ($rescued) { 'retry-ok' } else { 'retry-failed' })
        if (-not $rescued) {
            throw 'TV never came up at 2160p - most likely still asleep (Ex-Link power_on is send-only; this machine cannot verify TV power)'
        }
    }
    # retried=True tracks whether the TV's wake is drifting slower.
    Write-CgEvent 'profile_applied' @{ profile = 'TV-GAMING'; retried = $retried }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Stop-DisplayMagician

    # 5. Big Picture + foreground. Steam routes controller input to the FOCUSED
    # window. No fallback to the 'Steam' window title - that is the desktop
    # library window, which chimes but moves nothing, and it wins the race when
    # a game from a previous session is still running (Big Picture then has no
    # window ~1 s after the URL handoff).
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

    # 6. Ready marker - the K15 switches the TV input only after seeing this.
    # Measure the foreground: AppActivate's true is a claim, not an observation,
    # and desktop Steam in front must never read as success. Sampled ~1 s before
    # the K15 flips the input, so desk-side runs name whatever you were doing.
    $fg = Get-ForegroundTitle
    if ($fg -eq $CG.SteamWindow) {
        $focused = $false
        Log "WARNING: desktop Steam is in the foreground - the controller will not reach the TV"
    }
    Set-ReadyMarker
    Log "READY (foreground: '$fg')"
    # dur_ms here is time-to-READY, the launch-health distribution. Warn when
    # nothing took the foreground - the TV still switches, but the failure looks
    # like success. Warn too when a game was already up, focused or not: four
    # such sessions (2026-08-13, turns 2c7936/457a79/14852d/b01c9d) were all
    # abandoned inside three minutes, Big Picture holding focus and answering
    # the pad while the TV frame never changed. Not input routing -
    # steam://forceinputappid/0 changed nothing on b01c9d. The only cure known
    # to work is the game not running: 8289e9 ran 90 clean minutes once AC6
    # was gone.
    Write-CgEvent 'ready' @{ focused = $focused; fg = $fg; running_appid = $running } $(if ($focused -and -not $running) { 'info' } else { 'warn' })
}
catch {
    # Height is sampled BEFORE the recovery changes it, and guarded so a
    # throwing probe cannot skip the office restore. It separates "the TV never
    # came up" (desk still at its own height) from "the apply detached
    # everything and left no active display" (2026-08-13).
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
