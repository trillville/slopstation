# Platform constraints

Facts about the hardware, Windows, and the libraries that the code depends
on. Each names the code that enforces it and the test that would fail if it
stopped being true. The comment beside the code states the constraint in a
line; the measurement and the incident that found it live here.

## Pipecat 1.8.1

`pyproject.toml` pins `pipecat-ai==1.8.1`. Four behaviours of that version
shape `agent/speech/session.py`; recheck each before moving the pin.

- Flux connects during pipeline setup, before `StartFrame` opens the
  microphone. The wake capture is handed over live and stopped by the
  feeder at `StartFrame`, or the 0.3 to 1.5 s of speech in that window is
  lost. The chime deadline is disarmed for the same reason.
- A setup exception (device open, Flux connect) is swallowed by the runner's
  `gather(return_exceptions=True)`. Without the `started` flag set from
  `on_pipeline_started`, a failed build reads as a clean instant close and
  `session_crashed` never fires.
- Flux only proposes turn edges, and its stop proposal is a queued control
  frame resolved downstream of the gate. A stale stop can land after the
  next turn's start; `enable_interruptions=True` is what cuts an answer on
  talk-over.
- The local audio transport never terminates the PyAudio instance it
  creates and exposes no cleanup. The session terminates it after each
  run, or every wake leaks one.

Tests: `tests/test_voice_session.py`, `tests/test_assistant.py` (the
provider construction through the production `_make_llm`).

## Audio devices

- A WASAPI stream can outlive its endpoint (a Bluetooth profile flap) and
  deliver exact zeros forever with no error. A real microphone has a noise
  floor, so 30 s of zeros is a dead stream: `WakeListener` raises into the
  recovery path, which tears the whole PortAudio instance down and rebuilds
  against the current device table. Reopening on the old instance retries a
  stale index. `agent/speech/audio.py`; `tests/test_audio.py`.
- `open_audio` waits for the configured device rather than taking the
  Windows default: a silent fallback leaves the wake loop deaf on the wrong
  endpoint. Startup and recovery share the wait. The lane checks in with
  Sentry before that wait, so a dead microphone is a doctor row, not a dead
  lane. `agent/voice.py`; `tests/test_audio.py`, `tests/test_voice.py`.

## Files and locks on Windows

- Windows emulates `O_APPEND` as seek-to-end then write, so two processes
  can pick the same offset and one silently overwrites the other. Measured:
  about 20% loss with eight concurrent emitters. Every event writer takes a
  one-byte lock on a sidecar file first, for at most 0.2 s; an unlocked
  write beats a blocked lane. `events._append`;
  `tests/test_events.py::test_concurrent_emitters_keep_every_line` runs six
  processes.
- State files use `msvcrt.locking` with `LK_NBLCK` in a 5 ms spin, because
  `LK_LOCK` retries on its own one-second timer. Windows drops the byte lock
  when its holder dies, so a killed CLI cannot wedge the agent. Windows also
  denies a rename onto a file another process holds open (the doctor reads
  `operations.json`), so `statefile.write` retries `os.replace` eight times.
  `statefile.py`; `tests/test_operations.py::test_store_serialises_on_the_file`.
- A ledger that cannot be parsed is refused at construction and left as it
  is. Before Sep 2026 it loaded as empty and the next observation wrote it
  back empty. `agent/tools/operations.py`;
  `tests/test_operations.py::test_a_corrupt_ledger_is_refused_and_left_alone`.

## Processes and scheduled tasks

- Each lane runs inside a kill-on-close job object. Ending the scheduled
  task is `TerminateProcess` on the wrapper, which closes its handles and so
  the job, taking the lane and the interpreter the venv launcher spawned
  with it. Without the job the scheduler left the old lane running beside
  its replacement. This also means the graceful stop path (`Services.stop`)
  runs on Ctrl-C and `--once`, never in production. `supervise.py`;
  `tests/test_supervise.py::test_a_process_in_the_job_takes_its_children_with_it`.
- `schtasks` localises its Status text. The rig is English; the query
  matches `Running` literally. `supervise.query`.
- Manual Enter tasks and deployments before the turn id wrote an ISO
  timestamp into the ready marker. The reader accepts both shapes.
  `gamepc.py`.

## The TV and the controller

- Power on, then the input command, not the input after READY: the set
  wakes on HDMI 4 (measured 2026-09-03), and an input command sent to a set
  that is still waking is lost. `couch.start`; `tests/test_couch.py`.
- Ex-Link acknowledges receipt of every frame with the same three bytes and
  says nothing about whether the state changed. Volume and mute go over
  HTTP (UPnP RenderingControl, which with eARC controls the soundbar) and
  every change is verified by readback; a readback that did not move is
  retried, a partial move is left alone because it may be the remote.
  `tv.py`; `tests/test_tv_ducking.py`, `tests/test_tv_exlink.py`.
- VirtualHere unbinds the Puck from the K15's HID stack to forward it to the
  gaming PC. Reading its interfaces through that handoff can leave a
  controller that enumerates, rumbles, then ignores every button. The chord
  listener stands off while the session lock is held; a voice launch is
  another process, so the lock is the only signal. `chord_listener.py`;
  `tests/test_controller_handoff.py`.
- A broadcast the OS routes itself can leave down a VPN or WSL adapter and
  never reach the gaming PC's wire, so Wake-on-LAN binds each local address
  in turn. Measured 2026-09-06: the K15 held four addresses, one of them on
  the LAN; that outage sent every packet down the wrong one. `couch.wol`;
  `tests/test_couch.py::test_wol_sends_one_packet_from_each_local_address`,
  `tests/test_doctor.py::test_wake_on_lan_fails_when_no_sender_sits_on_the_pc_network`.

## The assistant

- The clock is the last line of the system prompt. Everything before it is
  a cached prefix only while it stays byte-identical; ahead of the catalog
  it left 850 stable tokens, under the provider's caching floor.
  `agent/llm/assistant.system_instruction`; `tests/test_assistant.py`.
- The display tool is in the default tool set. With only the session tools
  loaded, "put the desktop on the TV" became an input switch that started a
  session (2026-09-06 logs). `agent/llm/toolsets/rig.py`.
- The Steam refresh token from QR enrolment can carry a web audience that
  denies every mint while reading as valid. The doctor mints once to tell
  the two apart. `doctor.check_steam_session`.

## Sentry

- Every Sentry plan includes one cron monitor. Without a pay-as-you-go
  budget the second lane's monitor never registers, which looks exactly like
  a lane that never started. The doctor reads check-in results from the
  event stream, where a rejected check-in is named. `doctor.check_sentry`;
  `tests/test_doctor.py::test_cron_checkin_reads_back_past_today`.

## Windows tooling the doctor shells out to

- Walking firewall rules with the cmdlet costs 16 s; the doctor parses
  `netsh advfirewall`'s dump in about 0.2 s and answers "not recognised"
  rather than guessing when the output is localised. `doctor.py`.
- Radarr and Sonarr name history events differently per app; the map in
  `agent/llm/toolsets/media_ops.py` (`HISTORY_EVENTS`) is the one place
  those names live.
