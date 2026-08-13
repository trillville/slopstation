# Architecture review — 2026-08

**Status: findings landed.** A full read of both machines' code on 2026-08-12
(every module, every script, the tests, the docs), done to answer one
question: is this built right before it grows further? This note records the
verdict, what changed because of it, and — just as deliberately — what was
declined, so none of it gets re-litigated from scratch.

## Verdict

Keep the architecture. No services, no database, no merging the voice and
chord processes, no framework. The process model maps one-to-one onto failure
domains, physical observation outranks software state everywhere it matters,
and every piece of distributed state has a reconciler. The debt was narrow:
two holes in the coordination protocol, one prompt-rules-as-security-boundary
exception, no deploy-skew tripwire, two ambient side channels, and one file
past its size limit. All six were fixed the same day; the commits carry the
detail.

## What was found and fixed

| Finding | Fix (commit) |
|---|---|
| Session lock was check-then-write: a chord completing inside the few hundred ms between a voice dispatch's check and couch.py's first touch made BOTH launches proceed — and the second Enter's stale-claim recycle releases the Puck under the live session (the inputs-dead controller, manufactured by the launch path) | `acquire_lock`: exclusive create as the arbiter, guard-serialized stale recycle, owner note (turn + pid), ownership-checked release. Windows answers a racing create with a sharing violation, not `FileExistsError` — found by the new race test |
| READY had no generation identity: any non-`NOTREADY` read as ready, so a stale marker could switch the TV to a host still mid-Enter (fail-open on the one rule) | The marker's content is now the launch's turn. Verified / foreign-keep-waiting / legacy-timestamp-accept, deploy-order-free in both directions. `watch()` stands down when the marker changes identity |
| couch.py itself — the most load-bearing sequence — had no automated test | `tests/test_couch.py`: 50 acquire races, ownership, the one rule as event order, failure release, watch death, all reconcile endings |
| The worker shell was the boundary's hole: Read-deny rules never bound Bash, so an injected page + `type ..\..\secrets.json` + WebFetch was a live exfil path on the account holding the gamepc key | Claude lane runs with **no Bash** — research-only by construction. Codex cannot promise the same (sandbox confines writes, not reads/shell): documented, and doctor WARNs on that lane |
| Deploy skew was unmeasurable: PC deployed by hand-copy, and test_turn drills the REPO's Dispatch.ps1, so a drifted deployed copy passed every test | `Deploy.ps1` (one checked set + `build-id` stamp), the `version` verb, doctor comparison. Venv axis too: `constraints.txt` freezes the transitive as-built (a fresh resolve had already drifted) |
| Request context rode two mutable globals (`Dispatch.turn`, `JobStore.asked`) written at different moments, resting on an unstated one-utterance-at-a-time assumption | One immutable `Utterance(turn, asked)` snapshot, one write per transcript, contract documented at the type, barge-in interleave drilled. Surfaced and fixed a latent hole: Tier-2 `play_game` launches were reaching the PC uncorrelated |
| voice_agent.py at 876 lines held three lifecycles | Pure-move split: `audio.py` (the PortAudio world — every deafness incident), `session_runtime.py` (one session's pipeline + carry), `voice_agent.py` (composition + wake loop). Dissolved announce.py's documented resolver duplicate |

## Deliberately not done

Recorded so the next reviewer starts from the reasons, not the absence:

- **Typed config module.** The failure mode that occurs (deployed config
  missing new keys) is handled fail-loud by `REQUIRED_VOICE`; the
  `config_suspect` warnings encode semantic checks a type system can't.
  Revisit only if config knowledge scatters further.
- **Session phases/snapshots on disk** (STARTING/READY/STOPPING…). Optimistic
  software state — the probes (display, PnP, Steam registry, marker) already
  answer every phase question truthfully. The lock got atomicity and
  identity, not a state machine.
- **`pc_client` / `tv` extractions, any cglib split.** The verb responses
  mean different things per call site (BUSY:<id> becomes a spoken sentence in
  dispatch), so a typed client would just relocate the mapping. cglib is the
  deliberate twin of `CouchGaming.common.ps1`; its section headers are the
  fault lines, cut when size hurts.
- **PC marker files as JSON envelopes.** The turn file's single-slot overwrite
  costs a mislabeled transcript at worst, and the launch-app path re-validates
  everything downstream. Revisit if the verb set grows. (The one real corner —
  a mutating verb overwriting the turn file in the ~1 s before Enter reads it —
  now fails CLOSED via the foreign-turn wait.)
- **Typed ActionResult.** The earcon name IS the outcome enum, with exactly
  the three values the UX has.
- **Package layout / pyproject.** The `.bat`-from-checkout ergonomics are the
  product; the `sys.path` inserts are the price, already paid.

## The rule this produced

README § Code architecture now carries the module-extraction rule (cut on
incident-history/failure-domain, cycle-breaking duplication, or
one-sitting size — never for a diagram) and the moves-are-pure rule.
CLAUDE.md points agent sessions at both.

## Standing risks, eyes open

- The stale-lock recycle has a microsecond stat-to-unlink window
  (`_recycle_stale_lock` documents why the guard stops one level down);
  reaching it needs a prior crash plus two simultaneous launches.
- The codex worker lane keeps a shell. Structural research-only isolation is
  the anthropic lane; doctor says so whenever codex is selected. If codex
  ever needs to be real, the recorded fix is a low-privilege Windows user
  with a deny ACL on `secrets.json`.
- `constraints.txt` is seeded from the recorded as-built; a `pip freeze` from
  the live K15 venv should replace the seed (its header says how).
- test_assistant assumes a populated `state/` (it failed once on this
  worktree's first-ever run, before another test seeded the library index) —
  harmless on the K15, worth a seed step if the suite ever runs in CI.
