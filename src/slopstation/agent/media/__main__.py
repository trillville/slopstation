"""python -m slopstation.agent.media: read Proton's forwarded port and hold
qBittorrent to it. The stack's checks are the doctor's media rows."""

import argparse
import json

from slopstation import config, logbook
from slopstation.agent.media.clients import (
    MediaConfigurationError,
    MediaError,
    _qbit_from_config,
)
from slopstation.agent.media.proton import (
    ProtonPortMonitor,
    read_proton_port_state,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read Proton's forwarded port and set qBittorrent's"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("proton-port")
    proton_sync = sub.add_parser("sync-proton-port")
    proton_sync.add_argument("--execute", action="store_true")
    qbit_port = sub.add_parser("set-qbit-port")
    qbit_port.add_argument("port", type=int)
    qbit_port.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    log = logbook.logger("voice")
    cfg = config.current()
    secrets = config.secrets()
    try:
        if args.command == "proton-port":
            result = read_proton_port_state()
            print(json.dumps(result, indent=2))
            return 0 if result["state"] in ("active", "inactive", "transitional") else 1
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
