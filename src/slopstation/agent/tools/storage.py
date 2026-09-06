"""Read and tidy the media root: space, sizes, orphans, guarded deletion.

Scope stops at the media root and the K15's own volumes. Anything wider is
a different risk class and a separate ask.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from slopstation import paths
from slopstation.agent.tools.disk_health import FREE_WARN_BYTES

# The three folders Compose mounts under /data. Deleting one of them, or the
# root, is never a tidy-up.
TOP_FOLDERS = ("Movies", "TV", "torrents")
CONTAINER_PREFIX = "/data/"
GB = 1024**3


def media_root() -> Path | None:
    """MEDIA_ROOT from media/.env, as the disk watch reads it. None on a
    checkout that is not the K15."""
    from slopstation.agent.tools.media import _media_root

    root = _media_root(paths.HOME / "media" / ".env")
    return Path(root) if root else None


def host_path(root: Path, container_path: str) -> Path | None:
    """/data/Movies/x -> <root>/Movies/x. None for a path outside /data."""
    p = str(container_path).replace("\\", "/")
    if not p.startswith(CONTAINER_PREFIX):
        return None
    return root / p[len(CONTAINER_PREFIX) :]


def _usage(mount) -> dict:
    u = shutil.disk_usage(mount)
    return {
        "mount": str(mount),
        "total_gb": round(u.total / GB, 1),
        "free_gb": round(u.free / GB, 1),
        "pct_free": round(100.0 * u.free / u.total, 1) if u.total else 0.0,
        "low": u.free < FREE_WARN_BYTES,
    }


def tree_size(path: Path) -> tuple[int, int]:
    """(bytes, files) under a path, following no links. Errors on a single
    entry are skipped: a locked file must not fail the whole answer."""
    total = files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def disk_usage(root: Path, folders: bool = True) -> dict:
    """The media volume and the checkout volume, and the three top folders."""
    mounts = sorted({root.anchor or str(root), paths.HOME.anchor or str(paths.HOME)})
    out: dict = {"volumes": [_usage(m) for m in mounts]}
    if folders:
        rows = []
        for name in TOP_FOLDERS:
            p = root / name
            if p.is_dir():
                size, files = tree_size(p)
                rows.append({"folder": name, "gb": round(size / GB, 1), "files": files})
        out["folders"] = rows
    return out


def largest_items(root: Path, under: str = "", limit: int = 10) -> list[dict]:
    """The biggest immediate children of <root>/<under>, folders measured
    whole. `under` is one of the top folders or a path below one."""
    base = _inside(root, under) if under else root
    if base is None or not base.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                size, files = tree_size(Path(entry.path))
            else:
                size, files = entry.stat(follow_symlinks=False).st_size, 1
        except OSError:
            continue
        rows.append(
            {
                "name": entry.name,
                "path": str(Path(entry.path).relative_to(root)).replace("\\", "/"),
                "gb": round(size / GB, 2),
                "bytes": size,
                "files": files,
                "folder": entry.is_dir(follow_symlinks=False),
            }
        )
    # By bytes, not the rounded figure: two small items must still order.
    rows.sort(key=lambda r: -r["bytes"])
    return rows[: max(1, min(int(limit), 50))]


def _inside(root: Path, rel: str) -> Path | None:
    """<root>/<rel> resolved, or None when it escapes the root."""
    try:
        candidate = (root / str(rel).replace("\\", "/").lstrip("/")).resolve()
        rootr = root.resolve()
    except OSError:
        return None
    if candidate == rootr or rootr not in candidate.parents:
        return None
    return candidate


def orphan_files(root: Path, known: set[Path], torrent_paths: set[Path]) -> dict:
    """Video files under Movies and TV that no Radarr or Sonarr file record
    names, and entries under torrents that no torrent's content path covers.
    `known` and `torrent_paths` are host paths, already resolved."""
    video = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}
    unknown_media: list[dict[str, Any]] = []
    for top in ("Movies", "TV"):
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                p = Path(dirpath) / f
                if p.suffix.lower() not in video:
                    continue
                try:
                    rp = p.resolve()
                except OSError:
                    continue
                if rp not in known:
                    try:
                        size = rp.stat().st_size
                    except OSError:
                        size = 0
                    unknown_media.append(
                        {
                            "path": str(p.relative_to(root)).replace("\\", "/"),
                            "gb": round(size / GB, 2),
                        }
                    )
    stray: list[dict[str, Any]] = []
    tdir = root / "torrents"
    if tdir.is_dir():
        try:
            entries = list(os.scandir(tdir))
        except OSError:
            entries = []
        for entry in entries:
            try:
                ep = Path(entry.path).resolve()
            except OSError:
                continue
            covered = any(
                ep == t or ep in t.parents or t in ep.parents for t in torrent_paths
            )
            if covered:
                continue
            size, _ = tree_size(ep) if entry.is_dir() else (entry.stat().st_size, 1)
            stray.append(
                {
                    "path": str(Path(entry.path).relative_to(root)).replace("\\", "/"),
                    "gb": round(size / GB, 2),
                }
            )
    unknown_media.sort(key=lambda r: -r["gb"])
    stray.sort(key=lambda r: -r["gb"])
    return {"unknown_media": unknown_media, "stray_downloads": stray}


def deletable(root: Path, rel: str) -> Path | None:
    """The absolute path for a delete, or None when the target is the root,
    a top folder, outside the root, or absent."""
    target = _inside(root, rel)
    if target is None or not target.exists():
        return None
    try:
        parent_rel = target.relative_to(root.resolve())
    except ValueError:
        return None
    if len(parent_rel.parts) == 1 and parent_rel.parts[0] in TOP_FOLDERS:
        return None
    return target


def delete(target: Path) -> dict:
    if target.is_dir():
        size, files = tree_size(target)
        shutil.rmtree(target)
    else:
        size, files = target.stat().st_size, 1
        target.unlink()
    return {"deleted": str(target), "gb": round(size / GB, 2), "files": files}


def last_smart_warning() -> dict | None:
    """The newest smart_warning event in the local event logs, or None. The
    SMART daemon's alert script is what writes these."""
    newest = None
    for f in sorted(paths.logs().glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"smart_warning"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == "smart_warning" and (
                    newest is None or str(rec.get("ts", "")) > str(newest.get("ts", ""))
                ):
                    newest = rec
        except OSError:
            continue
    if newest is None:
        return None
    return {
        k: newest.get(k) for k in ("ts", "device", "failtype", "msg") if k in newest
    }


def drive_health(root: Path) -> dict:
    mounts = sorted({root.anchor or str(root), paths.HOME.anchor or str(paths.HOME)})
    return {
        "volumes": [_usage(m) for m in mounts],
        "warn_below_gb": FREE_WARN_BYTES // GB,
        "last_smart_warning": last_smart_warning(),
    }
