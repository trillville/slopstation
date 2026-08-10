# The entire remote attack surface: forced command for the K15's SSH key
# (administrators_authorized_keys). Six verbs; everything else is DENIED.
# Deliberately dependency-free - no dot-sourcing in the sshd context.
# The ready path mirrors $CG.ReadyMarker in CouchGaming.common.ps1.
#
# Voice (C2) adds: games (installed-library JSON), playing (RunningAppID),
# launch <appid> (READY-gated, BUSY/ALREADY-truthful, file-as-argument to the
# \CouchGaming\LaunchGame task - schtasks can't take arguments).
$ready = 'C:\ProgramData\CouchGaming\ready'
switch -Regex ($env:SSH_ORIGINAL_COMMAND) {
  '^enter$'  { schtasks /Run /TN '\CouchGaming\Enter' | Out-Null
               if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
               break }
  '^exit$'   { schtasks /Run /TN '\CouchGaming\Exit'  | Out-Null
               if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
               break }
  '^status$' { if (Test-Path $ready) { Get-Content $ready } else { 'NOTREADY' }
               break }
  '^playing$' {
      $v = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if ($null -eq $v) { '0' } else { "$v" }
      break }
  '^games$' {
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
  '^launch (\d{1,10})$' {
      $id = $Matches[1]
      if (-not (Test-Path $ready)) { 'NOTREADY'; break }
      $run = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
      if ($run -and $run -ne 0) {
        if ("$run" -eq $id) { 'ALREADY' } else { "BUSY:$run" }
        break
      }
      Set-Content 'C:\ProgramData\CouchGaming\launch-app' $id
      schtasks /Run /TN '\CouchGaming\LaunchGame' | Out-Null
      if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" }
      break }
  default    { 'DENIED'; exit 1 }
}
