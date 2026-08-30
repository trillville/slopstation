# Text interface

The K15 exposes the production assistant and its normal tools through an
authenticated LAN-only HTTP endpoint. `k15/slop.py` is a stdlib client for an
interactive conversation or one command. The request timeout is three minutes;
it is not the operation timeout. Media acquisition stays asynchronous and can
run for days while the operation monitor reconciles every configured poll
interval.

## One-time K15 setup

Generate a token in PowerShell:

```powershell
$bytes = New-Object byte[] 32
$rng = New-Object Security.Cryptography.RNGCryptoServiceProvider
$rng.GetBytes($bytes)
[Convert]::ToBase64String($bytes)
```

Add the result to `k15\secrets.json`:

```json
"textInterfaceToken": "paste-the-generated-value"
```

Add this top-level section to `k15\config.json`:

```json
"textInterface": {
  "enabled": true,
  "host": "0.0.0.0",
  "port": 8765
}
```

From an elevated PowerShell, allow only the private LAN:

```powershell
New-NetFirewallRule -DisplayName "Slopstation text interface" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
  -Profile Private -RemoteAddress LocalSubnet
```

Run `Start-K15.bat`. The text endpoint shares the production voice process,
assistant provider, durable operation store, and action tools. It is not
internet-facing and should not be forwarded through the router.

## Use

On the K15, the client reads the local token automatically:

```powershell
python .\k15\slop.py
python .\k15\slop.py "what is downloading?"
```

On the gaming PC, set the token for that PowerShell process and run from its
Slopstation checkout:

```powershell
$env:SLOPSTATION_TOKEN = "the-same-generated-value"
python .\k15\slop.py
```

The default endpoint is `http://192.168.68.75:8765`. Override it with
`SLOPSTATION_URL` or `--url`. A client process keeps one conversational session;
exiting it starts a fresh session next time.

Voice and text accept the same action language. Examples:

- `Get Heat in 1080p.`
- `Get Andor season 1 in 4K.`
- `What is downloading?`
- `Delete Andor season 1.`
- `Delete every season of Andor.`

Questions about current downloads, searches, imports, and installs refresh the
durable operation ledger before answering. Only an operation whose structured
phase is `downloading` is described as downloading; searching, waiting for a
match, importing, and finalizing retain their distinct names. Steam's broader
raw client activity remains available only when explicitly requested.

Series deletion without a named season or explicit whole-series wording is
refused. Selected-season deletion preserves the Sonarr series and every other
season. Movie and whole-series deletion remove their authority record and
files. An active matching operation becomes `CANCELED` only after authority
cleanup succeeds.
