"""The services the voice lane shares with text, MCP and the doctors, owned
in one place: built once, started before the microphone, stopped together.

Every piece is optional and says so with lane_up or lane_disabled. A dry run
starts nothing that would write to an authority."""

import time

from slopstation import events


class Services:
    def __init__(self, cfg, secrets, log, dry_run=False):
        self.cfg, self.secrets, self.log, self.dry_run = cfg, secrets, log, dry_run
        self.operations = None
        self.announcer = None
        self.steam = None
        self.media = None
        self.monitors: list = []
        self.servers: list = []
        self.tickers: list = []

    def start(self, stt_live, duck):
        """Build and start everything. `stt_live` gates the announcer, which
        speaks; `duck` is the room volume control the announcer shares with
        the sessions, built by the caller because a bulletin can arrive
        before the first wake."""
        cfg, secrets, log = self.cfg, self.secrets, self.log
        from slopstation.agent.interfaces import mcp, text
        from slopstation.agent.speech import announce
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

        self.operations = operations_mod.OperationStore(log)
        if stt_live and not self.dry_run:
            self.announcer = announce.Announcer(cfg["voice"], secrets, log)
            self.announcer.store = self.operations
            self.announcer.duck = duck
            self.operations.on_terminal = self.announcer.submit
            self.operations.on_notification = self.announcer.submit_notification
            for operation in self.operations.pending_announcements():
                self.announcer.submit(operation)
            for notification in self.operations.pending_notifications():
                self.announcer.submit_notification(notification)

        # Remote install + download status over ClientComm. Without a refresh
        # token, install_game keeps its controller-driven fallback. Never fatal.
        account = steam_session.SteamSession(
            secrets, log, machine_name=cfg.get("steamMachineName")
        )
        if account.available():
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
        else:
            log(
                "lane_disabled",
                what="steam_session",
                reason="no refresh token - run steam_session enroll",
            )

        if self.steam is not None:
            self._monitor(
                "operation_monitor",
                lambda: operations_monitors.SteamMonitor(
                    self.operations, self.steam, log
                ),
                lambda m: dict(
                    active=len(self.operations.active(kind="steam_install"))
                ),
                live_only=True,
            )

        self.media = media.from_config(cfg, secrets, log)
        if self.media is not None:
            # Its reconcile dispatches deferred Sonarr searches and
            # indexer-recovery retries, both of which POST to the authority.
            poll_s = cfg["media"].get("pollS", operations_mod.POLL_S)
            self._monitor(
                "media_operation_monitor",
                lambda: operations_monitors.MediaMonitor(
                    self.operations, self.media, log, poll_s=poll_s
                ),
                lambda m: dict(
                    active=sum(len(self.operations.active(kind=k)) for k in m.KINDS)
                ),
                live_only=True,
            )
        # It writes the listening port into a live qBittorrent, so a dry run
        # must not start it.
        self._monitor(
            "proton_port_sync",
            lambda: media.proton_port_monitor_from_config(cfg, secrets, log),
            lambda m: {},
            live_only=True,
        )
        self._monitor(
            "media_health_sync",
            lambda: media.media_health_monitor_from_config(
                cfg, secrets, log, operations=self.operations
            ),
            lambda m: {},
        )
        self._monitor(
            "disk_watch",
            lambda: media.disk_health_monitor_from_config(cfg, log),
            lambda m: dict(mounts=" ".join(m.mounts)),
        )

        self._server(
            text.start(
                cfg,
                secrets,
                log,
                operations=self.operations,
                steam=self.steam,
                media=self.media,
                dry_run=self.dry_run,
            )
        )
        # Forwards to the text interface over localhost, so it takes no tools
        # and no dry_run of its own - both ride along inside that hop.
        self._server(mcp.start(cfg, secrets, log))

    def _monitor(self, what, build, fields, live_only=False):
        """Build and start an optional poller and say so. A dry run never
        builds one that writes to an authority; None means not configured."""
        if live_only and self.dry_run:
            return
        monitor = build()
        if monitor is None:
            return
        monitor.start()
        self.monitors.append(monitor)
        self.log("lane_up", what=what, poll_s=monitor.poll_s, **fields(monitor))

    def _server(self, server):
        if server is not None:
            self.servers.append(server)

    def _ticker(self, ticker):
        ticker.start()
        self.tickers.append(ticker)
