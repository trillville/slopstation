"""Make a destructive tool ask before it acts, and honour a yes once."""

import time

# How long an unanswered question stays armed. Ten minutes suits every
# interface: a yes relayed through claude.ai and a phone still lands, and a
# question the user walked away from does not. One number to tune.
ASK_TTL_S = 600


class ConfirmGate:
    """The shared confirmation for destructive tools.

    A destructive tool calls `confirmed(scope, turn)` before it acts. The
    first call on a scope records the ask and answers False: the tool then
    returns its question and does nothing. The same call from a LATER turn,
    inside the lifetime, answers True and the tool acts. A repeat inside the
    same turn is refused again, so the model cannot confirm itself, and an
    ask older than the lifetime is treated as declined and asked afresh.
    Nothing here reads the user's answer: a no is the model's to honour by
    not calling again.
    """

    def __init__(self, now=time.time):
        self._now = now
        # scope -> (turn that asked, when)
        self._pending: dict[tuple, tuple[str | None, float]] = {}

    def confirmed(self, scope: tuple, turn: str | None) -> bool:
        asked_turn, asked_at = self._pending.get(scope, (None, 0.0))
        stale = self._now() - asked_at > ASK_TTL_S
        if asked_turn is None or asked_turn == turn or stale:
            self._pending[scope] = (turn, self._now())
            return False
        self._pending.pop(scope, None)
        return True

    def pending(self, scope: tuple) -> bool:
        return scope in self._pending
