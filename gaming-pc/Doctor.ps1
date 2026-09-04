# Gaming-PC chain diagnosis: powershell -ExecutionPolicy Bypass -File Doctor.ps1
# Read-only. Exit code = number of FAILs.

$libPath = Join-Path $PSScriptRoot 'CouchGaming.common.ps1'
if (-not (Test-Path $libPath)) {
    Write-Host "[FAIL] CouchGaming.common.ps1 missing beside this script - partial deploy; re-copy all files from the repo" -ForegroundColor Red
    exit 1
}
. $libPath

$script:Counts = @{ PASS = 0; WARN = 0; FAIL = 0 }
function Report([string]$Level, [string]$Name, [string]$Detail, [string]$Hint = '') {
    $script:Counts[$Level]++
    $color = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }[$Level]
    $line = "[$Level] ${Name}: $Detail"
    if ($Hint -and $Level -ne 'PASS') { $line += "  -> $Hint" }
    Write-Host $line -ForegroundColor $color
}

# 1. Deployed files
$files = @('CouchGaming.common.ps1','Enter-TV.ps1','Exit-TV.ps1','Office-Safety.ps1','Wake-Safety.ps1','Dispatch.ps1','Launch-Game.ps1','Nav-BigPicture.ps1','Stop-Game.ps1','Doctor.ps1','vhui64.exe','OFFICE.lnk','TV-GAMING.lnk')
$missing = $files | Where-Object { -not (Test-Path (Join-Path $CG.Root $_)) }
if ($missing) { Report FAIL 'files' "missing: $($missing -join ', ')" 're-copy from repo gaming-pc/; recreate .lnk files from DisplayMagician' }
else { Report PASS 'files' "$($files.Count)/$($files.Count) present" }

# 2. Scheduled tasks
foreach ($t in 'Enter','Exit','ForceOfficeAtLogon','WakeSafety','LaunchGame','Nav','StopGame') {
    schtasks /Query /TN "\CouchGaming\$t" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Report PASS "task $t" 'registered' }
    else { Report FAIL "task $t" 'not registered' 'register it with schtasks /Create as an interactive task for this user' }
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
if ($tv -match $CG.TvEdid) { Report PASS 'tv link' "$($CG.TvEdid) listed by Windows" }
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
