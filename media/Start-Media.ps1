$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvironmentFile = Join-Path $Here '.env'
if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
    Copy-Item -LiteralPath (Join-Path $Here '.env.example') -Destination $EnvironmentFile
    Write-Host "Created $EnvironmentFile with C:\Media defaults."
}

$Settings = @{}
Get-Content -LiteralPath $EnvironmentFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        $Settings[$Matches[1].Trim()] = $Matches[2].Trim()
    }
}
if (-not $Settings.SECRET_ENCRYPTION_KEY) {
    $Bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
    $Key = -join ($Bytes | ForEach-Object { $_.ToString('x2') })
    # WriteAllLines: BOM-free UTF-8. PowerShell 5.1 redirection writes UTF-16,
    # which Compose cannot read as an env file.
    $Lines = @(Get-Content -LiteralPath $EnvironmentFile) |
        Where-Object { $_ -notmatch '^\s*SECRET_ENCRYPTION_KEY=\s*$' }
    [IO.File]::WriteAllLines($EnvironmentFile,
        ([string[]]($Lines + "SECRET_ENCRYPTION_KEY=$Key")))
    $Settings.SECRET_ENCRYPTION_KEY = $Key
    Write-Host "Generated SECRET_ENCRYPTION_KEY in $EnvironmentFile."
}

foreach ($Directory in @(
    $Settings.MEDIA_CONFIG_ROOT,
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'prowlarr'),
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'radarr'),
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'sonarr'),
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'homarr'),
    $Settings.MEDIA_ROOT,
    (Join-Path $Settings.MEDIA_ROOT 'torrents'),
    (Join-Path $Settings.MEDIA_ROOT 'Movies'),
    (Join-Path $Settings.MEDIA_ROOT 'TV')
)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}

docker compose --project-directory $Here --env-file $EnvironmentFile up -d --remove-orphans
# -File exits with the LAST native command's code, and the ps/Write-Host below
# are what run last: without this a failed `up` returns 0 to CD.
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed ($LASTEXITCODE)." }
docker compose --project-directory $Here --env-file $EnvironmentFile ps

Write-Host ''
Write-Host 'Local setup pages: Prowlarr http://127.0.0.1:9696, Radarr http://127.0.0.1:7878, Sonarr http://127.0.0.1:8989'
Write-Host "Homarr dashboard: http://127.0.0.1:8575 locally, http://${env:COMPUTERNAME}:8575 from the LAN (needs the firewall rule in the runbook)."
Write-Host 'FlareSolverr is internal-only at http://flaresolverr:8191 for Prowlarr indexer proxies.'
Write-Host 'qBittorrent runs natively through Proton VPN; see README.md before configuring the Arr download clients.'
