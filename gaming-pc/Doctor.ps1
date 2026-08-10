# Gaming-PC chain diagnosis: powershell -ExecutionPolicy Bypass -File Doctor.ps1
# Read-only. Run when sessions misbehave, after Windows updates, or as a preflight.
# Exit code = number of FAILs.

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
$files = @('CouchGaming.common.ps1','Enter-TV.ps1','Exit-TV.ps1','Office-Safety.ps1','Wake-Safety.ps1','Dispatch.ps1','Doctor.ps1','vhui64.exe','OFFICE.lnk','TV-GAMING.lnk')
$missing = $files | Where-Object { -not (Test-Path (Join-Path $CG.Root $_)) }
if ($missing) { Report FAIL 'files' "missing: $($missing -join ', ')" 're-copy from repo gaming-pc/ (lnk files: recreate per guide Stage 6)' }
else { Report PASS 'files' "$($files.Count)/$($files.Count) present" }

# 2. Scheduled tasks
foreach ($t in 'Enter','Exit','ForceOfficeAtLogon','WakeSafety') {
    schtasks /Query /TN "\CouchGaming\$t" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Report PASS "task $t" 'registered' }
    else { Report FAIL "task $t" 'not registered' 'guide Stage 6/8 has the registration commands' }
}

# 3. SSH surface
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd -and $sshd.Status -eq 'Running') { Report PASS 'sshd' 'running' }
else { Report FAIL 'sshd' "service $(if ($sshd) { $sshd.Status } else { 'missing' })" 'Start-Service sshd; guide Stage 8.1' }

try {
    $rule = Get-NetFirewallRule -Name 'sshd-k15' -ErrorAction Stop
    if ($rule.Enabled -eq 'True') { Report PASS 'firewall' 'sshd-k15 rule enabled' }
    else { Report WARN 'firewall' 'sshd-k15 rule disabled' 'Enable-NetFirewallRule -Name sshd-k15' }
} catch { Report FAIL 'firewall' 'sshd-k15 rule missing' 'guide Stage 8.1' }

$ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
if (Test-Path $ak) {
    $acl = (icacls $ak 2>$null | Out-String)
    $unexpected = ($acl -split "`n" | Where-Object { $_ -match ':\(' -and $_ -notmatch 'SYSTEM|Administrators' })
    if ($unexpected) { Report WARN 'authorized_keys' 'unexpected ACL entries' 're-run the icacls command from guide Stage 8.2' }
    else { Report PASS 'authorized_keys' 'present, ACL sane' }
} else { Report FAIL 'authorized_keys' 'missing' 'guide Stage 8.2 - sshd ignores keys without this file' }

# 4. VirtualHere
if (Get-Process vhui64 -ErrorAction SilentlyContinue) {
    Report PASS 'virtualhere' 'client running'
    $list = Get-VhList
    if ($list -match [regex]::Escape($CG.Puck) -or $list -match 'K15') { Report PASS 'vh hub' 'K15 hub/Puck visible' }
    else { Report WARN 'vh hub' "LIST: $list" 'K15 server down, or transient - rerun' }
} else {
    Report FAIL 'virtualhere' 'client not running' 'launch vhui64.exe (Startup shortcut missing?)'
}

# 5. Display probe
$h = Get-PrimaryHeight
if ($h -gt 0) {
    if (Test-TvIsPrimary) { Report WARN 'display' "primary height $h = TV topology" 'expected at a desk? run Office-Safety or Ctrl+Alt+E' }
    else { Report PASS 'display' "probe ok (primary height $h, office topology)" }
} else { Report FAIL 'display' 'probe returned 0' 'Get-PrimaryHeight broken - PowerShell/DPI issue' }

# 6. Session state + logs
if (Test-ReadyMarker) { Report WARN 'ready marker' 'present - a session is (or looks) active' 'stale after a crash? Exit task or Office-Safety clears it' }
else { Report PASS 'ready marker' 'absent (idle)' }

try {
    $probeFile = Join-Path $CG.LogDir ".doctor-write-test"
    Set-Content $probeFile 'x' -ErrorAction Stop
    Remove-Item $probeFile -Force
    Report PASS 'logs dir' 'writable'
} catch { Report FAIL 'logs dir' "not writable: $($_.Exception.Message)" 'transcripts will fail - check folder exists/permissions' }

Write-Host ''
Write-Host "$($script:Counts.PASS) pass, $($script:Counts.WARN) warn, $($script:Counts.FAIL) fail"
exit $script:Counts.FAIL
