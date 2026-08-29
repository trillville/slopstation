# K15 media sidecars

`Start-Media.ps1` creates a local `.env` from the C-drive defaults and starts
Prowlarr, Radarr, Sonarr, and qBittorrent. Docker Desktop with Linux containers
must already be running.

The web interfaces bind to localhost. Configure them from the K15 or through
an SSH tunnel; do not expose their management ports to the internet.

Use these paths and service addresses during one-time setup:

- qBittorrent download path: `/data/torrents`
- Radarr root: `/data/media/movies`
- Sonarr root: `/data/media/tv`
- Radarr/Sonarr qBittorrent host: `qbittorrent`, port `8080`
- Radarr category: `radarr`
- Sonarr category: `sonarr`
- Prowlarr Radarr URL: `http://radarr:7878`
- Prowlarr Sonarr URL: `http://sonarr:8989`

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

Changing to the NAS later requires stopping the stack, copying `MEDIA_ROOT`,
editing `.env`, and starting it again. Container paths remain `/data`, so no
Radarr, Sonarr, qBittorrent, or Slopstation records need rewriting.
