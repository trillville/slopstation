"""Lower room volume during a conversation and restore verified movement."""

from slopstation.tv import Tv


class TvDucker:
    """Drop the room's volume for a voice session; restore it on close. A
    talker on the couch reaches the mic 10-20 dB below TV dialogue.

    The TV interface verifies volume changes. The ledger holds only measured
    movement, so a shortfall carries as debt to the next close. A human moving the remote
    mid-session is detected. It dies with the process."""

    def __init__(
        self,
        steps,
        device: Tv,
        log,
        dry_run=False,
        to_pct=None,
    ):
        # to_pct (1-99) wins over steps: duck TO that percent of the pre-duck
        # level, so the drop scales with how loud the room is.
        self.steps = int(steps)
        self.to_pct = int(to_pct) if to_pct else None
        self.tv = device
        self.log = log
        self.dry_run = dry_run
        self.out = 0  # verified steps down, not yet restored
        self.expect: int | None = None  # the readback our last op left behind

    def duck(self):
        with self.tv.volume_transaction():
            self._duck()

    def _duck(self):
        state = self.tv.power_state()
        if state != "on":
            self.log("tv_duck_skipped", state=state or "unknown", debt=self.out)
            return
        # Owed a duck already: the last close could not reach the set, so the
        # bar is still that far below the baseline - as far as a fresh duck
        # would take it. Ducking again lands on the 0-clamp (silence);
        # repaying first swings the room up and back down at the wake. Leave
        # it, and let the close restore the whole ledger.
        if self.out:
            self.log(
                "tv_duck_skipped", state="on", reason="already_ducked", debt=self.out
            )
            return
        v0 = self.tv.volume()
        if v0 is None:
            self.log("tv_duck_skipped", state="on", reason="no_readback", debt=self.out)
            return
        if self.to_pct:
            target = min(v0, round(v0 * self.to_pct / 100))
            asked = v0 - target  # scales with v0; never clamps below 0
        else:
            target = max(0, v0 - self.steps)
            asked = self.steps  # flat; the 0-clamp shows as landed<asked
        if self.dry_run:
            self.log("dry_run_would", action=f"duck vol {v0}->{target}")
            self.out += v0 - target
            self.expect = target
            return
        final = self.tv.set_volume(target).after
        landed = max(0, v0 - final)
        self.out += landed
        self.expect = final
        self.log("tv_ducked", steps=landed, asked=asked, vol=final, ok=final == target)

    def unduck(self):
        """Restore the ledger: this session's duck plus any earlier debt."""
        with self.tv.volume_transaction():
            self._unduck()

    def _unduck(self):
        if not self.out:
            return
        want = self.out
        if self.dry_run:
            self.log("dry_run_would", action=f"unduck +{want}")
            self.out, self.expect = 0, None
            return
        now = self.tv.volume()
        if now is None:
            # Cannot verify a restore. Keep the debt; the next close retries.
            self.log("tv_unducked", steps=0, asked=want, ok=False, reason="no_readback")
            self.log.warn("tv_duck_deficit", steps=self.out)
            return
        if self.expect is not None and now != self.expect:
            # A human moved the volume mid-session: adding our delta back
            # would land above their chosen level. Stand down.
            self.log(
                "tv_unducked",
                steps=0,
                asked=want,
                ok=True,
                reason="user_adjusted",
                vol=now,
            )
            self.out, self.expect = 0, None
            return
        target = min(100, now + want)
        final = self.tv.set_volume(target).after
        restored = max(0, final - now)
        self.out = max(0, self.out - restored)
        self.expect = final if self.out else None
        self.log("tv_unducked", steps=restored, asked=want, vol=final, ok=self.out == 0)
        if self.out:
            self.log.warn("tv_duck_deficit", steps=self.out)
