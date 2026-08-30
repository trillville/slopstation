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

For unattended voice and text requests, verify this operating state once:

- Docker Desktop starts at login. The four Compose services use
  `restart: unless-stopped`, so they return automatically with Docker.
- Native qBittorrent and Proton VPN start at login. Proton connects to a P2P
  server before qBittorrent transfers; after a Proton reconnect, its current
  forwarded port still matches qBittorrent's listening port.
- In both Radarr and Sonarr, qBittorrent is enabled and its **Test** succeeds.
  **Completed Download Handling** is enabled. Follow the staged cleanup rollout
  below before enabling automatic removal.
- In Prowlarr, both applications test successfully and use **Full Sync**.
  Manage synchronized indexers in Prowlarr, not separately in Radarr or
  Sonarr. Each indexer's RSS, Automatic Search, and Interactive Search modes
  are enabled, and its categories include movies for Radarr or TV for Sonarr.
- Radarr and Sonarr show no download-client, root-folder, or indexer warning
  under **System > Status**. Their quality profiles retain upgrades, cutoffs,
  custom-format scores, and minimum scores; Slopstation selects those profiles
  but never repairs their policy.

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

## Seeding and completed-download cleanup

Use one initial policy for both public indexers: ratio `0.25`, seed-time limit
`60` minutes (one hour), and stop when either limit is reached. Prowlarr owns
the per-indexer limits; qBittorrent owns the action; Radarr and Sonarr own
removal after import and seeding.

1. In Prowlarr, show advanced settings and edit **1337x**. Set **Seed Ratio**
   to `0.25` and **Seed Time** to `60`, then test and save. Repeat for
   **EZTV**. Keep both application connections on **Full Sync** so these values
   reach Radarr and Sonarr.
2. In native qBittorrent, open **Tools > Options > BitTorrent > Seeding
   Limits**. Check ratio `0.25` and total seeding time `60 min`; leave inactive
   seeding time unchecked. Set the action to **Stop torrent**. qBittorrent
   evaluates the enabled limits independently. Newer Web API versions call this
   `MatchAny`; older versions omit the mode field, and the Windows UI has no
   separate selector. Do not select a remove or delete action. The
   indexer-specific limits take precedence, so these global values are a
   matching fallback.
3. Keep the `radarr` and `sonarr` categories configured in their download
   clients. Do not use a post-import category change; Arr needs to continue
   recognizing its torrent while it seeds.
4. In Radarr and Sonarr, keep global **Completed Download Handling** enabled.
   Edit the qBittorrent download-client entry, show advanced settings, and
   leave **Remove Completed Downloads** disabled for the first test.
5. Request a small movie. Confirm it imports into `C:\Media\Movies`, plays from
   that library path, and remains in qBittorrent after import. Let it reach
   either limit and confirm qBittorrent stops it without deleting content.
6. Enable **Remove Completed Downloads** on Radarr's qBittorrent client.
   Confirm Radarr removes the stopped torrent and its download payload while
   the imported movie remains.
7. Repeat the same staged test with a small episode or season in Sonarr, then
   enable **Remove Completed Downloads** on Sonarr's qBittorrent client.

Do not enable automatic removal in an Arr application until its controlled
test reaches the final step. qBittorrent must never delete the content itself;
that can race the importing authority and lose the library copy.

## Proton forwarded port

The official Proton Windows client emits its current port-forwarding state to:

    %LOCALAPPDATA%\Proton\Proton VPN\Logs\client-logs.txt

Slopstation can read that local log and keep native qBittorrent synchronized.
The integration accepts only an active mapping observed within the last 45
seconds. Stopped, transitional, stale, missing, or unrecognized state never
changes qBittorrent.

Validate the read-only source first while Proton is connected to a P2P server
with port forwarding enabled:

    cd C:\Users\minipc\Desktop\slopstation\k15\voice
    .venv\Scripts\python media.py proton-port

The result must show `"state": "active"` and the same port shown by Proton.
Next perform one explicit synchronization:

    .venv\Scripts\python media.py sync-proton-port --execute

Confirm `changed` is true when the ports initially differ, and that
`listen_port` equals `port`. Then add this setting to the existing `media`
object in `k15\config.json`:

    "protonPortSync": true

Restart with `Start-K15.bat`. The voice supervisor checks immediately and then
at `media.pollS`; successful changes are logged as `proton_port_synced`. Proton
log-format changes fail closed and emit `proton_port_sync_failed` without
changing qBittorrent.

The manual fallback remains available:

    cd C:\Users\minipc\Desktop\slopstation\k15\voice
    .venv\Scripts\python media.py set-qbit-port 33125 --execute

Replace `33125` with the current Active port. The command validates the port,
authenticates to the native qBittorrent Web API, changes only its listening
port, and reads the value back. Recheck that qBittorrent still shows
`ProtonVPN`, **All addresses**, and UPnP/NAT-PMP disabled. A torrent-address
test remains the live proof that peer traffic exits through Proton.

The maintenance commands need two additional local secrets and three policy
values. Add the secrets to `k15\secrets.json`:

    "prowlarrApiKey": "your-prowlarr-api-key",
    "qbittorrentPassword": "your-native-web-ui-password"

Copy these keys from `config.example.json` into the existing `media` object in
`k15\config.json`:

    "qbittorrentUsername": "admin",
    "qbittorrentNetworkInterface": "ProtonVPN",
    "protonPortSync": false,
    "managedIndexers": ["1337x", "EZTV"],
    "seedRatio": 0.25,
    "seedTimeMinutes": 60

These credentials are not required by ordinary movie or series acquisition.
They are used by maintenance diagnostics, explicit port commands, and the
optional background synchronizer.

## Comprehensive health check

Run the read-only check after setup or after changing media infrastructure:

    cd C:\Users\minipc\Desktop\slopstation\k15\voice
    .venv\Scripts\python media.py doctor

It checks Compose state, all service APIs, Arr health, roots, profiles,
indexers, download clients, completed-download removal, Prowlarr Full Sync and
seed values, qBittorrent categories, interface binding, UPnP/NAT-PMP, share
limit behavior, Web UI authentication, and the listening port. When automatic
port synchronization is enabled, it also compares qBittorrent with Proton's
fresh live state. Any `FAIL` makes the command exit nonzero; `WARN` does not.

The doctor never adds a torrent or mutates a service. It cannot prove the
torrent-visible address; repeat a torrent-address test after VPN or network
changes.

Changing to the NAS later requires stopping the stack and native qBittorrent,
copying `MEDIA_ROOT`, editing `.env`, changing qBittorrent's download path and
the Arr remote path mappings, and starting them again. Radarr, Sonarr, and
Slopstation keep their `/data` paths.
