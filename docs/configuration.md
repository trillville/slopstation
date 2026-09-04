# Configuration

Slopstation separates settings from credentials. Copy the committed examples,
keep the live files outside Git, and run the doctor after every change.

## Files

| File | Machine | Purpose |
|---|---|---|
| `config.json` | Mini PC | Device addresses, feature switches, and tuning |
| `secrets.json` | Mini PC | API keys, refresh tokens, and interface tokens |
| `C:\CouchGaming\config.psd1` | Gaming PC | Controller and display identifiers |
| `media\.env` | Mini PC | Media storage paths and the Homarr encryption key |
| `gaming-pc\otelcol\config.yaml.example` | Gaming PC | Optional telemetry collector example |
| `otelcol\config.yaml.example` | Mini PC | Optional telemetry collector example |

The examples use documentation-only addresses and placeholder credentials. A
copied example is not a working configuration.

## Mini PC settings

The committed example supplies the defaults below. A missing required key stops
the controller path or voice lane with a configuration error. Optional values
are inert when blank or disabled.

| Key | Example/default | Required | Purpose |
|---|---|---|---|
| `gamingPcMac` | `02-00-00-00-00-01` | Yes | Gaming PC Wake-on-LAN address; replace it |
| `gamingPcIp` | `192.0.2.10` | Yes | Gaming PC reachability address; replace it |
| `sshHost` | `gaming-pc` | Yes | OpenSSH alias restricted to `Dispatch.ps1` |
| `steamMachineName` | blank | No | Steam client name when more than one is signed in |
| `tvComPort` | `COM3` | Yes | TV Ex-Link serial port |
| `tvIp` | `192.0.2.20` | No | Enables TV power checks and network volume control; replace it |
| `tvGamingCmd` | `hdmi4` | Yes | Gaming input Ex-Link command |
| `tvIdleCmd` | `hdmi1` | Yes | Idle input Ex-Link command |
| `tvOffWhenDone` | `true` | Yes | Power the TV off after exiting a session |
| `sentryDsn` | blank | No | Application errors, traces, and check-ins |

### Voice and assistant

These `voice` keys are required for the voice lane unless marked optional:

| Keys | Example/default | Purpose |
|---|---|---|
| `inputDeviceName`, `outputDeviceName` | blank | Optional Windows audio-device names; blank uses defaults |
| `wakeModel`, `wakeThreshold` | `hey_jarvis_v0.1`, `0.5` | Wake model and activation score |
| `wakeNearMissFactor`, `wakeClipsKeep` | `0.5`, `200` | Optional diagnostic scoring and retained clips |
| `wakeVadThreshold`, `wakePatience` | `0`, `0` | Optional speech gate and consecutive-frame gate |
| `wakeVerifier`, `wakeVerifierThreshold` | blank, `0.1` | Optional speaker-specific model and prefilter |
| `duckSteps`, `duckToPct` | `10`, `0` | Optional temporary TV-volume reduction; percentage overrides steps |
| `earconGain` | `1.0` | Optional cue volume multiplier |
| `holdWindowS`, `followupCarryS` | `10`, `60` | Open-microphone and follow-up context windows |
| `eotThreshold`, `eagerEotThreshold` | `0.7`, `0.5` | End-of-turn thresholds |
| `eagerEnabled` | `true` | Optional eager end-of-turn switch |
| `volumeStep`, `volumeMax` | `5`, `40` | Voice-requested TV volume bounds |
| `keytermCount`, `fuzzyTitleThreshold` | `40`, `87` | Game-title recognition tuning |
| `ttsVoice` | `aura-2-thalia-en` | Text-to-speech voice |
| `assistantProvider` | `anthropic` | `anthropic` or `openai` backend |
| `assistantModelAnthropic` | `claude-haiku-4-5` | Anthropic model name |
| `assistantModelOpenai` | `gpt-5.6-luna` | OpenAI model name |
| `assistantReasoningEffort` | `low` | Provider reasoning effort |
| `assistantWebSearch`, `assistantSearchMaxUses` | `false`, `2` | Search permission and per-turn limit |
| `steamDataTools` | `true` | Optional Steam catalog and account tools |
| `location` | blank city/region/timezone, `US` | Local context for assistant answers |
| `followUpAfterAnnounce` | `true` | Reopen listening after an operation announcement |
| `inputs` | phrase-to-HDMI map | Exact accepted TV input phrases |
| `navTargets` | phrase-to-target map | Optional exact Steam navigation phrases |

Use `hey_jarvis_v0.1` for the committed default. Custom `.onnx` wake and verifier
models belong under `src\slopstation\agent\models` and are ignored by Git.

### Optional interfaces

`textInterface` serves authenticated local-area-network chat and defaults to
disabled on `127.0.0.1:8765`. `remoteInterface` serves an authenticated Model
Context Protocol forwarder to that text interface and defaults to disabled on
`127.0.0.1:8766`. Leave both disabled unless needed. Keep the remote listener
on localhost behind an authenticated tunnel.

### Optional media settings

The `media` object defaults to disabled. Its local service URLs use ports 7878,
8989, 9696, and 8080 for Radarr, Sonarr, Prowlarr, and qBittorrent. The remaining
defaults are:

| Keys | Example/default | Purpose |
|---|---|---|
| `qbittorrentUsername` | `admin` | Web interface account |
| `qbittorrentNetworkInterface` | `ProtonVPN` | Required VPN-bound interface |
| `protonPortSync` | `false` | Synchronize qBittorrent's listening port |
| `movieRoot`, `seriesRoot` | `/data/Movies`, `/data/TV` | Container library roots |
| `managedIndexers` | `1337x`, `EZTV` | Prowlarr definitions managed by setup |
| `seedRatio`, `seedTimeMinutes` | `0.25`, `60` | qBittorrent seed limits |
| `pollS` | `30` | Active operation polling interval |
| `healthSync`, `healthPollS` | `true`, `300` | Service health monitoring |
| `diskWatch`, `diskPollS`, `diskFreeWarnGb` | `true`, `300`, `250` | Free-space monitoring |
| `moviePresets`, `seriesPresets` | named profiles in the example | Quality-profile selection |

Configure the services and storage first, then follow
[the media guide](../media/README.md). Media credentials belong in
`secrets.json`; host storage and the Homarr key belong in `media\.env`.

## Secrets

`secrets.json` may contain Deepgram, Anthropic, OpenAI, Steam, Radarr, Sonarr,
Prowlarr, and qBittorrent credentials. It also holds the text and remote
interface tokens. Only add values for enabled features.

Generate each interface token with at least 32 random bytes. Never place real
tokens in an issue, log excerpt, screenshot, example, or commit. If a credential
ever entered Git history, revoke it before publishing the repository.

## Gaming PC settings

`C:\CouchGaming\config.psd1` contains exactly four machine-specific values:

- `PuckName`: the controller receiver name shown by VirtualHere.
- `PuckHwId`: the identifying part of its Plug and Play instance ID.
- `TvEdid`: the TV name returned by `WmiMonitorID`.
- `TvHeight`: the desktop height that identifies the TV-primary profile.

`Deploy.ps1` updates `config.example.psd1` but preserves the live file.

## Environment variables

- `SLOPSTATION_HOME` moves `config.json`, `secrets.json`, `state\`, and
  `logs\` together from the checkout default.
- `SLOPSTATION_URL` and `SLOPSTATION_TOKEN` configure the text client.
- `SLOPSTATION_ENV` labels structured events.
- `SLOPSTATION_SERVICE` overrides the event service name.
- `SLOPSTATION_TEST_AUDIO=1` includes hardware audio tests.
- The GitHub repository variable `K15_CHECKOUT` tells continuous deployment
  where the live mini-PC checkout resides. It defaults to `C:\slopstation`.
