# Deploy.ps1 - ship the gaming-pc script set from this checkout to the runtime
# folder (default C:\CouchGaming) as ONE CHECKED SET, and stamp `build-id` so
# the K15 can ask what is actually running (`ssh gamepc version`, compared by
# doctor.py). Hand-copying made skew undetectable: test_turn.py drills the
# REPO's Dispatch.ps1, so a drifted deployed copy passed every test.
#
# Run from a checkout, on the PC:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Deploy.ps1
#
# The gitignored runtime pieces (vhui64.exe, OFFICE.lnk, TV-GAMING.lnk) are
# warned about, never touched. It does not copy itself: it runs from the
# checkout, where git can vouch for what it ships.
param([string]$Dest = 'C:\CouchGaming')

$scripts = @(
    'CouchGaming.common.ps1', 'Dispatch.ps1', 'Doctor.ps1',
    'Enter-TV.ps1', 'Exit-TV.ps1', 'Launch-Game.ps1',
    'Office-Safety.ps1', 'Wake-Safety.ps1'
)

# Refuse a partial set: shipping half a contract is exactly the skew this
# script exists to end.
$missing = $scripts | Where-Object { -not (Test-Path (Join-Path $PSScriptRoot $_)) }
if ($missing) {
    Write-Host "ABORT: this checkout is missing $($missing -join ', ') - nothing copied"
    exit 1
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($f in $scripts) {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $Dest $f) -Force
    Write-Host "  $f"
}

# The checkout's short rev, '-dirty' when the tree has uncommitted edits (a
# rev cannot vouch for those - doctor warns). No git degrades to a dated
# 'nogit' stamp, never a failure.
$rev = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $out = git -C $PSScriptRoot rev-parse --short HEAD
    if ($LASTEXITCODE -eq 0 -and $out) {
        $rev = "$out".Trim()
        if (git -C $PSScriptRoot status --porcelain) { $rev += '-dirty' }
    }
}
$stamp = if ($rev) { "$rev $(Get-Date -Format yyyy-MM-dd)" }
         else      { "nogit $(Get-Date -Format yyyy-MM-ddTHH:mm)" }
Set-Content (Join-Path $Dest 'build-id') $stamp
Write-Host "build-id: $stamp"

# The runtime pieces that CANNOT come from the repo - warn, never touch.
foreach ($f in 'vhui64.exe', 'OFFICE.lnk', 'TV-GAMING.lnk') {
    if (-not (Test-Path (Join-Path $Dest $f))) {
        Write-Host "WARNING: $Dest\$f is missing - guide Stage 5/6 (VirtualHere client, DisplayMagician shortcuts)"
    }
}
Write-Host "deployed $($scripts.Count) scripts to $Dest"
