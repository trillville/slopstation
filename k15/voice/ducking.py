"""Room ducking: drop the soundbar for the length of a voice session, put it
back on close. TvDucker is the whole of it - its docstring carries the two
incidents that shaped it. Its own file by README's extraction rule (a): its
own incident history and failure domain, a stateful ledger that spans
sessions, and no relation to the Result-returning side effects dispatch.py is
for (it was 31% of that file under a docstring that never mentioned it).
Constructed once per agent process by voice_agent; every call made from the
wake loop's duck() thread under its lock.

Voice lane: the write path needs samsungtvws (tv_remote.py, lazy), so none
of this can live in the chord-lane tv.py that holds the readback it verifies
against.
"""
import time

import tv


class TvDucker:
    """Drop the room's volume for the length of a voice session; put it back
    on close. One per agent process (the ledger spans sessions), every call
    made from the wake loop's duck() thread under its lock; synchronous.

    Ducking is the one lever that attacks the actual problem - a talker on
    the couch reaches the mic 10-20 dB BELOW dialogue from the TV, and no
    model or threshold recovers a signal that far under. It cannot help the
    wake word (nothing knows to duck until it has fired) but it hands the
    STT a quiet room for every command after it.

    Two incidents shaped this class, one mechanism each:

    - 2026-08-16, duckSteps' first morning: every Ex-Link burst fired at a
      TV that was never on - into standby ahead of a cold launch, into the
      middle of a wake the set was refusing - and the morning ended with the
      only receiver silence on record and an unduck abandoned on its first
      frame. Hence THE GATE: duck() asks the TV whether it is ON first
      (tv.tv_power_state answers from standby); anything else, unknown
      included, means skip. Skipping is always safe - the whole cost is one
      session of loud TV.
    - 2026-08-21, the eARC discovery: with audio on the soundbar the TV
      ACKS every direct volume write and refuses it on screen, so send-and-
      hope is not just fragile, it can be theater. Hence THE READBACK:
      writes are remote keys relayed over CEC (tv_remote.press - the one
      thing the eARC path honours, benched same day), and the TV's
      pairing-free UPnP volume (tv.tv_volume - it mirrors the BAR's
      level) is ground truth for what actually happened. The ledger holds
      only VERIFIED movement, so restore restores exactly what moved, a
      shortfall carries as debt the next session's close pays off, and a
      human working the remote mid-session is DETECTED (the readback is not
      where we left it) - the duck stands down rather than stomp their
      choice.

    The ledger dies with the process; that gap (restart between duck and
    unduck) is documented at the caller and unchanged."""

    TOPUPS = 2            # extra key rounds when the readback comes up short
    POLLS = 6             # readback polls per settle, POLL_GAP_S apart
    POLL_GAP_S = 0.4

    def __init__(self, steps, tv_ip, log, dry_run=False, to_pct=None,
                 probe=None, read=None, press=None, pause=time.sleep):
        # to_pct (1-99) wins over steps: duck TO that percent of the
        # pre-duck level, so the drop scales with how loud the room
        # actually is. Only expressible at all because the readback exists
        # - a blind channel cannot take a fraction of a level it cannot
        # read, which is why the Ex-Link design never had this knob.
        self.steps = int(steps)
        self.to_pct = int(to_pct) if to_pct else None
        self.log = log
        self.dry_run = dry_run
        self.probe = probe or (lambda: tv.tv_power_state(tv_ip))
        self.read = read or (lambda: tv.tv_volume(tv_ip))
        self.press = press or self._ws_press(tv_ip)
        self.pause = pause
        self.out = 0        # verified steps down, not yet restored
        self.expect = None  # the readback our last op left behind

    @staticmethod
    def _ws_press(tv_ip):
        def go(direction, n):
            import tv_remote            # lazy: samsungtvws lives in the venv
            tv_remote.TvRemote(tv_ip).press(direction, n)
        return go

    def _settle(self, target):
        """Poll the readback toward target; the relay lands in ~1 s."""
        for _ in range(self.POLLS):
            v = self.read()
            if v == target:
                return v
            self.pause(self.POLL_GAP_S)
        return self.read()

    def _drive(self, direction, now, target):
        """Press toward target, verify by readback, top up what got lost.
        Returns the last level actually SEEN - `now` if nothing verified,
        which is the honest answer when keys or readback die mid-drive: a
        press that raises still counts for nothing until the readback moves.
        Bounded rounds, so a dead relay gets a couple of bursts, never a
        storm (the 08-16 lesson, kept)."""
        best = now
        for _ in range(1 + self.TOPUPS):
            need = abs(target - best)
            if not need:
                break
            try:
                self.press(direction, need)
            except Exception as e:
                self.log.warn("tv_duck_failed", stage="press", err=str(e))
            final = self._settle(target)
            if final is None:
                break
            if final == best:
                break                   # keys verifiably bought nothing: stop
            best = final
        return best

    def duck(self):
        state = self.probe()
        if state != "on":
            self.log("tv_duck_skipped", state=state or "unknown",
                     debt=self.out)
            return
        v0 = self.read()
        if v0 is None:
            self.log("tv_duck_skipped", state="on", reason="no_readback",
                     debt=self.out)
            return
        if self.to_pct:
            target = min(v0, round(v0 * self.to_pct / 100))
            asked = v0 - target        # scales with v0; never clamps below 0
        else:
            target = max(0, v0 - self.steps)
            asked = self.steps         # flat; the 0-clamp shows as landed<asked
        if self.dry_run:
            self.log("dry_run_would", action=f"duck vol {v0}->{target}")
            self.out += v0 - target
            self.expect = target
            return
        final = self._drive("down", v0, target)
        landed = max(0, v0 - final)
        self.out += landed
        self.expect = final
        self.log("tv_ducked", steps=landed, asked=asked, vol=final,
                 ok=final == target)

    def unduck(self):
        """Restore everything on the ledger - this session's duck plus any
        debt an earlier shortfall left behind. Quiet when the ledger is
        empty (a skipped duck owes nothing)."""
        if not self.out:
            return
        want = self.out
        if self.dry_run:
            self.log("dry_run_would", action=f"unduck +{want}")
            self.out, self.expect = 0, None
            return
        now = self.read()
        if now is None:
            # TV gone (off, DMR asleep with the panel): keys would not
            # relay anyway. Keep the debt; the next close retries.
            self.log("tv_unducked", steps=0, asked=want, ok=False,
                     reason="no_readback")
            self.log.warn("tv_duck_deficit", steps=self.out)
            return
        if self.expect is not None and now != self.expect:
            # A human moved the volume mid-session. They have chosen a
            # level; adding our delta back lands ABOVE their choice. Only
            # the readback makes this detectable - stand down, owe nothing.
            self.log("tv_unducked", steps=0, asked=want, ok=True,
                     reason="user_adjusted", vol=now)
            self.out, self.expect = 0, None
            return
        target = min(100, now + want)
        final = self._drive("up", now, target)
        restored = max(0, final - now)
        self.out = max(0, self.out - restored)
        self.expect = final if self.out else None
        self.log("tv_unducked", steps=restored, asked=want, vol=final,
                 ok=self.out == 0)
        if self.out:
            self.log.warn("tv_duck_deficit", steps=self.out)
