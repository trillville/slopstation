# Durable operations

Slopstation tracks long-running work that another system executes. It does not
run that work itself. Steam owns game installation; Radarr and Sonarr will own
media acquisition. The K15 keeps only enough state to correlate a request,
observe its authority after a restart, answer status questions, and announce a
terminal result.

Steam installation was the first implementation. Media acquisition is the
second and extends the record only with structured request metadata. See
`docs/media-acquisition.md` for its service and completion contracts.

## Record

`state/operations.json` contains records with this shape:

- `id`: Slopstation identifier used by logs and the diagnostic CLI.
- `turn`: originating voice turn when one exists.
- `kind`: `steam_install`, `movie_acquisition`, or `series_acquisition`.
- `authority`: system whose observation decides the state: Steam, Radarr, or
  Sonarr.
- `external_ref`: authority identifier: Steam appid, Radarr movie id, or
  Sonarr series id.
- `title`: user-facing label resolved from Slopstation's catalog.
- `state`: `QUEUED`, `RUNNING`, `UNKNOWN`, `SUCCEEDED`, `FAILED`, or `CANCELED`.
- `progress`: small structured authority-specific status. Media uses the
  phases `searching`, `waiting_for_match`, `downloading`, `importing`, and
  `ready`, plus safe aggregate percent or episode counts.
- `detail`: the latest structured observation rendered as a short diagnostic.
- `metadata`: optional structured request policy such as catalog id, quality
  preset/profile, and selected seasons. Never release or indexer text.
- `created`, `updated`, and `last_observed`: Unix timestamps.
- `finished`: terminal-transition timestamp, otherwise null.
- `announcement_pending`: whether the terminal result still needs to be
  spoken.
- `delivered`: successful full-playback or explicit voice-retrieval timestamp,
  otherwise null.
- `notifications`: durable, one-time lifecycle receipts such as
  `download_started`; each has its own pending and delivered fields.

There is one K15 writer. Atomic JSON replacement is sufficient while that
remains true; the external authority is still the source of truth. Do not add a
database until concurrent writers or real query requirements appear.

## State rules

- A verified Steam submission starts `RUNNING`; an accepted but unverified
  submission starts `QUEUED`.
- Only a positive external observation can produce `SUCCEEDED`. An app
  disappearing from Steam's changing list is not completion evidence.
- A reachable Steam client reporting the app in its changing list produces
  `RUNNING` and replaces progress with the current observation.
- A fully-installed Steam manifest on the gaming PC produces `SUCCEEDED`.
- An offline client, failed observation, or ambiguous absence produces
  `UNKNOWN`, never `FAILED`.
- `UNKNOWN` can return to `RUNNING` or advance to a terminal state. It is
  visible in status output but never announced.
- Terminal records never regress after a restart or a later observation.
- Cancellation changes state only after the authority cleanup succeeds. Steam
  installation cancellation remains unsupported. Radarr/Sonarr abandonment
  stops monitoring, cancels an active search, removes its download and partial
  data, and deletes imported files in the requested scope before recording
  `CANCELED`.
- If every episode in a requested Sonarr scope becomes unmonitored outside
  Slopstation, reconciliation records that operation as `CANCELED` instead of
  leaving a zero-target request active forever.
- Repeating the same observation or polling a terminal record produces no
  duplicate announcement.

An active request with the same kind and external reference reuses its existing
record. This prevents repeated voice turns from creating multiple trackers for
one Steam install.

## Monitor and announcement

The voice agent owns concrete Steam and media daemon monitors when their
authorities are configured. Each polls only its nonterminal operation kinds,
immediately reconciles persisted active operations after process startup, and
sleeps between passes. Monitoring may stop while the voice agent is down;
restart resumes observation from the file.

Most progress, `UNKNOWN`, and recovery from `UNKNOWN` are silent. Media emits
one durable spoken receipt when an acceptable download starts, and one when an
initial search finishes without a match. A first terminal transition sets
`announcement_pending` and queues the existing
out-of-session announcer. The announcer retains its independent audio device,
session gate, wake-word abort, and follow-up window. Full playback clears the
pending flag; interrupted or failed playback leaves it set for a later retry.
An explicit request for recent results also clears the pending flag once the
tool result enters that active voice turn, preventing a duplicate announcement
after the session closes.

A media search that coincides with an authority-reported indexer outage arms a
durable recovery retry. The monitor waits until Radarr or Sonarr reports an
enabled automatic-search indexer without an aggregate search warning,
then performs at most three retries with 5-minute, 30-minute, and 2-hour minimum
backoffs. A healthy no-match search is not retried. The pending timestamp and
attempt count live in operation metadata, so restarting the voice agent neither
loses the retry nor resets its bound.

There is one notification channel today, so no per-channel delivery ledger is
part of this design. Add that only when a second production channel exists.

## Interfaces

The Steam path is deliberately concrete rather than an abstract job framework:

1. `install_game` submits through `SteamSession`.
2. A successful submission creates or reuses a `steam_install` operation.
3. `SteamMonitor` observes Steam progress and checks the gaming PC's manifest
   once the app leaves that list or reaches 100% downloaded. Only the
   fully-installed manifest flag completes the operation.
4. Voice can read structured recent or active operations through an assistant
   tool. Active reads first reconcile configured authorities; current-state
   questions never use catalog or conversation memory. No operation or release
   text is injected into conversation history.
5. `operations.py list|show|reconcile|cancel|abandon` provides diagnostics.
   `reconcile` uses live authority observations; `cancel` reports unsupported
   Steam cancellation honestly. `abandon <media-operation> --execute` performs
   authoritative media cleanup before changing the ledger.

Do not introduce an adapter base class for this first implementation. When
Radarr and Sonarr arrived, the only shared surface they justified was generic
record creation plus `MediaMonitor` consuming a structured observation. Steam
keeps its own concrete monitor and manifest evidence.

The media path is likewise concrete:

1. `find_media` resolves structured TMDB/TVDB candidates through an authority.
2. `request_movie` or `request_series` submits a resolved id and named preset.
3. A successful submission creates or reuses its media operation.
4. `MediaMonitor` asks the media boundary for file/episode evidence and never
   reads release-level data.
5. `media.py status|profiles|validate|find|request-*|delete-*` provides
   diagnostics and an explicit `--execute` mutation gate.

## Research-runner removal

The old research queue is removed as a feature, not migrated:

- Delete its subprocess workers, queue, worker home, probe, configuration,
  grammar, prompt tool, tests, and tracing bridge.
- Delete automatic worker-result injection into assistant history.
- Preserve the generic audio behavior in `announce.py`, changing only its
  source contract and telemetry from jobs to operations.
- Remove `job_*` and task-retrieval events deliberately from the frozen event
  vocabulary; add the operation lifecycle events there.

## Media placement

Prowlarr, Radarr, Sonarr, and native qBittorrent live on the always-on K15.
The Arr containers' `/data` path and qBittorrent's Windows download path both
refer to `C:\Media`; remote path mappings bridge their path syntax. The future
NAS changes those host mappings without changing Slopstation operation records.
Media acquisition therefore has no dependency on waking the gaming PC, display
profiles, the Puck, or a couch session.

## Acceptance and deferred live validation

Checkout-safe tests must prove persistence, active-record deduplication,
restart reconciliation, progress updates, `UNKNOWN` silence, positive-only
completion, one terminal callback, interrupted announcement retry, unsupported
cancellation, imports, lint, and the frozen event vocabulary.

The following checks require the deployed machines or the user's coordination
and are intentionally deferred until all repository work passes:

1. Queue a small owned game on the real gaming PC and verify the operation id,
   progress, and one completion announcement.
2. Restart the K15 voice agent during the download and verify reconciliation
   without a failure or duplicate announcement.
3. Sleep or disconnect the gaming PC during a download and verify `UNKNOWN`
   remains silent, then wake it and verify recovery.
4. Interrupt a completion announcement with the wake word and verify the
   result remains pending and is delivered later.
5. Run the K15 audio-bound suite and `doctor.py` after deployment.
6. Run the media checks in `docs/media-acquisition.md` after the sidecars,
   indexer, profiles, and API keys are configured.
