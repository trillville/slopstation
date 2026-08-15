# Stop-Game.ps1 - task \CouchGaming\StopGame, fired by Dispatch's `stop <appid>`
# verb. The appid arrives via the stop-app marker (schtasks /Run can't pass
# args); Dispatch already confirmed it IS the RunningAppID. This script does the
# actual quit and re-focuses Big Picture after, so the couch controller keeps
# working (the dead-controller lesson - see Enter-TV.ps1's ready event).
#
# Two-phase and SELF-ADAPTING, because whether `steam.exe +app_stop` reaches a
# running client is the open on-hardware question (docs/resume-game-design.md
# neighbourhood): try Steam's own graceful stop first, verify RunningAppID
# actually cleared, and only then fall back to a window-close and, last, a
# forced tree-kill. The event records WHICH path worked, so the first real runs
# answer the question instead of us guessing it now. Instrument the outcome,
# never the intent.
#
# Save-data safety is the whole reason for the ordering: app_stop is Steam's
# normal teardown, CloseMainWindow is a WM_CLOSE the game can honour (save +
# quit), and taskkill /T /F is the last resort that can lose unsaved progress -
# reached only when a game ignores both gentler asks.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'stopgame'

$method = 'none'
try {
    $marker = 'C:\ProgramData\CouchGaming\stop-app'
    if (-not (Test-Path $marker)) { Log 'no stop-app marker - nothing to do'; return }
    $raw = Get-Content $marker -TotalCount 1
    try { Remove-Item $marker -Force } catch {
        Log 'marker not deletable from this token - Dispatch overwrites it next stop'
    }
    $id = "$raw".Trim()
    if ($id -notmatch '^\d{1,10}$') { throw "invalid appid in marker: '$id'" }
    $idn = [int]$id

    if ((Get-RunningAppId) -ne $idn) {
        # It exited on its own between Dispatch's check and now - nothing to
        # kill, but still hand focus back to Big Picture and report it clean.
        Log "appid $id is not the running app any more - nothing to stop"
        $method = 'already-gone'
    }
    else {
        # Phase 1: Steam's own graceful stop. -ifrunning so it never cold-starts
        # a client; +app_stop is the console verb forwarded to the running one.
        Log "phase 1: steam -ifrunning +app_stop $id"
        try { & (Get-SteamExe) '-ifrunning' '+app_stop' $id } catch { Log "app_stop invoke failed: $_" }
        if (Wait-For { (Get-RunningAppId) -ne $idn } 8 "app_stop cleared appid $id") {
            $method = 'app_stop'
        }
        else {
            # Phase 2: find the game's own process via the ACF install dir and
            # ask its window to close (WM_CLOSE = save-and-quit for most games).
            Log 'phase 2: app_stop did not clear it - resolving process from the ACF install dir'
            $procs = Get-GameProcess $idn
            if ($procs) {
                foreach ($p in $procs) { try { $p.CloseMainWindow() | Out-Null } catch {} }
                if (Wait-For { (Get-RunningAppId) -ne $idn } 10 "window-close quit appid $id") {
                    $method = 'wm_close'
                }
                else {
                    # Last resort: forced tree-kill. Can lose unsaved progress,
                    # which is exactly why it is last and logged loudly.
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

    # Re-focus Big Picture so the controller reaches it (a game exit can leave
    # the desktop Steam window in front - the exact failure Enter-TV guards).
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
    Stop-Transcript | Out-Null
}
