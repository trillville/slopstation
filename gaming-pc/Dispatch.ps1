# The entire remote attack surface: forced command for the K15's SSH key
# (administrators_authorized_keys). Three verbs; everything else is DENIED.
# Deliberately dependency-free - no dot-sourcing in the sshd context.
# The ready path mirrors $CG.ReadyMarker in CouchGaming.common.ps1.
switch ($env:SSH_ORIGINAL_COMMAND) {
  'enter'  { schtasks /Run /TN '\CouchGaming\Enter' | Out-Null
             if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" } }
  'exit'   { schtasks /Run /TN '\CouchGaming\Exit'  | Out-Null
             if ($LASTEXITCODE -eq 0) { 'OK' } else { "FAILED:$LASTEXITCODE" } }
  'status' { if (Test-Path 'C:\ProgramData\CouchGaming\ready')
             { Get-Content 'C:\ProgramData\CouchGaming\ready' } else { 'NOTREADY' } }
  default  { 'DENIED'; exit 1 }
}
