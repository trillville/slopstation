# Media acquisition

Slopstation accepts a movie or series request, resolves the intended title,
submits it to an external media authority, and tracks the result as a durable
operation. It does not search torrent sites, rank releases, handle magnet
links, or move media files itself.

## Service boundary

The always-on K15 hosts four sidecars:

- Prowlarr owns indexer definitions and synchronizes them into Radarr and
  Sonarr.
- Radarr owns movie search, release selection, upgrades, import, and the final
  movie library.
- Sonarr owns the same lifecycle for series and episodes.
- qBittorrent owns transfer state. Radarr and Sonarr are its clients.

Slopstation talks only to the Radarr and Sonarr v3 APIs during normal use. The
K15 ledger stores their numeric movie or series id, never a release name,
tracker response, download URL, or magnet link. External strings are limited
to structured catalog metadata returned during an explicit lookup; indexer and
release text never enters assistant history.

The repository supplies a Docker Compose deployment because all four services
then share one stable `/data` namespace. The first host mapping is `C:\Media`;
moving to the NAS changes `MEDIA_ROOT` in the sidecar `.env`, not Slopstation's
API payloads or operation records.

## Request interface

Title resolution and mutation are separate tools:

1. `find_media(kind, query)` returns at most five movie or series candidates
   with canonical title, year, and TMDB or TVDB id.
2. `request_movie(tmdb_id, preset)` submits one resolved movie.
3. `request_series(tvdb_id, preset, seasons)` submits one resolved series.

The assistant must ask a short clarifying question when the lookup does not
produce one unambiguous candidate. A title string alone never crosses the
mutation boundary. Omitting `seasons` means all normal seasons; an explicit
list requests only those season numbers. Specials are not included by the
implicit all-seasons request.

The user-facing presets are `default`, `1080p`, and `2160p`. Configuration maps
each preset to an exact Radarr or Sonarr quality-profile name. Release policy,
including Blu-ray, HDR, TrueHD, custom-format scores, cutoffs, and upgrades,
belongs in those profiles rather than in assistant prose or Python matching
rules. The initial movie default is `Slopstation Blu-ray HDR TrueHD`; a request
such as “1080p is fine for this one” selects the configured `1080p` profile for
that movie only.

If a title already exists, Slopstation reuses its Radarr or Sonarr id. A movie
file already assigned to the requested profile returns immediately. Selecting
a different movie profile updates Radarr, starts an upgrade search, and keeps
the durable operation active until Radarr replaces the prior file. An
existing series profile change does the same per requested aired episode.
An incomplete item gets the requested profile, a new search, and an operation.

## Operation lifecycle

Media adds two concrete operation kinds to the existing ledger:

- `movie_acquisition`, authoritative in Radarr.
- `series_acquisition`, authoritative in Sonarr.

The external reference is the Radarr movie id or Sonarr series id. Structured
metadata records the requested preset, profile name, source catalog id, and
explicit season list when present. An active operation with the same kind and
external reference is reused.

Radarr positively completes a movie operation only when `hasFile` is true.
Sonarr positively completes a series operation when every requested,
monitored, already-aired normal episode has `hasFile` true. Future episodes
remain monitored by Sonarr but do not hold the initial request open. An
explicit future season with no aired episodes remains running until one airs
and the requested aired set is present.

Missing services, HTTP errors, deleted authority records, and malformed
responses produce `UNKNOWN`, never `FAILED`. `UNKNOWN`, normal progress, and
recovery are silent. Only the first positive terminal edge queues the existing
announcement path. Radarr and Sonarr have no safe “undo this request”
equivalent, so cancellation remains explicitly unsupported.

## Configuration and deployment

`config.json` contains non-secret topology and policy under `media`. The
Radarr and Sonarr API keys live in `secrets.json`. Existing installations stay
disabled until `media.enabled` is set true, so a pull cannot make the voice
agent depend on services that have not been provisioned.

The Compose stack binds its web interfaces to K15 localhost. Radarr and Sonarr
use `/data/media/movies` and `/data/media/tv`; qBittorrent uses
`/data/torrents`. Keeping one `/data` mount gives every container identical
paths and preserves hard-link/atomic-import behavior.

The one-time live setup that cannot be committed is:

1. Install Docker Desktop on the K15 and start the supplied stack.
2. Set qBittorrent credentials, then add it as the Radarr and Sonarr download
   client using the Compose service name `qbittorrent`.
3. Add Radarr and Sonarr as Prowlarr applications, then configure the chosen
   indexers in Prowlarr.
4. Create or import the named quality profiles and custom formats in Radarr and
   Sonarr.
5. Copy the generated Radarr and Sonarr API keys into `secrets.json`, enable
   media in `config.json`, deploy, and run the checkout-safe and live checks.

## Acceptance

Checkout-safe tests cover API authentication and payloads, preset lookup,
separate lookup and mutation, existing-item reuse, selected seasons, durable
deduplication, restart reconciliation, aired-episode progress, positive-only
completion, `UNKNOWN` silence, assistant tool gating, imports, lint, typing,
and the frozen event vocabulary.

Live validation is intentionally last: provision one indexer, request a small
movie through the diagnostic CLI, verify qBittorrent transfer and Radarr
import, repeat by voice with a `1080p` override, request a short or selected
series season, restart the voice agent during transfer, and confirm exactly one
completion announcement for each request.

The media CLI is a provisioning and diagnostic surface, not the general chat
client. A production text interface for the K15 and gaming PC should be a
separate slice that reuses the same assistant tools and operation ledger for
all Slopstation capabilities; it should not be coupled to media acquisition.
