# Design decisions

This file records the choices that shape the public version of Slopstation.

## Keep one repository

The mini PC and gaming PC implement one session protocol and are tested
together. Keeping both halves in one repository makes changes to SSH verbs,
events, deployment, and recovery reviewable as one contract.

## Keep mini-PC configuration at the root

`config.json` and `secrets.json` remain beside the checkout because the runtime,
doctor, tests, and deployer already share that layout. `SLOPSTATION_HOME`
provides one supported relocation mechanism without adding a second
configuration system.

## Isolate only gaming-PC machine values

Four hardware values moved to `C:\CouchGaming\config.psd1`: the controller
name, controller identifier, TV display name, and TV-primary height. The
remaining values describe the installed product layout and are intentionally
kept in scripts. This gives deployments a clear preserve boundary without
turning every path into a setting.

## Use scheduled tasks behind restricted SSH

The gaming PC must manipulate interactive Windows devices in the signed-in
user session. The mini PC therefore sends a small set of verbs to a
forced-command SSH key, and `Dispatch.ps1` starts predefined scheduled tasks.
The key cannot open a shell, forward ports, or allocate a terminal.

## Prefer verified convergence to rollback

A launch changes the TV only after the gaming PC reports ready. Deployment
waits for active use, validates the complete script set, preserves live state,
and runs doctors afterward. It fails visibly instead of trying to infer and
restore a previous distributed state.

## Keep optional surfaces optional

Voice, text, remote assistant access, media automation, and telemetry stay
behind configuration switches. The controller-to-couch path does not depend on
external model providers, media services, or telemetry.

## Keep documentation near its owner

Public setup, configuration, operations, and decisions live in `docs\`. The
media guide remains in `media\README.md` because it is maintained with that
optional stack and can be followed independently.
