# TV power detection

**Status:** nothing built. One approach measured and refuted, two leads open.

**Delete this doc when** TV power becomes observable to the K15 (the mechanism
and its residue move into `couch.py` alongside `exlink()`), or when the idea is
abandoned and the standing retry in `couch.py` is accepted as the whole answer.

## The problem

`exlink("power_on")` is send-only. The TV's serial receiver acknowledges the
frame with `030cf1` and nothing anywhere reads TV power back, so the launch
path cannot tell a set that woke from a set that stayed dark. It proceeds on
the assumption and finds out ~60 seconds later, from the gaming PC, that the
display profile had no display to apply to.

The gaming PC cannot close the gap either, and this is measured rather than
assumed: while the TV is detached, its EDID and all three WMI monitor classes
read identically whether the panel is awake or asleep (2026-08-13, across a
full power cycle). `Enter-TV.ps1`'s step-1 gate therefore passes on a dark set
and the launch dies later, at the profile apply.

Cost, from the K15's own logs across 38 launches:

| | |
|---|---|
| Launches reaching READY | 34 |
| Failures, all `host never reported READY` | 3 — 2026-08-13 17:20, 08-16 17:56, 08-19 01:18 |
| Silent death (Ctrl-C, no terminal event) | 1 — 08-16 18:15, `b43b74` |
| Every success | 9.1–19.8 s |
| Every failure | 120.9–121.6 s |

The failure is entry-point independent: `a8b522` was a voice launch, the other
two were chords.

## Refuted: read power state out of the existing Ex-Link command set

Probed 2026-08-19 from the K15 on COM3, frames emitted under `lane="manual"`.

| Probe | Result |
|---|---|
| Passive listen, 10 s, sending nothing | 0 bytes — the set volunteers nothing |
| `power_on` → TV **off** | `030cf1`, 3 bytes |
| `power_on` → TV **on** | `030cf1`, 3 bytes |
| `hdmi4` → TV on | `030cf1`, 3 bytes |

**The ack is a constant.** It does not vary with power state, and it does not
vary by command. There is no payload hiding behind `_exlink_txn`'s `s.read(3)`
truncation, because for these frames there is nothing past three bytes at all.

Do not re-run this probe. The measurement is here.

Two things it did establish, both useful:

- **The receiver is powered in standby and acks from it.** So on all three
  failures the TV *received and acknowledged* a well-formed `power_on` and
  declined to wake. This is not a delivery problem — not the cable, not the
  port, not the frame, not timing. Something in the set's own power handling
  refuses intermittently.
- **Idle duration does not predict it.** Failures at 11.8 h, 13.3 h and 20.0 h
  since the previous session; successes at 22.1 h and 22.7 h. The proxy is
  weak (the system never sees the TV being watched normally), but nothing
  clean is there.

## Open lead 1: a status/query command class

The frame table is entirely commands. Samsung's Ex-Link worksheet defines
status reads on many models; whether the S90C honours one is unknown. The
transport is already bidirectional, so this would be a table entry and a
wider read, not new plumbing.

**Gate:** the actual worksheet for this model year. **Do not brute-force the
command space** — the same protocol carries service-mode commands on Samsung
sets, and a valid-checksum guess is not a safe thing to fire at a TV someone
watches. A bad checksum is inert; a good one for the wrong command is not.

Note `_exlink_txn` reads exactly 3 bytes and would truncate any longer reply,
so this lands with a transport change or it silently reads nothing.

## Open lead 2: a second wake channel, and standby depth

Raised from the couch, 2026-08-19: the S90C has a **Power On with Mobile**
setting (network wake). Two separate consequences, neither measured:

- **As a cause.** It governs how much of the set stays alive in standby. A
  deeper standby is the leading suspect for a wake that is acked and then
  refused, and this is the cheapest thing on this page to try — it is a menu
  toggle, and the failure rate is the measurement.
- **As a mechanism.** If the TV answers network wake, the K15 could send it a
  WoL frame alongside the Ex-Link one. It already builds and broadcasts a
  magic packet for the gaming PC (`wol()`), so a second target is a MAC in
  config and one more call. Two independent wake channels beat one retried
  channel, and unlike lead 1 it needs no protocol archaeology.

Neither gives *detection* — both make the wake more likely rather than
observable. Only lead 1 or HDMI-CEC (a USB adapter answering `<Give Device
Power Status>`, ~$50) closes the hole properly.

## What shipped instead, and why this doc still exists

`couch.py` now detects that Enter has exited without writing the marker (via
the `enterstate` verb) and re-pokes + re-dispatches once, rather than polling a
dead task for the rest of its 120 s window. That converts "the set refused the
first wake" from a hard failure into a slower success **whenever a later frame
lands** — which is the common case, and it needed no knowledge of TV power.

It does not fix a set that refuses every frame. That still needs detection,
which is what this page is for.
