# Media acquisition on the K15

Slopstation sends movie requests to Radarr and series requests to Sonarr.
Prowlarr supplies indexers, qBittorrent downloads and seeds, and Slopstation
reports progress. Proton VPN carries qBittorrent's peer traffic.

Prowlarr, FlareSolverr, Radarr, Sonarr, Homarr, and Glances run in Docker.
qBittorrent and Proton run directly on Windows.

| Component | Purpose |
|---|---|
| Slopstation | Resolve titles, select profiles, and report progress |
| Prowlarr | Configure indexers and seeding limits |
| Radarr / Sonarr | Select releases, import files, and clean downloads |
| qBittorrent | Download, seed, and stop torrents |
| Proton VPN | Route peer traffic and provide a forwarded port |
| FlareSolverr | Handle browser challenges for tagged indexers |
| Homarr / Glances | Show service and disk status on the LAN |

Quality profiles in Radarr and Sonarr decide which releases are acceptable.
Slopstation only selects a configured profile.

## Paths and addresses

`media\.env` defines the Windows paths. `.env.example` uses `C:\Media` for
`MEDIA_ROOT`. Radarr and Sonarr always see that directory as `/data`.

| Purpose | Path or URL |
|---|---|
| Downloads | `<MEDIA_ROOT>\torrents` |
| Movies | `/data/Movies` → `<MEDIA_ROOT>\Movies` |
| TV | `/data/TV` → `<MEDIA_ROOT>\TV` |
| Prowlarr | `http://192.168.1.10:9696` |
| Radarr | `http://192.168.1.10:7878` |
| Sonarr | `http://192.168.1.10:8989` |
| qBittorrent | `http://192.168.1.10:8080` |
| Homarr | `http://192.168.1.10:8575` |
| FlareSolverr, internal only | `http://flaresolverr:8191` |
| Glances, internal only | `http://glances:61208` |

The LAN web interfaces require authentication and Private-profile firewall
rules limited to `LocalSubnet`. Do not expose them to the internet.

## Setup

### 1. Configure and start Docker services

Install Docker Desktop with Linux containers. Copy `media\.env.example` to
`media\.env`, set `MEDIA_ROOT` and `MEDIA_CONFIG_ROOT`, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\media\Start-Media.ps1
```

The script creates missing directories and starts the Compose services.
Complete the first-run login for each web interface.

### 2. Configure Proton and qBittorrent

Run qBittorrent as a native Windows application.

- In Proton, use WireGuard, a P2P server, port forwarding, LAN access, and an
  include-only split tunnel for `qbittorrent.exe`.
- Bind qBittorrent's peer interface to Proton. Disable UPnP and NAT-PMP.
- Enable the authenticated Web UI on port `8080` without authentication
  bypasses.
- Set the save path to `<MEDIA_ROOT>\torrents`.
- Create the `radarr` and `sonarr` categories.
- Set `media.qbittorrentNetworkInterface` in `config.json` to qBittorrent's
  exact interface name.

### 3. Configure Prowlarr

Add authorized indexers, then add Radarr and Sonarr with **Full Sync**:

| Application | URL |
|---|---|
| Radarr | `http://radarr:7878` |
| Sonarr | `http://sonarr:8989` |

Use each application's API key. Enable RSS, automatic search, and interactive
search in the sync profile.

For an indexer that needs FlareSolverr, create a proxy for
`http://flaresolverr:8191` and give the proxy and indexer the same tag.
FlareSolverr cannot solve interactive CAPTCHAs.

### 4. Configure Radarr and Sonarr

Enable Completed Download Handling. Add qBittorrent at
`host.docker.internal:8080` with its Web UI credentials.

| Application | Root | Category |
|---|---|---|
| Radarr | `/data/Movies` | `radarr` |
| Sonarr | `/data/TV` | `sonarr` |

Add this remote path mapping to both applications:

| Host | Remote path | Local path |
|---|---|---|
| `host.docker.internal` | `<MEDIA_ROOT>\torrents` | `/data/torrents` |

Test each root, download client, and path mapping.

### 5. Create quality profiles

Create every profile named by `moviePresets` and `seriesPresets` in
`config.json`. The example configuration expects:

| Preset | Movie profile | Series profile |
|---|---|---|
| `default` | `Slopstation Blu-ray HDR TrueHD` | `Slopstation Series` |
| `1080p` | `Slopstation 1080p` | `Slopstation Series 1080p` |
| `2160p` | `Slopstation Blu-ray HDR TrueHD` | `Slopstation Series 2160p` |

Set allowed qualities, upgrades, cutoffs, and custom-format scores in those
profiles.

### 6. Enable Slopstation

Add these values to `secrets.json`:

```json
{
  "radarrApiKey": "...",
  "sonarrApiKey": "...",
  "prowlarrApiKey": "...",
  "qbittorrentPassword": "..."
}
```

Copy the `media` section from `config.example.json` into `config.json`. Set its
URLs, roots, profile mappings, qBittorrent username and interface, and managed
indexers, then set `enabled` to `true`.

Validate the installation:

```powershell
.venv\Scripts\python -m slopstation.agent.tools.media doctor
```

Configure Docker Desktop, Proton, and qBittorrent to start at login. Compose
services already use `restart: unless-stopped`. K15 deployments rerun
`Start-Media.ps1` while media is enabled.

### 7. Optional Homarr dashboard

Allow the web interfaces from an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName 'Homarr dashboard (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8575 -Profile Private -RemoteAddress LocalSubnet
New-NetFirewallRule -DisplayName 'Media web UIs (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9696,7878,8989 -Profile Private -RemoteAddress LocalSubnet
New-NetFirewallRule -DisplayName 'qBittorrent Web UI (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Private -RemoteAddress LocalSubnet
```

Complete Homarr setup at `http://127.0.0.1:8575`. Configure Radarr, Sonarr,
Prowlarr, and qBittorrent with their K15 LAN URLs from the table above; use
`http://glances:61208` for Glances. Use a direct public board URL for
anonymous viewing: `http://192.168.1.10:8575/boards/<name>`.

## Requests and cleanup

Movie and series requests move through `searching`, `waiting_for_match`,
`downloading`, `importing`, and `ready`. Slopstation checks every 30 seconds.
If a service is unavailable, the operation remains pending until checks
resume. Sonarr continues watching future episodes after the original operation
completes.

The configured public-indexer policy seeds to ratio `0.25` or 60 minutes,
whichever comes first:

1. Prowlarr sends the limits to Radarr and Sonarr.
2. qBittorrent stops the torrent at the limit but does not delete it.
3. Radarr or Sonarr removes the stopped torrent and download after import when
   **Remove Completed Downloads** is enabled.

Keep the `radarr` and `sonarr` categories after import. Test cleanup with a
small movie and episode, and confirm the imported files remain.

## Proton forwarded port

Proton writes its active port to:

```text
%LOCALAPPDATA%\Proton\Proton VPN\Logs\client-logs.txt
```

Check and apply the port once:

```powershell
.venv\Scripts\python -m slopstation.agent.tools.media proton-port
.venv\Scripts\python -m slopstation.agent.tools.media sync-proton-port --execute
```

Confirm that Proton and qBittorrent show the same port. Then set
`media.protonPortSync` to `true` and restart Slopstation. For manual recovery:

```powershell
.venv\Scripts\python -m slopstation.agent.tools.media set-qbit-port <active-port> --execute
```

## Monitoring

Start diagnosis with:

```powershell
.venv\Scripts\python -m slopstation.agent.tools.media doctor
.venv\Scripts\python -m slopstation.agent.tools.operations list --active
docker compose --project-directory media --env-file media\.env ps
```

| Event | Meaning |
|---|---|
| `media_health_issue` / `media_health_cleared` | Radarr or Sonarr health changed |
| `media_import_failed` | An import failed |
| `media_queue_stalled` | A queued download reported a warning or error |
| `media_watch_failed` | A service could not be reached |
| `disk_space_low` / `disk_space_cleared` | A watched volume crossed the configured limit |
| `disk_watch_failed` | A watched volume could not be read |
| `smart_warning` | smartd reported a drive problem |

Configure media health checks with `media.healthSync` and
`media.healthPollS`. Configure disk checks with `media.diskWatch`,
`media.diskPollS`, and `media.diskFreeWarnGb`.

### SMART monitoring

Install smartmontools, edit the device and command paths in
`smartd.conf.example` and `smart-alert.bat`, then run as Administrator:

```powershell
Copy-Item smartd.conf.example "$env:ProgramFiles\smartmontools\bin\smartd.conf"
& "$env:ProgramFiles\smartmontools\bin\smartd.exe" install
Start-Service smartd
```

Confirm the physical device with `smartctl --scan`. USB enclosures may require
SAT translation (`-d sat`). Add `-M test`, restart the service, and confirm one
`smart_warning` before removing the test flag.

If the service stops immediately, run a foreground check:

```powershell
& "$env:ProgramFiles\smartmontools\bin\smartd.exe" `
    -c "$env:ProgramFiles\smartmontools\bin\smartd.conf" -q onecheck -d
```

## Pin container images

The Compose file uses `:latest`. To replace those tags with the exact running
images:

```powershell
'flaresolverr', 'prowlarr', 'radarr', 'sonarr', 'homarr', 'glances' | ForEach-Object {
    $container = docker compose --project-directory media --env-file media\.env ps -q $_
    $image = docker inspect --format '{{.Image}}' $container
    docker image inspect --format '{{index .RepoDigests 0}}' $image
}
```

Copy each `repository@sha256:...` value into that service's `image` field.

## Backup and restore

Service databases live under `MEDIA_CONFIG_ROOT`; credentials and paths are
not stored in this repository. Stop the stack before copying them:

```powershell
$cfg = @{}; Get-Content media\.env | ForEach-Object { if ($_ -match '^\s*([^#=]+)=(.*)$') { $cfg[$Matches[1].Trim()] = $Matches[2].Trim() } }
docker compose --project-directory media --env-file media\.env stop
Copy-Item -Recurse $cfg.MEDIA_CONFIG_ROOT "$($cfg.MEDIA_CONFIG_ROOT)-backup"
.\media\Start-Media.ps1
```

Back up `media\.env` with the config directory because it contains Homarr's
encryption key. Also back up `%APPDATA%\qBittorrent` and
`%LOCALAPPDATA%\qBittorrent`.

To restore, stop the stack, replace `MEDIA_CONFIG_ROOT` with the backup, and
run `Start-Media.ps1`.

## Move the media root

1. Let downloads finish, stop the Compose stack, and exit qBittorrent.
2. Copy the complete media root and keep the old copy until validation passes.
3. Update `MEDIA_ROOT` in `media\.env`, qBittorrent's save path, the Radarr and
   Sonarr remote path mappings, and any SMB share path.
4. Reapply NTFS permissions if the copy did not preserve ACLs.
5. Start the stack and confirm each application reports the new volume's free
   space.
6. Complete one download, import, and cleanup before deleting the old root.

Keep `/data/Movies`, `/data/TV`, and `/data/torrents` unchanged. Those are
container paths and do not depend on the Windows media root.
