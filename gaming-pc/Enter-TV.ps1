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

    # 5. Big Picture, and the foreground policy that decides whether the
    # session is usable at all. Steam delivers controller input to the FOCUSED
    # window, so this step is not cosmetic.
    #
    # It used to try 'Steam Big Picture Mode' and then fall back to 'Steam'.
    # That fallback matches the DESKTOP library window, which is present and
    # visible the whole time Steam is running - Exit means to minimize it on
    # teardown but that guard had never once fired (see Hide-DesktopSteam).
    # Worse, the fallback succeeding on the first pass set $focused and broke
    # out of the retry loop, so the wait for Big Picture never actually waited.
    #
    # Four launches, one transcript line apart: the one that logged
    # focused 'Steam Big Picture Mode' worked; the three that logged 'Steam'
    # gave a controller that chimed on every button and moved nothing on the
    # TV. The discriminator was a game left running by the previous session
    # (Exit closes Big Picture but never quits games): with a game up, Big
    # Picture has no window ~1 s after the URL handoff, so the fallback won
    # every time.
    Hide-DesktopSteam
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    $running = Get-RunningAppId
    if ($running) {
        # A game outlived the last teardown, and it is what the couch is for.
        # Leave it in front rather than yanking Big Picture on top of it -
        # Big Picture was still opened above, so quitting the game lands in
        # the TV shell instead of the desktop. Nothing here fights the game
        # for the foreground; the library window simply can no longer be what
        # ends up holding the controller.
        $focused = $true
        Log "game $running still running - leaving it in front"
    } else {
        $wsh = New-Object -ComObject WScript.Shell
        $focused = Wait-For { $wsh.AppActivate($CG.BpmWindow) } 20 'Big Picture focused'
        if (-not $focused) { Log 'WARNING: Big Picture never took focus - session will need a click' }
    }

    # 6. Ready marker - the K15 switches the TV input only after seeing this
    #
    # First, MEASURE the foreground rather than trusting the branch above to
    # have got it right. The game branch cannot verify what it did (there is no
    # reliable appid -> window mapping), so without this it would assert
    # focused=True the way the old code did - and an assertion is exactly what
    # let this bug survive three sessions: `focused=True` sat in the ready
    # event while the controller reached nothing. The desktop library window in
    # front is the one state we KNOW is broken, so it can never read as success.
    $fg = Get-ForegroundTitle
    if ($fg -eq $CG.SteamWindow) {
        $focused = $false
        Log "WARNING: desktop Steam is in the foreground - the controller will not reach the TV"
    }
    Set-ReadyMarker
    Log "READY (foreground: '$fg')"
    # The milestone the whole system is gated on: dur_ms here IS time-to-READY,
    # which is the distribution the launch-health dashboard is built from.
    # Warn-level when nothing took the foreground: the TV still switches (a
    # session you can rescue with one click beats no session), but the failure
    # this whole step exists to prevent looks EXACTLY like success from here,
    # so it must not be logged as one. `ready focused=False` is the alert, and
    # `fg` is the field that says what to go look at.
    Write-CgEvent 'ready' @{ focused = $focused; fg = $fg } $(if ($focused) { 'info' } else { 'warn' })
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
