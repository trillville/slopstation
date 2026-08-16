# Custom wake word — what is left to do

**Status: trained and vendored, NOT live.** `k15/voice/models/hey_alfred_v1.0.onnx`
is in the repo and inert — `config.json` still points `voice.wakeModel` at
`hey_jarvis_v0.1`, so resolution ends where it always did. The inference-side
code has landed (`audio.py` `_resolve_model`, `doctor.py`, `test_wake.py`,
`bench/probe_wake_model.py`); those files carry their own reasoning. What
remains is the deploy and the couch verification below. **Delete this note once
the soak passes** — at that point the model is just what the rig runs.

Everything about how it was trained (livekit-wakeword on the gaming PC, why
openWakeWord runs it, the eval numbers, the size bake-off) is in commit
`133cb6f`'s message. That is the record; it is not repeated here.

## Deploy

Two values in `k15\config.json`, on the K15:

```json
"wakeModel": "hey_alfred_v1.0",
"wakeThreshold": 0.45
```

Then `.\Start-K15.bat`. Rollback is those two values back and one more
`Start-K15.bat` — `hey_jarvis_v0.1` stays in the venv until the soak passes, on
a machine that is otherwise deaf while a bad model is live.

**0.45 is measured, not eval's number.** livekit's `evaluate.py` hardcodes its
printed `Threshold=` to 0.5 "for consistent comparison" — it is a fixed
comparison point and never a tuned value. 0.45 comes from openWakeWord
measurements on this rig: a 0.87 detection peak on augmented positives against a
0.095 floor over 40 minutes of the actual living room, ~9x separation.

## Verify on the K15, in this order

Each step gates the next.

0. **It loads, costs ~2%, and means the same thing here.** The training box runs
   onnxruntime 1.28.0 and the K15 pins 1.24.4, so this is the one comparison
   the gaming PC could not make against itself:

   ```
   .venv\Scripts\python bench\probe_wake_model.py models\hey_alfred_v1.0.onnx --wav speech.wav --dump k15.json
   ```

   Expect `always-on cost  ~2-3% of one core`. A FAIL against the gaming PC's
   oww dump is an onnxruntime version disagreement, not a bad model.

   **Known and accepted: the cross-runtime compare against LIVEKIT fails for
   every size.** Not a broken export (all three fire at 0.92-0.98 under
   openWakeWord), not an alignment lag (best-fit shift is +0 hops), not
   different feature models (the mel/embedding ONNX are byte-identical), not a
   normalisation mismatch. It is streaming-vs-whole-window mel framing, at
   0.021-0.075 mean per-hop. The consequence is why the threshold above came
   from measurement: **livekit's eval numbers do not transfer to this runtime.**

1. **Blind detection** — `tests\test_wake.py`. It reads `wakeModel` from config
   and derives the spoken phrase, so it follows the deployment. SAPI may
   mispronounce an invented name; a low score here with good live trials is the
   test's limit, not the model's.
2. **`--wake-trials`**, 20x per condition, {movie volume, loud movie} x
   {couch-left, couch-right}. **Pass: >=18/20 in every condition.**
3. **`--false-accept-soak`** through one full ~2 h movie. **Pass: <=1 false
   accept.** This is where a home-trained model fails; it is not optional.
4. **`--dry-run`**, one flowing sentence: confirm the `wake prefix stripped`
   line appears. That proves the keyterm and anchor derivations, not the model.

Steps 2-3 are the existing gate from [voice-testing.md](voice-testing.md) —
same numbers, deliberately, so a new wake word is held to the standard the mic
array was.

## If the soak fails

The levers are all free and all in the runtime already installed:
`vad_threshold` (Silero gate), `patience`/`debounce_time` (N consecutive
frames), openWakeWord's `custom_verifier_models` (a second-stage filter trained
on a few household recordings, addable without retraining anything), and
threshold tuning. Try those before another training run.

If a retrain IS needed, add more living-room negatives first — that is the one
input that moves this model, and `bench/record_room.py` (on the K15) plus
`bench/slice_room.py` (on the gaming PC, after `livekit-wakeword setup`) are
the tools. Both carry the why in their own docstrings. Re-run step 0's parity
probe on every retrain: an exported `.onnx` cannot break underneath you, but a
future livekit release could change the export, and step 0 is the only thing
between that and a crash-looping agent.

## Two constraints on any future phrase

Both come from this repo, not from the library, and both are easy to trip:

- **The filename is the interface.** `voice.wakeModel` is parsed by
  `rsplit("_v", 1)[0].replace("_", " ")` in two places
  ([audio.py](../k15/voice/audio.py), [session_runtime.py](../k15/voice/session_runtime.py)),
  so the file must be `word_word_vX.Y.onnx`.
- **The last word is the strip anchor.** `strip_wake` fuzzy-matches it at >=80,
  leading-only, so it must not collide with a word a command can start with:
  `any, cancel, end, game, go, let, louder, mute, never, quieter, show, softer,
  start, stop, switch, task, thank, that, turn, unmute, what`. ("alfred" vs
  those scores clear; "steam" would score 91 against "stream" and eat a real
  command's first word.)
