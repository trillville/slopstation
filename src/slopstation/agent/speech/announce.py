"""Proactive spoken announcements OUTSIDE any session: a finished operation
plays the announce earcon and speaks its summary. Two gates:
session_active (the pipeline owns the speaker, so wait for session close - a
mid-session retrieval may consume the result first) and abort (a wake word kills
playback; the operation stays pending).

Playback opens its own short-lived PyAudio world, never touching the wake
listener's. Synth failure fail-softs to earcon-only and pending."""

import json
import queue
import threading
import time
import urllib.request
from typing import Any

from slopstation.agent.speech import audio, earcons

CHUNK = 3200  # 100 ms per write; abort latency bound


def synth(text, api_key, voice_model):
    """Aura-2 REST -> raw linear16 PCM at earcons.SAMPLE_RATE. Raises on any
    HTTP/network problem; the caller fail-softs."""
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/speak"
        f"?model={voice_model}&encoding=linear16"
        f"&sample_rate={earcons.SAMPLE_RATE}&container=none",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


class Announcer:
    """Thread owning the queue; store is attached after construction."""

    def __init__(self, voice_cfg, secrets, log):
        self.voice = voice_cfg
        self.secrets = secrets
        self.log = log
        self.store: Any = None  # the OperationStore, attached by main()
        self.session_active = threading.Event()
        self.abort = threading.Event()
        # Set after a bulletin heard in full; the wake loop takes it as "open
        # a session now". Config voice.followUpAfterAnnounce.
        self.follow_up = threading.Event()
        self.follow_up_enabled = voice_cfg["followUpAfterAnnounce"]
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="announcer").start()

    def submit(self, operation):
        """OperationStore terminal hook, called off-thread."""
        self._q.put(("terminal", operation["id"], None))

    def submit_notification(self, notification):
        """OperationStore lifecycle hook, called off-thread."""
        self._q.put(("notification", notification["operation_id"], notification["key"]))

    def speak(self, text):
        """Earcon + one synthesized line, outside any session. True = played
        to the end (a wake or a session start cuts it short)."""
        pcm = synth(text, self.secrets["deepgramApiKey"], self.voice["ttsVoice"])
        return self._play(earcons.pcm("announce") + pcm)

    def abort_current(self):
        self.abort.set()

    # -- internals ------------------------------------------------------------

    def _output_index(self, pa):
        """Output device index on a FRESH pa - resolving against an old
        snapshot is the deafness bug audio.py exists for. The only
        required=False caller: a missing speakerphone falls back to the
        default rather than blocking operation delivery."""
        return audio.resolve_device(
            pa,
            self.voice.get("outputDeviceName"),
            want_input=False,
            log=None,
            required=False,
        )

    def _play(self, pcm):
        """Own PyAudio world per announcement; chunked writes so abort and a
        session start cut playback within ~100 ms. True if it all played."""
        import pyaudio

        pa = pyaudio.PyAudio()
        try:
            s = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=earcons.SAMPLE_RATE,
                output=True,
                output_device_index=self._output_index(pa),
            )
            try:
                for i in range(0, len(pcm), CHUNK):
                    if self.abort.is_set() or self.session_active.is_set():
                        return False
                    s.write(pcm[i : i + CHUNK])
            finally:
                s.stop_stream()
                s.close()
            return True
        finally:
            pa.terminate()

    def stop(self) -> None:
        """End the announcer thread once its queue has drained. A lane runs it
        for the life of the process; the tests do not."""
        self._q.put(None)

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            kind, operation_id, key = item
            self.abort.clear()
            # Session owns the speaker; a mid-session retrieval may mark the
            # operation acknowledged while we wait.
            while self.session_active.is_set():
                time.sleep(0.5)
            if kind == "terminal":
                item = next(
                    (
                        o
                        for o in (
                            self.store.pending_announcements() if self.store else []
                        )
                        if o["id"] == operation_id
                    ),
                    None,
                )
            else:
                item = next(
                    (
                        o
                        for o in (
                            self.store.pending_notifications() if self.store else []
                        )
                        if o["operation_id"] == operation_id and o["key"] == key
                    ),
                    None,
                )
            if item is None:
                continue  # already heard via pull
            try:
                done = self.speak(item["summary"])
            except Exception as e:
                # Earcon alone still means "news"; operation stays pending.
                self.log.warn(
                    "announce_failed",
                    operation=operation_id,
                    err=str(e),
                    fallback="earcon",
                )
                try:
                    self._play(earcons.pcm("announce"))
                except OSError as e2:
                    self.log.error(
                        "announce_earcon_failed", operation=operation_id, err=str(e2)
                    )
                continue
            if done:
                if kind == "terminal":
                    self.store.mark_delivered(operation_id)
                else:
                    self.store.mark_notification_delivered(operation_id, key)
                self.log("operation_announced", operation=operation_id)
                if self.follow_up_enabled:
                    # Only after a bulletin heard in FULL.
                    self.follow_up.set()
            else:
                self.log("announce_cut_short", operation=operation_id)
