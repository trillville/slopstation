"""python -m slopstation.agent.media: check the media stack, Proton's port
and the Servarr apps' updates."""

import argparse
import json

from slopstation import config, logbook, paths
from slopstation.agent.media.clients import (
    MediaConfigurationError,
    MediaError,
    _qbit_from_config,
)
from slopstation.agent.media.config import servarr_clients
from slopstation.agent.media.doctor import media_doctor
from slopstation.agent.media.proton import (
    ProtonPortMonitor,
    read_proton_port_state,
)
from slopstation.agent.media.updates import available_update, update_app


def _servarr_clients(cfg, secrets):
    media_cfg = cfg.get("media")
    if not isinstance(media_cfg, dict):
        raise MediaConfigurationError("media configuration is missing")
    return {
        client.name.lower(): client for client in servarr_clients(media_cfg, secrets)
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check the media stack, Proton's port and app updates"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "proton-port", "updates"):
        sub.add_parser(name)
    proton_sync = sub.add_parser("sync-proton-port")
    proton_sync.add_argument("--execute", action="store_true")
    qbit_port = sub.add_parser("set-qbit-port")
    qbit_port.add_argument("port", type=int)
    qbit_port.add_argument("--execute", action="store_true")
    update = sub.add_parser("update")
    update.add_argument("app", choices=("radarr", "sonarr", "prowlarr"))
    update.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    log = logbook.logger("voice")
    cfg = config.current()
    secrets = config.secrets()
    try:
        if args.command == "doctor":
            result = media_doctor(cfg, secrets)
            for check in result["checks"]:
                print(f"{check['level']:<5} {check['name']} - {check['detail']}")
            return 0 if result["ok"] else 1
        if args.command == "proton-port":
            result = read_proton_port_state()
            print(json.dumps(result, indent=2))
            return 0 if result["state"] in ("active", "inactive", "transitional") else 1
        if args.command == "updates":
            for client in _servarr_clients(cfg, secrets).values():
                found = available_update(client)
                if found:
                    print(
                        f"{client.name} {found['installed']} -> {found['latest']}"
                        f" (released {found['released']})"
                    )
                else:
                    print(f"{client.name} is current")
            return 0
        if args.command == "update":
            if not args.execute:
                print("change not submitted; repeat with --execute")
                return 2
            clients = _servarr_clients(cfg, secrets)
            if args.app not in clients:
                raise MediaConfigurationError(f"{args.app} is not configured")
            result = update_app(clients[args.app], paths.HOME / "media")
            if result["before"] == result["after"]:
                print(
                    f"{result['app']} still on {result['before']}: "
                    "no newer image to pull (not built yet, or the tag is pinned)"
                )
                return 1
            print(f"{result['app']} {result['before']} -> {result['after']}")
            print(
                f"roll back: pin lscr.io/linuxserver/{args.app}:{result['before']}"
                " in media/compose.yaml and rerun Start-Media.ps1"
            )
            return 0
        if args.command in ("sync-proton-port", "set-qbit-port"):
            if not args.execute:
                print("change not submitted; repeat with --execute")
                return 2
            media_cfg = cfg.get("media")
            if not isinstance(media_cfg, dict):
                raise MediaConfigurationError("media configuration is missing")
            qbit = _qbit_from_config(media_cfg, secrets)
            if args.command == "set-qbit-port":
                print(json.dumps(qbit.set_listen_port(args.port), indent=2))
                return 0
            result = ProtonPortMonitor(qbit, log).reconcile_once()
            print(json.dumps(result, indent=2))
            return 0 if result["state"] in ("active", "inactive") else 1

    except MediaError as e:
        print(f"media request failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
