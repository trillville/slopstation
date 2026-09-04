#Requires -RunAsAdministrator
# Configure the gaming-PC runtime from an administrator PowerShell.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$K15Address,
    [string]$K15PublicKeyPath = '',
    [string]$TaskUser = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateRange(0, 1440)]
    [int]$WaitMinutes = 120
)

$ErrorActionPreference = 'Stop'
$Dest = 'C:\CouchGaming'
$StateDir = 'C:\ProgramData\CouchGaming'
$TaskPath = '\CouchGaming\'

[Net.IPAddress]$parsedAddress = $null
if (-not [Net.IPAddress]::TryParse($K15Address, [ref]$parsedAddress)) {
    throw "-K15Address must be one IP address, got '$K15Address'"
}

function Assert-NativeCommand([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

function Set-CgDirectoryAcl([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    & icacls.exe $Path /inheritance:r /grant:r `
        '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' `
        "${TaskUser}:(OI)(CI)M" /T /C | Out-Null
    Assert-NativeCommand "setting permissions on $Path"
}

Set-CgDirectoryAcl $Dest
Set-CgDirectoryAcl $StateDir

& (Join-Path $PSScriptRoot 'Deploy.ps1') -Dest $Dest -WaitMinutes $WaitMinutes
if ($LASTEXITCODE -ne 0) { throw "Deploy.ps1 failed with exit code $LASTEXITCODE" }

$configPath = Join-Path $Dest 'config.psd1'
if (-not (Test-Path $configPath)) {
    Copy-Item (Join-Path $Dest 'config.example.psd1') $configPath
    Write-Host "Created $configPath."
    Write-Host 'Replace the TV EDID placeholder, then run this installer again.'
    exit 2
}

# Dot-sourcing validates config.psd1 before changing tasks or SSH.
. (Join-Path $Dest 'CouchGaming.common.ps1')

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$limited = New-ScheduledTaskPrincipal -UserId $TaskUser `
    -LogonType Interactive -RunLevel Limited
$highest = New-ScheduledTaskPrincipal -UserId $TaskUser `
    -LogonType Interactive -RunLevel Highest

function New-CgAction([string]$Script) {
    $path = Join-Path $Dest $Script
    New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $path) `
        -WorkingDirectory $Dest
}

function Register-CgTask([string]$Name, [string]$Script) {
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $Name `
        -Action (New-CgAction $Script) -Settings $settings `
        -Principal $limited -Force | Out-Null
    Write-Host "registered $TaskPath$Name -> $Script"
}

Register-CgTask 'Enter' 'Enter-TV.ps1'
Register-CgTask 'Exit' 'Exit-TV.ps1'
Register-CgTask 'LaunchGame' 'Launch-Game.ps1'
Register-CgTask 'Nav' 'Nav-BigPicture.ps1'
Register-CgTask 'StopGame' 'Stop-Game.ps1'

$logon = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
$logon.Delay = 'PT20S'
Register-ScheduledTask -TaskPath $TaskPath -TaskName 'ForceOfficeAtLogon' `
    -Action (New-CgAction 'Office-Safety.ps1') -Trigger $logon `
    -Settings $settings -Principal $highest -Force | Out-Null
Write-Host "${TaskPath}ForceOfficeAtLogon -> Office-Safety.ps1"

$wakeSubscription = @"
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>
"@
$wake = New-CimInstance -Namespace 'Root/Microsoft/Windows/TaskScheduler' `
    -ClassName 'MSFT_TaskEventTrigger' -ClientOnly `
    -Property @{ Enabled = $true; Subscription = $wakeSubscription }
Register-ScheduledTask -TaskPath $TaskPath -TaskName 'WakeSafety' `
    -Action (New-CgAction 'Wake-Safety.ps1') -Trigger $wake `
    -Settings $settings -Principal $limited -Force | Out-Null
Write-Host "${TaskPath}WakeSafety -> Wake-Safety.ps1"

$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if (-not $sshd) {
    throw 'Windows OpenSSH Server is not installed. Install the OpenSSH.Server Windows capability and rerun.'
}
Set-Service sshd -StartupType Automatic
if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }

$rule = Get-NetFirewallRule -Name 'sshd-k15' -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Set-NetFirewallRule -DisplayName 'OpenSSH from K15 only' `
        -Enabled True -Direction Inbound -Action Allow -Profile Private
    $rule | Get-NetFirewallPortFilter |
        Set-NetFirewallPortFilter -Protocol TCP -LocalPort 22
    $rule | Get-NetFirewallAddressFilter |
        Set-NetFirewallAddressFilter -RemoteAddress $parsedAddress.IPAddressToString
} else {
    New-NetFirewallRule -Name 'sshd-k15' -DisplayName 'OpenSSH from K15 only' `
        -Enabled True -Direction Inbound -Action Allow -Profile Private `
        -Protocol TCP -LocalPort 22 -RemoteAddress $parsedAddress.IPAddressToString |
        Out-Null
}
foreach ($name in 'OpenSSH-Server-In-TCP','OpenSSH-Server-In-TCP-In') {
    Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue |
        Disable-NetFirewallRule
}

$sshDir = 'C:\ProgramData\ssh'
$keyFile = Join-Path $sshDir 'administrators_authorized_keys'
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
$forced = 'command="powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Dispatch.ps1",no-port-forwarding,no-agent-forwarding,no-x11-forwarding,no-pty'
if ($K15PublicKeyPath) {
    $publicKey = (Get-Content -LiteralPath $K15PublicKeyPath -Raw).Trim()
    if ($publicKey -notmatch '^ssh-(ed25519|rsa|ecdsa-[^ ]+) [A-Za-z0-9+/=]+(?: .*)?$' -or $publicKey -match '[\r\n]') {
        throw "$K15PublicKeyPath does not contain exactly one OpenSSH public key"
    }
    $entry = "$forced $publicKey"
    $existingEntry = if (Test-Path $keyFile) { (Get-Content $keyFile -Raw).Trim() } else { '' }
    $backup = "$keyFile.before-slopstation"
    if ($existingEntry -and $existingEntry -ne $entry -and -not (Test-Path $backup)) {
        Copy-Item $keyFile $backup
        Write-Host "saved the previous authorized keys to $backup"
    }
    if ($existingEntry -ne $entry) {
        [IO.File]::WriteAllText($keyFile, "$entry`r`n", [Text.Encoding]::ASCII)
    }
    Write-Host "installed the restricted K15 key in $keyFile"
} elseif (-not (Test-Path $keyFile)) {
    Write-Warning "No K15 public key installed. Add this one line to ${keyFile}:`n$forced <K15-public-key>"
}

if (Test-Path $keyFile) {
    & icacls.exe $keyFile /inheritance:r /grant:r `
        '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    Assert-NativeCommand "setting permissions on $keyFile"
}

foreach ($file in 'vhui64.exe','OFFICE.lnk','TV-GAMING.lnk') {
    if (-not (Test-Path (Join-Path $Dest $file))) {
        Write-Warning "$Dest\$file is missing; install or create it, then rerun"
    }
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $Dest 'Doctor.ps1')
exit $LASTEXITCODE
