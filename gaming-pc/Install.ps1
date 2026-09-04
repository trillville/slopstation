# Install the gaming-PC side: the directories, config.psd1, the deployed
# script set, the scheduled tasks, the K15-only SSH firewall rule and the
# forced-command key. Safe to re-run: each step makes what is missing and
# leaves what is right.
#
# From an ELEVATED PowerShell, in a checkout, on the PC:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Install.ps1 -K15Address 192.168.1.10 [-K15PublicKey 'ssh-ed25519 AAAA... k15']
#
# The tasks run as the account that ran this script, so run it as the user
# who sits at the desktop, elevated through UAC, not as a separate
# administrator account.
param(
    [Parameter(Mandatory = $true)][string]$K15Address,
    [string]$K15PublicKey = '',
    [string]$Dest = 'C:\CouchGaming'
)
$ErrorActionPreference = 'Stop'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'run this from an elevated PowerShell (the firewall rule, the key file and the elevated tasks need it)'
}

function Step([string]$m) { Write-Host "== $m" }

# 1. config.psd1: the per-installation values. Created from the example once
# and then left alone; Deploy.ps1 never touches it either.
Step 'config'
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$config = Join-Path $Dest 'config.psd1'
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $PSScriptRoot 'config.example.psd1') $config
    Write-Host "created $config from config.example.psd1:"
    Get-Content $config | Where-Object { $_ -notmatch '^\s*#' -and $_.Trim() } | ForEach-Object { "    $_" }
    Write-Host 'Check those values against this PC, then run Install.ps1 again.'
    exit 1
}
Write-Host "  $config"

# 2. The script set, and the library it carries (the task table, the state
# directory). Deploy.ps1 refuses a partial checkout and parks on a live
# session only when asked to, so here it copies straight away.
Step 'deploy'
& (Join-Path $PSScriptRoot 'Deploy.ps1') -Dest $Dest
if ($LASTEXITCODE -or -not (Test-Path (Join-Path $Dest 'CouchGaming.common.ps1'))) {
    throw 'Deploy.ps1 did not ship the scripts'
}
. (Join-Path $Dest 'CouchGaming.common.ps1')
New-Item -ItemType Directory -Force -Path $CG.LogDir, $CG.StateDir | Out-Null

# 3. Scheduled tasks, from the table in CouchGaming.common.ps1. -Force
# replaces a task that already exists, so a re-run corrects drift.
Step 'scheduled tasks'
$user = "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
foreach ($t in $CG.Tasks) {
    $level = if ($t.Elevated) { 'Highest' } else { 'Limited' }
    $reg = @{
        TaskPath  = $CG.TaskPath
        TaskName  = $t.Name
        Action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (Get-CgTaskArguments $t)
        Principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $level
        Settings  = $settings
        Force     = $true
    }
    switch ($t.Trigger) {
        'logon' { $reg.Trigger = New-ScheduledTaskTrigger -AtLogOn -User $user }
        'wake'  {
            # No cmdlet builds an event trigger; the CIM class does.
            $class = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
            $trigger = New-CimInstance -CimClass $class -ClientOnly
            $trigger.Subscription = $CG.WakeEventQuery
            $trigger.Enabled = $true
            $reg.Trigger = $trigger
        }
    }
    Register-ScheduledTask @reg | Out-Null
    Write-Host "  $($CG.TaskPath)$($t.Name) -> $($t.Script) ($level, trigger $($t.Trigger))"
}

# 4. SSH from the K15 only. The rule Windows adds with the OpenSSH capability
# allows every address, so it is disabled rather than deleted.
Step 'ssh'
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd) {
    Set-Service sshd -StartupType Automatic
    if ($sshd.Status -ne 'Running') { Start-Service sshd }
    Write-Host '  sshd running, Automatic'
} else {
    Write-Host '  note: no sshd service - Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0, then re-run'
}
if (Get-NetFirewallRule -Name 'sshd-k15' -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name 'sshd-k15' -RemoteAddress $K15Address -Enabled True
} else {
    New-NetFirewallRule -Name 'sshd-k15' -DisplayName 'OpenSSH from K15 only' -Direction Inbound `
        -Protocol TCP -LocalPort 22 -RemoteAddress $K15Address -Action Allow | Out-Null
}
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Write-Host "  sshd-k15: TCP/22 from $K15Address only"

# 5. The K15's key, bound to Dispatch.ps1. sshd reads administrators' keys
# from this file and ignores it unless only Administrators and SYSTEM can
# touch it.
Step 'authorized key'
$ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
$options = 'command="powershell.exe -NoProfile -ExecutionPolicy Bypass -File ' + (Join-Path $CG.Root 'Dispatch.ps1') +
           '",no-port-forwarding,no-agent-forwarding,no-x11-forwarding '
if ($K15PublicKey) {
    if ($K15PublicKey -notmatch '^(ssh|ecdsa)-[a-z0-9-]+ [A-Za-z0-9+/=]+') { throw '-K15PublicKey is not a public key line' }
    $body = ($K15PublicKey -split '\s+')[1]
    if ((Test-Path $ak) -and ((Get-Content $ak) -match [regex]::Escape($body))) {
        Write-Host '  key already present'
    } else {
        [IO.File]::AppendAllText($ak, $options + $K15PublicKey + "`n", (New-Object Text.UTF8Encoding($false)))
        Write-Host "  key added to $ak"
    }
    icacls $ak /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
} else {
    Write-Host "  no -K15PublicKey given. Add this line to $ak, then restrict it with"
    Write-Host "  icacls $ak /inheritance:r /grant Administrators:F /grant SYSTEM:F"
    Write-Host "  $options<the K15's public key>"
}

# 6. The doctor's verdict is the result. Its exit code is its FAIL count.
Step 'doctor'
& (Join-Path $Dest 'Doctor.ps1')
exit $LASTEXITCODE
