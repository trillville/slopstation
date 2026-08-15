# Nav-BigPicture.ps1 - task \CouchGaming\Nav, fired by Dispatch's `nav` verb.
# The target arrives via the nav-target marker file (schtasks /Run can't pass
# arguments); Dispatch already READY-gated and validated it, so this script
# reads the marker, deletes it, RE-validates (defence in depth - the marker is
# a file between two processes and the value becomes part of a steam:// URL, so
# a mismatch throws rather than firing a malformed URL), maps it to a URL, and
# Start-Processes it into the running Big Picture session. Deliberately dumb,
# exactly like Launch-Game.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'nav'
try {
    $marker = 'C:\ProgramData\CouchGaming\nav-target'
    if (-not (Test-Path $marker)) { Log 'no nav-target marker - nothing to do'; return }
    # Stringify before trimming (Get-Content on an empty file returns $null in
    # PS 5.1), and delete best-effort: the marker is written by the ELEVATED
    # sshd context and this task runs limited, so the delete is DENIED - but
    # Dispatch clears it before every write, so a survivor is overwritten.
    $raw = Get-Content $marker -TotalCount 1
    try { Remove-Item $marker -Force } catch {
        Log 'marker not deletable from this token - Dispatch overwrites it next nav'
    }
    $parts = ("$raw".Trim()) -split '\s+', 2
    $kind = $parts[0]
    $arg = if ($parts.Count -gt 1) { $parts[1] } else { '' }
    $url = switch ($kind) {
        'downloads'  { 'steam://open/downloads' }
        'library'    { 'steam://open/library' }
        'store'      { if ($arg -match '^\d{1,10}$') { "steam://store/$arg" }
                       elseif (-not $arg) { 'steam://store' } else { $null } }
        'details'    { if ($arg -match '^\d{1,10}$') { "steam://open/library/details/$arg" } else { $null } }
        # Charset mirrors Dispatch's nav-collection pattern - Steam's ids are
        # base64-ish ("uc-mkD+r+pfQ1hu"), and the two must stay in step or a
        # real collection passes the verb and dies here.
        'collection' { if ($arg -match '^[A-Za-z0-9_.*+=-]{1,64}$') { "steam://open/library/collection/$arg" } else { $null } }
        default      { $null }
    }
    if (-not $url) { throw "unrecognized nav target: '$raw'" }
    Log "nav -> $url"
    Start-Process $url
    Write-CgEvent 'nav_fired' @{ kind = $kind; url = $url }
} catch {
    Write-CgEvent 'nav_failed' @{ err = "$_" } 'error'
    throw
} finally {
    Stop-Transcript | Out-Null
}
