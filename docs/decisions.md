# Decisions

Five choices that shape the repository, with the evidence for each. They are
not permanent; the last section says what would reopen them.

## Configuration stays at the checkout root

`config.json`, `secrets.json`, `state\` and `logs\` sit beside the code, and
`SLOPSTATION_HOME` moves all four together. There is no `config\` directory
and no separate override for each path.

Why: `paths.py` resolves every runtime path from one root at call time, the
K15's scheduled tasks point at the checkout by absolute path, and
`deploy.py` runs from that same checkout to update it. Moving the files would
add a hand migration on the K15 and a second way to relocate them, for no
reader-visible gain. The doctor already validates the files, so a JSON Schema
would be a second contract that could drift from the first.

## Deploys wait, and never roll back

Both deploy scripts wait for a live session to end and fail rather than
interrupt one. Neither reverts on a failed doctor. The K15 leg fast-forwards
only, refuses a dirty or off-`main` checkout, and restarts the chord listener
whether or not it was up.

Why: someone is usually on the couch. A deploy that cuts a session costs more
than a deploy that waits, and a rollback that restarts lanes mid-session
costs the same as the failure it undoes. A red doctor with a diagnosis is the
recovery path, and every hand step CD cannot do is listed in
[operations.md](operations.md) so the red is actionable.

## One forced command and an allowlist of verbs

The K15's key on the gaming PC is bound to `Dispatch.ps1`. It matches the
command against a fixed set of anchored patterns, answers, and denies
anything else. The turn id it accepts is one to eight lowercase hex
characters, checked before it becomes part of a filename. Interactive Steam
work runs in scheduled tasks in the logged-in session; the SSH session only
writes a marker and starts the task.

Why: the gaming PC is a full Windows desktop with an administrator account.
A general shell over SSH would make the K15 the PC's weakest point, and the
K15 accepts voice from anyone in the room. The verb set is small enough to
read in one screen, `tests/test_turn_ids.py` drills the turn regex from the
shipping script, a denied command leaves a record on the PC, and the K15
doctor sends a bogus verb to prove it is still denied.

## One turn id and one telemetry project

Each intent mints a short id on the K15. It rides the SSH verb, the PC's
marker file, every event on both machines, and the PC tasks' transcript
names. Both machines ship to one Sentry project.

Why: a launch crosses two machines and four processes, and the question
after a failure is always "what happened to that press". One id and one
project make that a single query instead of a timeline reconstructed from
clocks. The id's shape is fixed by the filename rule above, so the same
regex is both the correlation and the security boundary.

## Per-installation values on the PC are one file, and nothing else varies

`C:\CouchGaming\config.psd1` holds the four values that differ between
installations. Directory names, task names, marker names and shortcut names
are conventions in the scripts.

Why: those four are the values that name this house's hardware; everything
else is a path or a label the scripts agree on among themselves. Making the
conventions configurable would widen the installer, the doctor and the
dispatcher's marker paths for a second installation that does not exist yet.

## Deferred

Each of these waits for a concrete second case:

- Interfaces at the TV, controller, transport, speech, media or telemetry
  edges: a second TV, a second controller transport, a second provider.
- Splitting reusable modules into packages: a second consumer. They are
  importable today with no `config.json` dependency, which
  `tests/test_imports.py` enforces.
- A generated JSON Schema for `config.json`: only if runtime validation,
  documentation and tests are all derived from it.
- A private deployment repository: a machine file that cannot be gitignored
  in place.
- A separate case-study document: the README growing past a landing page.
