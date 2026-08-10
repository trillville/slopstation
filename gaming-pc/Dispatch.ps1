switch ($env:SSH_ORIGINAL_COMMAND) {
  'enter'  { schtasks /Run /TN '\CouchGaming\Enter' | Out-Null; 'OK' }
  'exit'   { schtasks /Run /TN '\CouchGaming\Exit'  | Out-Null; 'OK' }
  'status' { if (Test-Path 'C:\ProgramData\CouchGaming\ready')
             { Get-Content 'C:\ProgramData\CouchGaming\ready' } else { 'NOTREADY' } }
  default  { 'DENIED'; exit 1 }
}