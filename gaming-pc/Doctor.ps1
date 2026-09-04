# Gaming-PC chain diagnosis: powershell -ExecutionPolicy Bypass -File Doctor.ps1
# Read-only. Exit code = number of FAILs.

$libPath = Join-Path $PSScriptRoot 'CouchGaming.common.ps1'
if (-not (Test-Path $libPath)) {
    Write-Host "[FAIL] CouchGaming.common.ps1 missing beside this script - partial deploy; re-copy all files from the repo" -ForegroundColor Red
    exit 1
}
# Loading the library validates config.psd1; a bad file is the first finding.
try { . $libPath } catch {
    Write-Host "[FAIL] config: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$script:Counts = @{ PASS = 0; WARN = 0; FAIL = 0 }
function Report([string]$Level, [string]$Name, [string]$Detail, [string]$Hint = '') {
    $script:Counts[$Level]++
    $color = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }[$Level]
    $line = "[$Level] ${Name}: $Detail"
    if ($Hint -and $Level -ne 'PASS') { $line += "  -> $Hint" }
    Write-Host $line -ForegroundColor $color
}

# 1. Configuration and deployed files
Report PASS 'config' "Puck '$($CG.PuckName)' ($($CG.PuckHwId)), TV EDID $($CG.TvEdid), TV height $($CG.TvHeight)"

$files = @('CouchGaming.common.ps1','config.example.psd1','Enter-TV.ps1','Exit-TV.ps1','Office-Safety.ps1','Wake-Safety.ps1','Dispatch.ps1','Launch-Game.ps1','Nav-BigPicture.ps1','Stop-Game.ps1','Doctor.ps1','vhui64.exe','OFFICE.lnk','TV-GAMING.lnk')
$missing = $files | Where-Object { -not (Test-Path (Join-Path $CG.Root $_)) }
if ($missing) { Report FAIL 'files' "missing: $($missing -join ', ')" 'Deploy.ps1 ships the scripts; vhui64.exe is the VirtualHere client; the .lnk files are made in DisplayMagician' }
else { Report PASS 'files' "$($files.Count)/$($files.Count) present" }

# 2. Scheduled tasks: registered, and defined as CouchGaming.common.ps1 says.
# Durations compare as TimeSpans, so PT72H and P3D are the same limit.
function Get-Duration([string]$Iso) { if ($Iso) { [Xml.XmlConvert]::ToTimeSpan($Iso) } else { [TimeSpan]::Zero } }
foreach ($t in $CG.Tasks) {
    $task = Get-ScheduledTask -TaskPath $CG.TaskPath -TaskName $t.Name -ErrorAction SilentlyContinue
    if (-not $task) { Report FAIL "task $($t.Name)" 'not registered' 'run gaming-pc\Install.ps1 from a checkout'; continue }
    $drift = @()
    $a = @($task.Actions)[0]
    if ($a.Execute -notmatch 'powershell(\.exe)?$' -or $a.Arguments -ne (Get-CgTaskArguments $t)) { $drift += "runs '$($a.Execute) $($a.Arguments)'" }
    $level = if ($t.Elevated) { 'Highest' } else { 'Limited' }
    if ($task.Principal.RunLevel -ne $level) { $drift += "run level $($task.Principal.RunLevel), want $level" }
    if ($task.Principal.LogonType -ne 'Interactive') { $drift += "logon type $($task.Principal.LogonType)" }
    $kinds = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ','
    $want = @{ none = ''; logon = 'MSFT_TaskLogonTrigger'; wake = 'MSFT_TaskEventTrigger' }[$t.Trigger]
    if ($kinds -ne $want) { $drift += "triggers [$kinds], want [$want]" }
    elseif ($t.Trigger -eq 'logon' -and (Get-Duration @($task.Triggers)[0].Delay) -ne (Get-Duration $t.Delay)) {
        $drift += "logon delay '$(@($task.Triggers)[0].Delay)', want $($t.Delay)"
    }
    if ((Get-Duration $task.Settings.ExecutionTimeLimit) -ne (Get-Duration $t.TimeLimit)) {
        $drift += "time limit '$($task.Settings.ExecutionTimeLimit)', want $($t.TimeLimit)"
    }
    if ($drift) { Report FAIL "task $($t.Name)" ($drift -join '; ') 'Install.ps1 re-registers it' }
    else { Report PASS "task $($t.Name)" "registered as defined ($level, trigger $($t.Trigger), limit $($t.TimeLimit))" }
}

# 3. SSH surface
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd -and $sshd.Status -eq 'Running') { Report PASS 'sshd' 'running' }
else { Report FAIL 'sshd' "service $(if ($sshd) { $sshd.Status } else { 'missing' })" 'Start-Service sshd (and set it to Automatic)' }

try {
    $rule = Get-NetFirewallRule -Name 'sshd-k15' -ErrorAction Stop
    if ($rule.Enabled -eq 'True') { Report PASS 'firewall' 'sshd-k15 rule enabled' }
    else { Report WARN 'firewall' 'sshd-k15 rule disabled' 'Enable-NetFirewallRule -Name sshd-k15' }
} catch { Report FAIL 'firewall' 'sshd-k15 rule missing' 'add an inbound TCP/22 allow rule named sshd-k15' }

$ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
if (Test-Path $ak) {
    $acl = (icacls $ak 2>$null | Out-String)
    $unexpected = ($acl -split "`n" | Where-Object { $_ -match ':\(' -and $_ -notmatch 'SYSTEM|Administrators' })
    if ($unexpected) { Report WARN 'authorized_keys' 'unexpected ACL entries' 'reset the ACL with icacls to Administrators + SYSTEM only' }
    else { Report PASS 'authorized_keys' 'present, ACL sane' }
} else { Report FAIL 'authorized_keys' 'missing' 'sshd ignores keys without this file' }

# 4. Wake-on-LAN. The K15 wakes this PC with a magic packet (couch.py wol()),
# so MagicPacket must stay 1. Pattern must stay 0: at 1 the NIC wakes on
# ordinary LAN broadcast traffic
function Get-WolKeyword([string]$Adapter, [string]$Keyword) {
    $p = Get-NetAdapterAdvancedProperty -Name $Adapter -RegistryKeyword $Keyword -ErrorAction SilentlyContinue
    if ($p) { return "$($p.RegistryValue)" }
    return ''
}
$wolNic = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq 'Up' -and (Get-WolKeyword $_.Name '*WakeOnMagicPacket') -ne '' } |
    Select-Object -First 1
if (-not $wolNic) {
    Report WARN 'wake-on-lan' 'no connected wired adapter exposes the WoL keywords' 'Wi-Fi only? the K15 cannot WoL this PC'
} else {
    $n = $wolNic.Name
    if ((Get-WolKeyword $n '*WakeOnMagicPacket') -eq '1') { Report PASS 'wol magic packet' "$n enabled" }
    else { Report FAIL 'wol magic packet' "$n disabled" "the K15 cannot wake this PC: Set-NetAdapterAdvancedProperty -Name '$n' -RegistryKeyword '*WakeOnMagicPacket' -RegistryValue 1" }

    if ((Get-WolKeyword $n '*WakeOnPattern') -eq '0') { Report PASS 'wol pattern match' 'disabled (PC can hold sleep)' }
    else { Report WARN 'wol pattern match' "$n enabled" "LAN broadcast wakes the PC seconds after each sleep: Set-NetAdapterAdvancedProperty -Name '$n' -RegistryKeyword '*WakeOnPattern' -RegistryValue 0" }

    $s5 = Get-WolKeyword $n 'S5WakeOnLan'
    if ($s5 -eq '' -or $s5 -eq '1') { Report PASS 'wol from shutdown' 'S5 wake available' }
    else { Report WARN 'wol from shutdown' 'S5WakeOnLan disabled' 'the K15 can wake this PC from sleep but not from a full shutdown' }

    if ((powercfg /devicequery wake_armed 2>$null | Out-String) -match [regex]::Escape($wolNic.InterfaceDescription)) {
        Report PASS 'wol armed' 'NIC armed to wake the system'
    } else {
        Report WARN 'wol armed' 'NIC absent from wake_armed' "Device Manager > $n > Power Management > allow this device to wake the computer"
    }
}

# 5. VirtualHere
if (Get-Process vhui64 -ErrorAction SilentlyContinue) {
    Report PASS 'virtualhere' 'client running'
    # The Puck's own address, not just the hub name: a hub that answers while
    # the Puck is missing or renumbered is exactly the failure worth catching.
    $addr = Get-PuckAddress
    if ($addr) { Report PASS 'vh hub' "Puck visible at $addr" }
    else { Report WARN 'vh hub' "LIST: $(Get-VhList)" 'K15 server down, or the Puck is off the hub - rerun' }
} else {
    Report FAIL 'virtualhere' 'client not running' 'launch vhui64.exe (Startup shortcut missing?)'
}

# 6. Display probe
$h = Get-PrimaryHeight
if ($h -gt 0) {
    if (Test-TvIsPrimary) { Report WARN 'display' "primary height $h = TV topology" 'expected at a desk? run Office-Safety or Ctrl+Alt+E' }
    else { Report PASS 'display' "probe ok (primary height $h, office topology)" }
} else { Report FAIL 'display' 'probe returned 0' 'Get-PrimaryHeight broken - PowerShell/DPI issue' }

$tv = @(Get-TvNames)
if ($tv -match [regex]::Escape($CG.TvEdid)) { Report PASS 'tv link' "$($CG.TvEdid) listed by Windows" }
else { Report WARN 'tv link' "Windows lists: $($tv -join ', ')" 'Device Manager > Scan for hardware changes; else re-seat the HDMI cable at the GPU' }

# 7. Session state + logs
if (Test-ReadyMarker) { Report WARN 'ready marker' 'present - a session is (or looks) active' 'stale after a crash? Exit task or Office-Safety clears it' }
else { Report PASS 'ready marker' 'absent (idle)' }

# The verb markers are not a health signal on this rig: Dispatch writes them
# from the ELEVATED forced-command context and the unelevated tasks cannot
# delete them (Read-CgMarker logs the denial), so each survives as the last
# dispatch of that verb until the next one overwrites it. What it says is the
# useful datum.
foreach ($m in 'LaunchMarker','NavMarker','StopMarker') {
    $leaf = Split-Path $CG[$m] -Leaf
    if (Test-Path $CG[$m]) {
        $val = ("$(Get-Content $CG[$m] -TotalCount 1)").Trim()
        $age = ((Get-Date) - (Get-Item $CG[$m]).LastWriteTime).TotalHours
        Report PASS "$leaf marker" ("last dispatch '{0}', {1:n0}h ago" -f $val, $age)
    } else { Report PASS "$leaf marker" 'absent (never dispatched)' }
}

try {
    $probeFile = Join-Path $CG.LogDir ".doctor-write-test"
    Set-Content $probeFile 'x' -ErrorAction Stop
    Remove-Item $probeFile -Force
    Report PASS 'logs dir' 'writable'
} catch { Report FAIL 'logs dir' "not writable: $($_.Exception.Message)" 'transcripts will fail - check folder exists/permissions' }

Write-Host ''
Write-Host "$($script:Counts.PASS) pass, $($script:Counts.WARN) warn, $($script:Counts.FAIL) fail"
exit $script:Counts.FAIL
