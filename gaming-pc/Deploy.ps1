# Ship the gaming-pc script set from this checkout to the runtime folder
# (default C:\CouchGaming) as one checked set, and stamp `build-id` so the K15
# can ask what is running (`ssh gamepc version`, compared by doctor.py).
# test_turn.py drills the REPO's Dispatch.ps1, so a hand-copied deployed copy
# would drift undetected.
#
# Run from a checkout, on the PC:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Deploy.ps1
#
# Does not copy itself. The gitignored runtime pieces (vhui64.exe, OFFICE.lnk,
# TV-GAMING.lnk) are warned about, never touched.
param([string]$Dest = 'C:\CouchGaming')

$scripts = @(
    'CouchGaming.common.ps1', 'Dispatch.ps1', 'Doctor.ps1',
    'Enter-TV.ps1', 'Exit-TV.ps1', 'Launch-Game.ps1',
    'Nav-BigPicture.ps1', 'Stop-Game.ps1',
    'Office-Safety.ps1', 'Wake-Safety.ps1'
)

# Refuse a partial set - half a contract is skew.
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

# Short rev, '-dirty' when the tree has uncommitted edits. No git degrades to a
# dated 'nogit' stamp, never a failure.
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

# Runtime pieces that cannot come from the repo - warn, never touch.
foreach ($f in 'vhui64.exe', 'OFFICE.lnk', 'TV-GAMING.lnk') {
    if (-not (Test-Path (Join-Path $Dest $f))) {
        Write-Host "WARNING: $Dest\$f is missing - install it on this machine (VirtualHere client / DisplayMagician shortcuts)"
    }
}
Write-Host "deployed $($scripts.Count) scripts to $Dest"
