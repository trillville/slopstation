"""python -m slopstation.agent.operations: list, show, reconcile or abandon."""

import argparse
import json

from slopstation import config, logbook
from slopstation.agent import media
from slopstation.agent.monitor import Monitor
from slopstation.agent.operations import (
    CANCELED,
    TERMINAL,
    OperationStore,
    record_deleted,
)
from slopstation.agent.operations.monitors import MediaMonitor, SteamMonitor
from slopstation.agent.steam.session import SteamSession


def _line(operation):
    progress = operation.get("progress") or {}
    phase = progress.get("phase")
    pct = (
        progress.get("download_percent")
        if phase == "downloading"
        else progress.get("percent")
    )
    suffix = f" ({pct}%)" if pct is not None else ""
    if phase:
        suffix = f" [{phase}]" + suffix
    return (
        f"{operation['id']} {operation['state']} {operation['kind']} "
        f"{operation['title']}{suffix}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect durable operations")
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--active", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("operation")
    sub.add_parser("reconcile")
    abandon = sub.add_parser("abandon")
    abandon.add_argument("operation")
    abandon.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    log = logbook.logger("voice")
    try:
        store = OperationStore(log)
    except ValueError as e:
        print(e)
        return 1

    if args.command == "list":
        rows = store.active() if args.active else store.recent(50)
        if not rows:
            print("no operations")
        for row in rows:
            print(_line(row))
        return 0
    if args.command == "show":
        operation = store.get(args.operation)
        if operation is None:
            print(f"no operation named {args.operation}")
            return 1
        print(json.dumps(operation, indent=2))
        return 0
    if args.command == "abandon":
        operation = store.get(args.operation)
        if operation is None:
            print(f"no operation named {args.operation}")
            return 1
        if operation.get("state") in TERMINAL:
            print(f"{args.operation} is already {operation['state'].lower()}")
            return 1
        if not args.execute:
            print("nothing deleted; repeat with --execute")
            return 2
        if operation.get("kind") not in MediaMonitor.KINDS:
            # Nothing to undo on the service; the row just needs closing.
            store.observe(
                operation["id"], CANCELED, {}, "abandoned by hand", announce=False
            )
            print(f"{args.operation} marked canceled; nothing deleted")
            return 0
        service = media.from_config(config.current(), config.secrets(), log)
        if service is None:
            print("media is disabled or its configuration/API keys are incomplete")
            return 1
        metadata = operation.get("metadata") or {}
        command_ids = metadata.get("command_ids") or []
        try:
            if operation["kind"] == "movie_acquisition":
                result = service.delete_movie(metadata["catalog_id"], command_ids)
            else:
                seasons = metadata.get("seasons")
                episode_ids = metadata.get("episode_ids")
                if episode_ids is None and metadata.get("episodes"):
                    # A request still waiting for Sonarr to name its episodes
                    # has an episode scope all the same. Without resolving it
                    # here the scope reads as absent, which means every season
                    # - and this deletes files.
                    episode_ids = service.episodes_in_scope(
                        metadata["catalog_id"], metadata["episodes"]
                    )
                result = service.delete_series(
                    metadata["catalog_id"],
                    seasons=seasons,
                    all_seasons=seasons is None and episode_ids is None,
                    command_ids=command_ids,
                    episode_ids=episode_ids,
                )
            record_deleted(
                store, [operation], result, episodes=metadata.get("episodes")
            )
        except Exception as e:
            print(f"abandon failed; operation left active: {e}")
            return 1
        print(result["detail"])
        return 0

    cfg = config.current()
    secrets = config.secrets()
    counts = []
    steam = SteamSession(secrets, log, machine_name=cfg.get("steamMachineName"))
    if steam.available():
        monitor: Monitor = SteamMonitor(store, steam, log)
        counts.append(("Steam", monitor.reconcile_once()))
    service = media.from_config(cfg, secrets, log)
    if service is not None:
        monitor = MediaMonitor(store, service, log)
        counts.append(("media", monitor.reconcile_once()))
    if not counts:
        print("no external operation authorities are configured")
        return 1
    print(
        "; ".join(
            f"reconciled {count} active {name} operation(s)" for name, count in counts
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
