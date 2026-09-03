# CouchGaming.common.ps1 - shared library for the couch gaming session scripts.
# Dot-source from a sibling script:   . "$PSScriptRoot\CouchGaming.common.ps1"

$CG = @{
    Root        = $PSScriptRoot
    LogDir      = Join-Path $PSScriptRoot 'logs'
    Vh          = Join-Path $PSScriptRoot 'vhui64.exe'
    VhResult    = Join-Path $PSScriptRoot 'logs\vh-last.txt'
    VhNudge     = Join-Path $PSScriptRoot 'logs\vh-nudge.txt'
    PuckName    = 'Steam Controller Puck'  # the hub's device NAME; addresses are resolved per use
    PuckHwId    = 'VID_28DE&PID_1304'      # Valve Steam Controller Puck
    TvEdid      = 'QCQ90S'                 # S90C's EDID name as Windows reports it
    SteamWindow = 'Steam'                  # EXACT title of the desktop library window
    BpmWindow   = 'Steam Big Picture Mode' # EXACT title of the Big Picture window
    TvHeight    = 2160                     # see Test-TvIsPrimary
    OfficeLnk   = Join-Path $PSScriptRoot 'OFFICE.lnk'
    TvGamingLnk = Join-Path $PSScriptRoot 'TV-GAMING.lnk'
    StateDir    = 'C:\ProgramData\CouchGaming'   # cross-context state, not under Root
}
# Markers under StateDir, shared with the dependency-free Dispatch.ps1 as
# literals; agent/tests/test_ps_parse.py holds the two sets equal. Dispatch
# writes turn/launch-app/nav-target/stop-app (schtasks /Run can't pass args)
# and reads ready.
$CG.ReadyMarker  = Join-Path $CG.StateDir 'ready'
$CG.TurnMarker   = Join-Path $CG.StateDir 'turn'
$CG.LaunchMarker = Join-Path $CG.StateDir 'launch-app'
$CG.NavMarker    = Join-Path $CG.StateDir 'nav-target'
$CG.StopMarker   = Join-Path $CG.StateDir 'stop-app'

$script:CgStopwatch = [Diagnostics.Stopwatch]::StartNew()

# The K15's correlation id for this run; absent or malformed = no correlation,
# never a failure. Re-validated on read (it ends up in a filename) and
# age-gated (unelevated LaunchGame cannot delete elevated Dispatch's marker, so
# a logon hours later must not tag its events with a long-dead launch).
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
# Start-CgTranscript sets the lane (every emitting script calls it first);
# this default only labels an emit that precedes it.
$script:CgLane = if ($MyInvocation.PSCommandPath) {
    [IO.Path]::GetFileNameWithoutExtension($MyInvocation.PSCommandPath).ToLower()
} else { 'pc' }

function Log($m) {
    Write-Host ("[+{0,5:n1}s] {1}" -f $script:CgStopwatch.Elapsed.TotalSeconds, $m)
}

# Structured twin of Log, for the milestones dashboards count and alerts fire on.
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
        # A caller field must never overwrite an emitter-owned key (they are
        # the log attributes alerts select on), so it is prefixed rather than dropped.
        $owned = @('ts','level','env','service','lane','event','host')
        foreach ($k in $Fields.Keys) {
            if ($owned -contains $k) { $rec["f_$k"] = $Fields[$k] }
            else { $rec[$k] = $Fields[$k] }
        }
        $file = Join-Path $CG.LogDir ("pc-{0}.jsonl" -f (Get-Date -Format yyyyMMdd))
        $line = ConvertTo-Json -InputObject $rec -Compress -Depth 4
        # BOM-less encoder: PowerShell 5.1's `-Encoding utf8` means WITH BOM,
        # and those three bytes before the first '{' break JSON parsing.
        [IO.File]::AppendAllText(
            $file, $line + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }     # telemetry never costs a session
}

function Start-CgTranscript([string]$Tag) {
    $script:CgLane = $Tag
    New-Item -ItemType Directory -Force -Path $CG.LogDir -ErrorAction SilentlyContinue | Out-Null
    # The turn rides in the filename too, so a folder listing maps transcripts
    # to launches.
    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $name = if ($script:CgTurn) { "{0}-{1}-{2}.log" -f $Tag, $stamp, $script:CgTurn }
            else                { "{0}-{1}.log" -f $Tag, $stamp }
    # An unwritable logs\ must not abort a task before its try block.
    try { Start-Transcript (Join-Path $CG.LogDir $name) } catch { Log "note: transcript unavailable - $_" }
    Write-CgEvent "${Tag}_start"
}

# `| Out-Null` alone does not suppress the "not transcribing" error a failed
# Start-CgTranscript leaves behind.
function Stop-CgTranscript { try { Stop-Transcript | Out-Null } catch { } }

function Wait-For([scriptblock]$Cond, [double]$TimeoutSec, [string]$What) {
    $end = $script:CgStopwatch.Elapsed.TotalSeconds + $TimeoutSec
    while ($script:CgStopwatch.Elapsed.TotalSeconds -lt $end) {
        if (& $Cond) { Log $What; return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "TIMEOUT waiting for: $What"; return $false
}

# Display state must be probed from a fresh process: WMI display classes and
# in-process GetSystemMetrics report stale values in windowless scheduled tasks.
$script:CgProbe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$script:CgProbeEnc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script:CgProbe))

function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $script:CgProbeEnc | Select-Object -Last 1) }

# Topology sentinel: primary-display height equals the TV's. Holds only because
# the desk ultrawide is not 2160 tall - a 4K or 5K2K (5120x2160) desk monitor
# would read as "TV is primary" at every logon.
function Test-TvIsPrimary { (Get-PrimaryHeight) -eq $CG.TvHeight }

function Get-TvNames {
    Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue |
    ForEach-Object { -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 }) }
}

# Windows device enumeration is the arbiter of claim state - VirtualHere's IPC
# report can read "FAILED: API Timeout" on operations that succeeded.
function Test-PuckPresent {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match $CG.PuckHwId -and $_.Status -eq 'OK' })
}

# Console-less -t calls throw GUI popups unless redirected to a file with -r.
function Get-VhList {
    & $CG.Vh -t "LIST" -r $CG.VhResult | Out-Null
    Start-Sleep -Milliseconds 400
    (Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '
}

# A VirtualHere address is the server's kernel device number, not a physical
# port: every unbind/rebind - which is what a claim IS - can renumber the Puck.
# A pinned address goes stale silently, so resolve through the stable NAME
# per use.
# '' means the hub is not listing the Puck at all.
function Get-PuckAddress {
    if ((Get-VhList) -match ([regex]::Escape($CG.PuckName) + '\s*\(([^)]+)\)')) { $Matches[1] }
    else { '' }
}

# A lingering DisplayMagician instance produces the frozen profile window on
# the next apply - kill after every apply.
function Stop-DisplayMagician {
    Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force
}

# A failed profile apply explains itself only in DisplayMagician's own log,
# under LOCALAPPDATA, which it rotates away within hours. Copying it beside
# the transcript puts it in the shipper's
# C:/CouchGaming/logs/*.log glob and under Clear-OldLogs retention.
# Level-filtered: a run is ~800 KB of TRACE, ERROR/WARN/INFO ~5%. Swallows
# everything - it runs inside Enter's try block.
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

# Steam delivers input to the FOCUSED window, so a session left holding the
# desktop library window moves nothing on the TV while the Steam button keeps
# working (Steam Input handles that one globally). EXACT title, so it cannot
# hit 'Steam Big Picture Mode' - safe to call at any point. Minimize, not
# unfocus: a window laid out at 4K comes back garbled at the ultrawide's res.
function Hide-DesktopSteam {
    $h = [CG.Win]::WindowByTitle($CG.SteamWindow)
    if ($h -ne [IntPtr]::Zero) {
        [void][CG.Win]::ShowWindow($h, 6)      # 6 = SW_MINIMIZE
        Log 'desktop Steam minimized'
    }
}

# What is actually in front, for the ready event to carry: `focused=True` once
# rode in a ready event while the controller was reaching nothing.
function Get-ForegroundTitle { [CG.Win]::ForegroundTitle() }

# Steam's install path, from the registry. Shared with Launch-Game.
function Get-SteamExe {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath -replace '/', '\'
    $exe = Join-Path $steam 'steam.exe'
    if (-not (Test-Path $exe)) { throw "steam.exe not found at $exe" }
    $exe
}

# Every steamapps library root: SteamPath plus each "path" in libraryfolders.vdf
# (games live on any drive). Dispatch parses the same vdf inline, since it must
# stay dependency-free.
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

# The processes belonging to a game: those whose image path sits under its
# install dir (Steam exposes no appid->pid map). .Path throws for processes
# this unelevated task can't open - guard and skip. Empty if unresolvable.
function Get-GameProcess([int]$AppId) {
    $dir = Get-AppInstallDir $AppId
    if (-not $dir) { return @() }
    # Dir PLUS a trailing separator, so a sibling whose folder name is a string
    # prefix ("Half-Life 2" vs "Half-Life 2 Deathmatch") cannot cross-match.
    $prefix = $dir.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    Get-Process | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) }
        catch { $false }
    }
}

# No Resume-Game here: `-applaunch` on the already-running game just before the
# ready marker puts its 2160p re-init inside the window where the K15 is still
# flipping the TV input - black screen, dead controller, ~40 s. Only the K15
# can observe when the TV actually goes live.

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

# Claim the Puck, verified by Windows enumeration. A stale claim is released
# first: VirtualHere re-acquires the device on reconnect, and Steam holding
# handles to the old instance yields a haptics-alive / inputs-dead controller.
function Request-PuckClaim {
    if (Test-PuckPresent) {
        Log 'stale Puck claim detected - releasing for a fresh instance'
        & $CG.Vh -t "STOP USING,$(Get-PuckAddress)" -r $CG.VhResult
        if (-not (Wait-For { -not (Test-PuckPresent) } 6 'stale claim released')) {
            # Proceeding would pass the claim gate on the stale, dead instance,
            # reproducing the inputs-dead controller this recycle prevents.
            throw 'stale Puck claim would not release - aborting launch'
        }
    }
    $claimed = $false
    for ($i = 1; -not $claimed -and $i -le 2; $i++) {
        # Re-resolved per attempt: the stale release above renumbers the device.
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

# Task guards for the \CouchGaming\ folder. Get-ScheduledTask, not schtasks
# /Query: .State is an enum, while /FO LIST prints a LOCALISED string that a
# non-English install would read as idle - the direction that re-dispatches
# Enter on top of a healthy launch. A missing task reads as not running.
function Test-CgTaskRunning([string]$Name) {
    (Get-ScheduledTask -TaskPath '\CouchGaming\' -TaskName $Name -ErrorAction SilentlyContinue).State -eq 'Running'
}

function Stop-CgTask([string]$Name) { schtasks /End /TN "\CouchGaming\$Name" | Out-Null }

# Transcript retention; called from Office-Safety (logon) and Wake-Safety (wake).
function Clear-OldLogs([int]$Days = 30, [int]$ArchiveAfterDays = 2) {
    # Transcripts and daily jsonl move to archive\ after $ArchiveAfterDays and
    # are deleted at $Days. The move is for the SHIPPER: it tails every file
    # its glob matches at ~0.04% of a core each (110 finished files was ~4.5%),
    # and only a running transcript or today's jsonl can still grow.
    $archive = Join-Path $CG.LogDir 'archive'
    New-Item -ItemType Directory -Force -Path $archive -ErrorAction SilentlyContinue | Out-Null
    # Wildcard path + -Include and NO -Recurse: this directory's files only, so
    # archive\ is never re-scanned. (-Filter takes one pattern, hence -Include.)
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

# The cross-machine session API: Enter writes it last, the K15 switches the TV
# input only after seeing it, Exit/safeties remove it. Content is the launch's
# turn id, which the K15 must see echoed before it trusts a READY, so a stale
# marker cannot satisfy a new launch's poll. No turn falls back to a timestamp,
# accepted as a legacy READY, so either side may deploy first.
function Set-ReadyMarker {
    New-Item -ItemType Directory -Force (Split-Path $CG.ReadyMarker) | Out-Null
    $stamp = if ($script:CgTurn) { $script:CgTurn } else { (Get-Date).ToString('o') }
    Set-Content $CG.ReadyMarker $stamp
}
function Clear-ReadyMarker { Remove-Item $CG.ReadyMarker -ErrorAction SilentlyContinue }
function Test-ReadyMarker  { Test-Path $CG.ReadyMarker }

# One-shot payload from Dispatch to a task (LaunchMarker/NavMarker/StopMarker):
# the first line, trimmed, or $null when absent; the caller validates.
# Stringify before trimming: Get-Content on an empty file returns $null in
# PS 5.1 and .Trim() would throw before the delete, stranding the marker. The
# delete is best-effort: the marker is owned by the ELEVATED sshd forced-command
# context (BUILTIN\Administrators; Users read-only) and the tasks run limited,
# so it is DENIED - fatal under ErrorActionPreference=Stop. Dispatch clears it
# before every write, so a survivor is overwritten; only a MANUAL task run
# replays the last value.
function Read-CgMarker([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    $raw = Get-Content $Path -TotalCount 1
    try { Remove-Item $Path -Force } catch {
        Log 'marker not deletable from this token - Dispatch overwrites it next time'
    }
    "$raw".Trim()
}
