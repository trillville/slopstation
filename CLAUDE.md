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

## Working on the live system

Don't assume which machine you are on — a session runs on the K15, on the
gaming PC, or on a checkout that is neither. Establish that first, because what
matters is not where a command runs but what it reaches:

- **On the K15** — restarting a lane, editing `config.json`, clearing
  `state\session.lock`.
- **On the gaming PC** — applying a display profile, recycling the Puck claim,
  `Deploy.ps1`.
- **From anywhere on the LAN** — the `ssh gamepc` verbs reach the PC whether or
  not you are sitting at it.

On a checkout that is neither machine, local commands are free; the LAN verbs
and anything you land that later gets deployed are not. Any of the above can
end what is on the TV right now, and someone is usually on the couch.

So before a command that changes state on a live machine, check that the
evidence supports that specific action. A symptom that pattern-matches a known
failure often has a different cause, and the `turn` id exists so you can prove
which one it is rather than inferring it — the log and trace skills answer most
of these questions without touching either machine, so start there.

When the ask is a question or a description of something going wrong, the
deliverable is the diagnosis. Report it and stop; the fix is a separate ask.

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

A design doc is written for work that is NOT yet built and is deleted once it
is — the code and its comments become the record. Three survive, and they are
the genre: what was tried, what the measurements showed, what gates the next
step. `docs/resume-game-design.md` (nothing built, two designs refuted, one
open question), `docs/custom-wakeword-design.md` (built and vendored, not
deployed — so what is left is a deploy and a couch ladder), and
`docs/tv-power-detection-design.md` (one approach refuted on the wire, two
leads open, and a shipped mitigation that is explicitly not the fix). Each
names its own delete condition; honour it. README § Deliberately not doing carries the
standing "don't re-litigate this" list, and § Still genuinely open the short
list of things that are neither shipped nor closed.

When a doc's work ships, the doc goes and its live residue moves into the code
it describes — not into a second doc. That is why `workers.py` carries the
worker-boundary reasoning and `session_runtime.job_messages` carries the part
of it that is still open.
