# Durable operations

Slopstation tracks long-running work that another system executes. It does not
run that work itself. Steam owns game installation; Radarr and Sonarr will own
media acquisition. The K15 keeps only enough state to correlate a request,
observe its authority after a restart, answer status questions, and announce a
terminal result.

The first implementation is Steam installation. Media acquisition is the
second implementation and may extend this contract only where the two domains
demonstrably differ.

## Record

`state/operations.json` contains records with this shape:

- `id`: Slopstation identifier used by logs and the diagnostic CLI.
- `turn`: originating voice turn when one exists.
- `kind`: domain operation type; initially `steam_install`.
- `authority`: system whose observation decides the state; initially `steam`.
- `external_ref`: authority identifier; the Steam appid for an install.
- `title`: user-facing label resolved from Slopstation's catalog.
- `state`: `QUEUED`, `RUNNING`, `UNKNOWN`, `SUCCEEDED`, `FAILED`, or `CANCELED`.
- `progress`: small structured authority-specific status, initially percent,
  paused, and queue position.
- `detail`: the latest structured observation rendered as a short diagnostic.
- `created`, `updated`, and `last_observed`: Unix timestamps.
- `finished`: terminal-transition timestamp, otherwise null.
- `announcement_pending`: whether the terminal result still needs to be
  spoken.
- `delivered`: successful full-playback or explicit voice-retrieval timestamp,
  otherwise null.

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
- Cancellation changes state only after the authority confirms it. Steam
  installation cancellation is unsupported in the first slice and must be
  refused without changing the record.
- Repeating the same observation or polling a terminal record produces no
  duplicate announcement.

An active request with the same kind and external reference reuses its existing
record. This prevents repeated voice turns from creating multiple trackers for
one Steam install.

## Monitor and announcement

The voice agent owns one daemon monitor. It polls only nonterminal Steam
operations, immediately reconciles persisted active operations after process
startup, and sleeps between passes. Monitoring may stop while the voice agent
is down; restart resumes observation from the file.

Normal progress, `UNKNOWN`, and recovery from `UNKNOWN` are silent. A first
terminal transition sets `announcement_pending` and queues the existing
out-of-session announcer. The announcer retains its independent audio device,
session gate, wake-word abort, and follow-up window. Full playback clears the
pending flag; interrupted or failed playback leaves it set for a later retry.
An explicit request for recent results also clears the pending flag once the
tool result enters that active voice turn, preventing a duplicate announcement
after the session closes.

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
   tool. No operation or release text is injected into conversation history.
5. `operations.py list|show|reconcile|cancel` provides checkout-independent
   diagnostics. `reconcile` uses live Steam and PC observations; `cancel`
   reports the unsupported capability honestly.

Do not introduce an adapter base class for this first implementation. When
Radarr and Sonarr arrive, extract only the interface their real differences
justify.

## Research-runner removal

The old research queue is removed as a feature, not migrated:

- Delete its subprocess workers, queue, worker home, probe, configuration,
  grammar, prompt tool, tests, and tracing bridge.
- Delete automatic worker-result injection into assistant history.
- Preserve the generic audio behavior in `announce.py`, changing only its
  source contract and telemetry from jobs to operations.
- Remove `job_*` and task-retrieval events deliberately from the frozen event
  vocabulary; add the operation lifecycle events there.

## Media decision gate

Before media implementation, decide where Prowlarr, Radarr, Sonarr,
qBittorrent, downloads, and the final library live. The gaming PC sleeps by
design, while the K15 is always available but no always-on media storage is
described in the repository.

If the bytes live only on the gaming PC, media acquisition needs a headless
wake and keep-awake lifecycle that does not switch displays, claim the Puck, or
start a couch session. If suitable storage is always reachable from the K15 or
a NAS, the services can remain always on and this prerequisite disappears.
This choice must be made before writing the media submit path.

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
6. Record the media-service and storage placement, then implement Radarr and
   Sonarr as the second proving authority.
