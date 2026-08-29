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
    (Join-Path $Settings.MEDIA_CONFIG_ROOT 'qbittorrent'),
    $Settings.MEDIA_ROOT,
    (Join-Path $Settings.MEDIA_ROOT 'torrents'),
    (Join-Path $Settings.MEDIA_ROOT 'Movies'),
    (Join-Path $Settings.MEDIA_ROOT 'TV')
)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}

docker compose --project-directory $Here --env-file $EnvironmentFile up -d
docker compose --project-directory $Here --env-file $EnvironmentFile ps

Write-Host ''
Write-Host 'Local setup pages: Prowlarr http://127.0.0.1:9696, Radarr http://127.0.0.1:7878, Sonarr http://127.0.0.1:8989, qBittorrent http://127.0.0.1:8080'
Write-Host 'For qBittorrent first-login credentials, run:'
Write-Host "  docker compose --project-directory `"$Here`" --env-file `"$EnvironmentFile`" logs qbittorrent"
