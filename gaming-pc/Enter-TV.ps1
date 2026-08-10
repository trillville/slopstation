# Session setup: TV-GAMING profile, Puck claim, Big Picture, READY marker.
# Runs as Scheduled Task \CouchGaming\Enter, dispatched by the K15 over SSH.
# Any failure before READY restores the office and leaves the TV input alone
# (the K15 only switches input after seeing the marker).
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'enter'
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

    # 4. NOW verify the profile actually took (it had the whole USB phase to settle)
    if (-not (Wait-For { Test-TvIsPrimary } 20 'TV is primary (2160p)')) {
        throw 'TV-GAMING profile did not take'
    }
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
}
catch {
    & $CG.Vh -t "STOP USING,$($CG.Puck)" -r $CG.VhResult 2>$null
    Start-Process $CG.OfficeLnk
    Clear-ReadyMarker
    throw
}
finally { Stop-Transcript }
