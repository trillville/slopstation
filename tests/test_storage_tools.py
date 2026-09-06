"""Test the storage tools over a temporary media root."""

import json
import types

import pytest

from helpers import CapturingLog
from slopstation import paths
from slopstation.agent.llm import assistant
from slopstation.agent.tools import storage

GB = 1024**3


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "Media"
    write(root / "Movies" / "Dune (2021)" / "Dune.mkv", 3000)
    write(root / "Movies" / "Old Film (1999)" / "Old.Film.mkv", 2000)
    write(root / "Movies" / "Old Film (1999)" / "poster.jpg", 10)
    write(root / "TV" / "Andor" / "Season 01" / "Andor.S01E01.mkv", 1500)
    write(root / "torrents" / "Dune.2021.REMUX" / "Dune.mkv", 3000)
    write(root / "torrents" / "Leftover.Stuff" / "x.bin", 700)
    monkeypatch.setattr(storage, "media_root", lambda: root)
    return root


class Arr:
    def __init__(self, name, files):
        self.name = name
        self.files = files

    def get(self, endpoint, params=None):
        if endpoint in ("moviefile", "episodefile"):
            return [{"path": p} for p in self.files]
        if endpoint == "diskspace":
            return [{"path": "/data", "freeSpace": 500 * GB, "totalSpace": 14000 * GB}]
        raise AssertionError(endpoint)


class Qbit:
    def __init__(self, root):
        self.root = root

    def torrents(self, **kw):
        return [
            {
                "hash": "a" * 40,
                "content_path": str(self.root / "torrents" / "Dune.2021.REMUX"),
            }
        ]


@pytest.fixture
def rig(root):
    log = CapturingLog("voice")
    dispatch = types.SimpleNamespace(
        dry_run=False, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    media = types.SimpleNamespace(
        cfg={},
        radarr=Arr("Radarr", ["/data/Movies/Dune (2021)/Dune.mkv"]),
        sonarr=Arr("Sonarr", ["/data/TV/Andor/Season 01/Andor.S01E01.mkv"]),
        qbit=Qbit(root),
        prowlarr=None,
    )
    tk = assistant.Toolkit(dispatch, log, media=media)
    tk.load([s.name for s in assistant.REGISTRY if s.area == "storage"])
    return tk, dispatch, log


def test_disk_usage_and_largest_items(rig, root):
    tk, _, _ = rig
    out = tk.call("disk_usage", {})
    assert out["ok"] and out["root"] == str(root) and out["volumes"]
    assert {r["folder"] for r in out["folders"]} == {"Movies", "TV", "torrents"}
    assert next(r for r in out["folders"] if r["folder"] == "Movies")["files"] == 3
    assert out["arr_view"][0] == {
        "app": "Radarr",
        "path": "/data",
        "free_gb": 500.0,
        "total_gb": 14000.0,
    }
    assert "folders" not in tk.call("disk_usage", {"folders": False})
    top = tk.call("largest_items", {"under": "Movies", "limit": 1})
    assert (
        top["ok"]
        and top["items"][0]["name"] == "Dune (2021)"
        and top["items"][0]["folder"]
    )
    everything = tk.call("largest_items", {})
    assert [i["name"] for i in everything["items"]] == ["Movies", "torrents", "TV"]
    assert not tk.call("largest_items", {"under": "../../etc"})["ok"]


def test_orphan_files_cross_the_arr_records_and_the_torrents(rig):
    tk, _, _ = rig
    out = tk.call("orphan_files", {})
    assert out["ok"]
    # Old Film is on disk but Radarr never recorded it; Andor and Dune are known.
    assert [r["path"] for r in out["unknown_media"]] == [
        "Movies/Old Film (1999)/Old.Film.mkv"
    ]
    # Leftover.Stuff is not covered by any torrent; Dune's download is.
    assert [r["path"] for r in out["stray_downloads"]] == ["torrents/Leftover.Stuff"]


def test_delete_path_is_guarded_gated_and_final(rig, root, log=None):
    tk, dispatch, log = rig
    for bad in ("", "Movies", "TV", "torrents", "..", "../x", "Movies/Nope (2000)"):
        r = tk.call("delete_path", {"path": bad})
        assert not r["ok"], bad
    # Still referenced: pointed at the owning tool instead.
    r = tk.call("delete_path", {"path": "Movies/Dune (2021)"})
    assert not r["ok"] and "delete_media" in r["error"]
    r = tk.call("delete_path", {"path": "torrents/Dune.2021.REMUX/Dune.mkv"})
    assert not r["ok"] and "delete_torrent" in r["error"]
    # An orphan folder: asked, then deleted on a later turn.
    asked = tk.call("delete_path", {"path": "Movies/Old Film (1999)"})
    assert not asked["ok"] and "2 file(s)" in asked["acknowledgment"]
    assert (root / "Movies" / "Old Film (1999)").exists()
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    done = tk.call("delete_path", {"path": "Movies/Old Film (1999)"})
    assert (
        done["ok"]
        and done["files"] == 2
        and not (root / "Movies" / "Old Film (1999)").exists()
    )
    assert (root / "Movies" / "Dune (2021)").exists()
    # A single stray file, dry run first.
    dispatch.dry_run = True
    dry = tk.call("delete_path", {"path": "torrents/Leftover.Stuff/x.bin"})
    assert dry["dry_run"] and (root / "torrents" / "Leftover.Stuff" / "x.bin").exists()


def test_drive_health_reads_the_last_smart_warning(rig):
    tk, _, _ = rig
    out = tk.call("drive_health", {})
    assert (
        out["ok"] and out["last_smart_warning"] is None and out["warn_below_gb"] == 250
    )
    logs = paths.logs()
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "k15-20260901.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-01T01:00:00Z",
                "event": "smart_warning",
                "device": "/dev/sdb",
                "failtype": "Health",
                "msg": "old",
            }
        )
        + "\n"
        + json.dumps(
            {"ts": "2026-09-01T02:00:00Z", "event": "disk_space_low", "mount": "D:\\"}
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "k15-20260904.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-04T01:00:00Z",
                "event": "smart_warning",
                "device": "/dev/sdb",
                "failtype": "Usage",
                "msg": "new",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tk.call("drive_health", {})
    assert (
        out["last_smart_warning"]["msg"] == "new"
        and out["last_smart_warning"]["failtype"] == "Usage"
    )


def test_no_media_root_is_a_plain_error(monkeypatch):
    monkeypatch.setattr(storage, "media_root", lambda: None)
    log = CapturingLog("voice")
    dispatch = types.SimpleNamespace(dry_run=True, utterance=None)
    media = types.SimpleNamespace(
        cfg={}, radarr=None, sonarr=None, qbit=None, prowlarr=None
    )
    tk = assistant.Toolkit(dispatch, log, media=media)
    tk.load(["disk_usage"])
    out = tk.call("disk_usage", {})
    assert not out["ok"] and "MEDIA_ROOT" in out["error"]
