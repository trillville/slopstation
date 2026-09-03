# Working in this repo

Read `README.md` first — it describes the machines, the components, and how to
run and deploy them. This file holds the process rules an agent session needs.

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
which one it is rather than inferring it — the `sentry` skill answers most
of these questions without touching either machine, so start there.

When the ask is a question or a description of something going wrong, the
deliverable is the diagnosis. Report it and stop; the fix is a separate ask.

## Running the tests

    .venv\Scripts\pytest      (from the repo root; on the K15 set
                               SLOPSTATION_TEST_AUDIO=1 to include the audio tests)
    .venv\Scripts\ruff check .
    .venv\Scripts\mypy

`tests/conftest.py` sets SLOPSTATION_ENV, a fresh `paths.HOME` per test (state,
logs and markers move with it) and the config fixture, and - because pytest shares one process where the old runner
gave each file its own - restores whatever a test rebinds. Patch inside a test
or a fixture, never at module scope: module scope runs during collection,
before any fixture, and leaks into the whole session.

Several rules here are tests: `test_event_names` (the frozen vocabulary - a new
event is added there, a rename is a deliberate edit there), `test_imports`
(every module imports in a fresh interpreter with no config.json - run it after
any move), `test_ps_parse` (every `.ps1` parses; the PC-side contract agrees
with itself; every gaming-pc script is in `Deploy.ps1`'s set - one that is not
deploys green and is simply absent). `test_turn.py` reads the SHIPPING
`Dispatch.ps1`, so gaming-pc regex changes are drilled from here. mypy runs
with `check_untyped_defs`, so a moved attribute fails there even in unannotated
code.
`test_library` needs a local Steam (the gaming PC); `test_session_pipeline`
needs audio devices (the K15); both skip themselves. The python in `ci.yml` and
`pyproject.toml` MIRRORS the K15's interpreter and is not a floor:
`constraints.txt` is frozen from that venv, so a cp313 pin has no wheel for an
older CI and the install fails.

## Deploying

- K15: `git pull`, then `Start-Slopstation.bat` (ends and re-runs both lane
  tasks on current code). From a NORMAL window, never an elevated one: an
  elevated lane cannot be seen or stopped from the window the deployer uses.
- Gaming PC: `gaming-pc\Deploy.ps1` from a checkout on the PC — never
  hand-copy; it ships the set atomically and stamps `build-id`, which
  `doctor.py` compares (`ssh gamepc version`) to catch skew.
- After either: `.venv\Scripts\slopstation-doctor` on the K15 should end
  `0 fail`.
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
  The supervisor's install gate hashes `pyproject.toml` AND `constraints.txt`
  against a `deps-ok` sentinel in the venv, so a change to either installs
  itself on the next deploy. Regenerate it there with
  `.venv\Scripts\pip freeze --exclude-editable | Out-File -Encoding ascii constraints.txt`
  - not `>`, whose output under PowerShell 5.1 is UTF-16, which pip cannot
  read.
- **`config.json` keys.** Growing `config.REQUIRED` needs a hand edit on
  the K15; the file is gitignored, and it sits at the repo root beside
  `secrets.json`, `state/` and `logs/` (see `paths.py`).
- **Scheduled tasks**, on both machines. The K15's two lane tasks point at
  `.venv\Scripts\slopstation-lane.exe` by absolute path, so moving the
  checkout or renaming that entry point means re-running
  `Setup-K15-Tasks.ps1` there. The gaming PC's tasks, and the runtime pieces
  `Deploy.ps1` warns about but never touches (`vhui64.exe`, the
  DisplayMagician `.lnk`s), are the same kind of hand step.
