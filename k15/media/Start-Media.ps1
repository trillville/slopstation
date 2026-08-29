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
foreach ($Directory in @(
    $Settings.MEDIA_CONFIG_ROOT,
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'prowlarr'),
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'radarr'),
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'sonarr'),
    $Settings.MEDIA_ROOT,
    (Join-Path $Settings.MEDIA_ROOT 'torrents'),
    (Join-Path $Settings.MEDIA_ROOT 'Movies'),
    (Join-Path $Settings.MEDIA_ROOT 'TV')
)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}

docker compose --project-directory $Here --env-file $EnvironmentFile up -d --remove-orphans
docker compose --project-directory $Here --env-file $EnvironmentFile ps

Write-Host ''
Write-Host 'Local setup pages: Prowlarr http://127.0.0.1:9696, Radarr http://127.0.0.1:7878, Sonarr http://127.0.0.1:8989'
Write-Host 'FlareSolverr is internal-only at http://flaresolverr:8191 for Prowlarr indexer proxies.'
Write-Host 'qBittorrent runs natively through Proton VPN; see README.md before configuring the Arr download clients.'
