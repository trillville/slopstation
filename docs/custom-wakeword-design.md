# Custom wake word — attempt two

**Status: `hey_alfred_v1.0` is live and is a measured regression.** It is fine
in a quiet room and worse than the stock `hey_jarvis` it replaced once the TV
is on. This note is the plan to fix that. **Delete it once the new model
passes its soak** — at that point the code and its comments are the record.

## What the telemetry actually showed (2026-08-15)

| | `hey_jarvis` @ 0.5 | `hey_alfred` @ 0.2 |
|---|---|---|
| wakes logged | 38 | 18 |
| median first-crossing | 0.97 | 0.255 |
| crossings ≥ 0.90 | 27 of 38 | 0 of 18 |
| confirmed false accepts | none seen | 3 |

The three false accepts scored **0.25 / 0.26 / 0.28** against a median genuine
wake of **0.255**. They are not near the real wakes, they are *among* them —
so no threshold separates them, and moving the dial only chooses which failure
you get. That is the whole diagnosis; everything below follows from it.

Two supporting facts. Jarvis crossed at 0.97 against a 0.5 gate, i.e. its score
leapt past 0.9 in one 80 ms hop, while Alfred's crossings hug its threshold —
so Alfred's response is genuinely shallow, not merely measured lower. And a
talker on the couch reaches the mic 10–20 dB *below* TV dialogue, which is the
condition the model was never trained on.

## Why it came out this way

**The training SNR never covered the room.** livekit hardcodes background
mixing at `+5..+15 dB` (`data/augment.py`, `snr_db_range` default, no config
key) applied to 100% of clips. So every positive had the speaker *louder* than
the interference. openWakeWord's own recipe is `-10..+15 dB` at `p=0.75` — a
25 dB span with a quarter left clean, against a 10 dB span with none.

**The eval could not rank candidates.** All three sizes scored identically —
3 false positives in 17.85 h, ~99.3% recall, AUT 0.0000 — while on real couch
audio their medians were 0.083 / 0.892 / 0.585. The validation set was
livekit's synthetic negatives; 100% of the room recording was training data and
none of it was validation data.

**The threshold came from contaminated audio.** The 0.0353 "noise ceiling" that
justified 0.25 was measured on the 40-minute room recording — which was *in the
training set*. Game audio was never represented at all.

**The phrase is weak.** `AE1 L F R AH0 D` has no sibilant and no released
plosive; everything but /f/ is a sonorant, which is what broadband game audio
masks best. Compare `jarvis` (`JH ... S`) — an affricate and a sibilant.
Not being changed now, but it is the reason the ceiling is lower than it
looks. See § Constraints on any future phrase.

## Item 7 — retrain

Four changes, one run. `wake-training/` holds all of it; the CLI's six
hand-run steps and the old `sweep.py` are superseded by `pipeline.py`.

1. **Widen the SNR to `0..+20 dB` with 25% left clean.** `patch_augmentation()`
   monkeypatches `mix_with_background`; it cannot be a config change because
   livekit exposes no key. Do **not** also raise `augmentation.rounds` — rounds
   compound, and three rounds of a widened mix lands near −18 dB.
2. **Game audio in the backgrounds.** Games are spectrally unlike the
   dialogue-heavy film already in `backgrounds/room/`: percussive transients,
   compressed VO, a music bed. Recorded with the existing
   `k15/voice/bench/record_room.py`, sliced by `slice_room.py`.
3. **Hold out room + game audio as *validation*.** `make_validation.py` writes
   `validation_set_features.npy`, which the trainer concatenates into its
   validation negatives — so both the reported FPPH and `find_best_threshold`
   start describing this living room. This is the change that makes the eval
   able to rank candidates at all.
4. **Scale up.** `n_samples` 10k→25k, `steps` 50k→100k, `n_background_samples`
   200→2000, `max_negative_weight` 1500→3000, plus `custom_negative_phrases`
   led by the bare `"alfred"`. Medium took 12 min at 50k steps, so this is
   ~25 min — the cost is recording time, not compute.

Expected gain is real but partial. Amazon's playback-interference work reports
30–45% relative false-reject reduction from mixing TV/music into training at a
chosen SIR. It does not close a 20–40 dB gap on its own, which is why item 6
and the already-shipped ducking exist.

## Item 6 — the verifier

A second stage, because the first one has no axis left. openWakeWord's
`custom_verifier_models` is a logistic regression over the **same embeddings**
the wake model already computed — inference is a dot product — and it can use
what the score cannot express: whose voice this is. Google's cascade paper is
the precedent: a *loose* stage 1 plus a verifier beat a single tight stage on
**both** axes at once (0.02 FA/hr at 3.5% FRR, against 0.5 FA/hr at 4.1%).

Its negatives are the harvested false activations that `audio.py` now writes to
`k15/logs/wake/*.wav` on every fire — which the openWakeWord docs call one of
the most effective options. `bench/train_verifier.py` builds it on the K15,
where the clips already are.

**It depends on real-world clips, so it cannot be built the same day.** Run the
new model for a few evenings first, then sort the clips by ear.

Two properties to design around, both documented upstream. It **replaces** the
score above `custom_verifier_threshold` rather than gating it, so enabling it
voids every `wakeThreshold` ever measured without it. And it is
speaker-specific by design — train it on everyone who uses the room.

## Order, and why

Item 7 first and alone. It needs only a recording session, and its result
changes what item 6 is trained against — a verifier fitted to a model that is
about to be replaced is wasted labelling. Ducking (shipped) is already helping
the STT half in the meantime.

## Gates

The new model replaces the current one only if, on the K15:

- `--wake-trials` from the couch, TV at movie volume: **≥18/20**, and the
  logged `peak` values clear the false-accept ceiling by 3× or more. Peak, not
  `score` — see `bench/probe_wake_model.py` and `WakeListener._scan_peak`.
- `--false-accept-soak` through a full film **and** an hour of a game: **≤1**.
- `wake_near_miss` shows margin rather than a cluster just under the threshold.

If it fails all three, the phrase is the next variable, not the recipe.

## Constraints on any future phrase

Both learned the expensive way, both still binding:

- **The first word must be a greeting in `GREETINGS`** (`hey`/`hi`/`ok`/`okay`)
  or `strip_wake` cannot remove the phrase from the transcript.
- **Prefer a sibilant or an affricate.** Non-sibilant fricatives account for
  over half of consonant confusions at 12 dB SNR; sibilants are seldom confused
  at the same level. `alfred` has neither.
