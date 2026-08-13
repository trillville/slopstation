# Working in this repo

Read `README.md` — especially **§ Code architecture** — before any structural
change. The architecture rules live there (one home per README's "one home"
principle); this file holds only the process rules an agent session needs.

## Non-negotiables

- **The comments are load-bearing.** They carry incident history and
  constraints, not narration. Never strip them, never let a refactor orphan
  them — a moved function's comments move with it, verbatim.
- **Moves are pure.** A commit that relocates code changes zero behavior.
  Land behavior first, moves second, never both in one commit (README § Code
  architecture has the module-extraction rule itself).
- **Tests assert on events, never prose.** Event names are the interface —
  dashboards group by them and alerts fire on them — so rewording a message
  is free and renaming an event must break a test. `cglib.CapturingLog` is
  the double; don't hand-roll one.
- **Two lanes, one direction of dependency.** The chord lane (`cglib.py`,
  `events.py`, `couch.py`, `chord_listener.py`) is load-bearing, runs on
  system python, and must stay stdlib-only (`events.py` documents why).
  Voice is an overlay with its own venv and may depend on the chord lane's
  modules, never the reverse.
- **Telemetry never costs a session.** Anything on an emit path is fail-soft
  by construction — see `events.emit`'s positional-only signature for the
  standard this has to meet.

## Running the tests

The blind suite runs as scripts, not pytest — `events._env()` detects tests
by `sys.argv[0]`, so pytest would mislabel events as env=prod:

    .venv\Scripts\python tests\test_couch.py     (from k15\voice\)

`test_lint.py` (pyflakes, undefined names) sweeps every module and is the
cheapest full-repo check. `test_turn.py` reads the SHIPPING `Dispatch.ps1`,
so gaming-pc regex changes are drilled from here. Hardware-bound tests
(`test_standoff` needs hid + the Puck, `test_session_pipeline` needs audio
devices) only run on the K15.

## Deploying

- K15: `git pull`, then `Start-K15.bat` (reloads both lanes on current code).
- Gaming PC: `gaming-pc\Deploy.ps1` from a checkout on the PC — never
  hand-copy; it ships the set atomically and stamps `build-id`, which
  `doctor.py` compares (`ssh gamepc version`) to catch skew.
- After either: `python doctor.py` on the K15 should end `0 fail`.

## Design notes

Decisions with evidence live in `docs/` (`resume-game-design.md` is the
genre: what was tried, what the logs showed, what gates the next attempt).
`docs/architecture-review-2026-08.md` records the 2026-08 architecture
review — read it before re-litigating the session protocol, the worker
boundary, or the module layout.
