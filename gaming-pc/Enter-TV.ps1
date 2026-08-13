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
    $retried = $false
    if (-not (Wait-For { Test-TvIsPrimary } 20 'TV is primary (2160p)')) {
        # A retry is the ENTIRE defence here, because this condition cannot be
        # detected in advance. While the TV is detached, its EDID and all three
        # WMI monitor classes read identically whether the panel is awake or
        # asleep - measured across a full power cycle, 2026-08-13, nothing moved.
        # So step 1's TV-detect gate cannot catch a TV that never woke, and no
        # smarter gate can: the information does not exist on this machine.
        #
        # The OFFICE apply in the middle is load-bearing, not tidiness. A failed
        # TV-GAMING apply detaches the desk display WITHOUT activating the TV,
        # and in that state QueryDisplayConfig has no valid paths at all:
        # DisplayMagician cannot initialise, cannot load its own profiles, and
        # every further apply is a silent no-op (2026-08-13: seven consecutive
        # dead applies, recovering only when the TV was switched on by hand).
        # Re-applying OFFICE re-establishes a config it can work from - and
        # hands the desk back meanwhile, instead of leaving both screens dark.
        $retried = $true
        Log 'TV-GAMING did not take - restoring OFFICE, then one retry'
        Write-CgEvent 'profile_retry' @{ profile = 'TV-GAMING' } 'warn'
        Stop-DisplayMagician
        # ONE attempt, not the two the abort path uses: if OFFICE will not come
        # back within 20 s the retry below is hopeless anyway, and the whole of
        # step 4 has to fit inside the K15's 120 s READY wait (couch.py) with
        # room for the abort's own OFFICE attempts afterwards.
        if (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 20 1 'office restored before retry')) {
            Log 'WARNING: OFFICE did not verify - the retry below will probably be a no-op'
        }
        if (-not (Invoke-DisplayProfile $CG.TvGamingLnk { Test-TvIsPrimary } 20 1 'TV is primary (2160p)')) {
            # Names the likeliest cause, not the symptom: the old wording here
            # ('TV-GAMING profile did not take') pointed a whole investigation at
            # DisplayMagician when the TV had simply never powered on.
            throw 'TV never came up at 2160p - most likely still asleep (Ex-Link power_on is send-only; this machine cannot verify TV power)'
        }
    }
    # retried=True is the launch that would have failed outright before this
    # existed - the number to watch after deploying, and the one that says
    # whether the TV's wake is drifting slower.
    Write-CgEvent 'profile_applied' @{ profile = 'TV-GAMING'; retried = $retried }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Stop-DisplayMagician

    # 5. Big Picture, and the foreground policy that decides whether the
    # session is usable at all - Steam delivers controller input to the
    # FOCUSED window, so this step is not cosmetic.
    #
    # There is deliberately NO fallback to the 'Steam' window title: that is
    # the desktop library window (see Hide-DesktopSteam), and focusing it
    # gives a controller that chimes on every button and moves nothing on the
    # TV. It also wins the race whenever a game from a previous session is
    # still running, because Big Picture then has no window ~1 s after the
    # URL handoff.
    Hide-DesktopSteam
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    # Recorded, NOT acted on: Enter does exactly one thing about the
    # foreground (Big Picture) and merely reports whether a game was already
    # up, because how often that happens is what any future resume has to be
    # designed against. Do not branch on it here - docs/resume-game-design.md
    # has both failed attempts and why Enter is structurally the wrong place.
    $running = Get-RunningAppId
    $wsh = New-Object -ComObject WScript.Shell
    $focused = Wait-For { $wsh.AppActivate($CG.BpmWindow) } 20 'Big Picture focused'
    if (-not $focused) { Log 'WARNING: Big Picture never took focus - session will need a click' }
    if ($running) { Log "note: game $running was already running at Enter" }

    # 6. Ready marker - the K15 switches the TV input only after seeing this.
    #
    # First MEASURE the foreground rather than trusting the steps above:
    # AppActivate returning true is a claim, not an observation, and an
    # unchecked claim let `focused=True` sit in the ready event while the
    # controller reached nothing. The desktop library window in front is the
    # one state KNOWN to be broken, so it can never read as success. Sampled
    # a second or so before the K15 flips the input, so run desk-side it may
    # name whatever you were doing: conclusive on 'Steam', suggestive
    # otherwise.
    $fg = Get-ForegroundTitle
    if ($fg -eq $CG.SteamWindow) {
        $focused = $false
        Log "WARNING: desktop Steam is in the foreground - the controller will not reach the TV"
    }
    Set-ReadyMarker
    Log "READY (foreground: '$fg')"
    # The milestone the whole system is gated on: dur_ms here IS
    # time-to-READY, the distribution the launch-health dashboard is built
    # from. Warn-level when nothing took the foreground - the TV still
    # switches (a session rescuable with one click beats no session), but
    # this failure looks EXACTLY like success from here, so it must not log
    # as one. `ready focused=False` is the alert; `fg` says what to look at.
    Write-CgEvent 'ready' @{ focused = $focused; fg = $fg; running_appid = $running } $(if ($focused) { 'info' } else { 'warn' })
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
