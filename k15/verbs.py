"""The gaming PC's answers, named once.

`ssh gamepc <verb>` answers with one of a closed set of words. Dispatch.ps1 is
the producer and the security boundary (anything it does not recognise is
DENIED), and three Python files on the K15 used to spell its answers as bare
string literals - thirty of them, with the ":<payload>" split written three
times. This is the consumer's dictionary. test_turn.py reads the words OUT OF
THE SHIPPING Dispatch.ps1 and fails if the two sets differ, so a new answer on
the PC cannot go unnamed here, and a name here cannot outlive its answer.

What each word MEANS stays at the call site: OK from `launch` and OK from
`exit` are different promises, and README § Deliberately not doing keeps it
that way (the verb responses mean different things per call site). Stdlib
only - couch.py lives on this.
"""
OK = "OK"
ALREADY = "ALREADY"            # launch: that appid is already the running game
BUSY = "BUSY"                  # launch/stop: a DIFFERENT game is up - "BUSY:<appid>"
NOTREADY = "NOTREADY"          # status/launch/nav: no READY marker (no session, or mid-Enter)
NOTINSTALLED = "NOTINSTALLED"  # launch: no ACF for that appid on the PC
NOTRUNNING = "NOTRUNNING"      # stop: nothing is running
NOTASK = "NOTASK"              # a scheduled task is not registered - "NOTASK:<name>"
FAILED = "FAILED"              # schtasks /Run failed - "FAILED:<code>"
DENIED = "DENIED"              # not a verb, or a malformed argument (fail-closed)
RUNNING = "RUNNING"            # enterstate: the Enter task is still running
IDLE = "IDLE"                  # enterstate: it is not
UNKNOWN = "UNKNOWN"            # version: no build-id stamped (never shipped by Deploy.ps1)

# Every bare word Dispatch.ps1 emits - the drift check in test_turn compares
# exactly this set against the script.
ANSWERS = frozenset((OK, ALREADY, BUSY, NOTREADY, NOTINSTALLED, NOTRUNNING,
                     NOTASK, FAILED, DENIED, RUNNING, IDLE, UNKNOWN))


def code_of(answer):
    """('BUSY', '42') from 'BUSY:42'; ('OK', None) from 'OK'. The payload
    after the colon is an appid, a task name or an exit code - the caller
    knows which from the word."""
    word, _, payload = (answer or "").partition(":")
    return word, (payload or None)
