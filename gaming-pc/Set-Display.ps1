# Task \CouchGaming\Display, fired by Dispatch's `display` verb: put the desktop
# on the TV, or back on the monitor, and nothing else. Not a session: the Puck
# stays with the K15 and Big Picture stays closed, so the chord still starts a
# real session from here, and its own profile step finds the TV already
# primary. The target arrives via the display-target marker (schtasks /Run
# can't pass arguments); Dispatch already validated it, and it is re-checked
# here because it picks a shortcut to run.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'display'
try {
    $target = Read-CgMarker $CG.DisplayMarker
    if ($null -eq $target) { Log 'no display-target marker - nothing to do'; return }
    # The session flow owns the displays while it runs.
    if ((Test-CgTaskRunning 'Enter') -or (Test-CgTaskRunning 'Exit')) {
        throw 'a session task is running - it owns the displays'
    }
    switch ($target) {
        'tv'      { $lnk = $CG.TvGamingLnk; $until = { Test-TvIsPrimary };        $what = 'TV is primary' }
        'monitor' { $lnk = $CG.OfficeLnk;   $until = { -not (Test-TvIsPrimary) }; $what = 'monitor is primary' }
        default   { throw "unrecognized display target: '$target'" }
    }
    if (& $until) {
        Log "$what already - nothing to change"
        Write-CgEvent 'display_set' @{ target = $target; changed = $false }
        return
    }
    if (-not (Invoke-DisplayProfile $lnk $until 20 2 $what)) {
        throw "the profile did not take: $what"
    }
    Write-CgEvent 'display_set' @{ target = $target; changed = $true }
} catch {
    Write-CgEvent 'display_failed' @{ err = "$_" } 'error'
    throw
} finally {
    Stop-CgTranscript
}
