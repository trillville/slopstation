# Task \CouchGaming\StopGame, fired by Dispatch's `stop <appid>` verb. The appid
# arrives via the stop-app marker (schtasks /Run can't pass args); Dispatch
# already confirmed it IS the RunningAppID. Re-focuses Big Picture afterwards so
# the couch controller keeps working.
#
# Escalation order is for save-data safety: app_stop (Steam's own teardown),
# then CloseMainWindow (WM_CLOSE, honoured as save+quit by most games), then
# taskkill /T /F, which can lose unsaved progress. Each phase verifies
# RunningAppID cleared before escalating; the event records which path worked.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'stopgame'

$method = 'none'
try {
    $id = Read-CgMarker $CG.StopMarker
    if ($null -eq $id) { Log 'no stop-app marker - nothing to do'; return }
    if ($id -notmatch '^\d{1,10}$') { throw "invalid appid in marker: '$id'" }
    $idn = [int]$id

    if ((Get-RunningAppId) -ne $idn) {
        # Exited on its own between Dispatch's check and now - still hand focus
        # back to Big Picture and report clean.
        Log "appid $id is not the running app any more - nothing to stop"
        $method = 'already-gone'
    }
    else {
        # Phase 1: -ifrunning so it never cold-starts a client; +app_stop is the
        # console verb forwarded to the running one.
        Log "phase 1: steam -ifrunning +app_stop $id"
        try { & (Get-SteamExe) '-ifrunning' '+app_stop' $id } catch { Log "app_stop invoke failed: $_" }
        if (Wait-For { (Get-RunningAppId) -ne $idn } 8 "app_stop cleared appid $id") {
            $method = 'app_stop'
        }
        else {
            # Phase 2: resolve the game's process via the ACF install dir and
            # ask its window to close (WM_CLOSE = save-and-quit for most games).
            Log 'phase 2: app_stop did not clear it - resolving process from the ACF install dir'
            $procs = Get-GameProcess $idn
            if ($procs) {
                foreach ($p in $procs) { try { $p.CloseMainWindow() | Out-Null } catch {} }
                if (Wait-For { (Get-RunningAppId) -ne $idn } 10 "window-close quit appid $id") {
                    $method = 'wm_close'
                }
                else {
                    # Last resort: forced tree-kill; can lose unsaved progress.
                    Log 'phase 3: forced tree-kill (window close was ignored)'
                    foreach ($p in $procs) {
                        try { taskkill /T /F /PID $p.Id 2>$null | Out-Null } catch {}
                    }
                    Wait-For { (Get-RunningAppId) -ne $idn } 5 "kill cleared appid $id" | Out-Null
                    $method = if ((Get-RunningAppId) -ne $idn) { 'kill' } else { 'failed' }
                }
            }
            else {
                Log "WARNING: no process found under the appid $id install dir - cannot force-quit"
                $method = 'failed'
            }
        }
    }

    # Re-focus Big Picture so the controller reaches it: a game exit can leave
    # the desktop Steam window in front.
    Hide-DesktopSteam
    $wsh = New-Object -ComObject WScript.Shell
    if (Wait-For { $wsh.AppActivate($CG.BpmWindow) } 8 'Big Picture re-focused') { }
    else { Log 'WARNING: Big Picture never took focus after the stop' }

    $cleared = (Get-RunningAppId) -eq 0
    Write-CgEvent 'game_stopped' @{ appid = $idn; method = $method; cleared = $cleared } `
        $(if ($method -eq 'failed') { 'error' } elseif ($method -eq 'kill') { 'warn' } else { 'info' })
    Log "done (method=$method, cleared=$cleared)"
}
catch {
    Write-CgEvent 'game_stop_failed' @{ err = "$_"; method = $method } 'error'
    throw
}
finally {
    Stop-CgTranscript
}
