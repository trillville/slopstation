Start-Transcript "C:\CouchGaming\logs\exit-$(Get-Date -Format yyyyMMdd-HHmmss).log"
$sw  = [Diagnostics.Stopwatch]::StartNew()
$vhr = 'C:\CouchGaming\logs\vh-last.txt'
$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
function Log($m) { Write-Host ("[+{0,5:n1}s] {1}" -f $sw.Elapsed.TotalSeconds, $m) }
function Test-PuckClaimed {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match 'VID_28DE&PID_1304' -and $_.Status -eq 'OK' })
}
Start-Process 'steam://close/bigpicture'
Log 'closing Big Picture'
Start-Sleep 2
Start-Process 'C:\CouchGaming\OFFICE.lnk'   # office first: controller stays live during teardown
Log 'OFFICE profile launched'
while ($sw.Elapsed.TotalSeconds -lt 15 -and (Get-PrimaryHeight) -eq 2160) {
    Start-Sleep -Milliseconds 250
}
if ((Get-PrimaryHeight) -eq 2160) {
    Log 'office did not take - retrying'
    Start-Process 'C:\CouchGaming\OFFICE.lnk'; Start-Sleep 5
} else { Log 'ultrawide restored' }

Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force

# Release the Puck; retry until Windows agrees it's gone
$released = $false
for ($i = 1; -not $released -and $i -le 3; $i++) {
    & 'C:\CouchGaming\vhui64.exe' -t 'STOP USING,K15.5' -r $vhr
    Start-Sleep 1
    Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '))
    $released = -not (Test-PuckClaimed)
    if (-not $released) { Start-Sleep 2 }
}
if ($released) { Log 'Puck released' } else { Log 'WARNING: Puck may still be claimed - check VirtualHere client' }

# Repaint guard: minimize desktop Steam so it re-lays-out fresh (at the ultrawide's
# resolution) the next time it's opened - prevents the stale-4K garbled window
Add-Type -Namespace P2 -Name W -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
$sp = Get-Process steam -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($sp) { [void][P2.W]::ShowWindow($sp.MainWindowHandle, 6); Log 'Steam minimized' }   # 6 = SW_MINIMIZE

Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
Log 'done'
Stop-Transcript
Start-Sleep 5
Add-Type -Namespace P -Name M -MemberDefinition '[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, int m, IntPtr w, IntPtr l);'
[void][P.M]::PostMessage([IntPtr]0xFFFF, 0x0112, [IntPtr]0xF170, [IntPtr]2)