# The entire remote attack surface: forced command for the K15's SSH key
# (administrators_authorized_keys). Everything not matched below is DENIED.
# Dependency-free - no dot-sourcing in the sshd context. The ready path mirrors
# $CG.ReadyMarker in CouchGaming.common.ps1.
#
# The switch below is the verb catalog. Its five mutating verbs
# (enter/exit/launch/nav/stop) take an optional ` --turn <hex>`; the read-only
# polls do not. launch/nav/stop need the INTERACTIVE session (steam://
# forwarding, window focus), so they fire a scheduled task.
#
# The collection-id charset ([A-Za-z0-9_.*+=-]) is fail-closed but must cover
# Steam's base64-ish ids ("uc-mkD+r+pfQ1hu", "uc-odwxN*+G1zDb*+") - a tighter
# [A-Za-z0-9_.-] DENIED 3 of this rig's 11 collections (2026-08-14). '/' stays
# out: every value here is one URL path segment.
$ready = 'C:\ProgramData\CouchGaming\ready'
$turnFile = 'C:\ProgramData\CouchGaming\turn'

# Correlation id for one user intent, minted on the K15, riding a marker file
# because schtasks /Run cannot pass arguments.
#
# SECURITY: the anchored '[0-9a-f]{1,8}' in each pattern below IS the
# validation - the turn reaches a FILENAME on the far side, so anything laxer
# is a path-traversal primitive. A non-matching turn falls through to DENIED.
# Two regex-dialect requirements:
#  * Verbs end in \z, not $: in .NET '$' also matches before a trailing
#    newline, so '^status$' would accept "status`n".
#  * The turn group is wrapped in (?-i: ) because switch -Regex is
#    case-insensitive by default, so plain [0-9a-f] would accept '9F2C1A'.
#    The verbs themselves stay case-insensitive.
# voice/tests/test_turn.py reads these patterns out of this file and fails if
# one loses its anchor or its bound.
function Set-Turn($t) {
  Remove-Item $turnFile -Force -ErrorAction SilentlyContinue
  if ($t) { Set-Content $turnFile $t }
}

# Fire a scheduled task and say WHICH way it failed: schtasks /Run answers 1
# both for "task not registered" and for a real failure (2026-08-14: every nav
# read as broken when the Nav task had simply never been registered).
# The /Query only runs on the failure path.
function Start-CgTask([string]$Name) {
  schtasks /Run /TN "\CouchGaming\$Name" | Out-Null
  if ($LASTEXITCODE -eq 0) { return 'OK' }
  $code = $LASTEXITCODE
  schtasks /Query /TN "\CouchGaming\$Name" 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return "NOTASK:$Name" }
  "FAILED:$code"
}

switch -Regex ($env:SSH_ORIGINAL_COMMAND) {
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
  # Is the Enter task still running? `status` cannot say: mid-Enter and
  # already-died both read NOTREADY, so the K15 would burn its whole READY
  # window polling a dead task (3 times, latest 2026-08-19 01:18).
  # Get-ScheduledTask, not schtasks /Query: .State is an enum, while /FO LIST
  # prints a LOCALISED string that a non-English install would read as idle -
  # the direction that re-dispatches Enter on top of a healthy launch.
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
      $roots = @($steam)
      $lf = Join-Path $steam 'steamapps\libraryfolders.vdf'
      if (Test-Path $lf) {
        foreach ($line in (Get-Content $lf)) {
          if ($line -match '^\s*"path"\s+"(.+)"\s*$') { $roots += ($Matches[1] -replace '\\\\', '\') }
        }
      }
      # Registry SteamPath and vdf paths differ in case - dedupe roots
      # case-insensitively, and dedupe appids as belt-and-braces.
      $seen = @{}
      $apps = foreach ($root in ($roots | ForEach-Object { $_.ToLower() } | Select-Object -Unique)) {
        foreach ($acf in (Get-ChildItem (Join-Path $root 'steamapps\appmanifest_*.acf') -ErrorAction SilentlyContinue)) {
          $t = Get-Content $acf.FullName -Raw -Encoding UTF8
          $f = @{}
          foreach ($k in 'appid','name','StateFlags','SizeOnDisk','LastPlayed') {
            if ($t -match ('"' + $k + '"\s+"([^"]*)"')) { $f[$k] = $Matches[1] }
          }
          # Names go ASCII-only: keeps the JSON identical across every
          # shell/ssh encoding hop.
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
      Set-Turn $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      $run = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if ($run -and $run -ne 0) {
        if ("$run" -eq $id) { 'ALREADY' } else { "BUSY:$run" }
        break
      }
      # Install state lives only here (the PC's ACFs). Refuse an uninstalled
      # appid: a stale K15 index would otherwise make steam -applaunch pop the
      # install dialog on the TV, which needs the controller.
      $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
      $roots = @()
      if ($steam) { $steam = $steam -replace '/', '\'; $roots += $steam }
      $lf = Join-Path $steam 'steamapps\libraryfolders.vdf'
      if (Test-Path $lf) {
        foreach ($line in (Get-Content $lf)) {
          if ($line -match '^\s*"path"\s+"(.+)"\s*$') { $roots += ($Matches[1] -replace '\\\\', '\') }
        }
      }
      $installed = $false
      foreach ($root in ($roots | Select-Object -Unique)) {
        if (Test-Path (Join-Path $root "steamapps\appmanifest_$id.acf")) { $installed = $true; break }
      }
      if (-not $installed) { 'NOTINSTALLED'; break }
      # This context is ELEVATED (admin-key forced command), the LaunchGame task
      # is not, so the task cannot delete the marker. Clear it here, where the
      # delete always succeeds; Launch-Game's own delete is best-effort.
      Remove-Item 'C:\ProgramData\CouchGaming\launch-app' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\launch-app' $id
      Start-CgTask 'LaunchGame'
      break }
  # nav: fire a steam:// URL into the running Big Picture session, READY-gated.
  # (kind [arg]) travels via the nav-target marker; the unelevated Nav task
  # reads and maps it to a URL. Cleared here, where the delete succeeds.
  # 'store' spans two patterns (front page, game page), disjoint because the
  # second requires digits.
  '^nav (downloads|library|store)( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      Set-Turn $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Remove-Item 'C:\ProgramData\CouchGaming\nav-target' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\nav-target' $Matches[1]
      Start-CgTask 'Nav'
      break }
  '^nav (details|store) (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      Set-Turn $Matches[4]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Remove-Item 'C:\ProgramData\CouchGaming\nav-target' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\nav-target' "$($Matches[1]) $($Matches[2])"
      Start-CgTask 'Nav'
      break }
  '^nav collection ([A-Za-z0-9_.*+=-]{1,64})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      Set-Turn $Matches[3]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      Remove-Item 'C:\ProgramData\CouchGaming\nav-target' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\nav-target' "collection $($Matches[1])"
      Start-CgTask 'Nav'
      break }
  # stop: quit the running game. The appid is REQUIRED and re-checked against
  # RunningAppID, so a raced/wrong id refuses (BUSY:<other>) instead of killing
  # the wrong game. The StopGame task quits it and re-focuses Big Picture.
  '^stop (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $id = $Matches[1]
      Set-Turn $Matches[3]
      $run = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if (-not $run -or $run -eq 0) { 'NOTRUNNING'; break }
      if ("$run" -ne $id) { "BUSY:$run"; break }
      Remove-Item 'C:\ProgramData\CouchGaming\stop-app' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\stop-app' $id
      Start-CgTask 'StopGame'
      break }
  # collections: library collections as [{name,id}] JSON, from the per-user
  # cloud-storage file. Best-effort per entry - the format is
  # community-reverse-engineered, so unparseable entries are skipped.
  '^collections\z' {
      $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
      if (-not $steam) { '[]'; break }
      $steam = $steam -replace '/', '\'
      $out = @()
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
              $out += [pscustomobject]@{ name = $name; id = "$($v.id)" }
            }
          }
        }
      }
      ConvertTo-Json -InputObject @($out) -Compress -Depth 3
      break }
  default    { 'DENIED'; exit 1 }
}
