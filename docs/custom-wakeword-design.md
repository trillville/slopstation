# Custom wake word — build note

**Status: not built.** Training runs on the gaming PC (4090, native Windows); the
model ships to the K15 by `git pull` and runs on the **existing** openWakeWord
runtime. Delete this note once a custom model is live — the code and its
comments become the record.

The plan below is not the obvious one. The obvious one is "train an
openWakeWord model with the openWakeWord notebook", and it is wrong twice: the
upstream training path is stale and broken, and openWakeWord's own head is the
weaker architecture. What survives from openWakeWord is its *runtime*, which is
excellent and 48× cheaper than the alternative.

## The want

Replace `hey jarvis` with a bespoke phrase, without regressing wake reliability
from the couch or false accepts during a movie.

## What runs where

Three machines are involved and they are easy to confuse, so: nothing in §1–§5
touches the K15, and `livekit-wakeword` is **never installed on the K15**.

| Step | Machine | Why there |
|---|---|---|
| §0 phrase | anywhere | |
| §1–§4 train | gaming PC, native Windows | the 4090 |
| §5 eval gate | gaming PC | livekit's own eval |
| §5b parity | gaming PC | needs BOTH runtimes; keeps the K15 clean |
| §3 room recording | **K15** | it has the mic that has to hear the phrase, in the room |
| §6 ship | gaming PC checkout → git → K15 | |
| §7 code | any checkout | lands before training finishes |
| §8 verify | **K15** | the only place the answer counts |

A training run occupies the gaming PC for hours. It is the TV machine — check
nobody wants the couch first.

## Decisions already made, and the evidence

Measured on the K15 on 2026-08-13, production venv (onnxruntime 1.24.4), same
model and audio through both runtimes, driven at the real 80 ms hop schedule.

| Decision | Why |
|---|---|
| **Train with `livekit-wakeword`** | openWakeWord's last release is v0.6.0 (Feb 2025) and its training notebook is broken (py3.12 / `piper-phonemize`, torchaudio backends, shifted YAML keys). livekit is Apache-2.0, actively developed, one command end to end. |
| **Run on openWakeWord** | livekit's runtime is `stateless`: every call recomputes the full mel and runs the embedding model **16×**. Measured 76.28 ms per 80 ms hop = **95.4% of one core**, p95 170 ms — slower than real time. openWakeWord streams (80 ms of audio is exactly one embedding stride, so its form is exact, not approximate): **1.60 ms/hop, 2.0% of a core**. |
| **`conv_attention` head** | livekit's own default, and the source of their published 0.08 FP/hr vs openWakeWord's 8.50. openWakeWord's DNN head *flattens* (16,96)→1536, discarding temporal structure; conv-attention keeps it. |
| **ONNX, not TFLite** | The `NotImplementedError` blocking conv-attention export is TFLite-only. `export/onnx.py` emits every head as `(batch,16,96) → (batch,1)`, opset 18 — exactly the contract openWakeWord's runtime consumes. |
| **Not Porcupine** | Free-tier AccessKeys were disabled 2026-06-30; paid starts ~$6k/yr. Independently disqualifying: it validates a key at init, which puts an expiring credential in the always-on path. |
| **Not microWakeWord** | Embedded target (ESP32 / tflite-micro). Home Assistant runs openWakeWord server-side and microWakeWord on-device precisely because they are not interchangeable. |

Both runtimes were verified to agree on detection — same model, same audio,
first hop over threshold **10** on both, peak hop **14** on both, scores within
0.003. The 48× is pure bookkeeping, not accuracy.

### Why the same ONNX costs 48× more on one runtime

This number deserves suspicion, so it was taken apart with livekit's *own*
feature extractors (not a cross-library comparison):

| stage | livekit, per hop | openWakeWord, per hop |
|---|---|---|
| mel | 2.51 ms (recomputes the 2 s window) | 0.08 ms (80 ms increment) |
| embeddings | 30.99 ms (**16**, sequential) | 0.80 ms (**1**) |
| total | ~33.5 ms | ~0.88 ms |

Two independent causes:

- **~16× is structural.** A stateless API must recompute all 16 embeddings on
  every call. A streaming one computes the 1 that is new and reuses 15. This is
  inherent to the API shape, not an implementation defect.
- **~4× is implementation.** livekit issues 16 separate ORT calls in a python
  loop; the same 16 windows batched into one call take 5.52 ms instead of
  30.99. Even if upstream fixed that, it would be ~8 ms/hop against
  openWakeWord's 0.88.

This is not livekit doing something foolish — their `_audio_loop` polls
`predict()` on every 80 ms frame with the full 2 s window (and passes
`exception_on_overflow=False` on the mic read, i.e. it expects to fall behind
and drop audio). That is a fine shape on a server. It is the wrong shape for an
always-on mini PC.

### Cross-runtime numerical parity — verified, with one caveat

The plan depends on an interop that livekit's docs **do not claim**: they
document TFLite → openWakeWord for the DNN head, and say nothing about running
their ONNX under openWakeWord. It was therefore verified directly, per hop, on
a real exported conv-attention head:

| | all hops | after warm-up (hop ≥ 5) |
|---|---|---|
| conv_small | max 0.5423 | **max 0.0080**, mean 0.0043 |
| hey_jarvis (control) | max 0.1071 | max 0.1071 |

**The warm-up caveat:** openWakeWord returns exactly `0.0` for its first ~5 hops
(400 ms) while its embedding buffer primes; livekit scores zero-padded audio
instead. That is openWakeWord being the more conservative of the two, it only
happens once per start/`reset()`, and the live wake loop already has this
behavior today.

After warm-up the two agree to 0.008 on conv-attention — tighter than the
trained DNN control, whose residual 0.107 sits entirely on rising/falling
edges, where a sub-hop mel-alignment offset moves a score that is swinging 0→1.
The runtimes are equivalent **up to sub-hop alignment**, not bit-exact, and the
detection decision (first-crossing hop, peak hop, max score) is identical.

That parity is also what makes livekit's `eval` numbers meaningful for this
deployment: a model scored on their runtime behaves the same on ours.

### Model size is not a performance decision

Real conv-attention heads exported through livekit's own exporter, loaded into
the production openWakeWord runtime:

| head | params | file | per hop | % of one core |
|---|---|---|---|---|
| `hey_jarvis` (current, DNN) | — | 1.3 MB | 1.74 ms | 2.2% |
| conv tiny | 7.6k | 120 KB | 1.68 ms | 2.1% |
| conv small | 18.7k | 163 KB | 1.72 ms | 2.2% |
| conv medium | 214k | 956 KB | 1.93 ms | 2.4% |
| conv large | 961k | 3.9 MB | 2.23 ms | 2.8% |

Every size is real-time with two orders of magnitude of headroom. The shared
embedding front-end dominates; the head is noise. **So size is decided by
accuracy alone.** The one caution: these train on 100% synthetic TTS, so a
larger head can fit the synthetic distribution better and your living room
worse. That is an empirical question — §4 trains all three and lets the numbers
answer it. Ops are all standard (`Conv`, `LayerNormalization`, `Softmax`,
`MatMul`, `Gemm`), `ir_version=10`, opset 18 — no gap against the pinned
onnxruntime.

## 0. Choose the phrase

Three constraints come from *this repo*, not from the library:

- **The filename is the interface.** `voice.wakeModel` is parsed by
  `rsplit("_v", 1)[0].replace("_", " ")` in two places
  ([audio.py](../k15/voice/audio.py), [session_runtime.py](../k15/voice/session_runtime.py)),
  so the file must be `word_word_vX.Y.onnx` — words separated by underscores,
  version suffix mandatory. `hey_wintermute_v1.0.onnx` → phrase "hey
  wintermute".
- **The last word is the strip anchor.** `strip_wake` fuzzy-matches it at ≥80,
  leading-only. It must not collide with a word a command can start with:
  `any, cancel, end, game, go, let, louder, mute, never, quieter, show, softer,
  start, stop, switch, task, thank, that, turn, unmute, what`. ("jarvis" vs
  "travis" scores 67 — safe. An anchor like "steam" scores 91 against "stream"
  and would eat a real command's first word.)
- **The phrase becomes a Deepgram keyterm**, so an invented word is an
  advantage: Flux is biased toward one canonical spelling and the strip lands
  every time.

Otherwise the usual: two words, 3–4 syllables, uncommon in normal speech, and
nothing an Echo in earshot answers to.

### You never record the wake phrase

Worth stating plainly, because it is the opposite of how wake words used to
work and §3 below is about recording: **the phrase is text.** `generate`
synthesizes it. Where each piece of training data comes from:

| Training data | Source | Yours? |
|---|---|---|
| ~10,000 positives of your phrase | Piper VITS over 904 LibriTTS speakers — and not merely 904 voices: it SLERP-blends speaker PAIRS at 5 blend weights × 3 speaking rates × prosody noise, so every clip is a distinct synthetic voice | no, just the text |
| phonetic near-misses | `generate_adversarial_phrases` wildcards phonemes and mines CMUDict; invented words are split into known subwords (their example: "livekit" → "live" + "kit") | no |
| ~2000 hrs of general negatives | ACAV100M features + MUSAN | no |
| extra near-misses | `custom_negative_phrases` | optional top-up |
| your living room | §3 | optional, highest value |

The only thing you ever record is the **room** (§3) — background audio, never
the phrase.

**Why that works at all:** the trained head never sees audio. It sees a 16×96
matrix from Google's frozen `speech_embedding` model, which learned a
speaker-invariant representation of speech from a large corpus of *real*
humans. Generalising across pitch, accent, speed and timbre was solved there,
on real data. The head only has to learn "does this trajectory look like the
target phoneme sequence" in an already-good space, which is why a few thousand
synthetic clips suffice. `hey_jarvis`, running on the couch today, was trained
this way.

### The one real risk: pronunciation, not voice

Speaker variation is handled. *Pronunciation* is not. espeak-ng derives
phonemes from the **spelling**, so an invented word can be phonemised as
something you never say — and the model then learns a trajectory you cannot
produce. No amount of speaker diversity fixes the wrong target. Two gates,
both cheap, both before any real time is spent:

**Gate 1 — phonemes.** Needs espeak-ng from §1, so run it after that and
before §4. This is the exact call `synthesis.py` shells out to, verbatim:

```
espeak-ng --ipa -q -v en-us "hey wintermute"
```

**Pass: the IPA is how you actually say it.** If it is not, change the
*spelling* until it is, or add the closer spelling to `target_phrases`. This is
the cheapest five seconds in the project — it is the difference between
training the word you say and a homograph of it.

**Gate 2 — ears.** During §4, between `generate` and `augment`. `generate`
writes real WAVs, so listen to a handful:

```
output\<model_name>\positive_train\clip_000000.wav
```

**Pass: they sound like your phrase, in many different voices.** If they do
not, stop — everything downstream is 50,000 steps of learning the wrong word,
and the first honest signal otherwise would be §8.3 failing from the couch.

This is also the counterweight to "an invented word is an advantage" above:
that advantage is about Deepgram transcribing it canonically, and it trades
directly against phonemisation risk. Prefer a phrase spelled close to how it
sounds.

### If you want your own voice in the positives anyway

Undocumented, but it works: `augment` discovers clips by globbing
`clip_\d{6}\.wav` in the positives directory, with no manifest. Real
recordings dropped in **after `generate` and before `augment`** — named
`clip_010001.wav` onward, 16 kHz mono — are augmented and featurised alongside
the synthetic ones.

Two caveats before reaching for it. A dozen real clips against 10,000 synthetic
ones barely moves the weights unless heavily duplicated; and pushing hard that
way overfits to *your* voice, which is worse for everyone else in the room. The
supported version of "make it work for my voice" is openWakeWord's
`custom_verifier_models` — a second-stage filter trained on a few of your own
recordings, addable later without retraining anything (§9).

`target_phrases` is a **list** for a reason that follows from this: espeak-ng
phonemizes the TEXT, so an invented word's spelling decides its pronunciation.
"hey wintermute" and "hey winter mute" may phonemize differently, and listing
both puts both pronunciations in the positive set. It covers how the
SYNTHESIZER says it, not how you do. Let the filename carry the canonical one.

## 1. Gaming PC — native Windows

**WSL2 is not required.** The usual reason it would be — `piper-phonemize`,
the C extension that breaks the official openWakeWord notebook on Windows —
does not apply: livekit shells out to the `espeak-ng` **binary** instead, and
says so in `data/piper/synthesis.py` ("cross-platform, no C binding issues").
Verified on 2026-08-13: the whole `[train,export,eval]` extra installs from
wheels with no build step, all eight pipeline stages import, nothing shells out
to `sox`/`ffmpeg`, and the dataloader defaults to `num_workers=0` so there is
no Windows spawn hazard.

Two Windows-specific gotchas, both cheap and both silent if missed.

**espeak-ng must be on PATH** — it is looked up with `shutil.which` at
generate time, and its own error message only names the macOS/Linux installs:

```
winget install eSpeak-NG.eSpeak-NG
```

```
espeak-ng --version
```

**With it installed, run §0's Gate 1 now** — before §2 downloads 16 GB. It
costs five seconds and it is the one check that invalidates the phrase itself
rather than a run.

**The default PyPI torch on Windows is CPU-only.** This is the opposite of
Linux and it fails silently — you get a training run that works and takes all
day. Take the current command from
[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)
(the CUDA tag moves), which looks like:

```
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

Then the rest, from a working directory you will keep:

```
mkdir %USERPROFILE%\wake && cd %USERPROFILE%\wake && python -m venv .venv && .venv\Scripts\pip install "livekit-wakeword[train,export,eval]" openwakeword
```

`openwakeword` rides along for the §5b parity check — pure python plus
onnxruntime, it disturbs nothing.

**Verify the card before spending an evening on it.** `nvidia-smi` only proves
the driver; this proves training will use it, and a `+cpu` build here is the
whole point of the index-url above:

```
.venv\Scripts\python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Everything below runs from `%USERPROFILE%\wake`, because `data_dir`/`output_dir`
in the config are **relative to the working directory**. Run the same commands
from somewhere else and you will re-download 16 GB into a second tree.

> Verified: installs and imports on Windows. NOT verified: an end-to-end
> training run on Windows — import is not execution, and the untested surface
> is runtime path handling plus the espeak-ng CLI invocation. If a stage does
> break there, WSL2 remains the fallback (`wsl --install -d Ubuntu`, then
> `sudo apt install -y espeak-ng python3-venv`, and on Linux the default PyPI
> torch already carries CUDA). Nothing else in this plan changes — the
> deliverable is an `.onnx` either way.

## 2. Download the training data

Write the config from §3 first (`setup` reads it), then:

```
livekit-wakeword setup --config wintermute.yaml
```

Pulls the Piper VITS checkpoint, RIRs, the MUSAN noise subset (~1.1 GB), and
the precomputed **ACAV100M negative features (~16 GB, ~2000 hrs)**. With the
generated clips and extracted features on top, **budget ~40 GB free** in the
drive you run this from.

`--skip-acav` fetches validation features only — do not use it for a real run;
those 2000 hours of negatives are most of why the false-accept rate is good.

## 3. The config

Save as `wintermute.yaml`. Only `model_name` and `target_phrases` are required;
everything below is shown with its real default so you know what you are
changing.

```yaml
model_name: hey_wintermute
target_phrases:
  - "hey wintermute"
  - "hey winter mute"        # spelling variants of the same sound

# --- synthetic data ---
n_samples: 10000             # positives; raise for a harder phrase
n_samples_val: 2000
n_background_samples: 200
n_background_samples_val: 40
tts_backend: piper_vits      # or voxcpm (30+ languages, slower, more diverse)
custom_negative_phrases:     # <-- HIGH LEVERAGE, see below
  - "hey winter"
  - "wintermute"
  - "hey computer"

# --- paths ---
data_dir: ./data
output_dir: ./output

augmentation:
  clip_duration: 2.0
  rounds: 1                  # raise to 2-3 for more augmented variety
  background_paths: ["./data/backgrounds"]   # <-- add your living room here
  rir_paths: ["./data/rirs"]

# --- model ---
model:
  model_type: conv_attention
  model_size: small          # tiny | small | medium | large

# --- training ---
steps: 50000
learning_rate: 1.0e-4
max_negative_weight: 1500.0
target_fp_per_hour: 0.2      # the objective the trainer optimises toward
```

### The one step worth doing properly — record the room

Your gate is ≤1 false accept per ~2 h movie. **The single highest-leverage
thing in this whole document is putting your actual living room into the
negative set** — recorded through the K15's own mic, from where it actually
sits, with the TV at real listening volume. That captures the room, the mic and
the TV in one shot, which is exactly the distribution the model fails on.

**On the K15.** Close the voice supervisor window first or two processes fight
over the mic. Save as `k15\voice\record_room.py`, run it, play a
dialogue-heavy film (not music) at your normal volume, and leave the room:

```python
import pyaudio, sys, wave
secs = int(sys.argv[1]); pa = pyaudio.PyAudio()
s = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
            frames_per_buffer=1280)
w = wave.open("room.wav", "wb")
w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
for _ in range(int(16000 / 1280 * secs)):
    w.writeframes(s.read(1280, exception_on_overflow=False))
w.close()
```

```
.venv\Scripts\python record_room.py 2400
```

**One long file is fine** — no need to slice it. The augmenter collects
backgrounds with a recursive `**/*.wav` glob and takes a random crop per clip,
so a single 40-minute WAV becomes thousands of distinct backgrounds. It must be
**16 kHz mono**, which is what the script above writes.

Copy it to the gaming PC alongside MUSAN, then delete it from the repo
checkout — it is a big file and it is not repo material:

```
copy room.wav %USERPROFILE%\wake\data\backgrounds\
```

No architecture choice, model size, or threshold tweak comes close to this. The
model's failure mode is *your* room, so train against it.

Also add near-misses of your own phrase to `custom_negative_phrases` — the
partial forms someone would actually say.

## 4. Train, three sizes

`generate` and `augment` write to `output_dir/model_name/`, keyed by
**`model_name`** and not by size — so generate the data **once** and reuse it
for all three trainings. Do not change `model_name` between sizes or you will
re-synthesise 10k clips each time.

```
livekit-wakeword generate wintermute.yaml
```

**Stop here and run §0's Gate 2** — play a few of
`output\<model_name>\positive_train\clip_000000.wav`. This is the last point
where a phonemisation mistake is cheap to fix; past `augment` you are paying
feature extraction and 50,000 training steps to learn the wrong word, and the
next honest signal is §8.3 failing from the couch.

```
livekit-wakeword augment wintermute.yaml
```

Then per size — edit `model.model_size` in the YAML, and **copy the artifact
out between runs**, because each export overwrites
`output/hey_wintermute/hey_wintermute.onnx`:

```
livekit-wakeword train wintermute.yaml && livekit-wakeword export wintermute.yaml && livekit-wakeword eval wintermute.yaml
```

```
copy output\hey_wintermute\hey_wintermute.onnx hey_wintermute_small_v1.0.onnx
```

Repeat for `medium` and `large`. (`livekit-wakeword run wintermute.yaml` does
generate → augment → train → export → eval in one shot — convenient, but it
re-runs `generate`, so use the staged commands above once you are doing more
than one size.)

**Timing, honestly:** not measured here. `generate` is TTS synthesis of 10k
clips and is largely CPU-bound, so the 4090 does not help much there; `train` is
50k steps on the GPU and is where the card earns its keep. Reference points
from comparable pipelines are ~75–90 min total on a much weaker cloud L4, and
the 4090 should beat that comfortably. Run the first one when you do not need
the TV, and watch what the stages actually cost before planning the other two.

## 5. Gate on the gaming PC before touching the K15

`eval` prints `AUT`, `FPPH`, `Recall`, `Threshold` and writes a DET curve PNG.
This is a free filter that runs against held-out validation data — use it to
kill bad runs before spending a movie on a soak.

- **FPPH** — livekit's own conv-attention reference is 0.08. Anything above ~1
  will not survive your living room.
- **Recall** — their reference is 86.1%.
- **Threshold** — the value eval picked; carry it to `voice.wakeThreshold`
  rather than assuming 0.5.

Pick the best one or two sizes on these numbers. Take a second size to the K15
as the A/B — the CPU cost is a rounding error, so there is no reason not to.

> Caveat worth holding: their validation set is not your living room. These
> numbers rank candidates; they do not predict your false-accept rate. Only §8
> does that.

## 5b. Parity — still on the gaming PC

Do this before shipping anything. It is the one undocumented link in the chain
(§ *Cross-runtime numerical parity*), and it is cheap here because both
runtimes are already installed. `bench/probe_wake_model.py` is in the repo;
copy it and one speech WAV into `%USERPROFILE%\wake`, then:

```
.venv\Scripts\python probe_wake_model.py hey_wintermute_small_v1.0.onnx --engine livekit --wav speech.wav --dump lk.json
```

```
.venv\Scripts\python probe_wake_model.py hey_wintermute_small_v1.0.onnx --engine oww --wav speech.wav --dump ow.json
```

```
.venv\Scripts\python probe_wake_model.py --compare lk.json ow.json
```

**Pass = `PASS - peaks within 0.02, firing hop within 1`,** exit 0. It asserts
on peak score and firing hop rather than per-hop scores deliberately: the
runtimes agree only up to a sub-hop mel alignment, so on a rising edge the
shipped `hey_jarvis` legitimately differs by ~0.1 between them. A per-hop
assertion rejects good models; the wake loop compares a peak to a threshold, so
that is what has to transfer.

A FAIL here means the export is not transferring and **every eval number from
§5 is void for the K15**. Stop and fix it rather than shipping.

## 6. Ship it to the K15

The K15 runs from a git clone, so the model travels by git. It is a
hand-trained artifact with no upstream to re-fetch, so it gets vendored —
unlike the pretrained binaries `.gitignore` currently excludes.

Into the checkout (adjust to wherever the repo lives on the gaming PC):

```
copy %USERPROFILE%\wake\hey_wintermute_small_v1.0.onnx <repo>\k15\voice\models\hey_wintermute_v1.0.onnx
```

`.gitignore` already tracks `*.onnx` in that directory and ignores everything
else (§7), so the file is committable as-is. Commit from the gaming PC's
checkout, then `git pull` on the K15. Verify it actually arrived — the agent
exits 1 on a missing model, and a `.onnx` that got ignored is a silent
failure until then:

```
git status --porcelain k15/voice/models/
```

## 7. Code changes — LANDED

The inference side is done and green; the model is the only missing piece.
What changed, and why it is not obvious:

- **[audio.py](../k15/voice/audio.py)** — `WakeListener._resolve_model()`
  replaces `_ensure_model()`. It resolves `voice/models/` first, then
  openWakeWord's package dir, and passes a **path** to `Model()`. The path is
  the point: a bare name resolves only against openWakeWord's six official
  models and raises `ValueError` on anything else, which behind the supervisor
  is a crash loop every 10 s rather than a message. Sets `model_source`
  (`vendored`/`pretrained`) so `agent_up` says which copy answered.
  - **The subtle one:** the mel/embedding extractors ship *outside* the wheel,
    and `download_models` is what fetches them. Returning a vendored model
    early skips that call, so on a rebuilt venv a perfectly good custom model
    would load against nothing. The extractor top-up therefore runs *before*
    the vendored branch. Verified against a package dir emptied of models.
  - A model that resolves nowhere logs `wake_model_missing` with both paths
    tried and raises `FileNotFoundError`; `voice_agent.main` turns that into a
    clean exit 1 rather than a traceback under every supervisor restart.
- **[doctor.py](../k15/doctor.py)** — checks `voice\models\` then the venv, in
  the same order the agent resolves, so it answers which copy *would* load. A
  missing custom model gets a remedy line saying it has no upstream.
- **[test_wake.py](../k15/voice/tests/test_wake.py)** — reads `wakeModel` from
  config (falling back to `config.example.json`) and derives the spoken phrase
  from it, so the blind suite follows the deployment instead of pinning
  `hey_jarvis` forever.
- **[.gitignore](../.gitignore)** — `k15/voice/models/*` plus
  `!k15/voice/models/*.onnx`. Contents, not the directory: git cannot
  re-include a file whose parent directory is excluded, so the old
  `k15/voice/models/` rule would have silently beaten the negation.
- **[bench/probe_wake_model.py](../k15/voice/bench/probe_wake_model.py)** — new,
  the harness §5b and §8.0 run.

**Still yours to do when the model lands:** `config.json` — `voice.wakeModel`
to the new stem, `voice.wakeThreshold` to eval's chosen threshold.

Keep `openwakeword` in `requirements.txt` and leave `hey_jarvis_v0.1.onnx` in
the venv until the new model clears §8. Rollback is then one config value and a
`Start-K15.bat`, on a machine that is otherwise deaf.

## 8. Verify on the K15

In escalating order, each step gating the next:

0. **It loads, costs ~2%, and agrees with the machine that trained it.** Same
   tool, same WAV as §5b (copy `speech.wav` over with the model), from
   `k15\voice`:

   ```
   .venv\Scripts\python bench\probe_wake_model.py models\hey_wintermute_v1.0.onnx --wav speech.wav --dump k15.json
   ```

   Expect `always-on cost  ~2-3% of one core`. Then compare against the
   gaming PC's **oww** dump — this is the one thing §5b cannot check, because
   the K15 pins onnxruntime 1.24.4 and the training box will have something
   newer:

   ```
   .venv\Scripts\python bench\probe_wake_model.py --compare ow.json k15.json
   ```

   A FAIL here is an onnxruntime version disagreement, not a bad model.
2. **Blind detection** — `test_wake.py` against the new model. SAPI speaks the
   phrase, negatives must stay quiet. An invented name may be mispronounced by
   SAPI; if it scores low here but fine live, that is the test's limit.
3. **`--wake-trials`**, 20× per condition, {movie volume, loud movie} ×
   {couch-left, couch-right}. **Pass: ≥18/20 in every condition.** Tune
   `wakeThreshold` from the logged scores.
4. **`--false-accept-soak`** through one full ~2 h movie. **Pass: ≤1 false
   accept.** This is where a home-trained model fails; it is not optional.
5. **`--dry-run`**, one flowing sentence: confirm the `wake prefix stripped`
   line appears. That proves the keyterm and anchor derivations, not the model.

The gate in steps 3–4 is the existing one from
[voice-testing.md](voice-testing.md) — same numbers, deliberately, so a new
wake word is held to the standard the mic array was.

## 9. What would falsify this plan

- **Closed:** conv-attention op coverage on onnxruntime 1.24.4 (measured, all
  four sizes), cross-runtime numerical parity (measured per hop, 0.008 after
  warm-up), the 48× cost gap (decomposed into a 16× structural and a 4×
  implementation cause), Windows/livekit install (pure-python wheel, numpy +
  onnxruntime only).
- **Known risk, accepted:** ONNX → openWakeWord interop is **undocumented
  upstream**. It is verified here, and an exported `.onnx` is a static artifact
  that cannot break underneath you — but a *future* livekit release could
  change the export (a custom op, a changed input signature) and the only thing
  standing between that and a crash-looping agent is step 8.0. Re-run parity on
  every retrain, not just the first.
- **Open:** a full training run has not been done here. Installs and imports
  are verified on Windows; execution is not.
- **Open:** whether livekit's eval FPPH predicts this living room. It almost
  certainly under-predicts, since their negatives are not your TV. §8.4 is the
  only real answer.
- **If the soak fails at every size:** the levers before re-architecting are all
  free and all in the runtime you already have — `vad_threshold` (Silero gate),
  `patience`/`debounce_time` (N consecutive frames), a custom verifier for
  household voices, and threshold tuning. Try those before another training
  run, and add more living-room negatives before trying a different head.
