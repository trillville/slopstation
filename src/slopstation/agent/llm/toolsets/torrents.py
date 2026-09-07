"""Tools over qBittorrent, under the ownership rule.

Radarr and Sonarr own the identity, location and lifecycle of their
downloads. These tools may observe anything and may change speed, order and
tracker contact, but never the identity, location or existence of a linked
torrent. The torrent-to-media link (MediaService.download_index) enforces
it: a linked torrent gets a refusal naming the arr tool to use instead.
"""

from __future__ import annotations

import re
from typing import Any

from slopstation.agent.llm import paging
from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.tools import media_proton

HASH_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
STATES = {
    "all": None,
    "downloading": "downloading",
    "seeding": "seeding",
    "completed": "completed",
    "paused": "stopped",
    "stalled": "stalled",
    "errored": "errored",
    "active": "active",
    "inactive": "inactive",
}
ORDER = {
    "top": "topPrio",
    "bottom": "bottomPrio",
    "up": "increasePrio",
    "down": "decreasePrio",
}

_OWNERSHIP = (
    " Radarr and Sonarr own their downloads: use the movie and TV tools for "
    "anything that changes what a linked torrent is or whether it exists."
)

LIST_TORRENTS = (
    """\
List torrents in qBittorrent: by state (downloading, seeding, completed,
paused, stalled, errored, active, inactive, all), category, or a name
fragment. Each row carries the movie or series it belongs to when Radarr or
Sonarr is waiting on it, so say the title, not the release name. Returns the
count first and one page of rows, most recently added first."""
    + _OWNERSHIP
)

TORRENT_DETAILS = """\
One torrent in full: state, sizes, ratio, seeding time, save path, trackers'
status (not their URLs), and its files with progress. Pass the hash from
list_torrents."""

PAUSE_TORRENT = """\
Pause (stop) torrents by hash, or every torrent with hashes ['all']. Harmless
and reversible; Radarr and Sonarr show a paused download and carry on when it
resumes. Reports the state qBittorrent holds afterwards."""

RESUME_TORRENT = """\
Resume (start) torrents by hash, or every torrent with hashes ['all']. Reports
the state qBittorrent holds afterwards."""

RECHECK_TORRENT = "Re-verify a torrent's data on disk, by hash. Slow on a large one; the torrent pauses while it runs."
REANNOUNCE_TORRENT = (
    "Force tracker contact for a torrent, by hash: the first thing to try on a stall."
)
FORCE_START = "Start a torrent regardless of the queue limits, by hash. Use for the one download that must not wait."
SET_TORRENT_PRIORITY = (
    "Move a torrent in the download queue: top, bottom, up or down, by hash."
)

DELETE_TORRENT = """\
Remove a torrent qBittorrent holds, optionally with its files - the cleanup
for an ORPHAN: a torrent Radarr and Sonarr are not waiting on. A torrent one
of them is waiting on is refused here; cancel it through resolve_queue_item
or delete_media so the arr app stays consistent. This erases data and cannot
be undone: the first call answers with what would go; say it back and call
again unchanged only once the user has said yes."""

TRANSFER_INFO = """\
qBittorrent's global picture: download and upload speeds, session totals,
connection status, DHT nodes, whether the alternative speed limits are on,
free space on the download disk, and the current global limits."""

SET_SPEED_LIMITS = """\
Set qBittorrent speed limits in KB/s. Without a hash: the global download
and upload limits (0 lifts a limit), and `alternative` true or false switches
the alternative limits on or off. With a hash: that torrent's own limits.
Share limits (ratio, seed time) are set by the central seed policy in config
and are not changed here. Reports the limits in force afterwards."""

SEEDING_REPORT = """\
What is seeding: each torrent's ratio, upload total and seeding time, the
share limits it is under, and totals. Sorted by ratio, highest first. Returns
the count and one page of rows."""

ORPHAN_TORRENTS = """\
Torrents nobody asked for through Radarr or Sonarr: completed torrents that
no arr app is waiting on and that appear in neither app's history, so they
were never imported and will never be cleaned up automatically. These are
what delete_torrent is for."""

VPN_STATUS = """\
Whether qBittorrent's peer traffic is on the VPN: the network interface it is
bound to, its listening port, the port Proton last forwarded and when, and
whether the two agree. A disagreement means peers cannot reach it."""

QBIT_LOG = "qBittorrent's recent log lines, warnings and critical by default, newest first. Returns the count and one page of lines."

SPECS = [
    ToolSpec(
        "list_torrents",
        LIST_TORRENTS,
        {
            "state": {"type": "string", "enum": list(STATES)},
            "category": {"type": "string", "description": "exact category name"},
            "name": {"type": "string", "description": "a fragment of the torrent name"},
            **paging.properties(),
        },
        (),
        risk="read",
        area="media",
        keywords=(
            "torrents",
            "what is downloading",
            "seeding",
            "stalled",
            "qbittorrent",
            "download list",
            "ratio",
        ),
        default=False,
        needs=("torrents",),
        paged=True,
        busy="checking the torrents",
    ),
    ToolSpec(
        "torrent_details",
        TORRENT_DETAILS,
        {"hash": {"type": "string", "description": "torrent hash from list_torrents"}},
        ("hash",),
        risk="read",
        area="media",
        keywords=(
            "torrent details",
            "torrent files",
            "trackers",
            "availability",
            "save path",
        ),
        default=False,
        needs=("torrents",),
        busy="checking the torrents",
    ),
    ToolSpec(
        "pause_torrent",
        PAUSE_TORRENT,
        {
            "hashes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "hashes, or ['all']",
            }
        },
        ("hashes",),
        risk="act",
        area="media",
        keywords=(
            "pause torrent",
            "stop torrent",
            "pause download",
            "pause all torrents",
        ),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "resume_torrent",
        RESUME_TORRENT,
        {
            "hashes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "hashes, or ['all']",
            }
        },
        ("hashes",),
        risk="act",
        area="media",
        keywords=("resume torrent", "start torrent", "unpause", "resume all torrents"),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "recheck_torrent",
        RECHECK_TORRENT,
        {"hash": {"type": "string"}},
        ("hash",),
        risk="act",
        area="media",
        keywords=("recheck torrent", "verify torrent data", "force recheck"),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "reannounce_torrent",
        REANNOUNCE_TORRENT,
        {"hash": {"type": "string"}},
        ("hash",),
        risk="act",
        area="media",
        keywords=("reannounce", "tracker contact", "stalled torrent", "no peers"),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "force_start",
        FORCE_START,
        {"hash": {"type": "string"}},
        ("hash",),
        risk="act",
        area="media",
        keywords=("force start", "start now", "skip the queue", "bypass queue"),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "set_torrent_priority",
        SET_TORRENT_PRIORITY,
        {
            "hash": {"type": "string"},
            "position": {"type": "string", "enum": list(ORDER)},
        },
        ("hash", "position"),
        risk="act",
        area="media",
        keywords=(
            "torrent priority",
            "move to top",
            "top of the queue",
            "move that torrent",
            "queue position",
            "download first",
        ),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "delete_torrent",
        DELETE_TORRENT,
        {
            "hash": {"type": "string"},
            "delete_files": {
                "type": "boolean",
                "description": "also erase the data on disk",
            },
        },
        ("hash",),
        risk="destructive",
        area="media",
        keywords=(
            "delete torrent",
            "remove torrent",
            "orphan torrent",
            "clean up torrents",
            "erase download",
        ),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "transfer_info",
        TRANSFER_INFO,
        {},
        (),
        risk="read",
        area="media",
        keywords=(
            "download speed",
            "upload speed",
            "transfer",
            "qbittorrent status",
            "dht",
            "connection status",
        ),
        default=False,
        needs=("torrents",),
        busy="checking the torrents",
    ),
    ToolSpec(
        "set_speed_limits",
        SET_SPEED_LIMITS,
        {
            "download_kbps": {"type": "integer", "description": "0 lifts the limit"},
            "upload_kbps": {"type": "integer", "description": "0 lifts the limit"},
            "alternative": {
                "type": "boolean",
                "description": "switch the alternative limits on or off",
            },
            "hash": {
                "type": "string",
                "description": "limit one torrent instead of everything",
            },
        },
        (),
        risk="act",
        area="media",
        keywords=(
            "speed limit",
            "throttle",
            "limit upload",
            "limit download",
            "alternative limits",
            "bandwidth",
        ),
        default=False,
        needs=("torrents",),
    ),
    ToolSpec(
        "seeding_report",
        SEEDING_REPORT,
        {**paging.properties()},
        (),
        risk="read",
        area="media",
        keywords=("seeding", "ratio", "upload total", "seed time", "share limits"),
        default=False,
        needs=("torrents",),
        paged=True,
        busy="checking the torrents",
    ),
    ToolSpec(
        "orphan_torrents",
        ORPHAN_TORRENTS,
        {},
        (),
        risk="read",
        area="media",
        keywords=(
            "orphan torrents",
            "never imported",
            "stray downloads",
            "unknown torrents",
            "leftover",
        ),
        default=False,
        needs=("torrents",),
        busy="checking the torrents",
    ),
    ToolSpec(
        "vpn_status",
        VPN_STATUS,
        {},
        (),
        risk="read",
        area="media",
        keywords=(
            "vpn",
            "proton",
            "listening port",
            "port forward",
            "interface binding",
            "is the vpn up",
        ),
        default=False,
        needs=("torrents",),
        busy="checking the VPN",
    ),
    ToolSpec(
        "qbit_log",
        QBIT_LOG,
        {
            "everything": {
                "type": "boolean",
                "description": "include normal and info lines",
            },
            **paging.properties(what="lines"),
        },
        (),
        risk="read",
        area="media",
        keywords=("qbittorrent log", "torrent errors", "client log", "why did it fail"),
        default=False,
        needs=("torrents",),
        paged=True,
        busy="reading the log",
    ),
]


def _kbps(value):
    return round(int(value or 0) / 1024)


def _row(t, link):
    """One torrent as the model reads it. The release name is present for the
    text lane; the linked title is what gets spoken."""
    linked = link.get(str(t.get("hash", "")).lower())
    return {
        "hash": t.get("hash"),
        "name": t.get("name"),
        "media": linked["title"] if linked else None,
        "owner": linked["authority"] if linked else None,
        "state": t.get("state"),
        "percent": round(100 * float(t.get("progress", 0) or 0)),
        "size_gb": round(int(t.get("size", 0) or 0) / 1024**3, 2),
        "down_kbps": _kbps(t.get("dlspeed")),
        "up_kbps": _kbps(t.get("upspeed")),
        "eta_min": None
        if int(t.get("eta", 0) or 0) >= 8640000
        else round(int(t.get("eta", 0) or 0) / 60),
        "ratio": round(float(t.get("ratio", 0) or 0), 2),
        "seeds": t.get("num_seeds"),
        "category": t.get("category"),
        "added": t.get("added_on"),
    }


def impls(ctx: ToolContext):
    bind = Bindings(ctx, SPECS)
    log, media = ctx.log, ctx.media
    qbit: Any = getattr(media, "qbit", None)

    def _hash(value):
        h = str(value or "").strip()
        return h.lower() if HASH_RE.match(h) else None

    def _hashes(args):
        raw = args.get("hashes")
        if raw == ["all"] or raw == "all":
            return "all", None
        if not isinstance(raw, list) or not raw:
            return None, {
                "ok": False,
                "error": "pass a list of torrent hashes, or ['all']",
            }
        out = []
        for v in raw:
            h = _hash(v)
            if h is None:
                return None, {"ok": False, "error": f"{v!r} is not a torrent hash"}
            out.append(h)
        return out, None

    def _one(args):
        h = _hash(args.get("hash"))
        return (
            (h, None)
            if h
            else (
                None,
                {"ok": False, "error": "pass the torrent hash from list_torrents"},
            )
        )

    def _link():
        """For reads: an unreadable queue means rows show no owner."""
        try:
            return media.download_index()
        except Exception as e:
            log.warn("download_index_failed", err=str(e))
            return {}

    def _after(hashes):
        """The state qBittorrent holds after an action, for the reply."""
        rows = qbit.torrents()
        wanted = None if hashes == "all" else set(hashes)
        return [
            {"hash": t.get("hash"), "name": t.get("name"), "state": t.get("state")}
            for t in rows
            if wanted is None or str(t.get("hash", "")).lower() in wanted
        ][: paging.CAP]

    def _act(name, args, fn, hashes_key="hashes"):
        if hashes_key == "hashes":
            hashes, err = _hashes(args)
        else:
            h, err = _one(args)
            hashes = [h] if h else None
        if err:
            return err
        if dry := ctx.preview(f"{name} {hashes}"):
            return dry
        try:
            fn(hashes)
            return {"ok": True, "torrents": _after(hashes)}
        except Exception as e:
            log.error("tool_error", tool=name, err=str(e))
            return {"ok": False, "error": str(e)}

    @bind
    def list_torrents(args):
        state = str(args.get("state") or "all")
        if state not in STATES:
            return {"ok": False, "error": f"state must be one of {', '.join(STATES)}"}
        try:
            rows = qbit.torrents(
                filter=STATES[state],
                category=args.get("category") or None,
                sort="added_on",
                reverse=True,
            )
        except Exception as e:
            log.error("tool_error", tool="list_torrents", err=str(e))
            return {"ok": False, "error": str(e)}
        frag = str(args.get("name") or "").lower().strip()
        if frag:
            rows = [t for t in rows if frag in str(t.get("name", "")).lower()]
        out = paging.page(rows, args, "torrents")
        if out["ok"]:
            link = _link()
            out["torrents"] = [_row(t, link) for t in out["torrents"]]
        return out

    @bind
    def torrent_details(args):
        h, err = _one(args)
        if err:
            return err
        try:
            props = qbit.torrent_properties(h)
            files = qbit.torrent_files(h)
            trackers = qbit.torrent_trackers(h)
        except Exception as e:
            log.error("tool_error", tool="torrent_details", err=str(e))
            return {"ok": False, "error": str(e)}
        link = _link().get(h)
        return {
            "ok": True,
            "hash": h,
            "media": link["title"] if link else None,
            "owner": link["authority"] if link else None,
            "save_path": props.get("save_path"),
            "size_gb": round(int(props.get("total_size", 0) or 0) / 1024**3, 2),
            "downloaded_gb": round(
                int(props.get("total_downloaded", 0) or 0) / 1024**3, 2
            ),
            "uploaded_gb": round(int(props.get("total_uploaded", 0) or 0) / 1024**3, 2),
            "ratio": round(float(props.get("share_ratio", 0) or 0), 2),
            "seeding_hours": round(int(props.get("seeding_time", 0) or 0) / 3600, 1),
            "seeds": props.get("seeds_total"),
            "peers": props.get("peers_total"),
            "added": props.get("addition_date"),
            "completed": props.get("completion_date"),
            # Tracker URLs carry passkeys: status only.
            "trackers": [
                {
                    "status": t.get("status"),
                    "msg": t.get("msg"),
                    "peers": t.get("num_peers"),
                }
                for t in (trackers or [])
                if not str(t.get("url", "")).startswith("**")
            ],
            "file_count": len(files or []),
            "files": [
                {
                    "name": f.get("name"),
                    "gb": round(int(f.get("size", 0) or 0) / 1024**3, 2),
                    "percent": round(100 * float(f.get("progress", 0) or 0)),
                    "priority": f.get("priority"),
                }
                for f in sorted(files or [], key=lambda f: -int(f.get("size", 0) or 0))[
                    :20
                ]
            ],
        }

    @bind
    def pause_torrent(args):
        return _act("pause_torrent", args, lambda h: qbit.torrent_action("stop", h))

    @bind
    def resume_torrent(args):
        return _act("resume_torrent", args, lambda h: qbit.torrent_action("start", h))

    @bind
    def recheck_torrent(args):
        return _act(
            "recheck_torrent", args, lambda h: qbit.torrent_action("recheck", h), "hash"
        )

    @bind
    def reannounce_torrent(args):
        return _act(
            "reannounce_torrent",
            args,
            lambda h: qbit.torrent_action("reannounce", h),
            "hash",
        )

    @bind
    def force_start(args):
        return _act(
            "force_start", args, lambda h: qbit.set_force_start(h, True), "hash"
        )

    @bind
    def set_torrent_priority(args):
        position = str(args.get("position") or "")
        if position not in ORDER:
            return {"ok": False, "error": f"position must be one of {', '.join(ORDER)}"}
        return _act(
            "set_torrent_priority",
            args,
            lambda h: qbit.torrent_action(ORDER[position], h),
            "hash",
        )

    @bind.destructive
    def delete_torrent(args):
        h, err = _one(args)
        if err:
            return err
        delete_files = bool(args.get("delete_files", False))
        try:
            # Strict: an unread queue must refuse, not pass as unlinked.
            link = media.download_index(strict=True).get(h)
        except Exception as e:
            log.warn("download_index_failed", err=str(e))
            return {
                "ok": False,
                "error": "could not read Radarr's or Sonarr's queue, so cannot "
                "tell whether this download is theirs - try again shortly",
            }
        if link:
            log.warn(
                "tool_refused",
                tool="delete_torrent",
                reason="linked",
                catalog_id=link.get("queue_id"),
            )
            return {
                "ok": False,
                "error": f"{link['authority']} is waiting on this download for "
                f"{link['title']}: cancel it with resolve_queue_item, or remove the "
                "title with delete_media, so the arr app stays consistent",
            }
        try:
            rows = [t for t in qbit.torrents() if str(t.get("hash", "")).lower() == h]
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not rows:
            return {"ok": False, "error": "no torrent with that hash"}
        name = rows[0].get("name")
        size_gb = round(int(rows[0].get("size", 0) or 0) / 1024**3, 2)

        def act():
            try:
                qbit.delete_torrents([h], delete_files)
            except Exception as e:
                log.error("tool_error", tool="delete_torrent", err=str(e))
                return {"ok": False, "error": str(e)}
            return {
                "ok": True,
                "deleted": name,
                "files_erased": delete_files,
                "size_gb": size_gb,
            }

        return Plan(
            ("torrent", h, delete_files),
            f"Remove the torrent {name}"
            + (
                f" and erase its {size_gb} GB of files?"
                if delete_files
                else ", keeping its files?"
            ),
            act,
            f"delete torrent {name} (files: {delete_files})",
        )

    @bind
    def transfer_info(args):
        try:
            info = qbit.transfer_info()
            state = qbit.server_state()
            alt = qbit.speed_limits_mode()
        except Exception as e:
            log.error("tool_error", tool="transfer_info", err=str(e))
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "down_kbps": _kbps(info.get("dl_info_speed")),
            "up_kbps": _kbps(info.get("up_info_speed")),
            "session_down_gb": round(
                int(info.get("dl_info_data", 0) or 0) / 1024**3, 2
            ),
            "session_up_gb": round(int(info.get("up_info_data", 0) or 0) / 1024**3, 2),
            "down_limit_kbps": _kbps(info.get("dl_rate_limit")),
            "up_limit_kbps": _kbps(info.get("up_rate_limit")),
            "alternative_limits": alt,
            "connection": info.get("connection_status"),
            "dht_nodes": info.get("dht_nodes"),
            "free_space_gb": round(
                int(state.get("free_space_on_disk", 0) or 0) / 1024**3, 1
            )
            if state.get("free_space_on_disk") is not None
            else None,
            "queued_disk_jobs": state.get("queued_io_jobs"),
        }

    @bind
    def set_speed_limits(args):
        down, up = args.get("download_kbps"), args.get("upload_kbps")
        alt = args.get("alternative")
        if alt is not None and not isinstance(alt, bool):
            return {"ok": False, "error": "alternative must be true or false"}
        h = _hash(args.get("hash")) if args.get("hash") else None
        if args.get("hash") and h is None:
            return {"ok": False, "error": "that is not a torrent hash"}
        for v in (down, up):
            if v is not None and (
                isinstance(v, bool) or not isinstance(v, int) or v < 0
            ):
                return {"ok": False, "error": "limits are whole KB/s, 0 to lift one"}
        if down is None and up is None and alt is None:
            return {"ok": False, "error": "say which limit to set"}
        if h and alt is not None:
            return {
                "ok": False,
                "error": "the alternative limits are global, not per torrent",
            }
        plan = f"limits down={down} up={up} alt={alt} hash={h}"
        if dry := ctx.preview(f"set {plan}"):
            return dry
        try:
            if h:
                qbit.set_torrent_limits(
                    [h],
                    None if down is None else down * 1024,
                    None if up is None else up * 1024,
                )
            else:
                qbit.set_global_limits(
                    None if down is None else down * 1024,
                    None if up is None else up * 1024,
                )
                if alt is not None and qbit.speed_limits_mode() != bool(alt):
                    qbit.toggle_speed_limits_mode()
            info = qbit.transfer_info()
            return {
                "ok": True,
                "down_limit_kbps": _kbps(info.get("dl_rate_limit")),
                "up_limit_kbps": _kbps(info.get("up_rate_limit")),
                "alternative_limits": qbit.speed_limits_mode(),
                "torrent": h,
            }
        except Exception as e:
            log.error("tool_error", tool="set_speed_limits", err=str(e))
            return {"ok": False, "error": str(e)}

    @bind
    def seeding_report(args):
        try:
            rows = qbit.torrents(filter="seeding")
            prefs = qbit.preferences()
        except Exception as e:
            log.error("tool_error", tool="seeding_report", err=str(e))
            return {"ok": False, "error": str(e)}
        rows.sort(key=lambda t: -float(t.get("ratio", 0) or 0))
        link = _link()

        def share(t):
            ratio = t.get("ratio_limit", -2)
            time_limit = t.get("seeding_time_limit", -2)
            return {
                "ratio_limit": "global"
                if ratio == -2
                else ("none" if ratio == -1 else ratio),
                "time_limit_min": "global"
                if time_limit == -2
                else ("none" if time_limit == -1 else time_limit),
            }

        def shape(t):
            return {
                **{
                    k: _row(t, link)[k]
                    for k in ("hash", "name", "media", "ratio", "up_kbps", "size_gb")
                },
                "uploaded_gb": round(int(t.get("uploaded", 0) or 0) / 1024**3, 2),
                "seeding_hours": round(int(t.get("seeding_time", 0) or 0) / 3600, 1),
                **share(t),
            }

        out = paging.page(
            rows,
            args,
            "torrents",
            global_policy={
                "max_ratio": prefs.get("max_ratio")
                if prefs.get("max_ratio_enabled")
                else None,
                "max_seeding_minutes": prefs.get("max_seeding_time")
                if prefs.get("max_seeding_time_enabled")
                else None,
            },
            total_uploaded_gb=round(
                sum(int(t.get("uploaded", 0) or 0) for t in rows) / 1024**3, 2
            ),
        )
        if out["ok"]:
            out["torrents"] = [shape(t) for t in out["torrents"]]
        return out

    @bind
    def orphan_torrents(args):
        try:
            rows = qbit.torrents(filter="completed")
            link = media.download_index(strict=True)
            unlinked = [t for t in rows if str(t.get("hash", "")).lower() not in link]
            orphans = [
                t
                for t in unlinked
                if not media.download_known(str(t.get("hash", "")).lower())
            ]
        except Exception as e:
            log.error("tool_error", tool="orphan_torrents", err=str(e))
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "count": len(orphans),
            "torrents": [
                {
                    "hash": t.get("hash"),
                    "name": t.get("name"),
                    "size_gb": round(int(t.get("size", 0) or 0) / 1024**3, 2),
                    "category": t.get("category"),
                    "completed": t.get("completion_on"),
                }
                for t in orphans[: paging.CAP]
            ],
            "detail": "none of these was requested through Radarr or Sonarr; "
            "delete_torrent is the cleanup",
        }

    @bind
    def vpn_status(args):
        try:
            prefs = qbit.preferences()
        except Exception as e:
            log.error("tool_error", tool="vpn_status", err=str(e))
            return {"ok": False, "error": str(e)}
        try:
            proton = media_proton.read_proton_port_state()
        except Exception as e:
            proton = {"state": "unreadable", "detail": str(e)}
        listen = int(prefs.get("listen_port", 0) or 0)
        forwarded = proton.get("port") if isinstance(proton, dict) else None
        active = isinstance(proton, dict) and proton.get("state") == "active"
        return {
            "ok": True,
            "interface": prefs.get("current_interface_name")
            or prefs.get("current_network_interface"),
            "bound_address": prefs.get("current_interface_address"),
            "listen_port": listen,
            "proton": proton,
            "ports_agree": active
            and forwarded is not None
            and int(forwarded) == listen,
        }

    @bind
    def qbit_log(args):
        try:
            lines = qbit.main_log(warnings_only=not bool(args.get("everything")))
        except Exception as e:
            log.error("tool_error", tool="qbit_log", err=str(e))
            return {"ok": False, "error": str(e)}
        return paging.page(
            [
                {
                    "id": ln.get("id"),
                    "type": ln.get("type"),
                    "ts": ln.get("timestamp"),
                    "msg": ln.get("message"),
                }
                for ln in reversed(lines)
            ],
            args,
            "lines",
        )

    return bind.impls()
