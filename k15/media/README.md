# K15 media sidecars

`Start-Media.ps1` creates a local `.env` from the C-drive defaults and starts
FlareSolverr, Prowlarr, Radarr, and Sonarr. Docker Desktop with Linux containers
must already be running. qBittorrent runs as the native Windows application so
Proton VPN's include-only split tunnel can target `qbittorrent.exe`; it must not
run in this Compose project.

The web interfaces bind to localhost. Configure them from the K15 or through
an SSH tunnel; do not expose their management ports to the internet.

Use these paths and service addresses during one-time setup:

- Native qBittorrent download path: `C:\Media\torrents`
- Radarr root: `/data/Movies` (`C:\Media\Movies` on the host)
- Sonarr root: `/data/TV` (`C:\Media\TV` on the host)
- Radarr/Sonarr qBittorrent host: `host.docker.internal`, port `8080`
- Radarr category: `radarr`
- Sonarr category: `sonarr`
- Radarr/Sonarr remote path mapping: host `host.docker.internal`, remote
  `C:\Media\torrents`, local `/data/torrents`
- Prowlarr Radarr URL: `http://radarr:7878`
- Prowlarr Sonarr URL: `http://sonarr:8989`
- Prowlarr FlareSolverr URL: `http://flaresolverr:8191`

FlareSolverr has no published host port. Its unauthenticated browser API is
reachable only by services on this Compose network. To use it for a protected
indexer:

1. In Prowlarr, open **Settings > Indexers > Indexer Proxies**, add
   **FlareSolverr**, and set its host to `http://flaresolverr:8191`.
2. Give the proxy a tag such as `flaresolverr`, then test and save it.
3. Add the same `flaresolverr` tag to each indexer that needs the proxy, then
   test that indexer again. Leave unprotected indexers untagged.

If the test still fails, inspect `docker compose logs flaresolverr`; it handles
browser challenges but does not solve interactive CAPTCHAs.

The native qBittorrent Web UI must listen on all addresses at port `8080`, use
authentication, and must not bypass authentication for localhost or allowlisted
subnets. Its peer connection remains bound to the `ProtonVPN` interface with
UPnP/NAT-PMP disabled and Proton's current forwarded port. Verify its torrent
address is the Proton exit IP before submitting media.

Create the quality profiles named in `config.json` before enabling the media
lane. The profile owns quality and custom-format policy; Slopstation only
selects its name. Copy the generated Radarr and Sonarr API keys into
`secrets.json`, set `media.enabled` true, then run:

    .venv\Scripts\python media.py status
    .venv\Scripts\python media.py profiles
    .venv\Scripts\python media.py validate

`validate` exits nonzero until both configured root folders and every preset's
exact quality-profile name exist. It does not inspect or change releases,
indexers, or downloads.

Changing to the NAS later requires stopping the stack and native qBittorrent,
copying `MEDIA_ROOT`, editing `.env`, changing qBittorrent's download path and
the Arr remote path mappings, and starting them again. Radarr, Sonarr, and
Slopstation keep their `/data` paths.
