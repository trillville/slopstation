"""Blind test: the wake model detects SYNTHESIZED speech - no mic, no human.
Windows SAPI speaks the CONFIGURED wake phrase in two voices (oWW trains on
synthetic TTS), plus negatives that must NOT fire. Follows config.json rather
than pinning hey_jarvis; the model ships in from the gaming PC. Downloads the
oWW models on first run. SAPI may mispronounce an invented phrase, so a low
score here alongside good live trials is the test's limit, not the model's.
    .venv\\Scripts\\python tests\\test_wake.py
"""
import json
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

import _bootstrap  # noqa: F401
import cglib
from audio import WakeListener, wake_phrase


def deployed_model():
    """config.example.json is the fallback so a fresh checkout still runs."""
    for path in (cglib.BASE / "config.json", cglib.BASE / "config.example.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))["voice"]["wakeModel"]
        except (OSError, ValueError, KeyError):
            continue
    return "hey_jarvis_v0.1"


MODEL = deployed_model()
# audio.wake_phrase, the production derivation: the filename IS the phrase, so
# an off-convention model name breaks here loudly.
PHRASE = wake_phrase(MODEL)
VOICE_CFG = {"wakeModel": MODEL}
CHUNK = WakeListener.CHUNK

CASES = [
    ("Microsoft David Desktop", PHRASE, True),
    ("Microsoft Zira Desktop", PHRASE, True),
    ("Microsoft David Desktop", "hello world", False),
    ("Microsoft Zira Desktop", "what time is it", False),
    ("Microsoft David Desktop", "start the session now please", False),
]


def synth(voice, text, path):
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SelectVoice('{voice}'); "
        f"$s.SetOutputToWaveFile('{path}', $f); "
        f"$s.Speak('{text}'); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True)


def max_score(listener, path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    listener.model.reset()
    best = 0.0
    pcm = np.concatenate([pcm, np.zeros(CHUNK, np.int16)])   # flush tail
    for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
        best = max(best, listener.score_chunk(pcm[i:i + CHUNK]))
    return best


def main():
    listener = WakeListener(None, VOICE_CFG, None)   # model only; no mic
    print(f"  model: {MODEL} ({listener.model_source}) -> phrase '{PHRASE}'")
    tmp = Path(tempfile.mkdtemp())
    pos, neg = [], []
    for i, (voice, text, is_wake) in enumerate(CASES):
        p = tmp / f"case{i}.wav"
        synth(voice, text, p)
        s = max_score(listener, p)
        (pos if is_wake else neg).append(s)
        print(f"  {'WAKE' if is_wake else 'neg '} '{text}' ({voice.split()[1]}): {s:.3f}")

    assert min(pos) > 0.30, f"a wake sample scored only {min(pos):.3f}"
    assert max(neg) < 0.15, f"a negative scored {max(neg):.3f}"
    assert min(pos) > 3 * max(max(neg), 0.01), "wake/negative separation too thin"
    print(f"OK - wake detection: positives >= {min(pos):.2f}, "
          f"negatives <= {max(neg):.2f} (threshold 0.5 default sits between)")


if __name__ == "__main__":
    main()
