# The entire remote attack surface: forced command for the K15's SSH key
# (administrators_authorized_keys). Seven verbs; everything else is DENIED.
# Deliberately dependency-free - no dot-sourcing in the sshd context.
# The ready path mirrors $CG.ReadyMarker in CouchGaming.common.ps1.
#
# Verbs: enter/exit/status (session), games (installed-library JSON), playing
# (RunningAppID), launch <appid> (READY-gated, BUSY/ALREADY-truthful; the appid
# travels via marker file because schtasks /Run cannot pass arguments),
# version (the build-id Deploy.ps1 stamped - doctor.py compares it against
# the K15's checkout, so deploy skew is measured instead of assumed).
# The three mutating verbs also take an optional ` --turn <hex>` correlation id
# (see Set-Turn below); the read-only polls deliberately do not.
$ready = 'C:\ProgramData\CouchGaming\ready'
$turnFile = 'C:\ProgramData\CouchGaming\turn'

# Correlation id for one user intent, minted on the K15 and travelling with
# the three MUTATING verbs so this machine's transcript and events join that
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

switch -Regex ($env:SSH_ORIGINAL_COMMAND) {
  '^enter( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { Set-Turn $Matches[2]
               schtasks /Run /TN '\CouchGaming\Enter' | Out-Null
               if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
               break }
  '^exit( --turn ((?-i:[0-9a-f]{1,8})))?\z'
             { Set-Turn $Matches[2]
               schtasks /Run /TN '\CouchGaming\Exit'  | Out-Null
               if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
               break }
  '^status\z' { if (Test-Path $ready) { Get-Content $ready } else { 'NOTREADY' }
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
      Set-Turn $Matches[3]
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
      # This context is ELEVATED (admin-key forced command), the LaunchGame
      # task is not - so the task can't delete the marker we create. Clearing
      # it here, where deletion always succeeds, keeps the protocol clean;
      # Launch-Game's own delete is best-effort.
      Remove-Item 'C:\ProgramData\CouchGaming\launch-app' -Force -ErrorAction SilentlyContinue
      Set-Content 'C:\ProgramData\CouchGaming\launch-app' $id
      schtasks /Run /TN '\CouchGaming\LaunchGame' | Out-Null
      if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
      break }
  default    { 'DENIED'; exit 1 }
}
