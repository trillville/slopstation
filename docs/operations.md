# Operations

Run the doctors first. They are read-only, return the number of failures, and
usually identify the next safe action.

## Health checks

On the mini PC:

```powershell
Set-Location C:\slopstation
.venv\Scripts\slopstation-doctor
```

On the gaming PC:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\CouchGaming\Doctor.ps1
```

Warnings describe optional features or conditions that may be intentional.
Resolve failures before deploying or starting a couch session.

## Continuous deployment

Continuous integration runs for pushes and pull requests. Continuous deployment
runs only after a successful push to `main`, or by manual dispatch. Code from a
pull request does not run on either self-hosted machine.

The `gamepc` runner checks out the selected commit, waits while a session is
active, deploys the complete checked script set to `C:\CouchGaming`, and runs
the gaming-PC doctor. It preserves `config.psd1`, third-party binaries,
DisplayMagician shortcuts, state, and logs.

The `k15` runner uses the repository variable `K15_CHECKOUT`, which defaults to
`C:\slopstation`. The deployer requires a clean live checkout on `main`, waits
for an active session to finish, fast-forwards to the selected commit, reloads
the previously running lanes, starts the listener, optionally refreshes the
media stack, and runs the mini-PC doctor.

Both jobs can wait for up to two hours rather than interrupting someone using
the TV. They run independently because the gaming PC is often offline.

## Manual deployment

To update the gaming PC from a checkout on that machine:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File .\gaming-pc\Deploy.ps1
```

To reload the mini-PC lanes after a manual fast-forward:

```powershell
git pull --ff-only
.\Start-Slopstation.bat
.venv\Scripts\slopstation-doctor
```

Do not hand-copy individual gaming-PC scripts. `Deploy.ps1` validates the whole
set, copies it atomically, and stamps the deployed build identifier.

## State and logs

The mini PC stores state under `state\` and daily structured logs under
`logs\`. `SLOPSTATION_HOME` moves configuration, state, and logs together.

The gaming PC stores cross-account state under
`C:\ProgramData\CouchGaming` and transcripts under
`C:\CouchGaming\logs`. Do not delete a session marker merely because it is
old; first correlate its turn identifier with the structured logs.

The example OpenTelemetry Collector configurations can ship both machines'
logs to Sentry. Telemetry is optional and never controls a session.

## Common recovery

- If the mini-PC doctor reports missing configuration, compare the live file
  with `config.example.json` and add the named key.
- If a mini-PC lane does not restart, rerun `Setup-K15-Tasks.ps1` from a
  non-administrator PowerShell.
- If the gaming-PC doctor reports a script, task, firewall, or forced-command
  key mismatch, rerun `gaming-pc\Install.ps1` from an administrator
  PowerShell.
- If display recovery is needed, run the registered `Exit` or
  `ForceOfficeAtLogon` task. Avoid applying unverified display profiles
  remotely.
- If continuous deployment rejects the mini-PC checkout, restore it to a clean
  `main` that can fast-forward. Do not overwrite local work.
- If dependencies changed, regenerate `constraints.txt` on the Python 3.13
  mini PC with:

  ```powershell
  .venv\Scripts\pip freeze --exclude-editable |
      Out-File -Encoding ascii constraints.txt
  ```

## Changes that require an operator

Continuous deployment does not edit live configuration, register scheduled
tasks, create DisplayMagician shortcuts, install VirtualHere or OpenSSH, create
SSH keys, or regenerate dependency pins. Call out any such requirement in the
pull request and perform it on the affected machine before relying on the new
code.

The mini-PC deployer runs from the live checkout, so a change to the deployer
itself takes effect one deployment later unless the new version is pulled once
by hand.

## Public-release gate

Before changing repository visibility:

1. Add a short real end-to-end demonstration near the top of the README.
2. Scan the full Git history, not only the current tree, for names, usernames,
   addresses, device identifiers, tokens, recordings, and custom models.
3. Revoke exposed credentials and remove private artifacts from distributable
   history. Coordinate any history rewrite with all contributors.
4. Verify `K15_CHECKOUT` and both self-hosted runner labels.
5. Run continuous integration and both doctors, then repeat the acceptance
   checks in [setup.md](setup.md).

The current-tree cleanup does not make old commits safe to publish.
