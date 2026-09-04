# Forced SSH command for the K15. Unrecognized commands are denied.
# Mutating commands accept an optional ``--turn <hex>`` correlation ID.
# Interactive Steam commands run through scheduled tasks.
$ready = 'C:\ProgramData\CouchGaming\ready'
$turnFile = 'C:\ProgramData\CouchGaming\turn'
$launchMarker = 'C:\ProgramData\CouchGaming\launch-app'
$navMarker = 'C:\ProgramData\CouchGaming\nav-target'
$stopMarker = 'C:\ProgramData\CouchGaming\stop-app'

# The anchored lowercase turn pattern prevents path traversal in marker names.
# Use \z because .NET's $ also matches before a trailing newline. Set the turn
# only after command guards pass and before starting its scheduled task.
function Set-Turn($t) {
  Remove-Item $turnFile -Force -ErrorAction SilentlyContinue
  if ($t) { Set-Content $turnFile $t }
}

# Distinguish a missing task from other schtasks failures.
function Start-CgTask([string]$Name) {
  schtasks /Run /TN "\CouchGaming\$Name" | Out-Null
  if ($LASTEXITCODE -eq 0) { return 'OK' }
  $code = $LASTEXITCODE
  schtasks /Query /TN "\CouchGaming\$Name" 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return "NOTASK:$Name" }
  "FAILED:$code"
}

# Steam's install path from the registry, '\'-normalized, or $null.
function Get-Steam {
  $p = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
  if ($p) { $p -replace '/', '\' } else { $null }
}

# Return all Steam library roots, normalized and deduplicated.
function Get-SteamRoots {
  $steam = Get-Steam
  if (-not $steam) { return @() }
  $roots = @($steam)
  $lf = Join-Path $steam 'steamapps\libraryfolders.vdf'
  if (Test-Path $lf) {
    foreach ($line in (Get-Content $lf)) {
      if ($line -match '^\s*"path"\s+"(.+)"\s*$') { $roots += ($Matches[1] -replace '\\\\', '\') }
    }
  }
  $roots | ForEach-Object { $_.ToLower() } | Select-Object -Unique
}

# Steam's RunningAppID as stored: $null when the value is absent, 0 when
# nothing runs. Callers keep their own null/0 handling.
function Get-RunningAppId {
  (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
}

# Write elevated SSH events to a separate file so task logs remain writable.
# Event failures are ignored because stdout is the command response.
function Write-Event([string]$Event, [hashtable]$Fields = @{}, [string]$Level = 'info') {
  try {
    $rec = [ordered]@{
      ts      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
      level   = $Level
      env     = 'prod'
      service = 'gamepc'
      lane    = 'dispatch'
      event   = $Event
      host    = $env:COMPUTERNAME
    }
    $owned = @('ts','level','env','service','lane','event','host')
    foreach ($k in $Fields.Keys) {
      if ($owned -contains $k) { $rec["f_$k"] = $Fields[$k] } else { $rec[$k] = $Fields[$k] }
    }
    $dir = Join-Path $PSScriptRoot 'logs'
    [IO.Directory]::CreateDirectory($dir) | Out-Null
    $file = Join-Path $dir ("pc-dispatch-{0}.jsonl" -f (Get-Date -Format yyyyMMdd))
    [IO.File]::AppendAllText(
      $file, (ConvertTo-Json -InputObject $rec -Compress -Depth 4) + [Environment]::NewLine,
      (New-Object System.Text.UTF8Encoding($false)))
  } catch { }
}

# Record and return a mutating command's response.
function Write-Answer([string]$Verb, [string]$Answer, [string]$Turn) {
  if ($Turn) { Write-Event 'verb' @{ verb = $Verb; answer = $Answer; turn = $Turn } }
  else       { Write-Event 'verb' @{ verb = $Verb; answer = $Answer } }
  $Answer
}

switch -Regex ($env:SSH_ORIGINAL_COMMAND) {
  '^enter( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { $turn = $Matches[2]
               Set-Turn $turn
               Write-Answer 'enter' (Start-CgTask 'Enter') $turn
               break }
  '^exit( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { $turn = $Matches[2]
               Set-Turn $turn
               Write-Answer 'exit' (Start-CgTask 'Exit') $turn
               break }
  '^status\z' { if (Test-Path $ready) { Get-Content $ready } else { 'NOTREADY' }
               break }
  # Distinguish an active Enter task from one that stopped before READY.
  '^enterstate\z' {
      $t = Get-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Enter' -ErrorAction SilentlyContinue
      if (-not $t) { 'NOTASK' } elseif ($t.State -eq 'Running') { 'RUNNING' } else { 'IDLE' }
      break }
  '^version\z' {
      $bid = Join-Path $PSScriptRoot 'build-id'
      if (Test-Path $bid) { Get-Content $bid -TotalCount 1 } else { 'UNKNOWN' }
      break }
  '^playing\z' {
      $v = Get-RunningAppId
      if ($null -eq $v) { '0' } else { "$v" }
      break }
  '^games\z' {
      $roots = Get-SteamRoots
      if (-not $roots) { '[]'; break }
      # A game can appear in more than one library entry.
      $seen = @{}
      $apps = foreach ($root in $roots) {
        foreach ($acf in (Get-ChildItem (Join-Path $root 'steamapps\appmanifest_*.acf') -ErrorAction SilentlyContinue)) {
          $t = Get-Content $acf.FullName -Raw -Encoding UTF8
          $f = @{}
          foreach ($k in 'appid','name','StateFlags','SizeOnDisk','LastPlayed','LastUpdated') {
            if ($t -match ('"' + $k + '"\s+"([^"]*)"')) { $f[$k] = $Matches[1] }
          }
          # Keep names stable across PowerShell and SSH encodings.
          if ($f['name']) { $f['name'] = ($f['name'] -replace '[^\x20-\x7E]', '').Trim() }
          if ($f['appid'] -and $f['name'] -and -not $seen.ContainsKey($f['appid'])) {
            $seen[$f['appid']] = $true
            [pscustomobject]@{
              appid = [long]$f['appid']; name = $f['name']
              state = [int]$f['StateFlags']; size = [long]$f['SizeOnDisk']
              lastPlayed = [long]$f['LastPlayed']
              # Steam's last install or update time, or 0 when absent.
              updated = [long]$f['LastUpdated'] }
          }
        }
      }
      ConvertTo-Json -InputObject @($apps) -Compress -Depth 3
      break }
  '^launch (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $id = $Matches[1]; $turn = $Matches[3]
      if (-not (Test-Path $ready)) { Write-Answer 'launch' 'NOTREADY' $turn; break }
      $run = Get-RunningAppId
      if ($run -and $run -ne 0) {
        if ("$run" -eq $id) { Write-Answer 'launch' 'ALREADY' $turn } else { Write-Answer 'launch' "BUSY:$run" $turn }
        break
      }
      # Verify installation locally before launching.
      $installed = $false
      foreach ($root in (Get-SteamRoots)) {
        if (Test-Path (Join-Path $root "steamapps\appmanifest_$id.acf")) { $installed = $true; break }
      }
      if (-not $installed) { Write-Answer 'launch' 'NOTINSTALLED' $turn; break }
      Set-Turn $turn
      # Clear elevated markers before writing the next task payload.
      Remove-Item $launchMarker -Force -ErrorAction SilentlyContinue
      Set-Content $launchMarker $id
      Write-Answer 'launch' (Start-CgTask 'LaunchGame') $turn
      break }
  # nav: fire a steam:// URL into the running Big Picture session, READY-gated.
  # (kind [arg]) travels via the nav-target marker; the unelevated Nav task
  # reads and maps it to a URL. Cleared here, where the delete succeeds.
  # 'store' spans two patterns (front page, game page), disjoint because the
  # second requires digits.
  '^nav (downloads|library|store)( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[3]
      if (-not (Test-Path $ready)) { Write-Answer 'nav' 'NOTREADY' $turn; break }
      Set-Turn $turn
      Remove-Item $navMarker -Force -ErrorAction SilentlyContinue
      Set-Content $navMarker $Matches[1]
      Write-Answer 'nav' (Start-CgTask 'Nav') $turn
      break }
  '^nav (details|store) (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[4]
      if (-not (Test-Path $ready)) { Write-Answer 'nav' 'NOTREADY' $turn; break }
      Set-Turn $turn
      Remove-Item $navMarker -Force -ErrorAction SilentlyContinue
      Set-Content $navMarker "$($Matches[1]) $($Matches[2])"
      Write-Answer 'nav' (Start-CgTask 'Nav') $turn
      break }
  '^nav collection ([A-Za-z0-9_.*+=-]{1,64})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $turn = $Matches[3]
      if (-not (Test-Path $ready)) { Write-Answer 'nav' 'NOTREADY' $turn; break }
      Set-Turn $turn
      Remove-Item $navMarker -Force -ErrorAction SilentlyContinue
      Set-Content $navMarker "collection $($Matches[1])"
      Write-Answer 'nav' (Start-CgTask 'Nav') $turn
      break }
  # stop: quit the running game. The appid is REQUIRED and re-checked against
  # RunningAppID, so a raced/wrong id refuses (BUSY:<other>) instead of killing
  # the wrong game. The StopGame task quits it and re-focuses Big Picture.
  '^stop (\d{1,10})( --turn ((?-i:[0-9a-f]{1,8})))?\z' {
      $id = $Matches[1]; $turn = $Matches[3]
      $run = Get-RunningAppId
      if (-not $run -or $run -eq 0) { Write-Answer 'stop' 'NOTRUNNING' $turn; break }
      if ("$run" -ne $id) { Write-Answer 'stop' "BUSY:$run" $turn; break }
      Set-Turn $turn
      Remove-Item $stopMarker -Force -ErrorAction SilentlyContinue
      Set-Content $stopMarker $id
      Write-Answer 'stop' (Start-CgTask 'StopGame') $turn
      break }
  # collections: library collections as [{name,id}] JSON. Skip entries that
  # cannot be parsed from Steam's cloud-storage file.
  '^collections\z' {
      $steam = Get-Steam
      if (-not $steam) { '[]'; break }
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
  # A DENIED command leaves a PC-side record too, truncated - it is untrusted.
  default    { $cmd = "$env:SSH_ORIGINAL_COMMAND"
               if ($cmd.Length -gt 60) { $cmd = $cmd.Substring(0, 60) }
               Write-Event 'verb' @{ answer = 'DENIED'; cmd = $cmd } 'warn'
               'DENIED'; exit 1 }
}
