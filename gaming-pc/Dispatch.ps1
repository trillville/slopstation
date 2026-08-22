# The entire remote attack surface: forced command for the K15's SSH key
# (administrators_authorized_keys). Eleven verbs; everything else is DENIED.
# Deliberately dependency-free - no dot-sourcing in the sshd context.
# The marker paths below mirror $CG.ReadyMarker/TurnMarker/LaunchMarker/
# NavMarker/StopMarker in CouchGaming.common.ps1 - the two lists must agree.
#
# Verbs: enter/exit/status (session), enterstate (whether the Enter task is
# still running - the difference between a launch that is working and one that
# has already lost, which status alone cannot express), games (installed-library
# JSON), playing
# (RunningAppID), launch <appid> (READY-gated, BUSY/ALREADY-truthful; the appid
# travels via marker file because schtasks /Run cannot pass arguments),
# version (the build-id Deploy.ps1 stamped - doctor.py compares it against
# the K15's checkout, so deploy skew is measured instead of assumed),
# nav <kind> [<appid>|<collection>] (fire a steam:// URL into Big Picture -
# READY-gated, via the Nav task + marker for the same schtasks-can't-pass-args
# reason), stop <appid> (quit the running game - refuses if that appid isn't
# the one running, via the StopGame task), collections (the library
# collections as {name,id} JSON, read from the cloud-storage file like games).
# The FIVE mutating verbs (enter/exit/launch/nav/stop) also take an optional
# ` --turn <hex>` correlation id (see Set-Turn below); the read-only polls
# (status/enterstate/version/playing/games/collections) deliberately do not.
#
# nav/stop run in the INTERACTIVE session (steam:// forwarding, window focus)
# so they fire a scheduled task exactly as launch does; collections is a pure
# read and runs inline here. The nav 'store' kind is split across two patterns
# on purpose - "store" is the front page, "store <appid>" a game page - and
# they are disjoint (the second needs digits), so each switch case still breaks
# cleanly. The collection-id charset ([A-Za-z0-9_.*+=-]) is fail-closed like
# the appid and turn, because the value reaches a steam:// URL in the task -
# but it has to cover what Steam actually generates: the ids are base64-ish
# ("uc-mkD+r+pfQ1hu", "uc-odwxN*+G1zDb*+"), and a tighter [A-Za-z0-9_.-] read
# of them DENIED 3 of this rig's 11 collections (2026-08-14, caught before a
# couch test hit it). '/' stays OUT on purpose: everything here is one URL
# path segment, and a slash is the one character that could leave it.
$ready = 'C:\ProgramData\CouchGaming\ready'
$turnFile = 'C:\ProgramData\CouchGaming\turn'
# The payload markers for the three task-firing verbs (schtasks /Run cannot
# pass arguments). Cleared and rewritten here, in the elevated context where
# the delete always succeeds; the tasks re-validate what they read.
$launchApp = 'C:\ProgramData\CouchGaming\launch-app'
$navTarget = 'C:\ProgramData\CouchGaming\nav-target'
$stopApp = 'C:\ProgramData\CouchGaming\stop-app'

# Correlation id for one user intent, minted on the K15 and travelling with
# the five MUTATING verbs so this machine's transcript and events join that
# story. Like the appid it rides a marker file, because schtasks /Run cannot
# pass arguments.
#
# SECURITY: the '[0-9a-f]{1,8}' in each pattern below IS the validation, and
# the patterns are anchored - so a turn that is not short lowercase hex never
# matches a verb at all and falls through to DENIED. That matters because the
# value reaches a FILENAME on the far side: anything laxer here would be a
# path-traversal primitive. Fail closed; the K15 only ever sends ids it minted.
#
# Two regex-dialect traps this file has to dodge, both found by drilling the
# patterns rather than by reading them:
#
#  * Every verb ends in \z, not $. In .NET (as in most engines) '$' also
#    matches just BEFORE a trailing newline, so '^status$' accepts "status`n".
#    No bad capture was reachable that way - [0-9a-f] cannot eat a newline -
#    but an anchor that needs a paragraph of reasoning to call safe is the
#    wrong anchor on the one file that is the whole remote attack surface.
#
#  * The turn group is wrapped in (?-i: ) because switch -Regex, like -match,
#    is CASE-INSENSITIVE by default: plain [0-9a-f] quietly also accepts
#    '9F2C1A'. Still a harmless filename, but then the pattern is not the
#    validation its own comment claims, and that gap is where the next bug
#    lives. The verbs themselves stay case-insensitive, exactly as before.
#
# voice/tests/test_turn.py reads these patterns out of this file (so it can
# never drill a stale copy) and fails if one loses its anchor or its bound.
function Set-Turn($t) {
  Remove-Item $turnFile -Force -ErrorAction SilentlyContinue
  if ($t) { Set-Content $turnFile $t }
}

# Fire a scheduled task and say WHICH way it failed. schtasks /Run answers 1
# both for "that task does not exist" and for a real failure, and those are
# opposite problems: the first is a one-time registration someone skipped
# (guide Stage 6/8), the second is the PC misbehaving. On 2026-08-14 every nav
# in a couch test answered FAILED:1 and read as "nav is broken" when the Nav
# task had simply never been registered - an hour to find, one command to fix.
# The /Query only runs on the failure path, so the happy path is still one call.
function Start-CgTask([string]$Name) {
  schtasks /Run /TN "\CouchGaming\$Name" | Out-Null
  if ($LASTEXITCODE -eq 0) { return 'OK' }
  $code = $LASTEXITCODE
  schtasks /Query /TN "\CouchGaming\$Name" 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return "NOTASK:$Name" }
  "FAILED:$code"
}

# Every Steam library root: the install dir plus libraryfolders.vdf's paths.
# ONE copy, used by `games` and `launch` - they used to carry it twice, and the
# case-insensitive dedupe (registry SteamPath and vdf paths differ in case)
# had landed in one of them.
function Get-SteamRoots([string]$SteamPath) {
  $roots = @($SteamPath)
  $lf = Join-Path $SteamPath 'steamapps\libraryfolders.vdf'
  if (Test-Path $lf) {
    foreach ($line in (Get-Content $lf)) {
      if ($line -match '^\s*"path"\s+"(.+)"\s*$') { $roots += ($Matches[1] -replace '\\\\', '\') }
    }
  }
  @($roots | ForEach-Object { $_.ToLower() } | Select-Object -Unique)
}

# One line per call, DENIED included, into the same daily jsonl
# Write-CgEvent feeds (CouchGaming.common.ps1; the shape is mirrored here,
# not imported - this file dot-sources nothing). A rejected or refused verb
# used to leave no trace on this machine at all, which on the one file that
# is the whole remote attack surface is the wrong default. The answer is
# logged only when it is a word - `games`, `status` and `collections` answer
# with payloads that belong in the transcript, not the audit. Fail-soft:
# telemetry never costs a session.
function Write-CgAudit([string]$Cmd, $Answer) {
  try {
    $word = if (($Answer -is [string]) -and ($Answer.Length -le 40) -and ($Answer -notmatch '[\[{]')) { $Answer } else { 'payload' }
    $rec = [ordered]@{
      ts      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
      level   = $(if ($word -eq 'DENIED') { 'warn' } else { 'info' })
      env     = 'prod'
      service = 'gamepc'
      lane    = 'dispatch'
      event   = 'dispatch'
    }
    $m = [regex]::Match("$Cmd", ' --turn (?-i:([0-9a-f]{1,8}))$')
    if ($m.Success) { $rec.turn = $m.Groups[1].Value }
    $rec.host = $env:COMPUTERNAME
    $rec.cmd = "$Cmd".Substring(0, [Math]::Min(120, "$Cmd".Length))
    $rec.answer = $word
    $dir = Join-Path $PSScriptRoot 'logs'
    New-Item -ItemType Directory -Force -Path $dir -ErrorAction SilentlyContinue | Out-Null
    $file = Join-Path $dir ('pc-{0}.jsonl' -f (Get-Date -Format yyyyMMdd))
    $line = ConvertTo-Json -InputObject $rec -Compress -Depth 3
    [IO.File]::AppendAllText($file, $line + [Environment]::NewLine,
                             (New-Object System.Text.UTF8Encoding($false)))
  } catch { }
}

$cmd = $env:SSH_ORIGINAL_COMMAND
$out = switch -Regex ($cmd) {
  '^enter( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { Set-Turn $Matches[2]
               Start-CgTask 'Enter'
               break }
  '^exit( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { Set-Turn $Matches[2]
               Start-CgTask 'Exit'
               break }
  '^status\z' { if (Test-Path $ready) { Get-Content $ready } else { 'NOTREADY' }
               break }
  # Is the Enter task still running? `status` cannot answer that: a launch
  # that is mid-Enter and one whose Enter has already died both read NOTREADY,
  # so the K15 used to sit out its entire READY window polling a task that had
  # exited (three times, most recently 2026-08-19 01:18). Windows' own task
  # state is the authority and no marker is written, so this adds no
  # distributed state and owes no reconciler.
  #
  # Get-ScheduledTask, not schtasks /Query: .State is an enum, where /FO LIST
  # prints a LOCALISED string - and a non-English install would read Running
  # as idle, which is the direction that re-dispatches Enter on top of a
  # healthy launch. Read-only, so no turn (see the header).
  '^enterstate\z' {
      $t = Get-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Enter' -ErrorAction SilentlyContinue
      if (-not $t) { 'NOTASK' } elseif ($t.State -eq 'Running') { 'RUNNING' } else { 'IDLE' }
      break }
  '^version\z' {
      $bid = Join-Path $PSScriptRoot 'build-id'
      if (Test-Path $bid) { Get-Content $bid -TotalCount 1 } else { 'UNKNOWN' }
      break }
  '^playing\z' {
      $v = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if ($null -eq $v) { '0' } else { "$v" }
      break }
  '^games\z' {
      $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
      if (-not $steam) { '[]'; break }
      $steam = $steam -replace '/', '\'
      # Dedupe appids as belt-and-braces (Get-SteamRoots already dedupes roots).
      $seen = @{}
      $apps = foreach ($root in (Get-SteamRoots $steam)) {
        foreach ($acf in (Get-ChildItem (Join-Path $root 'steamapps\appmanifest_*.acf') -ErrorAction SilentlyContinue)) {
          $t = Get-Content $acf.FullName -Raw -Encoding UTF8
          $f = @{}
          foreach ($k in 'appid','name','StateFlags','SizeOnDisk','LastPlayed') {
            if ($t -match ('"' + $k + '"\s+"([^"]*)"')) { $f[$k] = $Matches[1] }
          }
          # Names go ASCII-only: (tm)-glyphs are noise to voice, and it keeps
          # the JSON identical across every shell/ssh encoding hop.
          if ($f['name']) { $f['name'] = ($f['name'] -replace '[^\x20-\x7E]', '').Trim() }
          if ($f['appid'] -and $f['name'] -and -not $seen.ContainsKey($f['appid'])) {
            $seen[$f['appid']] = $true
            [pscustomobject]@{
              appid = [long]$f['appid']; name = $f['name']
              state = [int]$f['StateFlags']; size = [long]$f['SizeOnDisk']
              lastPlayed = [long]$f['LastPlayed'] }
          }
        }
      }
      ConvertTo-Json -InputObject @($apps) -Compress -Depth 3
      break }
  '^launch (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $id = $Matches[1]
      $turn = $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      $run = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if ($run -and $run -ne 0) {
        if ("$run" -eq $id) { 'ALREADY' } else { "BUSY:$run" }
        break
      }
      # Install state lives only here (the PC's ACFs). Refuse an uninstalled
      # appid - the authoritative guard, since a stale K15 index or a future
      # caller could otherwise make steam -applaunch pop the install dialog on
      # the TV (needs the controller - deliberately not a voice action).
      $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
      if (-not $steam) { 'NOTINSTALLED'; break }
      $installed = $false
      foreach ($root in (Get-SteamRoots ($steam -replace '/', '\'))) {
        if (Test-Path (Join-Path $root "steamapps\appmanifest_$id.acf")) { $installed = $true; break }
      }
      if (-not $installed) { 'NOTINSTALLED'; break }
      # The turn is set only now, once every guard has passed: a refused
      # launch used to write it first, and the NEXT task to start within
      # 300 s (a logon's Office-Safety, say) tagged its events with a launch
      # that never happened.
      Set-Turn $turn
      # This context is ELEVATED (admin-key forced command), the LaunchGame
      # task is not - so the task can't delete the marker we create. Clearing
      # it here, where deletion always succeeds, keeps the protocol clean;
      # Launch-Game's own delete is best-effort.
      Remove-Item $launchApp -Force -ErrorAction SilentlyContinue
      Set-Content $launchApp $id
      Start-CgTask 'LaunchGame'
      break }
  # nav: fire a steam:// URL into the running Big Picture session. READY-gated
  # (no session, nothing to navigate). The (kind [arg]) travels via the
  # nav-target marker for the same reason launch's appid does; the Nav task,
  # unelevated, reads and maps it to a URL. Cleared here in the elevated
  # context where the delete always succeeds (the task cannot delete it).
  '^nav (downloads|library|store)( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Set-Turn $turn                      # after the guard - see launch
      Remove-Item $navTarget -Force -ErrorAction SilentlyContinue
      Set-Content $navTarget $Matches[1]
      Start-CgTask 'Nav'
      break }
  '^nav (details|store) (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[4]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Set-Turn $turn
      Remove-Item $navTarget -Force -ErrorAction SilentlyContinue
      Set-Content $navTarget "$($Matches[1]) $($Matches[2])"
      Start-CgTask 'Nav'
      break }
  '^nav collection ([A-Za-z0-9_.*+=-]{1,64})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Set-Turn $turn
      Remove-Item $navTarget -Force -ErrorAction SilentlyContinue
      Set-Content $navTarget "collection $($Matches[1])"
      Start-CgTask 'Nav'
      break }
  # stop: quit the running game. The appid is REQUIRED and re-checked against
  # RunningAppID here, so a raced/wrong id refuses (BUSY:<other>) instead of
  # killing the wrong game - the same truthfulness launch has. NOTRUNNING when
  # nothing is up. The StopGame task does the graceful-then-forceful work and
  # re-focuses Big Picture after (the dead-controller lesson from Enter-TV).
  '^stop (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $id = $Matches[1]
      $turn = $Matches[3]
      $run = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if (-not $run -or $run -eq 0) { 'NOTRUNNING'; break }
      if ("$run" -ne $id) { "BUSY:$run"; break }
      Set-Turn $turn                      # after the guards - see launch
      Remove-Item $stopApp -Force -ErrorAction SilentlyContinue
      Set-Content $stopApp $id
      Start-CgTask 'StopGame'
      break }
  # collections: library collections as [{name,id}] JSON, from the per-user
  # cloud-storage file. Same shape and encoding discipline as `games` (ASCII
  # names, compact JSON). Best-effort per entry: the file format is
  # community-reverse-engineered, so anything that doesn't parse is skipped
  # rather than failing the verb - a keyless/empty answer beats a crash.
  '^collections\z' {
      $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
      if (-not $steam) { '[]'; break }
      $steam = $steam -replace '/', '\'
      $rows = @()
      $seen = @{}
      $userdata = Join-Path $steam 'userdata'
      if (Test-Path $userdata) {
        foreach ($udir in (Get-ChildItem $userdata -Directory -ErrorAction SilentlyContinue)) {
          $cf = Join-Path $udir.FullName 'config\cloudstorage\cloud-storage-namespace-1.json'
          if (-not (Test-Path $cf)) { continue }
          try { $arr = Get-Content $cf -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
          foreach ($entry in $arr) {
            # Each entry is a [key, record] pair; user collections key on
            # 'user-collections.<id>' and carry a JSON STRING in .value.
            $k = "$($entry[0])"; $rec = $entry[1]
            if ($k -notmatch '^user-collections\.') { continue }
            if ($rec.is_deleted) { continue }
            try { $v = $rec.value | ConvertFrom-Json } catch { continue }
            if (-not ($v.name -and $v.id)) { continue }
            $name = ($v.name -replace '[^\x20-\x7E]', '').Trim()
            if ($name -and -not $seen.ContainsKey("$($v.id)")) {
              $seen["$($v.id)"] = $true
              $rows += [pscustomobject]@{ name = $name; id = "$($v.id)" }
            }
          }
        }
      }
      ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
      break }
  default    { 'DENIED' }
}
Write-CgAudit $cmd $out
$out
if ($out -eq 'DENIED') { exit 1 }
