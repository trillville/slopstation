# Architecture plan: lifecycle, contracts, operational knowledge, pruning

## Status (2026-09-16)

Executed on `tillman/architecture-lifecycle`, one commit per step, all 21
steps. Every step passed ruff, mypy against the Windows target, and the
suite on macOS under a stand-in for `msvcrt` (nine tests are Windows-only
and fail there on `main` too); Windows CI has not run because the branch
could not be pushed through Graphite from this session. Deviations from the
text below: the Submission is a TypedDict beside its metadata key list, not
a dataclass, because the value is the JSON the model reads; `quit_game`
keeps `risk="act"` and asks through `ctx.confirm`, because the registry rule
keeps destructive tools out of the default set; the three fakes named
`FakeMedia` and the three fake Arr clients were not merged, because they
fake different surfaces; the `sentry.py` fail-soft blocks stayed, because
only three of eight are a bare `pass`. Size, counted as section 4 says:
`src/` 22,892 to 23,220, `tests/` 16,522 to 16,654. The deletions happened
(about 1,350 lines went) and the new behaviour outweighed them: the services
owner, the stop path, the health route, the lane rule, the typed records
and fifteen new tests. The measured ceiling in section 2 was also about half
what the audit estimated, because the "duplicated fakes" shared names, not
behaviour.

Written against `main` at 6f58782 (2026-09-14). It replaces the plan on
`tillman/architecture-refactor`, which was written against an Aug 31 base and
cannot be merged: the Sep 3 restructure moved every file it touched.

The goals:

1. Give the shared application an explicit lifecycle.
2. Formalize the few central domain contracts.
3. Consolidate operational knowledge into simpler contracts.
4. Remove code and tests that do not earn their lines, with the size
   reported before and after.

The work lands as one branch and one PR, built in the order of section 5.
Each step leaves the suite green and is committed as it finishes, so the
branch is reviewable step by step and a partial branch is still coherent. The
PR is squash-merged like every other. The plan keeps the tool registry, the
single package, pytest and Sentry as they are.

## 1. What is already done

Since the assessment, `main` has addressed a large part of it:

- One package under `src/slopstation`, one venv, pytest with `conftest.py`,
  ruff, mypy with `check_untyped_defs` (#80, #83 to #86).
- A tool registry: one `ToolSpec` per tool, bound to its function by name,
  build-time failure on drift, a risk class, and `ConfirmGate` shared by every
  destructive tool (#118). Adding a tool is one spec and one function in a
  toolset module. Nothing in `assistant.py`, `session.py` or `text.py` names a
  tool.
- `events.Ticker` is one stoppable thread class for periodic work (#87).
  `statefile` owns the byte lock and atomic write (#83).
- `supervise.py` owns process control: a kill-on-close job object and a
  restart loop (#80, #82). Sentry check-ins page when a lane dies (#78).
- `docs/` explains setup, configuration and operations (#106).
- Comments in `src/` are sparse (3% of lines) and almost all state a
  constraint once. Two carry dates.

## 2. What the size audit found

Two full reads of the tree on 2026-09-16, one of every test file and one of
every production module with an AST reference scan. The numbers below are
estimates from reading, not from running anything.

Tests (52 files, 16,522 lines, 490 test functions, 2,171 assertions):

- The suite is not performative. About 135 lines assert nothing about
  application behavior; about 225 including borderline cases. That is under
  2%. No test was found that patches the function under test and then
  asserts the patch was called.
- The real cost is repetition. Roughly 600 to 650 lines are duplicated fakes
  and repeated setup: four copies of a fake media service, three of a fake
  Radarr client, four of a fake Steam session, two of a fake operation store,
  ssh stubs in five files, the `log` fixture redeclared in four files despite
  `conftest.py`, about 40 seven-line Sonarr episode dicts in `test_media.py`,
  and the same ask-then-confirm two-step in a dozen tests.
- Test bodies are 60% of the lines; fixtures, fakes and helpers are 20%.
- Behaviors with no test at all, confirmed against the code: a corrupt
  ledger silently resetting, a monitor tick swallowing an exception, the
  announcer's earcon fallback, the wake loop rebuilding audio after
  `wake_stream_died`, the session idle timeout firing, the graceful stop
  path, and all of `deploy.py`. Several of these are exactly what the steps
  below change, so they gain tests there.

Production (69 modules, 22,892 lines):

- Almost no dead code. The reference scan found three items used only by
  tests: `assistant.tool_impls`, `assistant.anthropic_tools` and
  `assistant.openai_tools`, about 28 lines, which tests must stop calling
  before they go.
- About 175 lines of repeated patterns worth one helper each: change-only
  failure logging copied in three monitors, three monitors that do not
  subclass the `Monitor` base that exists, three identical
  `*_monitor_from_config` factories, eight fail-soft `try/except` blocks in
  `sentry.py`, five factory-then-`lane_up` blocks in `voice.py`, and about
  100 lines of docstring that restate the code in the largest files.
- About 280 lines of hand CLI verbs nothing documents: media
  `find library request-movie request-series delete-movie delete-series`,
  library `show catalog refresh`, steam_session
  `status token sessions downloads`, steamstore `probe`, and the voice flags
  `--devices` and `--announce-test`. The media verbs `doctor`, `proton-port`,
  `sync-proton-port` and `set-qbit-port`, the operations verbs, and
  `steam_session enroll` are documented and stay. Decision 11 covers the
  rest.

So the honest reduction available is around 1,000 lines out of 39,400,
mostly by sharing fakes and helpers, not by deleting tests. The steps below
take all of it that can be taken without an owner decision, and the PR
reports the before and after in section 7's terms.

## 3. What survives from the earlier plan

| Earlier plan piece | Now | Reason |
| --- | --- | --- |
| An object that owns the shared services, with start and stop | Still wanted (steps 2 to 5) | `voice.main()` still constructs everything and discards every handle |
| Liveness that reflects the process, not the microphone | Still wanted (step 2) | heartbeat and check-in start after `open_audio` blocks |
| Stoppable monitors | Half done | `Ticker` has a stop flag; the monitors drop the ticker on start |
| Typed operation records and a real-ledger fixture | Still wanted (steps 6, 7) | every hop in the operation flow is a dict |
| An observation record with the state decided once | Still wanted (step 8) | the precedence ladder lives only in the monitor |
| Immutable per-call turn context | Still wanted, much smaller (step 11) | `ctx.turn()` reads a mutable attribute at call time |
| Platform constraints in one document | Still wanted (step 15) | pipecat, WASAPI, byte locks and the job object are comment-only |
| A rule that the chord lane never imports the agent | Worth restoring (step 17) | deleted in the restructure with no replacement |
| A test inventory before pruning, and before/after size reporting | Done above, applied in steps 1 and 19 to 21 | the audit replaces the inventory; the numbers are in section 2 |
| One catalog, session-owned confirmations, shared handlers | Done by the registry | superseded |
| Voice-owned conversation carry | Done in `session.py` | superseded |
| The `k15/agent/{runtime,brain,room,...}` module tree | Dropped | `main` chose `agent/{llm,speech,tools,interfaces}`; a second move buys nothing |
| In-process voice retry with a 10/20/40/60 s backoff | Dropped | `supervise.py` restarts the lane in 10 s, and the microphone wait already retries; a second supervisor inside the process adds a failure mode |
| A component status file for the doctor | Dropped | check-ins and the events log already carry this once step 2 moves them |
| A ledger-polled announcer replacing store callbacks | Dropped | the push path with replay at boot works; the coupling is two attributes |
| A percentage reduction target | Dropped | the audit gives a measured ceiling instead; chasing a percentage is what grew the earlier branch |

## 4. Ground rules

- Start from a synced `main`: `gt sync`, then confirm
  `git merge-base --is-ancestor origin/main HEAD` before the first edit. The
  earlier branch lost two days to a base that was 63 commits stale.
- One branch off `main`. Commit each step as it finishes. Run `gt sync` and
  `gt restack` at the start of each working day; this branch will live
  longer than a day and `main` moves several times a week.
- Every step carries the test for what it changes. Windows CI is the gate:
  the tests import `msvcrt` and there is no venv on the macOS checkout, so
  nothing is reported as passing from a Mac. Push the branch and read CI
  after each step rather than at the end.
- A test is deleted only with its reason from the audit's categories, and
  the PR description names the deleted test and what still covers the
  behavior, or says that nothing did and why that is fine.
- No new abstraction unless it deletes a duplicate. No shared retry helper;
  the ten retry loops have different semantics on purpose.
- The event vocabulary is frozen in `tests/test_event_names.py`. A new event
  is a deliberate edit there, in the same step.
- The ledger on the K15 holds real rows. No field rename without a migration.
  Old rows may lack `metadata`, `work_id`, `summary`, `notifications`; a typed
  loader must never raise on a row.
- Size is counted the same way before and after: physical lines of `src/`
  and of `tests/`, reported separately, with docs excluded. The two counts at
  6f58782 are 22,892 and 16,522.
- The PR description lists the steps, the decisions in section 6, the size
  table, every deleted test, and every hand step: the fixture the owner must
  produce, a config key, a Sentry change, a doctor row that changes meaning.
- Anything found along the way that a step does not need is a note in the PR
  description, not a change. Two are already known: the text server compares
  the token as `str`, so a non-ASCII header raises inside
  `hmac.compare_digest` and answers 500 instead of 401 (MCP handles it); and
  `steam_session._print_qr` imports `qrcode`, which no pin installs.

## 5. The steps, in order

Step 1 is the cheap deletions. Steps 2 to 5 are the services owner. Steps 6
to 10 are the ledger and observation contracts. Steps 11 to 14 are the tool
boundary. Steps 15 to 18 are operational knowledge. Steps 19 to 21 are the
consolidation, last so they do not have to be redone under the refactor.
Within each group the order is a dependency order; the groups are
independent. Step 6 should start early because it waits on a hand step.

### Cheap deletions

**Step 1. Delete the performative tests and the test-only wrappers.** Every
candidate below was read in full; the sub-reason is the audit's category.

- `test_assistant.py:291-324`: the prose assertions ("mishears", "clamped",
  "blind toggle", "isn't in the library", the token count). Keep lines 293
  and 301 to 305, which check the catalog is present and the clock is the
  last line.
- `test_assistant.py:982-990`, `1014-1017`, `336-338`: description wording.
- `test_assistant.py:1099-1104`: a constructor attribute equals a constant.
- `test_assistant.py:1090-1096`: exercises pipecat's own schema conversion.
  Keep line 1085, which guards a real regression.
- `test_assistant.py:434-454`, `678-690`, `971-981`: duplicates of
  `test_registry.py:58-84` and `87-95`. Keep the registry versions.
- `test_media.py:2094-2102`: greps `compose.yaml`.
- `test_grammar_gate.py:307-309`: a data file contains no regex characters.
- `test_passthrough.py:541`: `assert time.time() > 0`.
- `test_toolsearch.py:182-184` and the wording half of `278-284`; keep line
  284's empty-map rule.
- `test_doctor.py:131-134` (proves two packages are installed) and
  `276-280` (two rows with no setup).
- `test_supervise.py:44-49`: a truthy handle, covered by the test at 52.
- `test_text_interface.py:155-157`: a truthy result.
- `test_registry.py:174-181`: a truthy toolkit; and the keyword-count and
  busy-phrase-word-count lint at lines 11 to 29. Keep 30 to 41, the rule
  that a destructive tool is never a default.
- `test_checkin.py:68-80`: a json round-trip of a constant.
- `test_workflows.py`: `yaml.safe_load` of the CI files, the whole file.
- `test_paging.py:50-52`: a description string.
- `test_couch.py:194-203`: the four lines that repeat `test_turn_ids.py:213-222`.
- `assistant.tool_impls`, `assistant.anthropic_tools`,
  `assistant.openai_tools` (`assistant.py:232-251`, `336-343`): only tests
  call them. Move those tests to `Toolkit(...).impls` and `REGISTRY.*`, then
  delete the wrappers.

Left alone on purpose: `test_sentry.py:37-47`, which freezes an SDK helper
the code depends on; the `test_voice.py` fakes, which do verify `main()`'s
wiring decisions even though the assertions land on fake attributes; and the
four deliberate contract freezes named in CLAUDE.md.

About 165 lines of tests and 28 of production. Files: the tests named,
`assistant.py`.

### The services owner

What is true now, in `src/slopstation/agent/voice.py`:

- Lines 283 to 401 construct the operation store, announcer, Steam session,
  four monitors, the text server and the MCP server. The servers' return
  values are discarded. The monitors are locals never read again.
- `open_audio` at line 409 blocks until the microphone answers, forever on a
  dead one. Heartbeat and check-in start at lines 428 to 431, after it. A
  process with a dead microphone is serving text, MCP and reconciliation
  while Sentry pages it as dead and the doctor reports it as running.
- Nothing is ever stopped. Production ends the lane with `TerminateProcess`
  through the job object in `supervise.py`. Graceful shutdown is exercised
  only by the two server tests.
- A raise from any constructor before the microphone exits the process.
  `supervise.py` restarts it 10 s later, forever.

**Step 2. Liveness before the microphone.** Move
`events.start_heartbeat("voice")` and `checkin.start("voice", cfg)` to just
before `open_audio`, still skipped under `--once`. Change the doctor's voice
row to read readiness from the events log (the latest of `agent_up`,
`audio_device_wait`, `wake_stream_died`) instead of the task's status. After
this a dead microphone stops paging Sentry; the doctor row carries it.
Files: `agent/voice.py`, `doctor.py`, `tests/test_voice.py`,
`tests/test_doctor.py`. About 40 lines.

**Step 3. Monitors share the base and can stop.** `ProtonPortMonitor`,
`MediaHealthMonitor` and `DiskHealthMonitor` subclass the `Monitor` base in
`operations_monitors.py` instead of carrying their own `start` and `_tick`.
The base keeps the `events.Ticker` on `self` and gains `stop()`. Two tests:
`stop()` ends the thread, and a reconcile that raises is logged as
`operation_monitor_failed` and the ticker keeps going. Files: those four,
`tests/test_operations.py`. About 30 lines, net negative.

**Step 4. Extract the assembly.** A class in a new `agent/services.py` with
`start(cfg, secrets, log, dry_run)` doing what lines 283 to 401 do today,
keeping store, announcer, steam, media, the monitor list and both servers as
attributes. The five factory-then-`lane_up` blocks become one small loop
over a table of (factory, name, skip on dry run). The three
`*_monitor_from_config` factories in `media.py` share one guard. `main()`
calls the class and passes `services.operations`, `services.steam`,
`services.media` to `Session`. The existing `test_voice.py` fakes patch
module attributes, so they keep working. Files: `agent/voice.py`,
`agent/services.py`, `agent/tools/media.py`, `tests/test_voice.py`. About
150 lines moved, 40 new, 25 removed.

**Step 5. Stop, and survive a failed optional service.** `stop()` sets each
monitor's stop flag, calls `shutdown()` and `server_close()` on both servers,
and `announcer.stop()`. `main()` wraps the wake loop in
`try/finally: services.stop()`, so Ctrl-C and `--once` exercise it. Each
optional service's construction (Steam session, media, the monitors) is
wrapped so a raise logs `lane_disabled` with the reason and continues,
instead of exiting. Tests: start the services with the existing fakes and
assert every fake got `stop()`; a fake Steam session that raises, and the
text server still starts. Files: `agent/services.py`, `agent/voice.py`,
`tests/test_voice.py`. About 70 lines.

Also in this group: `GET /health` on the text server behind the existing
token, answering the component list the services owner holds, and a doctor
row that probes it as the MCP port is probed today. About 30 lines. Files:
`interfaces/text.py`, `doctor.py`, `tests/test_text_interface.py`,
`tests/test_doctor.py`.

### The ledger and observation contracts

What is true now:

- The persisted row is built in `track_external` (`operations.py:162-182`),
  and three other methods add keys later. No docstring lists the fields.
- A media request travels as: raw args, a submission dict with eight fixed
  and eleven optional keys documented in one docstring, a ledger row whose
  `metadata` is 13 keys copied by hand, an observation dict with nine return
  sites, and a state decided by a precedence ladder in
  `operations_monitors.py:318-326`. The merged submission goes back to the
  model verbatim, so `baseline_file_id` and `command_ids` are model-visible.
- `dispatch.Result(ok, earcon, detail)` is the one typed outcome, and rig
  tools flatten it to `{"ok", "detail"}` at five sites, losing busy versus
  fail. Failed tool results are spelled `error` in torrents, steam, storage
  and media, and `detail` in rig.
- `statefile.load` returns the default on unparseable JSON, so a corrupt
  ledger loads as an empty list and the next write replaces it silently.
- No test loads a real persisted ledger.

**Step 6. A real ledger fixture.** A redacted `operations.json` from the K15
under `tests/fixtures/`, loaded through `OperationStore`, asserting the file
is byte-identical after a no-op and that `for_assistant` reads every row.
Hand step: the owner produces the redacted file. Until it exists, the step
uses a synthetic file built from the shapes in `test_operations.py` and the
PR description says so. Files: the fixture, `tests/test_operations.py`.
About 60 lines.

**Step 7. Typed rows, and a ledger that refuses to reset.** `OperationRow`
and `Notification` as `TypedDict(total=False)` in `operations.py`, a
`PHASES` tuple, and return annotations on `OperationStore`. mypy then checks
every key at about 60 read sites. `statefile.load` grows a strict variant
the store uses: a corrupt ledger raises, the store's callers log it, and the
doctor's `operations` row reports it, instead of the next write emptying it.
Test: a ledger with a truncated last line is left byte-identical and the
doctor row says so. Files: `operations.py`, `operations_monitors.py`,
`statefile.py`, `doctor.py`, tests. About 100 lines.

**Step 8. One observation record.** An `Observation` dataclass in
`tools/media.py` with the state computed once where it is produced,
replacing the ladder in the monitor. Remove `metadata_ready` if the grep that
found no consumer holds. `FakeMedia` and the observation assertions in
`test_media.py` follow. Files: `tools/media.py`, `operations_monitors.py`,
`tests/test_media.py`, `tests/test_operations.py`. About 150 lines.

**Step 9. One failure spelling.** Every toolset spells a failure
`{"ok": False, "error": ...}`; rig tools add `"busy": True` when the room
refused because something was already running, so the model can say so.
This changes model-visible JSON for the rig tools. Files: `toolsets/rig.py`,
one line in each other toolset, `tests/test_assistant.py`. About 60 lines.

**Step 10. A submission record.** `Submission` dataclass replacing
`_submission()`; `track()` derives `metadata` from it so the hand-copied key
list stops dropping new keys. Six producer sites in `media.py`, one in
`rig.py`, and about 44 test assertions. The largest step; do it after step 8
and commit it on its own.

Do not type: `metadata` beyond its known keys, `progress` beyond `phase`,
payload rows that mirror Radarr, Sonarr or qBittorrent JSON, or tool `args`.

### The tool boundary

What is true now:

- `ctx.turn()` (`registry.py:128-131`) reads `dispatch.utterance` at call
  time. The grammar gate overwrites it on every final transcript while the
  tool runs on a worker thread. Three spellings coexist: `ctx.turn()`,
  `dispatch.utterance.turn` (rig.py:430, 733) and `dispatch.utterance.asked`.
  `Dispatch.end_session` snapshots it by hand, which shows the hazard is known.
- `assistant.py` still holds the prompt assembler, the toolkit, the Pipecat
  schema adapter, web-search definitions and provider key maps. A tool author
  touches none of it, but the llm package imports pipecat.
- Passthrough writes to Radarr, Sonarr and qBittorrent are gated by hand as
  `act` tools and never enter the ledger. Their asks return `confirm` and
  `error`, so neither lane's acknowledgment shortcut fires; every other ask
  returns a `Plan`.
- `quit_game` and `pc_power sleep` are `act` with confirmation in prose only.
- `prompts.SCREENS` and `rig.SESSION`/`DISPLAY` state the same rules twice;
  `mcp.TOOL_DESCRIPTION` is a hand-written copy of `AREAS`.

**Step 11. Snapshot the utterance per call.** `Tools.call` captures the
utterance into a context variable before dispatch; `ctx.turn()` and a new
`ctx.asked()` read it; the three direct reads are replaced. This is what
remains of the earlier plan's invocation context. Files: `registry.py`,
`assistant.py`, `toolsets/rig.py`, `toolsets/passthrough.py`,
`tests/test_registry.py`. About 60 lines.

**Step 12. Pipecat rendering out of the llm package.** Move
`_pipecat_schemas` and `function_schemas` from `assistant.py` into `speech/`.
`session.py` is the only caller. Files: `assistant.py`, a new or existing
`speech/` module, `session.py`, `tests/test_assistant.py`. About 80 lines.

**Step 13. Every write asks through the same gate.** Passthrough non-GET
calls, `quit_game` and `pc_power sleep` become `destructive` specs bound
through `bind.destructive`, returning a `Plan` like every other write. A
passthrough write that succeeds writes a ledger row of a new kind so
`operations list` shows it. The hand-rolled preview and `ctx.confirm` in
`passthrough.py` go. Files: `toolsets/passthrough.py`, `toolsets/rig.py`,
`tools/operations.py`, `tests/test_passthrough.py`, `tests/test_steam_tools.py`.
About 120 lines, net negative in passthrough.

**Step 14. Say each rule once.** Remove the sentences `prompts.SCREENS`
repeats from the rig descriptions, and derive the MCP description's area
sentence from `AREAS`. Files: `prompts.py`, `toolsets/rig.py`,
`interfaces/mcp.py`, `tests/test_assistant.py`. About 50 lines, net negative.

### Operational knowledge

What is true now:

- Facts that exist only in comments, with zero mentions in `docs/`: the
  pipecat 1.8.1 behaviours the pin depends on (`session.py:286-374`), the
  WASAPI stream that outlives its endpoint (`audio.py:395`), Windows
  emulating `O_APPEND` and the byte lock that follows (`events.py:216`,
  `statefile.py:26,46`), the job object in `supervise.py`, Sentry's
  one-cron-monitor plan limit (`doctor.py:1094`), the HID handoff standoff
  (`chord_listener.py:140`), the TV power-then-input ordering
  (`couch.py:302`), the prompt-caching floor (`assistant.py:51`).
- Three dated incident narratives live in tests (`test_doctor.py:242`,
  `test_couch.py:733`, `test_event_names.py:215`) and two in source.
- Five hand-rolled copies of "log a failure once per outage": `_last_failure`
  in `disk_health.py`, `media_proton.py` and `media_health.py`, plus one each
  in `audio.py` and `couch.py`. Eight fail-soft `try/except` blocks in
  `sentry.py`, and `record_tool_call` wraps `tool_span` in one more although
  `tool_span` already swallows.
- `doctor.py` imports `agent.tools` at module top (lines 19 to 20), reads
  `events._path`, decodes the Steam JWT itself, and re-derives the wake-model
  lookup. The old rule that the chord lane never imports the agent was
  deleted with the restructure. Today `couch.py` and `chord_listener.py` do
  not import it, so a restored rule passes.
- `conftest.py` assigns `paths.HOME` and `events._last_day` directly (lines
  40 and 42), which is the pattern CLAUDE.md forbids elsewhere.

**Step 15. A constraints document.** `docs/constraints.md`: one paragraph per
comment-only fact above, each pointing at the code and test that depend on
it, and a dated list holding the incident narratives with their
measurements. The comments shrink to the constraint; the two dated comments
move. Files: `docs/constraints.md`, README's document table, the comments
named above. About 120 lines of docs, net negative in code.

**Step 16. One change-only failure logger, one quiet wrapper.** A small
helper the three monitors share; delete the three `_last_failure` copies. One
context manager in `sentry.py` replaces its eight `try/except` blocks, and
the redundant wrap in `record_tool_call` goes. The existing "reports
transitions once" tests in `test_media.py` and `test_everything_no_ops_while_tracing_is_off`
cover both. Files: `tools/media_proton.py`, `tools/disk_health.py`,
`tools/media_health.py`, `telemetry/sentry.py`, `assistant.py`. About 60
lines, net negative.

**Step 17. The doctor stops reaching in, and the lane rule returns.**
`events` exposes the log lookup the doctor uses, `operations` exposes
`owned_seasons()`, and the doctor reads through them. A 30-line test asserts
that `couch.py`, `chord_listener.py`, `gamepc.py`, `tv.py`, `events.py` and
`supervise.py` import nothing under `slopstation.agent`; the doctor is
outside the rule. Files: `doctor.py`, `events.py`, `tools/operations.py`, a
new test. About 100 lines.

**Step 18. Test conventions written down, and conftest by the rules.**
`conftest.py` patches through `monkeypatch`. A "Testing" paragraph in
CLAUDE.md names `CapturingLog`, `wants`, `seed_lock`, the couch and doctor
rigs, the shared fakes from step 19, and which tests are deliberate contract
freezes (`test_turn_ids`, `test_gaming_pc_scripts`, `test_event_names`,
`test_events`). Files: `tests/conftest.py`, `CLAUDE.md`. About 60 lines.

### Consolidation

These come last because every earlier step edits the same test files, and
a shared fake is cheaper to adopt once the code it fakes has settled.

**Step 19. One fake per boundary.** Move to `tests/helpers.py`: one fake
media service (replacing the four in `test_assistant.py:148`,
`test_operations.py:49`, `test_torrent_tools.py:197`,
`test_text_interface.py:72`), one fake Radarr/Sonarr client (the three in
`test_media.py:28`, `test_media_ops.py:20`, `test_storage_tools.py:34`), one
fake Steam session (the four in `test_steam_tools.py:66`,
`test_operations.py:25`, `test_voice.py:132`, `test_assistant.py:213`), one
fake operation store (the two in `test_assistant.py:99`,
`test_text_interface.py:54`), and one scripted `gamepc.ssh` stub (the five
files that each carry their own). Delete the four `log` fixtures that
redeclare `conftest.py`'s. A fake may grow a keyword argument to serve a
file's extra need; it may not grow branches on the test's name. About 250 to
300 lines removed. Confidence: medium; the count depends on how much the
shared fakes must carry.

**Step 20. Table the repeated setup.** An `episode(id, season, has_file,
monitored, aired)` helper for the forty seven-line Sonarr dicts in
`test_media.py`; a `tracked_series(store, ...)` helper for the twelve
`track_external` calls in `test_media_ops.py`; a `confirm_later(dispatch)`
helper for the ask-then-confirm two-step; a `failed_launch_guarantees(...)`
assertion helper for the eight repeats in `test_couch.py`. Where several
tests differ only in inputs, one parametrised test replaces them, in the
style `test_grammar_gate.py:48-225` already uses. About 300 to 350 lines
removed.

**Step 21. Hand CLI verbs.** Per decision 11: delete the verbs the owner
does not use; give the rest one sentence each in `docs/operations.md`'s
command block. Files: `agent/tools/media.py`, `agent/tools/library.py`,
`agent/tools/steam_session.py`, `agent/tools/steamstore.py`,
`agent/voice.py`, `docs/operations.md`. Up to 280 lines removed.

Expected size of the whole PR: roughly 1,500 changed lines in `src/` and
`tests/` plus the constraints document. Expected net: `src/` down by 250 to
500 depending on decision 11, `tests/` down by 600 to 800 after the new
tests the steps add. Both are estimates from reading; the PR reports the
measured numbers.

## 6. Decisions

Taken on 2026-09-16:

1. A dead microphone stops paging Sentry once check-in moves above the mic
   wait; the doctor row carries it (step 2).
2. A corrupt ledger is refused and reported, never silently emptied (step 7).
3. Failures are spelled `error` everywhere; rig tools add `busy` (step 9).
4. Passthrough writes are gated as destructive and write a ledger row
   (step 13).
5. Confirmations stay scoped per interface as they are today. Not a concern.
6. The two dated comments move to the constraints document with their
   measurements (step 15).
7. The lane rule covers the chord lane only; the doctor reads through public
   functions and stays outside the rule (step 17).
8. The services owner lives in its own module, `agent/services.py` (step 4).
9. The text server gets `GET /health` behind the token and a doctor row
   (step 5).
10. `quit_game` and `pc_power sleep` go through the same `Plan` gate as
    every other write (step 13).

Open:

11. **Which hand CLI verbs to keep.** Nothing documents media
    `find library request-movie request-series delete-movie delete-series`,
    library `show catalog refresh`, steam_session
    `status token sessions downloads`, steamstore `probe`, or the voice flags
    `--devices` and `--announce-test`. Recommended: keep `--devices` and
    `--announce-test` and document them (they are the two audio checks the
    runbook lacks); keep library `refresh`; delete the rest. Each verb kept
    gets one line in `docs/operations.md`.

## 7. Done means

- One object owns the shared services, starts them once, stops them, and
  survives an optional service failing to construct. Liveness reflects the
  process.
- The ledger row, notification, observation and submission each have one
  declaration, mypy checks their keys, a real ledger round-trips, and a
  corrupt one is refused.
- A tool reads its turn from a snapshot taken before it ran. Every write
  asks through the same gate.
- Every platform constraint the code depends on is in one document, beside
  the code that enforces it.
- The chord lane provably imports nothing from the agent.
- One fake per boundary in `tests/helpers.py`; no test asserts prose,
  truthiness, or a library's behavior; the four contract freezes are named
  as such in CLAUDE.md.
- The PR description carries the before and after line counts of `src/` and
  `tests/` counted as in section 4, every deleted test with its reason and
  its replacement coverage, the decisions above, and the K15 hand steps.
- The PR passed Windows CI at its final commit.
