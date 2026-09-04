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
  `Deploy.ps1`, `Install.ps1`.
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

## Writing to the user

Whoever reads your message is on a phone between other things, or reading
the PR months later with no memory of the session. Write for that reader.

- Say what you mean. When a plain phrase exists, use it: "a setting worth
  changing", not "a dial worth turning". Metaphor and flourish are there to
  show off the writer, they make the reader work harder, and they drag in
  meanings you did not choose. Cut them.
- No jargon. Give an acronym its expansion the first time, and give this
  repo's own words (lane, turn, chord, Puck) a plain gloss when the reader
  might not have `README.md` open.
- Short sentences, one idea each. Break paragraphs often. Lead with the
  answer or the outcome. If something could not be verified, say that first.
- Lists are for items that really are parallel: findings, steps, files to
  look at. Everything else is prose. No headers in a short message.
- Commit messages, PR descriptions, code comments and this file follow the
  same rules. They outlive the session.

## Changing code

- Keep the change to what the task needs. A pre-existing bug, a slow path
  or behavior the task does not mention is a follow-up to report in your
  summary, not something to fix in this change, unless the requested
  behavior cannot work without it.
- Where the task is ambiguous, build the reading its wording and the
  surrounding code most directly support, say so in your summary, and do
  not build the other readings as well.
- Tests go where this repo already keeps tests for that kind of change,
  sized like the neighboring files: roughly one focused test per stated
  behavior. Scratch checks stay scratch and are not committed.
- Edit the lines that change rather than rewriting the file, unless the
  file is short or most of it is changing.

## Running the tests

    .venv\Scripts\pytest      (from the repo root; on the K15 set
                               SLOPSTATION_TEST_AUDIO=1 to include the audio tests)
    .venv\Scripts\ruff check .
    .venv\Scripts\mypy

`tests/conftest.py` sets SLOPSTATION_ENV, a fresh `paths.HOME` per test (state,
logs and markers move with it) and the config fixture. Nothing restores what a
test changes: patch through `monkeypatch` inside a test or a fixture, never by
assignment and never at module scope, which runs during collection and leaks
into the whole session.

Several rules here are tests: `test_event_names` (the frozen vocabulary - a new
event is added there, a rename is a deliberate edit there), `test_imports`
(every module imports in a fresh interpreter with no config.json - run it after
any move), `test_gaming_pc_scripts` (every `.ps1` parses; the PC-side contract
agrees with itself; every gaming-pc script is included in `Deploy.ps1`, so one
cannot be omitted from a successful deployment). `test_turn_ids.py` reads the
SHIPPING `Dispatch.ps1`, so gaming-pc regex changes are drilled from here. mypy runs
with `check_untyped_defs`, so a moved attribute fails there even in unannotated
code.
`test_library` needs a local Steam (the gaming PC); `test_voice_session`
needs audio devices (the K15); both skip themselves. The python in `ci.yml` and
`pyproject.toml` MIRRORS the K15's interpreter and is not a floor:
`constraints.txt` is frozen from that venv, so a cp313 pin has no wheel for an
older CI and the install fails.

The venv's editable install points at ONE checkout, the main one. From a git
worktree, pytest tests the worktree's code (pyproject's `pythonpath` puts its
`src` first, and conftest passes that on to the interpreters the suite
spawns) - but `python -m slopstation.<module>` and the console scripts run
the MAIN checkout's code. To run a module from a worktree, set
`PYTHONPATH=src`.

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
- The live checkout never receives a commit. To ship something produced on
  the K15 (a refreeze): `git switch -c <branch>` FIRST, commit, push,
  `git switch main`, and `git status` must read "up to date with
  origin/main". "Ahead by 1" means a commit landed on main, and the
  fast-forward deployer refuses every merge until it is gone.

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
- **`gaming-pc\config.psd1`** is the same thing on the PC: gitignored, created
  by `Install.ps1` at `C:\CouchGaming\config.psd1`, never written by a
  deploy. A new key in `config.example.psd1` is a hand edit there.
- **Scheduled tasks**, on both machines. The K15's two lane tasks point at
  `.venv\Scripts\slopstation-lane.exe` by absolute path, so moving the
  checkout or renaming that entry point means re-running
  `Setup-K15-Tasks.ps1` there. The gaming PC's tasks are re-registered by
  `gaming-pc\Install.ps1`, run elevated from a checkout on the PC. The
  runtime pieces `Deploy.ps1` warns about but never touches (`vhui64.exe`,
  the DisplayMagician `.lnk`s) stay hand steps.
