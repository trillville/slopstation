"""Tools over the media root: space, sizes, orphans, guarded deletion."""

from __future__ import annotations

from pathlib import Path

from slopstation.agent.llm import paging
from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.tools import storage

DISK_USAGE = """\
Free and used space: the media volume and the checkout volume, and how much
Movies, TV and the torrents folder each take (folders=false skips the folder
sizes, which walk the disk). Radarr's and Sonarr's own view of their disks is
included when they answer."""

LARGEST_ITEMS = """\
The biggest items under the media root, or under one of its folders (Movies,
TV, torrents, or a path below one), folders measured whole. Returns the
count and up to `limit` rows, biggest first."""

ORPHAN_FILES = """\
Files the media stack has lost track of: video files under Movies and TV that
no Radarr or Sonarr file record names, and entries under the torrents folder
that no torrent in qBittorrent covers. Both are candidates for delete_path;
nothing here deletes."""

DELETE_PATH = """\
Delete one file or folder inside the media root, given as a path relative to
it (e.g. 'torrents/Some.Release' or 'Movies/Old Film (1999)'). Refused for
the root, the Movies, TV and torrents folders themselves, anything outside
the root, and anything Radarr, Sonarr or qBittorrent still references - use
their tools for those. This erases data and cannot be undone: the first call
answers with what would go; say it back and call again unchanged only once
the user has said yes."""

DRIVE_HEALTH = """\
The drives' health: free space against the low-space threshold on each
volume, and the most recent SMART warning the disk monitor raised, if any.
The media drive is a recertified external, so a SMART warning is worth
saying plainly."""

SPECS = [
    ToolSpec(
        "disk_usage",
        DISK_USAGE,
        {
            "folders": {
                "type": "boolean",
                "description": "measure Movies, TV and torrents (default true)",
            }
        },
        (),
        risk="read",
        area="storage",
        keywords=(
            "disk space",
            "free space",
            "how full",
            "storage",
            "how much room",
            "drive",
        ),
        default=False,
        needs=("media",),
        busy="checking the drives",
    ),
    ToolSpec(
        "largest_items",
        LARGEST_ITEMS,
        {
            "under": {
                "type": "string",
                "description": "Movies, TV, torrents, or a path below one; empty for the root",
            },
            **paging.properties(cap=50),
        },
        (),
        risk="read",
        area="storage",
        keywords=(
            "largest files",
            "biggest folders",
            "what takes the most space",
            "space hogs",
        ),
        default=False,
        needs=("media",),
        paged=True,
        busy="measuring the folders",
    ),
    ToolSpec(
        "orphan_files",
        ORPHAN_FILES,
        {},
        (),
        risk="read",
        area="storage",
        keywords=(
            "orphan files",
            "leftover files",
            "unknown files",
            "lost track",
            "stray files",
        ),
        default=False,
        needs=("media",),
        busy="checking the files",
    ),
    ToolSpec(
        "delete_path",
        DELETE_PATH,
        {"path": {"type": "string", "description": "path relative to the media root"}},
        ("path",),
        risk="destructive",
        area="storage",
        keywords=(
            "delete file",
            "delete folder",
            "delete that folder",
            "folder from the disk",
            "remove files",
            "erase folder",
            "clean up disk",
        ),
        default=False,
        needs=("media",),
    ),
    ToolSpec(
        "drive_health",
        DRIVE_HEALTH,
        {},
        (),
        risk="read",
        area="storage",
        keywords=(
            "drive health",
            "smart",
            "disk failing",
            "disk warning",
            "is the drive ok",
        ),
        default=False,
        needs=("media",),
        busy="checking the drive",
    ),
]


def impls(ctx: ToolContext):
    bind = Bindings(ctx, SPECS)
    log, media = ctx.log, ctx.media

    def _root():
        root = storage.media_root()
        if root is None:
            return None, {
                "ok": False,
                "error": "no MEDIA_ROOT in media/.env on this machine",
            }
        return root, None

    def _arr_files(root: Path) -> set[Path]:
        """Host paths of every file Radarr and Sonarr hold. Raises when an
        app cannot be read: callers decide whether a partial index is safe."""
        known = set()
        for path in media.arr_files():
            hp = storage.host_path(root, path)
            if hp is not None:
                try:
                    known.add(hp.resolve())
                except OSError:
                    continue
        return known

    def _torrent_paths() -> set[Path]:
        """Every torrent's content path. A torrent still fetching metadata has
        none and is skipped: its save path is the whole torrents folder."""
        qbit = getattr(media, "qbit", None)
        if qbit is None:
            return set()
        out = set()
        for t in qbit.torrents():
            cp = t.get("content_path")
            if cp:
                try:
                    out.add(Path(str(cp)).resolve())
                except OSError:
                    continue
        return out

    def _index(root: Path):
        """Both indexes, or None with the reason when one cannot be read."""
        try:
            return _arr_files(root), _torrent_paths(), None
        except Exception as e:
            log.warn("file_index_failed", err=str(e))
            return None, None, str(e)

    @bind
    def disk_usage(args):
        root, err = _root()
        if err:
            return err
        folders = args.get("folders", True)
        out = {
            "ok": True,
            "root": str(root),
            **storage.disk_usage(root, folders=bool(folders)),
        }
        arr = []
        for client in (media.radarr, media.sonarr):
            try:
                for row in client.get("diskspace") or []:
                    arr.append(
                        {
                            "app": client.name,
                            "path": row.get("path"),
                            "free_gb": round(
                                int(row.get("freeSpace", 0) or 0) / 1024**3, 1
                            ),
                            "total_gb": round(
                                int(row.get("totalSpace", 0) or 0) / 1024**3, 1
                            ),
                        }
                    )
            except Exception as e:
                log.warn("diskspace_read_failed", authority=client.name, err=str(e))
        if arr:
            out["arr_view"] = arr
        return out

    @bind
    def largest_items(args):
        root, err = _root()
        if err:
            return err
        under = str(args.get("under") or "")
        bounds, err = paging.window(args, cap=50)
        if err:
            return err
        limit, offset = bounds
        rows = storage.largest_items(root, under, offset + limit)
        if not rows and under and storage._inside(root, under) is None:
            return {"ok": False, "error": "that path is not under the media root"}
        return paging.page(rows, args, "items", cap=50, under=under or "/")

    @bind
    def orphan_files(args):
        root, err = _root()
        if err:
            return err
        known, torrents, why = _index(root)
        if why is not None:
            return {
                "ok": False,
                "error": f"could not read what the media apps hold ({why}), so "
                "nothing can be called an orphan right now",
            }
        out = storage.orphan_files(root, known, torrents)
        return {
            "ok": True,
            "unknown_media_count": len(out["unknown_media"]),
            "stray_download_count": len(out["stray_downloads"]),
            "unknown_media": out["unknown_media"][:40],
            "stray_downloads": out["stray_downloads"][:40],
        }

    @bind.destructive
    def delete_path(args):
        root, err = _root()
        if err:
            return err
        rel = str(args.get("path") or "").strip()
        target = storage.deletable(root, rel) if rel else None
        if target is None:
            return {
                "ok": False,
                "error": "that path is the root, a top folder, outside the root, "
                "or does not exist",
            }
        resolved = target.resolve()
        known_files, torrents, why = _index(root)
        if why is not None:
            return {
                "ok": False,
                "error": f"could not read what the media apps hold ({why}), so "
                "cannot tell whether this is still theirs - try again shortly",
            }
        for known in known_files:
            if known == resolved or resolved in known.parents:
                return {
                    "ok": False,
                    "error": "Radarr or Sonarr still holds a file there: use delete_media",
                }
        for tp in torrents:
            if tp == resolved or resolved in tp.parents or tp in resolved.parents:
                return {
                    "ok": False,
                    "error": "a torrent in qBittorrent still covers that path: use delete_torrent",
                }
        size, files = (
            storage.tree_size(target) if target.is_dir() else (target.stat().st_size, 1)
        )

        def act():
            try:
                return {"ok": True, **storage.delete(target)}
            except Exception as e:
                log.error("tool_error", tool="delete_path", err=str(e))
                return {"ok": False, "error": str(e)}

        return Plan(
            ("path", str(resolved)),
            f"Delete {rel} - {files} file(s), {round(size / 1024**3, 2)} GB? "
            "That cannot be undone.",
            act,
            f"delete {rel}",
        )

    @bind
    def drive_health(args):
        root, err = _root()
        if err:
            return err
        return {"ok": True, **storage.drive_health(root)}

    return bind.impls()
