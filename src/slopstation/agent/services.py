"""The services the voice lane shares with text, MCP and the doctors, owned
in one place: built once, started before the microphone, stopped together.

Every piece is optional and says so with lane_up or lane_disabled. A dry run
starts nothing that would write to an authority."""

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
        # Every thread this owner started, by lane name, for health().
        self.threads: list[tuple[str, threading.Thread]] = []

    def start(self, stt_live, duck):
        """Build and start everything. `stt_live` gates the announcer, which
        speaks; `duck` is the room volume control the announcer shares with
        the sessions, built by the caller because a bulletin can arrive
        before the first wake."""
        cfg, secrets, log = self.cfg, self.secrets, self.log
        from slopstation.agent.interfaces import mcp, text
        from slopstation.agent.tools import (
            library,
            media,
            operations_monitors,
            steam_session,
        )
        from slopstation.agent.tools import operations as operations_mod

        # The catalog refreshes on its own clock, never blocking wake detection.
        self._ticker(
            events.Ticker("library-sync", library.SYNC_S, library.periodic_sync())
        )

        # A ledger that cannot be read disables everything that would write
        # it; the file is left for a person, and the doctor names it.
        self.operations = self._optional(
            "operations", operations_mod.OperationStore, log
        )
        if self.operations is not None and stt_live and not self.dry_run:
            self.announcer = self._optional("announcer", self._announcer, duck)

        # Remote install + download status over ClientComm. Without a refresh
        # token, install_game keeps its controller-driven fallback. Never fatal.
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
            # Its reconcile dispatches deferred Sonarr searches and
            # indexer-recovery retries, both of which POST to the authority.
            poll_s = cfg["media"].get("pollS", operations_mod.POLL_S)
            self._monitor(
                "media_operation_monitor",
                lambda: operations_monitors.MediaMonitor(
                    self.operations, self.media, log, poll_s=poll_s
                ),
                live_only=True,
            )
        # It writes the listening port into a live qBittorrent, so a dry run
        # must not start it.
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
        # and no dry_run of its own - both ride along inside that hop.
        mcp.start(self)

    def toolkit(self, dispatch, **kwargs):
        """The tools one conversation runs over these services. A session
        builds its own Dispatch (it carries the utterance) and its own
        Toolkit (it carries the loaded set); what every session shares is
        filled in here."""
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
        """Signal every thread this owner started; the servers close their
        sockets. Runs on a clean exit or an exception out of the wake loop;
        a supervisor kill never reaches it, and nothing here needs it to.
        Nothing waits for an authority: outstanding work stays outstanding
        in the ledger."""
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
        """What is up, for the text interface's /health and the doctor: which
        services were built, and whether each thread this owner started is
        still running. A name missing from `threads` was never started (off
        by config, or a dry run); False means it died since."""
        return {
            "operations": self.operations is not None,
            "steam": self.steam is not None,
            "media": self.media is not None,
            "threads": {name: thread.is_alive() for name, thread in self.threads},
        }

    def serve(self, server, name):
        """Run an HTTP server on its own thread and keep both, so stop()
        closes it and health() reports it."""
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
        """Build one optional piece; a raise disables that piece, not the
        lane. The rest keep starting, and the doctor names what is missing."""
        try:
            return build(*args, **kwargs)
        except Exception as e:
            self.log.error("lane_disabled", what=what, reason=str(e))
            return None

    def _monitor(self, what, build, live_only=False):
        """Build and start an optional poller and say so. A dry run never
        builds one that writes to an authority; None means not configured."""
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
