"""Proactive spoken announcements OUTSIDE any session: a finished background
job plays a rising two-note earcon and speaks its summary immediately - movies
included, deliberately. The two gates:

- session_active: the pipeline owns the speaker during a session, so an
  announcement arriving mid-session waits for session close (seconds, not
  minutes - and a mid-session "what did you find" may consume it first).
- abort: a wake word mid-announcement kills playback instantly (user intent
  wins); the job stays unread, so the next-wake mention still surfaces it.

Synthesis is one Aura-2 REST call (stdlib urllib, no SDK, no socket held);
playback opens its own short-lived PyAudio world, never touching the wake
listener's. Synth failure fail-softs to earcon-only + unread - the offline
answer is the pull path, not a local TTS stack."""
import json
import queue
import threading
import time
import urllib.request

import audio
import earcons

CHUNK = 3200                    # 100 ms per write; abort latency bound


def synth(text, api_key, voice_model):
    """Aura-2 REST -> raw linear16 PCM at earcons.SAMPLE_RATE. Raises on any
    HTTP/network problem; the caller fail-softs."""
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/speak"
        f"?model={voice_model}&encoding=linear16"
        f"&sample_rate={earcons.SAMPLE_RATE}&container=none",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Authorization": f"Token {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


class Announcer:
    """Thread owning the announce queue. jobs is attached after construction
    (the store needs the announcer as its on_done hook first)."""

    def __init__(self, voice_cfg, secrets, log):
        self.voice = voice_cfg
        self.secrets = secrets
        self.log = log
        self.jobs = None                        # attached by main()
        self.session_active = threading.Event()
        self.abort = threading.Event()
        # Set after a bulletin the user actually heard in full: the wake loop
        # takes it as "open a session now", so the obvious follow-up needs no
        # wake word. Config voice.followUpAfterAnnounce.
        self.follow_up = threading.Event()
        self.follow_up_enabled = voice_cfg["followUpAfterAnnounce"]
        self._q = queue.Queue()
        threading.Thread(target=self._run, daemon=True,
                         name="announcer").start()

    def submit(self, job):
        """jobs.on_done hook - called off-thread when a job finishes."""
        self._q.put(job["id"])

    def speak(self, text):
        """Earcon + one synthesized line, outside any session. True = it
        played to the end (a wake or a session start cuts it short). The
        whole out-of-session audio path in one call, so --announce-test
        rehearses exactly what a finished job will do."""
        pcm = synth(text, self.secrets["deepgramApiKey"],
                    self.voice["ttsVoice"])
        return self._play(earcons.pcm("announce") + pcm)

    def abort_current(self):
        self.abort.set()

    # -- internals ------------------------------------------------------------

    def _output_index(self, pa):
        """Output device index on a FRESH pa - resolving against an old
        snapshot is the deafness bug audio.py exists for."""
        return audio.resolve_device(pa, self.voice.get("outputDeviceName"),
                                    want_input=False, log=None)

    def _play(self, pcm):
        """Own PyAudio world per announcement; chunked writes so abort and a
        session start can cut playback within ~100 ms. Returns True if the
        whole thing played."""
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1,
                        rate=earcons.SAMPLE_RATE, output=True,
                        output_device_index=self._output_index(pa))
            try:
                for i in range(0, len(pcm), CHUNK):
                    if self.abort.is_set() or self.session_active.is_set():
                        return False
                    s.write(pcm[i:i + CHUNK])
            finally:
                s.stop_stream()
                s.close()
            return True
        finally:
            pa.terminate()

    def _run(self):
        while True:
            job_id = self._q.get()
            self.abort.clear()
            # Session owns the speaker: wait it out (a mid-session retrieval
            # may mark the job read meanwhile - then there's nothing to say).
            while self.session_active.is_set():
                time.sleep(0.5)
            job = next((j for j in (self.jobs.unread() if self.jobs else [])
                        if j["id"] == job_id), None)
            if job is None:
                continue                        # already heard via pull
            try:
                done = self.speak(job["summary"])
            except Exception as e:
                # Offline, dead key, dead output device: say SOMETHING (the
                # earcon alone still means "news"), keep the job unread, and
                # let the next wake mention it.
                self.log.warn("announce_failed", job=job_id, err=str(e),
                              fallback="earcon")
                try:
                    self._play(earcons.pcm("announce"))
                except OSError as e2:
                    self.log.error("announce_earcon_failed", job=job_id, err=str(e2))
                continue
            if done:
                self.jobs.mark_read(job_id)
                self.log("job_announced", job=job_id)
                if self.follow_up_enabled:
                    # Only after a bulletin heard in FULL - opening the mic
                    # off an announcement nobody heard is just an open mic.
                    self.follow_up.set()
            else:
                self.log("announce_cut_short", job=job_id)
