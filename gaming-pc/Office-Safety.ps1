$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
Start-Transcript "C:\CouchGaming\logs\office-safety-$(Get-Date -Format yyyyMMdd-HHmmss).log"
for ($try = 1; $try -le 3; $try++) {
    if ((Get-PrimaryHeight) -ne 2160) { Write-Host "office confirmed (attempt $try)"; break }
    Write-Host "TV is primary - applying OFFICE (attempt $try)"
    Start-Process 'C:\CouchGaming\OFFICE.lnk'
    $end = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $end -and (Get-PrimaryHeight) -eq 2160) { Start-Sleep -Milliseconds 500 }
}
if ((Get-PrimaryHeight) -eq 2160) { Write-Host 'WARNING: OFFICE never took after 3 attempts' }
Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
Stop-Transcript