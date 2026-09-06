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
    inside the lifetime, answers True and the tool acts, then calls `done` so
    the ask is spent. The ask stays armed until then: a transient failure
    after the yes can be retried without asking again. A repeat inside the
    same turn is refused, so the model cannot confirm itself, and an ask
    older than the lifetime is treated as declined and asked afresh. Nothing
    here reads the user's answer: a no is the model's to honour by not
    calling again.

    A call with no turn id never confirms, on purpose: every live lane (the
    voice grammar, the text interface) mints one per utterance, and the one
    caller without it, the dry-run REPL, answers before the gate. Failing
    closed there is what keeps an untagged path from acting.
    """

    def __init__(self, now=time.time):
        self._now = now
        # scope -> (turn that asked, when)
        self._pending: dict[tuple, tuple[str | None, float]] = {}

    def _prune(self):
        cutoff = self._now() - ASK_TTL_S
        for scope in [s for s, (_, at) in self._pending.items() if at < cutoff]:
            del self._pending[scope]

    def confirmed(self, scope: tuple, turn: str | None) -> bool:
        self._prune()
        asked_turn, asked_at = self._pending.get(scope, (None, 0.0))
        if asked_turn is None or asked_turn == turn or turn is None:
            self._pending[scope] = (turn, self._now())
            return False
        return True

    def done(self, scope: tuple) -> None:
        """The action succeeded: the ask is spent."""
        self._pending.pop(scope, None)

    def pending(self, scope: tuple) -> bool:
        self._prune()
        return scope in self._pending
