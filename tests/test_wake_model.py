"""Test the configured wake model with synthesized positive and negative speech."""

import subprocess
import wave

import numpy as np

import helpers
from slopstation.agent.speech.audio import WakeListener, wake_phrase

CHUNK = WakeListener.CHUNK
MODEL = helpers.CONFIG["voice"]["wakeModel"]  # the suite's config


def cases(phrase):
    """(SAPI voice, text, is the wake phrase)."""
    return [
        ("Microsoft David Desktop", phrase, True),
        ("Microsoft Zira Desktop", phrase, True),
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
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True
    )


def max_score(listener, path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    listener.model.reset()
    best = 0.0
    pcm = np.concatenate([pcm, np.zeros(CHUNK, np.int16)])  # flush tail
    for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
        best = max(best, listener.score_chunk(pcm[i : i + CHUNK]))
    return best


def test_wake(tmp_path):
    # audio.wake_phrase, the production derivation: the filename IS the
    # phrase, so an off-convention model name breaks here loudly.
    phrase = wake_phrase(MODEL)
    listener = WakeListener(None, {"wakeModel": MODEL}, None)  # model only; no mic
    pos, neg = [], []
    for i, (voice, text, is_wake) in enumerate(cases(phrase)):
        p = tmp_path / f"case{i}.wav"
        synth(voice, text, p)
        s = max_score(listener, p)
        (pos if is_wake else neg).append(s)

    assert min(pos) > 0.30, f"a wake sample scored only {min(pos):.3f}"
    assert max(neg) < 0.15, f"a negative scored {max(neg):.3f}"
    assert min(pos) > 3 * max(max(neg), 0.01), "wake/negative separation too thin"
