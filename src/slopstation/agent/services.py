"""The services the voice lane shares with the text lane, the MCP wrapper and the
doctor: built once, started before the microphone, stopped together.

Every piece is optional and logs lane_up or lane_disabled. A dry run starts
nothing that writes to Radarr, Sonarr, qBittorrent or Steam."""

import threading
import time
from typing import Any

from slopstation import events


class Services:
    def __init__(self, cfg, secrets, log, dry_run=False):
        self.cfg, self.secrets, self.log, self.dry_run = cfg, secrets, log, dry_run
        self.operations: Any = None
        self.announcer: Any = None
        self.steam: Any = None
        self.media: Any = None
        self.monitors: list = []
        self.servers: list = []
        self.tickers: list = []
        # every thread this owner started, by name, for health()
        self.threads: list[tuple[str, threading.Thread]] = []

    def start(self, stt_live, duck):
        """Build and start everything. `stt_live` gates the announcer, which
        speaks. `duck` is the room volume control; the caller builds it because
        a bulletin can arrive before the first wake."""
        cfg, secrets, log = self.cfg, self.secrets, self.log
        from slopstation.agent.interfaces import mcp, text
        from slopstation.agent.tools import (
            library,
            media,
            operations_monitors,
            steam_session,
        )
        from slopstation.agent.tools import operations as operations_mod

        # Refreshes the catalog on its own clock; never blocks wake detection.
        self._ticker(
            events.Ticker("library-sync", library.SYNC_S, library.periodic_sync())
        )

        # An unreadable ledger disables everything that writes it. The file is
        # left for a person; the doctor names it.
        self.operations = self._optional(
            "operations", operations_mod.OperationStore, log
        )
        if self.operations is not None and stt_live and not self.dry_run:
            self.announcer = self._optional("announcer", self._announcer, duck)

        # Remote install and download status over ClientComm. Without a refresh
        # token, install_game falls back to the controller. Never fatal.
        account = self._optional(
            "steam_session",
            steam_session.SteamSession,
            secrets,
            log,
            machine_name=cfg.get("steamMachineName"),
        )
        if account is not None and account.available():
            self.steam = account
            exp = account.token_expiry()
            log(
                "lane_up",
                what="steam_session",
                steamid=account.steamid,
                token_expires=(
                    time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else None
                ),
            )
        elif account is not None:
            log(
                "lane_disabled",
                what="steam_session",
                reason="no refresh token - run steam_session enroll",
            )

        if self.steam is not None and self.operations is not None:
            self._monitor(
                "operation_monitor",
                lambda: operations_monitors.SteamMonitor(
                    self.operations, self.steam, log
                ),
                live_only=True,
            )

        self.media = self._optional("media", media.from_config, cfg, secrets, log)
        if self.media is not None and self.operations is not None:
            # Its reconcile POSTs deferred searches and indexer retries to
            # Sonarr and Radarr.
            poll_s = cfg["media"].get("pollS", operations_mod.POLL_S)
            self._monitor(
                "media_operation_monitor",
                lambda: operations_monitors.MediaMonitor(
                    self.operations, self.media, log, poll_s=poll_s
                ),
                live_only=True,
            )
        # Writes the listening port into a live qBittorrent; a dry run must not
        # start it.
        self._monitor(
            "proton_port_sync",
            lambda: media.proton_port_monitor_from_config(cfg, secrets, log),
            live_only=True,
        )
        self._monitor(
            "media_health_sync",
            lambda: media.media_health_monitor_from_config(
                cfg, secrets, log, operations=self.operations
            ),
        )
        self._monitor(
            "disk_watch",
            lambda: media.disk_health_monitor_from_config(cfg, log),
        )

        text.start(self)
        # Forwards to the text interface over localhost, so it takes no tools
        # and no dry_run of its own.
        mcp.start(self)

    def dispatch(self, **hooks):
        """The Dispatch one conversation runs its commands through."""
        from slopstation.agent.dispatch import Dispatch

        return Dispatch(self.cfg, self.log, dry_run=self.dry_run, **hooks)

    def toolkit(self, dispatch, **kwargs):
        """The tools for one conversation. The session owns its Dispatch and
        Toolkit; the shared services are filled in here."""
        from slopstation.agent.llm.assistant import Toolkit

        return Toolkit(
            dispatch,
            self.log,
            operations=self.operations,
            voice=self.cfg["voice"],
            steam=self.steam,
            media=self.media,
            **kwargs,
        )

    def stop(self):
        """Signal every thread this owner started and close the servers. Runs
        on a clean exit, not on a supervisor kill. Nothing waits: outstanding
        work stays in the ledger."""
        for ticker in self.tickers:
            ticker.stop.set()
        for monitor in self.monitors:
            monitor.stop()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        if self.announcer is not None:
            self.announcer.stop()

    def health(self):
        """What is up, for /health and the doctor: which services were built,
        and whether each thread this owner started is alive. A name missing
        from `threads` was never started; False means it died."""
        return {
            "operations": self.operations is not None,
            "steam": self.steam is not None,
            "media": self.media is not None,
            "threads": {name: thread.is_alive() for name, thread in self.threads},
        }

    def serve(self, server, name):
        """Run an HTTP server on its own thread; keep both for stop() and
        health()."""
        thread = threading.Thread(target=server.serve_forever, daemon=True, name=name)
        thread.start()
        self.servers.append(server)
        self.threads.append((name, thread))

    def _announcer(self, duck):
        from slopstation.agent.speech import announce

        announcer = announce.Announcer(self.cfg["voice"], self.secrets, self.log)
        announcer.store = self.operations
        announcer.duck = duck
        self.operations.on_terminal = announcer.submit
        self.operations.on_notification = announcer.submit_notification
        for operation in self.operations.pending_announcements():
            announcer.submit(operation)
        for notification in self.operations.pending_notifications():
            announcer.submit_notification(notification)
        self.threads.append(("announcer", announcer.start()))
        return announcer

    def _optional(self, what, build, *args, **kwargs):
        """Build one optional piece. A raise disables that piece only, with a
        lane_disabled line."""
        try:
            return build(*args, **kwargs)
        except Exception as e:
            self.log.error("lane_disabled", what=what, reason=str(e))
            return None

    def _monitor(self, what, build, live_only=False):
        """Build and start an optional poller and log lane_up. A dry run skips
        one that writes to a service; None means not configured."""
        if live_only and self.dry_run:
            return
        monitor = self._optional(what, build)
        if monitor is None:
            return
        self.threads.append((what, monitor.start()))
        self.monitors.append(monitor)
        self.log("lane_up", what=what, poll_s=monitor.poll_s)

    def _ticker(self, ticker):
        ticker.start()
        self.tickers.append(ticker)
        self.threads.append((ticker.name, ticker))
