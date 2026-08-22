# Working in this repo

Read `README.md` first — it describes the machines, the components, and how to
run and deploy them. This file holds the process rules an agent session needs.

## Non-negotiables

- **Comments state constraints, not narration.** Keep them terse: the measured
  number, the hardware quirk, the ordering that must hold. No rationale essays,
  no debugging stories, no restating the code. A comment that would cause a bug
  if deleted stays; one that only explains a past decision does not. A moved
  function's comments move with it.
- **Moves are pure.** A commit that relocates code changes zero behavior. Land
  behavior first, moves second, never both in one commit.
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

