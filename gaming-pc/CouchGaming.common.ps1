# Shared functions for couch-gaming scripts.

# Per-installation values come from config.psd1 beside this file; see
# config.example.psd1. A missing or incomplete file stops every task here,
# with one message, instead of running against another house's hardware.
$script:CgConfigKeys = @{ PuckName = [string]; PuckHwId = [string]; TvEdid = [string]; TvHeight = [int] }

function Import-CgConfig {
    $path = Join-Path $PSScriptRoot 'config.psd1'
    if (-not (Test-Path $path)) {
        throw "$path is missing - copy config.example.psd1 there and edit it (Install.ps1 does this)"
    }
    # The file's one hashtable, evaluated without running code: what
    # Import-PowerShellDataFile does, on hosts that lack the cmdlet too.
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errs)
    $table = $ast.Find({ $args[0] -is [System.Management.Automation.Language.HashtableAst] }, $false)
    if ($errs -or -not $table) { throw "config.psd1 does not parse as one hashtable - see config.example.psd1" }
    $cfg = $table.SafeGetValue()
    foreach ($k in $script:CgConfigKeys.Keys) {
        if (-not $cfg.ContainsKey($k) -or "$($cfg[$k])" -eq '') {
            throw "config.psd1: $k is missing - see config.example.psd1"
        }
        if ($cfg[$k] -isnot $script:CgConfigKeys[$k]) {
            throw "config.psd1: $k must be a $($script:CgConfigKeys[$k].Name)"
        }
        if ($cfg[$k] -is [string] -and $cfg[$k] -match '^<.+>$') {
            throw "config.psd1: $k is still the example placeholder - see config.example.psd1"
        }
    }
    $cfg
}
$script:CgConfig = Import-CgConfig

$CG = @{
    Root        = $PSScriptRoot
    LogDir      = Join-Path $PSScriptRoot 'logs'
    Vh          = Join-Path $PSScriptRoot 'vhui64.exe'
    VhResult    = Join-Path $PSScriptRoot 'logs\vh-last.txt'
    VhNudge     = Join-Path $PSScriptRoot 'logs\vh-nudge.txt'
    PuckName    = $script:CgConfig.PuckName  # the hub's device NAME; addresses are resolved per use
    PuckHwId    = $script:CgConfig.PuckHwId
    TvEdid      = $script:CgConfig.TvEdid
    SteamWindow = 'Steam'                  # EXACT title of the desktop library window
    BpmWindow   = 'Steam Big Picture Mode' # EXACT title of the Big Picture window
    TvHeight    = $script:CgConfig.TvHeight  # see Test-TvIsPrimary
    OfficeLnk   = Join-Path $PSScriptRoot 'OFFICE.lnk'
    TvGamingLnk = Join-Path $PSScriptRoot 'TV-GAMING.lnk'
    StateDir    = 'C:\ProgramData\CouchGaming'   # cross-context state, not under Root
}
# Marker paths shared with Dispatch.ps1.
$CG.ReadyMarker  = Join-Path $CG.StateDir 'ready'
$CG.TurnMarker   = Join-Path $CG.StateDir 'turn'
$CG.LaunchMarker = Join-Path $CG.StateDir 'launch-app'
$CG.NavMarker    = Join-Path $CG.StateDir 'nav-target'
$CG.StopMarker   = Join-Path $CG.StateDir 'stop-app'

# The scheduled tasks. Install.ps1 registers them from this table and
# Doctor.ps1 checks what is registered against it. Trigger 'logon' fires at
# the user's logon, 'wake' on the resume-from-sleep event below, and 'none'
# means Dispatch.ps1 starts the task on demand. Delay (logon triggers only)
# and TimeLimit are ISO 8601 durations. Office-Safety keeps the 72-hour limit
# it has always had; narrowing it is a separate change.
$CG.TaskPath = '\CouchGaming\'
$CG.Tasks = @(
    @{ Name = 'Enter';              Script = 'Enter-TV.ps1';       Hidden = $true;  Elevated = $true;  Trigger = 'none';  TimeLimit = 'PT5M'  }
    @{ Name = 'Exit';               Script = 'Exit-TV.ps1';        Hidden = $true;  Elevated = $true;  Trigger = 'none';  TimeLimit = 'PT5M'  }
    @{ Name = 'ForceOfficeAtLogon'; Script = 'Office-Safety.ps1';  Hidden = $true;  Elevated = $true;  Trigger = 'logon'; TimeLimit = 'PT72H'; Delay = 'PT20S' }
    @{ Name = 'WakeSafety';         Script = 'Wake-Safety.ps1';    Hidden = $true;  Elevated = $false; Trigger = 'wake';  TimeLimit = 'PT5M'  }
    @{ Name = 'LaunchGame';         Script = 'Launch-Game.ps1';    Hidden = $false; Elevated = $false; Trigger = 'none';  TimeLimit = 'PT5M'  }
    @{ Name = 'Nav';                Script = 'Nav-BigPicture.ps1'; Hidden = $false; Elevated = $false; Trigger = 'none';  TimeLimit = 'PT5M'  }
    @{ Name = 'StopGame';           Script = 'Stop-Game.ps1';      Hidden = $false; Elevated = $false; Trigger = 'none';  TimeLimit = 'PT5M'  }
)
$CG.WakeEventQuery = "<QueryList><Query><Select Path='System'>*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>"

# What a task runs: its script from this directory.
function Get-CgTaskArguments([hashtable]$Task) {
    $hidden = if ($Task.Hidden) { ' -WindowStyle Hidden' } else { '' }
    "-NoProfile$hidden -ExecutionPolicy Bypass -File " + (Join-Path $CG.Root $Task.Script)
}

$script:CgStopwatch = [Diagnostics.Stopwatch]::StartNew()

# Read a recent correlation ID. The value is validated before use in filenames.
$script:CgTurnStaleSec = 300

function Get-CgTurn {
    try {
        $f = Get-Item $CG.TurnMarker -ErrorAction Stop
        if (((Get-Date) - $f.LastWriteTime).TotalSeconds -gt $script:CgTurnStaleSec) { return '' }
        $t = (Get-Content $CG.TurnMarker -ErrorAction Stop -Raw).Trim()
        if ($t -match '^[0-9a-f]{1,8}$') { return $t }
    } catch { }
    return ''
}

$script:CgTurn = Get-CgTurn
# Start-CgTranscript replaces this default lane.
$script:CgLane = if ($MyInvocation.PSCommandPath) {
    [IO.Path]::GetFileNameWithoutExtension($MyInvocation.PSCommandPath).ToLower()
} else { 'pc' }

function Log($m) {
    Write-Host ("[+{0,5:n1}s] {1}" -f $script:CgStopwatch.Elapsed.TotalSeconds, $m)
}

# Emit one structured event.
function Write-CgEvent {
    param([string]$Event, [hashtable]$Fields = @{}, [string]$Level = 'info')
    try {
        $rec = [ordered]@{
            ts      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
            level   = $Level
            env     = 'prod'
            service = 'gamepc'
            lane    = $script:CgLane
            event   = $Event
        }
        if ($script:CgTurn) { $rec.turn = $script:CgTurn }
        $rec.host = $env:COMPUTERNAME
        $rec.dur_ms = [int]$script:CgStopwatch.Elapsed.TotalMilliseconds
        # Prefix fields that would overwrite event metadata.
        $owned = @('ts','level','env','service','lane','event','host')
        foreach ($k in $Fields.Keys) {
            if ($owned -contains $k) { $rec["f_$k"] = $Fields[$k] }
            else { $rec[$k] = $Fields[$k] }
        }
        $file = Join-Path $CG.LogDir ("pc-{0}.jsonl" -f (Get-Date -Format yyyyMMdd))
        $line = ConvertTo-Json -InputObject $rec -Compress -Depth 4
        # PowerShell 5.1's UTF-8 encoding adds a BOM, so use .NET directly.
        [IO.File]::AppendAllText(
            $file, $line + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }     # telemetry never costs a session
}

function Start-CgTranscript([string]$Tag) {
    $script:CgLane = $Tag
    New-Item -ItemType Directory -Force -Path $CG.LogDir -ErrorAction SilentlyContinue | Out-Null
    # Include the turn ID so transcripts can be matched to launches.
    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $name = if ($script:CgTurn) { "{0}-{1}-{2}.log" -f $Tag, $stamp, $script:CgTurn }
            else                { "{0}-{1}.log" -f $Tag, $stamp }
    # An unwritable logs\ must not abort a task before its try block.
    try { Start-Transcript (Join-Path $CG.LogDir $name) } catch { Log "note: transcript unavailable - $_" }
    Write-CgEvent "${Tag}_start"
}

# Stop-Transcript writes an error when no transcript is active.
function Stop-CgTranscript { try { Stop-Transcript | Out-Null } catch { } }

function Wait-For([scriptblock]$Cond, [double]$TimeoutSec, [string]$What) {
    $end = $script:CgStopwatch.Elapsed.TotalSeconds + $TimeoutSec
    while ($script:CgStopwatch.Elapsed.TotalSeconds -lt $end) {
        if (& $Cond) { Log $What; return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "TIMEOUT waiting for: $What"; return $false
}

# Probe display state in a fresh process to avoid stale scheduled-task data.
$script:CgProbe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$script:CgProbeEnc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script:CgProbe))

function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $script:CgProbeEnc | Select-Object -Last 1) }

# The current desk monitor is not 2160 pixels tall, so height identifies the TV.
function Test-TvIsPrimary { (Get-PrimaryHeight) -eq $CG.TvHeight }

function Get-TvNames {
    Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue |
    ForEach-Object { -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 }) }
}

# Use Windows device enumeration to verify VirtualHere claim state.
function Test-PuckPresent {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match $CG.PuckHwId -and $_.Status -eq 'OK' })
}

# Redirect console-less VirtualHere calls to prevent GUI error dialogs.
function Get-VhList {
    & $CG.Vh -t "LIST" -r $CG.VhResult | Out-Null
    Start-Sleep -Milliseconds 400
    (Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '
}

# Resolve the Puck by name because VirtualHere addresses can change.
function Get-PuckAddress {
    if ((Get-VhList) -match ([regex]::Escape($CG.PuckName) + '\s*\(([^)]+)\)')) { $Matches[1] }
    else { '' }
}

# Stop DisplayMagician after each profile application.
function Stop-DisplayMagician {
    Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force
}

# Copy DisplayMagician warnings and errors into the retained session logs.
function Copy-DisplayMagicianLog([string]$Tag) {
    try {
        $src = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'DisplayMagician\Logs\*.log') `
                   -File -ErrorAction Stop |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $src) { return }
        $stamp = Get-Date -Format yyyyMMdd-HHmmss
        $name = if ($script:CgTurn) { "dm-$stamp-$($script:CgTurn)-$Tag.log" }
                else                { "dm-$stamp-$Tag.log" }
        $keep = Select-String -Path $src.FullName -Pattern '\|(ERROR|WARN|INFO)\|' |
                ForEach-Object { $_.Line }
        # BOM-less, for the same reason Write-CgEvent hand-rolls its encoder.
        [IO.File]::WriteAllLines((Join-Path $CG.LogDir $name), $keep,
                                 (New-Object System.Text.UTF8Encoding($false)))
        Log "captured DisplayMagician log -> $name ($($keep.Count) lines of $($src.Name))"
    } catch {
        Log "note: could not capture the DisplayMagician log - $_"
    }
}

# Steam's own record of what is running; 0 = nothing. Same registry value the
# Dispatch `playing` verb answers from.
function Get-RunningAppId {
    $id = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
    if ($id) { [int]$id } else { 0 }
}

if (-not ('CG.Win' -as [type])) {
    # WindowByTitle, not FindWindow(null, title): FindWindow returns 0 for
    # windows EnumWindows reports under exactly that title (Steam's UI is an
    # embedded browser drawn by steamwebhelper.exe - also why steam.exe's
    # MainWindowHandle is 0).
    Add-Type -Namespace CG -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
delegate bool EnumProc(IntPtr h, IntPtr p);
public static string ForegroundTitle() {
    IntPtr h = GetForegroundWindow();
    if (h == IntPtr.Zero) return "";
    var sb = new System.Text.StringBuilder(300);
    GetWindowTextW(h, sb, 300);
    return sb.ToString();
}
public static IntPtr WindowByTitle(string want) {
    IntPtr hit = IntPtr.Zero;
    EnumWindows((h, p) => {
        if (!IsWindowVisible(h)) return true;
        var sb = new System.Text.StringBuilder(300);
        GetWindowTextW(h, sb, 300);
        if (sb.ToString() == want) { hit = h; return false; }
        return true;
    }, IntPtr.Zero);
    return hit;
}
'@
}

# Minimize the desktop Steam window so Big Picture can receive controller input.
function Hide-DesktopSteam {
    $h = [CG.Win]::WindowByTitle($CG.SteamWindow)
    if ($h -ne [IntPtr]::Zero) {
        [void][CG.Win]::ShowWindow($h, 6)      # 6 = SW_MINIMIZE
        Log 'desktop Steam minimized'
    }
}

# Return the foreground window title for the ready event.
function Get-ForegroundTitle { [CG.Win]::ForegroundTitle() }

# Steam's install path, from the registry. Shared with Launch-Game.
function Get-SteamExe {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath -replace '/', '\'
    $exe = Join-Path $steam 'steam.exe'
    if (-not (Test-Path $exe)) { throw "steam.exe not found at $exe" }
    $exe
}

# Return every library root listed in libraryfolders.vdf.
function Get-SteamLibraryRoots {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
    if (-not $steam) { return @() }
    $steam = $steam -replace '/', '\'
    $roots = @($steam)
    $lf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    if (Test-Path $lf) {
        foreach ($line in (Get-Content $lf)) {
            if ($line -match '^\s*"path"\s+"(.+)"\s*$') { $roots += ($Matches[1] -replace '\\\\', '\') }
        }
    }
    $roots | Select-Object -Unique
}

# appid -> its full install directory (…\steamapps\common\<installdir>), from
# the appmanifest's `installdir` field, or $null.
function Get-AppInstallDir([int]$AppId) {
    foreach ($root in (Get-SteamLibraryRoots)) {
        $acf = Join-Path $root "steamapps\appmanifest_$AppId.acf"
        if (Test-Path $acf) {
            $t = Get-Content $acf -Raw -Encoding UTF8
            if ($t -match '"installdir"\s+"([^"]+)"') {
                return (Join-Path $root ("steamapps\common\" + $Matches[1]))
            }
        }
    }
    $null
}

# Find game processes by matching executable paths to the install directory.
function Get-GameProcess([int]$AppId) {
    $dir = Get-AppInstallDir $AppId
    if (-not $dir) { return @() }
    # The trailing separator prevents matches against sibling directory names.
    $prefix = $dir.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    Get-Process | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) }
        catch { $false }
    }
}

# Apply a profile shortcut, verify with $Until, kill DisplayMagician either way.
function Invoke-DisplayProfile([string]$Lnk, [scriptblock]$Until, [double]$TimeoutSec = 20, [int]$Attempts = 1, [string]$What = 'profile applied') {
    for ($i = 1; $i -le $Attempts; $i++) {
        Start-Process $Lnk
        $ok = Wait-For $Until $TimeoutSec "$What (attempt $i)"
        Stop-DisplayMagician
        if ($ok) { return $true }
    }
    return $false
}

# Recycle stale claims, then claim the Puck and verify it through Windows.
function Request-PuckClaim {
    if (Test-PuckPresent) {
        Log 'stale Puck claim detected - releasing for a fresh instance'
        & $CG.Vh -t "STOP USING,$(Get-PuckAddress)" -r $CG.VhResult
        if (-not (Wait-For { -not (Test-PuckPresent) } 6 'stale claim released')) {
            throw 'stale Puck claim would not release - aborting launch'
        }
    }
    $claimed = $false
    for ($i = 1; -not $claimed -and $i -le 2; $i++) {
        # Re-resolve because releasing a stale claim can change the address.
        $addr = Get-PuckAddress
        & $CG.Vh -t "USE,$addr" -r $CG.VhResult
        $claimed = Wait-For { Test-PuckPresent } 8 "Puck enumerated (attempt $i)"
        Log ("vh attempt {0} ({1}): {2}" -f $i, $addr, ((Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '))
    }
    if (-not $claimed) { throw 'VirtualHere claim did not produce a device after 2 attempts' }
}

# Release the Puck; retry until Windows enumeration agrees it's gone.
function Request-PuckRelease([int]$Attempts = 3) {
    $released = $false
    for ($i = 1; -not $released -and $i -le $Attempts; $i++) {
        $addr = Get-PuckAddress
        & $CG.Vh -t "STOP USING,$addr" -r $CG.VhResult
        Start-Sleep 1
        Log ("vh attempt {0} ({1}): {2}" -f $i, $addr, ((Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '))
        $released = -not (Test-PuckPresent)
        if (-not $released) { Start-Sleep 2 }
    }
    $released
}

# Query task state through the locale-independent ScheduledTasks API.
function Test-CgTaskRunning([string]$Name) {
    (Get-ScheduledTask -TaskPath $CG.TaskPath -TaskName $Name -ErrorAction SilentlyContinue).State -eq 'Running'
}

function Stop-CgTask([string]$Name) { schtasks /End /TN "$($CG.TaskPath)$Name" | Out-Null }

# Transcript retention; called from Office-Safety (logon) and Wake-Safety (wake).
function Clear-OldLogs([int]$Days = 30, [int]$ArchiveAfterDays = 2) {
    # Archive completed logs so the shipper watches only files that may grow.
    $archive = Join-Path $CG.LogDir 'archive'
    New-Item -ItemType Directory -Force -Path $archive -ErrorAction SilentlyContinue | Out-Null
    # Search only the live log directory, not archive\.
    Get-ChildItem (Join-Path $CG.LogDir '*') -Include *.log, *.jsonl -File `
            -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$ArchiveAfterDays) } |
        Move-Item -Destination $archive -Force -ErrorAction SilentlyContinue

    # Then retention over both folders and both streams.
    Get-ChildItem $CG.LogDir -Include *.log, *.jsonl -File -Recurse `
            -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$Days) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# Enter writes the READY marker after setup; Exit and safety tasks remove it.
function Set-ReadyMarker {
    New-Item -ItemType Directory -Force (Split-Path $CG.ReadyMarker) | Out-Null
    $stamp = if ($script:CgTurn) { $script:CgTurn } else { (Get-Date).ToString('o') }
    Set-Content $CG.ReadyMarker $stamp
}
function Clear-ReadyMarker { Remove-Item $CG.ReadyMarker -ErrorAction SilentlyContinue }
function Test-ReadyMarker  { Test-Path $CG.ReadyMarker }

# Read a one-shot task payload. Deletion can fail when Dispatch created the
# marker from an elevated SSH session.
function Read-CgMarker([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    $raw = Get-Content $Path -TotalCount 1
    try { Remove-Item $Path -Force } catch {
        Log 'marker not deletable from this token - Dispatch overwrites it next time'
    }
    "$raw".Trim()
}
