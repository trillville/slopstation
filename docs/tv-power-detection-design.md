# TV power detection

**Status:** the question is ANSWERED — the set reports its own power state
over the network, measured end to end on 2026-08-19. First consumer landed
2026-08-21: `tv.tv_power_state` wraps the read and the voice lane's duck
gates on it (`TvDucker` in `dispatch.py`, after the 08-16 stranding). What is
left is wiring it into `couch.py`'s launch path, not finding it.

**Delete this doc when** that wiring lands (the mechanism and its residue move
into `couch.py` alongside `exlink()`).

## The answer, first

The S90C runs an unauthenticated HTTP endpoint that reports its own power
state, **and it answers from standby**:

```
GET http://<tv>:8001/api/v2/   ->   .device.PowerState  ==  "on" | "standby" | ""
```

(`""` — the empty string — is a third, deeper state; see the standby-depth
note below.)

Measured on this rig, TV at `192.168.68.51`, MAC `68:FC:CA:B4:02:22`,
`QN77S90CAFXZA`, `networkType: wireless`:

```
before power_on : PowerState=standby   (33 ms)
Ex-Link power_on -> ack 030cf1
  t+1s..t+4s     : PowerState=standby
  t+5s           : PowerState=on
```

So the K15 can ask, in ~30 ms and with no new hardware, the one question the
whole launch path has been guessing at. Notes for whoever wires it up:

- **~5 s of lag** between the frame landing and the state flipping. Poll, don't
  read once. Irrelevant against a 120 s window and vastly better than learning
  it from Enter at 60-90 s.
- **No pairing.** `/api/v2/` needs no token; it is the same endpoint the TV
  serves for discovery. The websocket remote on 8001/8002 *does* need pairing —
  don't reach for it, nothing here needs to send anything over IP.
- **The address must not drift.** Give the TV a DHCP reservation, or resolve it
  by MAC, before anything depends on the IP.
- **It is on Wi-Fi.** Fine for reading state; a wired drop would matter if the
  WoL wake channel below is ever built.
- **Standby depth, partially measured 2026-08-21:** after hours off, the IP
  server STAYS UP (3 ms answers, full device blob) but `PowerState` drains to
  the **empty string** — distinct from the `"standby"` a recently-used set
  reports, and turning the set on brings back a clean `"on"`. So the depth
  ladder is directly observable — `"on"` / `"standby"` (shallow) /
  `""` (deep) — and `tv.tv_power_state` maps `""` to None (unknown), which
  every current caller treats as not-on. Samsung's own worksheet says the TV
  goes offline to IP about a minute after power-off and needs WoL after that;
  this set does not go offline, it degrades. Whether a still-deeper,
  IP-silent state exists is the remaining unknown — and the refusals
  presumably live in the deep rungs. Which makes the endpoint a possible
  **predictor** and not merely a detector, now sharper than first written:
  log the RAW value at `launch_start` and see whether `""`-then predicts
  refused-wake. That correlation is the next measurement, and it is free.

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

## Closed: HDMI-CEC, and the Ex-Link status hunt

Both were the plan before the endpoint above turned up. Neither is worth doing
now, recorded so nobody re-opens them:

- **CEC** would have meant a USB-CEC adapter (Pulse-Eight, ~$50) on the gaming
  PC — that is the machine with HDMI to the TV — plus libcec, plus a new
  Dispatch verb to relay the answer to the K15. It reads power state correctly,
  but it is hardware and a dependency to buy an answer that is already free
  over the LAN. Worse, Samsung's IP-control worksheet reports that some control
  partners ask for **Anynet+/CEC to be OFF** because it disrupts IP control —
  so adopting CEC could cost the very channel that solved this.
- **An Ex-Link status frame.** Still unknown whether the S90C has one, and now
  moot. Third-party integrators describe Ex-Link power feedback as unreliable
  in general ("no way of knowing, when first powered up, what the current state
  of the display is"), which matches the measurement below.

## Refuted earlier: a status/query command class

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

Neither gives *detection* — the endpoint at the top of this page does that.
These make the wake more likely, which is the other half.

Samsung's Consumer IP Control Worksheet is the source for both, and it is
specific: **"Samsung TVs use WoL for Power On"**, the TV drops off IP roughly a
minute after power-off, and it therefore advises sending *both* an IP power-on
and a WoL frame. `wol()` already builds and broadcasts magic packets for the
gaming PC, so a second target is a MAC in `config.json` and one more call —
`68:FC:CA:B4:02:22` here, though note that is the **wireless** MAC and WoL over
Wi-Fi is the weaker case.

The same worksheet names two settings that bear directly on the refusals:

- **Eco settings** "may affect timing and reliable expected behaviors from the
  TV", with control vendors asking for specific ones off.
- **Keep Bixby in Standby** (`Settings > General > Voice > Voice Assistant >
  Bixby Wake-up Options`) is described as keeping the TV's **IP server open in
  standby**, explicitly to make Power On more reliable. That is a direct lever
  on standby depth — the leading suspect for a wake that is acked and refused.

## What shipped instead, and why this doc still exists

`couch.py` now detects that Enter has exited without writing the marker (via
the `enterstate` verb) and re-pokes + re-dispatches once, rather than polling a
dead task for the rest of its 120 s window. That converts "the set refused the
first wake" from a hard failure into a slower success **whenever a later frame
lands** — which is the common case, and it needed no knowledge of TV power.

It does not fix a set that refuses every frame, and it still guesses about the
TV. The endpoint at the top of this page is what replaces the guess; the shape
it wants in `couch.py` is roughly:

    power_on  ->  poll PowerState until "on"  ->  only THEN dispatch Enter

which spends the budget on the thing that actually has to happen, and makes
`enter_died` rare rather than the primary signal. Re-poke while polling, fail
early and honestly if the set never reports on — at that point the launch knows
the TV is the problem, which no version of this has ever been able to say.
