# Configuration

Where every value an operator sets lives, and what happens without it.

| File | Machine | Committed example | Read by |
|---|---|---|---|
| `config.json` | mini PC, checkout root | `config.example.json` | everything on the mini PC |
| `secrets.json` | mini PC, checkout root | `secrets.example.json` | the voice agent, the text and MCP interfaces, the Steam and media tools, the doctor |
| `C:\CouchGaming\config.psd1` | gaming PC | `gaming-pc/config.example.psd1` | every gaming-PC task and `Doctor.ps1` |
| `media\.env` | mini PC | `media/.env.example` | Docker Compose and `Start-Media.ps1` |
| the collector's `config.yaml` | both | `otelcol/config.yaml.example`, `gaming-pc/otelcol/config.yaml.example` | the OpenTelemetry Collector service |
| `smartd.conf` | mini PC | `smartd.conf.example` | smartd |

No live file is committed. `SLOPSTATION_HOME` moves `config.json`,
`secrets.json`, `state\` and `logs\` together to another directory; nothing
else relocates them.

## config.json

Read once per process on first use. The example carries a `_comment` beside
each key that needs one; this is the map of what is required and what each
group is for.

**Required.** The keys in `config.REQUIRED`. A launch refuses to start
(`config_invalid` in the log) and the doctor fails when one is missing.

| Key | Meaning |
|---|---|
| `gamingPcMac`, `gamingPcIp` | Wake-on-LAN target, and the address the mini PC polls until SSH answers |
| `sshHost` | the gaming PC's entry in the mini PC user's `.ssh\config` |
| `tvComPort` | the Ex-Link adapter's COM port |
| `tvGamingCmd`, `tvIdleCmd` | the Ex-Link input commands for the PC's input and the idle input |
| `tvOffWhenDone` | turn the TV off when a session ends |

**Optional, top level.**

| Key | Meaning | Without it |
|---|---|---|
| `steamMachineName` | which signed-in Steam client is the gaming PC | needed only with more than one PC on the account |
| `tvIp` | the TV's address, best with a DHCP reservation | no volume ducking, and a launch cannot read whether the TV came on |
| `sentryDsn` | the voice agent's errors, traces and check-ins | nothing leaves the machine; log shipping is the collector's own config |
| `media` | the media stack; `enabled` gates every media tool | media verbs off. Keys are described in `media/README.md` |
| `textInterface`, `remoteInterface` | HTTP chat and the MCP endpoint | off. Each needs its token in `secrets.json` |

**`voice`.** The keys in `config.REQUIRED_VOICE` must be present or the voice
agent does not start. Every other voice key has a default in code, so a
`config.json` written before a key existed keeps working after a pull.

| Keys | For |
|---|---|
| `inputDeviceName`, `outputDeviceName` | the audio devices, by name |
| `wakeModel`, `wakeThreshold`, `wakeNearMissFactor`, `wakeClipsKeep`, `wakeVadThreshold`, `wakePatience`, `wakeVerifier`, `wakeVerifierThreshold` | the wake word: a stock openWakeWord name or a `.onnx` vendored in `src/slopstation/agent/models`, and its tuning |
| `duckSteps`, `duckToPct` | volume ducking during a voice session; needs `tvIp` and the pairing step |
| `holdWindowS`, `followupCarryS`, `eotThreshold`, `eagerEotThreshold`, `eagerEnabled` | turn taking |
| `keytermCount`, `fuzzyTitleThreshold` | what the speech recogniser is told to expect, and how loosely a spoken title matches the library |
| `ttsVoice` | the Deepgram voice |
| `assistantProvider`, `assistantModelAnthropic`, `assistantModelOpenai`, `assistantReasoningEffort`, `assistantWebSearch`, `assistantSearchMaxUses` | the assistant model and its tools |
| `inputs`, `navTargets` | spoken names for TV inputs and for Big Picture destinations |
| `volumeStep`, `volumeMax`, `earconGain`, `location`, `followUpAfterAnnounce`, `steamDataTools` | volume verbs, earcon level, the assistant's time zone and locale, and whether an announcement opens a follow-up window |

## secrets.json

| Key | Needed for | Without it |
|---|---|---|
| `deepgramApiKey` | speech to text and text to speech | no voice |
| `anthropicApiKey`, `openaiApiKey` | the assistant, per `assistantProvider` | fixed voice commands still work; the assistant does not |
| `steamApiKey`, `steamId64` | library enrichment | a thinner catalog |
| `steamRefreshToken` | installing by voice and download status; written by `python -m slopstation.agent.tools.steam_session enroll` | those verbs unavailable |
| `radarrApiKey`, `sonarrApiKey`, `prowlarrApiKey`, `qbittorrentPassword` | media requests, the media doctor, Proton port sync | media disabled |
| `textInterfaceToken`, `remoteInterfaceToken` | the text and MCP interfaces | the interface does not start |

The doctor's `voice keys` row names each missing key and the lane it
disables. A malformed file disables every keyed feature and prints
`secrets.json is malformed`.

## Gaming PC: config.psd1

| Key | Meaning |
|---|---|
| `PuckName` | the controller receiver as VirtualHere names it |
| `PuckHwId` | its hardware id as Windows enumerates it |
| `TvEdid` | the TV's EDID name as Windows reports it |
| `TvHeight` | the primary-display height that means the TV profile is active |

`CouchGaming.common.ps1` validates the file on load. A missing, blank or
mistyped key stops every task with one message, and is the first line
`Doctor.ps1` prints. The example ships `TvEdid` as a placeholder in angle
brackets, and a value still in that form stops the tasks the same way.
`Install.ps1` creates the file from the example once; `Deploy.ps1` never
writes it.

Everything else on the PC is a convention rather than a setting:
`C:\CouchGaming`, `C:\ProgramData\CouchGaming`, the task names under
`\CouchGaming\`, the marker file names, the Steam window titles, and the
`OFFICE.lnk` and `TV-GAMING.lnk` shortcut names.

## Environment variables

| Variable | Effect |
|---|---|
| `SLOPSTATION_HOME` | the directory holding `config.json`, `secrets.json`, `state\` and `logs\`; default is the checkout |
| `SLOPSTATION_URL`, `SLOPSTATION_TOKEN` | the text client's endpoint and token when it talks to another machine |
| `SLOPSTATION_ENV`, `SLOPSTATION_SERVICE` | the `env` and `service` attributes on every event. The test suite sets `env` to `test` so a test can never look like an outage |
| `SLOPSTATION_TEST_AUDIO`, `SLOPSTATION_TEST_HAS` | opt the test suite into real audio devices; override what the machine is detected to have |

## Repository settings

| Setting | Effect |
|---|---|
| Actions variable `K15_CHECKOUT` | where the mini PC runner finds the live checkout |
| runner labels `k15`, `gamepc` | which machine each deploy job lands on |
