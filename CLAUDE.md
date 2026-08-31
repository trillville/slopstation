# Working in this repo

Read `README.md` first — it describes the machines, the components, and how to
run and deploy them. This file holds the process rules an agent session needs.

## Non-negotiables

- **Comments state constraints, not narration.** Keep them terse: the measured
  number, the hardware quirk, the ordering that must hold. No rationale essays,
  no debugging stories, no restating the code. A comment that would cause a bug
  if deleted stays; one that only explains a past decision does not. A moved
  function's comments move with it.
- **Tests assert on events, never prose.** Event names are the interface —
  dashboards group by them and alerts fire on them — so rewording a message
  is free and renaming an event must break a test. `cglib.CapturingLog` is
  the double; don't hand-roll one.
- **Two lanes, one direction of dependency.** The chord lane is EVERY module
  directly in `k15/`: load-bearing, runs on system python, and must stay
  stdlib-only at import (`events.py` documents why; `test_lint` globs the
  directory rather than keeping a list, so a new module is in the lane the
  moment it lands). The agent lane in `k15/agent/` has its own venv and may
  depend on chord-lane modules, never the reverse.
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

    .venv\Scripts\python tests\run.py      (from k15\agent\; --all forces the
                                            machine-bound tests; on the K15
                                            set CG_TEST_AUDIO=1 first)

Every test that touches the K15 modules begins with `import _bootstrap`
(`test_ps_parse` is stdlib-only). The rules above are tests:
`test_event_names` (the frozen vocabulary - a new event is added there, a
rename is a deliberate edit there), `test_imports` (imports without config or
hardware; every `module.attr` resolves - run it after any move), `test_lint`
(pyflakes + the lane rule from the AST), `test_ps_parse` (every `.ps1` parses;
the PC-side contract agrees with itself; every gaming-pc script is in
`Deploy.ps1`'s set - one that is not deploys green and is simply absent).
`test_turn.py` reads the SHIPPING `Dispatch.ps1`, so gaming-pc regex changes
are drilled from here.
`test_library` needs a local Steam (the gaming PC); `test_session_pipeline`
needs audio devices (the K15). The python in `ci.yml` and `mypy.ini` MIRRORS
the K15's interpreter and is not a floor: `constraints.txt` is frozen from that
venv, so a cp313 pin has no wheel for an older CI and the install fails.

## Deploying

- K15: `git pull`, then `Start-K15.bat` (reloads both lanes on current code).
- Gaming PC: `gaming-pc\Deploy.ps1` from a checkout on the PC — never
  hand-copy; it ships the set atomically and stamps `build-id`, which
  `doctor.py` compares (`ssh gamepc version`) to catch skew.
- After either: `python doctor.py` on the K15 should end `0 fail`.
- CD (`.github/workflows/cd.yml`) does both of the above and both
  doctors on self-hosted runners after a green `ci` on `main`. It parks
  while a session is live and never rolls back. Hand-deploying stays
  valid - it is the same two scripts.
- PRs are SQUASH-merged. A push that races the user's merge loses - what
  lands is the head GitHub had cached, not the branch tip. Push, then let
  them merge; never force-push a PR that is theirs to land.

Two properties of the K15 leg that change what a session may leave behind:

- `deploy.py` runs from the LIVE checkout and refuses one that is dirty
  (tracked files) or off `main`. A session that leaves that checkout on a
  branch has disabled CD until someone switches it back.
- It runs the PREVIOUS commit's copy of itself, because it is the thing that
  updates the checkout. A change to `deploy.py` takes effect one deploy later,
  and a new deployer has to be pulled by hand once before CD can use it.

### What CD does not do

Land the change, then TELL THE USER which of these it needs. Each fails at
`doctor.py` rather than at the thing that broke, so the deploy goes red with a
diagnosis - but nobody fixes it automatically.

- **`constraints.txt`** is a `pip freeze` on the K15, committed by hand: it
  records the cp313 venv's transitives, and no other machine can produce it.
  The voice supervisor's install gate compares `requirements.txt` ALONE
  against `.venv\deps-ok`, so a requirements change installs itself on the
  next CD deploy, while a constraints-only change that is meant to alter what
  gets installed must ride a `requirements.txt` touch. Regenerating it to
  match a venv that already resolved needs no touch. Use
  `k15/agent/refreeze.py`, never a hand-rolled `pip freeze >`: that writes
  pins only and eats the header, and PowerShell 5.1's `>` writes UTF-16.
- **System-python packages** (`k15/system-requirements.txt`) sit outside
  every venv. `doctor.py`'s import rows are what catch a missing one.
- **`config.json` keys.** Growing `cglib.REQUIRED_CONFIG` needs a hand edit on
  the K15; the file is gitignored.
- **A deploy that MOVES a lane's supervisor `.bat`.** cmd.exe re-reads the file
  between lines, so the running supervisor dies the moment `deploy.py` kills its
  agent, and nothing relaunches it; `deploy.py` then waits out its whole budget
  and fails. Its death does release the fd-9 lock, so the fix is one
  `Start-K15.bat` on the K15 - that creates the venv at the new path and
  installs the pins itself. The old lane directory survives the pull (its venv
  is gitignored) and is yours to delete.
- **Scheduled tasks** on the gaming PC, and the runtime pieces `Deploy.ps1`
  warns about but never touches (`vhui64.exe`, the DisplayMagician `.lnk`s).
