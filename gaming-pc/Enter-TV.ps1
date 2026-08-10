$ErrorActionPreference = 'Stop'
Start-Transcript "C:\CouchGaming\logs\enter-$(Get-Date -Format yyyyMMdd-HHmmss).log"
$sw   = [Diagnostics.Stopwatch]::StartNew()
$vh   = 'C:\CouchGaming\vhui64.exe'
$puck = 'K15.5'
$vhr  = 'C:\CouchGaming\logs\vh-last.txt'
$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
function Log($m) { Write-Host ("[+{0,5:n1}s] {1}" -f $sw.Elapsed.TotalSeconds, $m) }
function Get-TvNames {
    Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue |
    ForEach-Object { -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 }) }
}
function Test-PuckPresent {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match 'VID_28DE&PID_1304' -and $_.Status -eq 'OK' })
}
function Get-VhList {
    & $vh -t "LIST" -r $vhr | Out-Null
    Start-Sleep -Milliseconds 400
    (Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '
}
function Wait-For([scriptblock]$Cond, [double]$TimeoutSec, [string]$What) {
    $end = $sw.Elapsed.TotalSeconds + $TimeoutSec
    while ($sw.Elapsed.TotalSeconds -lt $end) {
        if (& $Cond) { Log $What; return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "TIMEOUT waiting for: $What"; return $false
}
try {
    # Kick the VirtualHere client immediately so dead-socket detection + reconnect
    # start now and overlap everything below
    Start-Process -WindowStyle Hidden $vh -ArgumentList '-t','LIST','-r','C:\CouchGaming\logs\vh-nudge.txt'
    Log ("primary height at start: {0}" -f (Get-PrimaryHeight))

    # 1. TV EDID visible (in the real flow the K15 just powered it on)
    if (-not (Wait-For { (Get-TvNames) -match 'QCQ90S' } 30 'TV detected')) {
        throw 'S90C never appeared over HDMI - aborting, office display untouched'
    }

    # 2. Launch the TV-only profile and DON'T wait for it - it settles while we do USB work
    Start-Process 'C:\CouchGaming\TV-GAMING.lnk'
    Log 'TV-GAMING profile launched'

    # 3a. Wait for the VirtualHere client to (re)connect to the K15 hub
    if (-not (Wait-For { (Get-VhList) -match [regex]::Escape($puck) } 30 'VirtualHere sees Puck')) {
        throw 'VirtualHere client never re-connected to the K15 hub'
    }

    # 3b. Claim the Puck - up to 2 attempts, verified by Windows enumeration, not the IPC report
    if (Test-PuckPresent) {
        Log 'stale Puck claim detected - releasing for a fresh instance'
        & $vh -t "STOP USING,$puck" -r $vhr
        Wait-For { -not (Test-PuckPresent) } 6 'stale claim released' | Out-Null
    }
    $claimed = $false
    for ($i = 1; -not $claimed -and $i -le 2; $i++) {
        & $vh -t "USE,$puck" -r $vhr
        $claimed = Wait-For { Test-PuckPresent } 8 "Puck enumerated (attempt $i)"
        Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '))
    }
    if (-not $claimed) { throw 'VirtualHere claim did not produce a device after 2 attempts' }

    # 4. NOW verify the profile actually took (it had the whole USB phase to settle)
    if (-not (Wait-For { (Get-PrimaryHeight) -eq 2160 } 20 'TV is primary (2160p)')) {
        throw 'TV-GAMING profile did not take'
    }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force

    # 5. Big Picture, forced to the foreground
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    Start-Sleep 1
    $wsh = New-Object -ComObject WScript.Shell
    $focused = $false
    for ($i = 0; -not $focused -and $i -lt 5; $i++) {
        foreach ($t in 'Steam Big Picture Mode','Steam') {
            if ($wsh.AppActivate($t)) { $focused = $true; Log "focused '$t'"; break }
        }
        if (-not $focused) { Start-Sleep 1 }
    }

    # 6. Ready marker - the K15 switches the TV input only after seeing this
    New-Item -ItemType Directory -Force 'C:\ProgramData\CouchGaming' | Out-Null
    Set-Content 'C:\ProgramData\CouchGaming\ready' (Get-Date).ToString('o')
    Log 'READY'
}
catch {
    & $vh -t "STOP USING,$puck" -r $vhr 2>$null
    Start-Process 'C:\CouchGaming\OFFICE.lnk'
    Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
    throw
}
finally { Stop-Transcript }