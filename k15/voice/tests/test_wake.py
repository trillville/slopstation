"""Blind test (C1 s2): the wake model detects SYNTHESIZED speech - no mic,
no human. Windows SAPI speaks "hey jarvis" in two different voices (oWW was
trained on synthetic TTS, so this is a legitimate detection test), plus
negative phrases that must NOT fire. Downloads the oWW models on first run.
    .venv\\Scripts\\python tests\\test_wake.py
"""
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from voice_agent import WakeListener

VOICE_CFG = {"wakeModel": "hey_jarvis_v0.1"}
CHUNK = WakeListener.CHUNK

CASES = [
    ("Microsoft David Desktop", "hey jarvis", True),
    ("Microsoft Zira Desktop", "hey jarvis", True),
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
