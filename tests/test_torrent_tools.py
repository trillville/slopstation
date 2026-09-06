"""Test the torrent tools: reads, harmless controls, and the ownership rule."""

import types

import pytest

from helpers import CapturingLog
from slopstation.agent.llm import assistant, confirm
from slopstation.agent.tools import media_proton

LINKED = "a" * 40  # Radarr is waiting on this one
ORPHAN = "b" * 40  # completed, nobody asked for it
SEEDING = "c" * 40  # imported long ago, still seeding


class FakeQbit:
    """The client methods the tools use, over a few torrents. Every action
    is recorded and reflected in the state the next read reports."""

    def __init__(self):
        self.actions = []
        self.prefs = {
            "listen_port": 51820,
            "current_interface_name": "ProtonVPN",
            "current_interface_address": "10.2.0.2",
            "max_ratio_enabled": True,
            "max_ratio": 2.0,
            "max_seeding_time_enabled": False,
        }
        self.alt = False
        self.limits = {"dl": 0, "up": 0}
        self.rows = [
            {
                "hash": LINKED,
                "name": "Dune.Part.Two.2024.2160p.REMUX-GRP",
                "state": "downloading",
                "progress": 0.4,
                "size": 60 * 1024**3,
                "dlspeed": 30 * 1024**2,
                "upspeed": 0,
                "eta": 1800,
                "ratio": 0.1,
                "num_seeds": 12,
                "category": "radarr",
                "added_on": 200,
                "content_path": "C:/Media/torrents/Dune.Part.Two",
            },
            {
                "hash": ORPHAN,
                "name": "Some.Thing.Nobody.Asked.For",
                "state": "stalledUP",
                "progress": 1.0,
                "size": 2 * 1024**3,
                "dlspeed": 0,
                "upspeed": 0,
                "eta": 8640000,
                "ratio": 0.0,
                "num_seeds": 0,
                "category": "",
                "added_on": 100,
                "completion_on": 150,
                "content_path": "C:/Media/torrents/Some.Thing",
            },
            {
                "hash": SEEDING,
                "name": "Arrival.2016.1080p-GRP",
                "state": "uploading",
                "progress": 1.0,
                "size": 8 * 1024**3,
                "dlspeed": 0,
                "upspeed": 512 * 1024,
                "eta": 8640000,
                "ratio": 1.7,
                "num_seeds": 3,
                "uploaded": 13 * 1024**3,
                "seeding_time": 7200,
                "ratio_limit": -2,
                "seeding_time_limit": -1,
                "category": "radarr",
                "added_on": 300,
                "completion_on": 350,
                "content_path": "C:/Media/torrents/Arrival",
            },
        ]

    def torrents(
        self, filter=None, category=None, sort=None, reverse=False, limit=None
    ):
        rows = list(self.rows)
        if filter == "completed":
            rows = [r for r in rows if r["progress"] >= 1]
        elif filter == "seeding":
            rows = [r for r in rows if r["state"] == "uploading"]
        elif filter == "downloading":
            rows = [r for r in rows if r["state"] == "downloading"]
        elif filter == "stopped":
            rows = [r for r in rows if r["state"].startswith("stopped")]
        if category is not None:
            rows = [r for r in rows if r["category"] == category]
        if sort == "added_on":
            rows.sort(key=lambda r: r["added_on"], reverse=reverse)
        return rows

    def _find(self, hashes):
        return (
            self.rows
            if hashes == "all"
            else [r for r in self.rows if r["hash"] in hashes]
        )

    def torrent_action(self, action, hashes):
        self.actions.append((action, hashes))
        for r in self._find(hashes):
            if action == "stop":
                r["state"] = "stoppedDL" if r["progress"] < 1 else "stoppedUP"
            elif action == "start":
                r["state"] = "downloading" if r["progress"] < 1 else "uploading"

    def set_force_start(self, hashes, value):
        self.actions.append(("forceStart", hashes, value))

    def delete_torrents(self, hashes, delete_files):
        self.actions.append(("delete", hashes, delete_files))
        self.rows = [r for r in self.rows if r["hash"] not in hashes]

    def torrent_properties(self, h):
        return {
            "save_path": "C:/Media/torrents",
            "total_size": 60 * 1024**3,
            "total_downloaded": 24 * 1024**3,
            "total_uploaded": 1024**3,
            "share_ratio": 0.04,
            "seeding_time": 0,
            "seeds_total": 40,
            "peers_total": 9,
        }

    def torrent_files(self, h):
        return [
            {"name": "Dune.mkv", "size": 59 * 1024**3, "progress": 0.4, "priority": 1},
            {"name": "sample.mkv", "size": 1024**3, "progress": 1.0, "priority": 0},
        ]

    def torrent_trackers(self, h):
        return [
            {"url": "** [DHT] **", "status": 2},
            {
                "url": "https://tracker.example/abc123/announce",
                "status": 2,
                "msg": "",
                "num_peers": 9,
            },
        ]

    def transfer_info(self):
        return {
            "dl_info_speed": 30 * 1024**2,
            "up_info_speed": 512 * 1024,
            "dl_info_data": 100 * 1024**3,
            "up_info_data": 20 * 1024**3,
            "dl_rate_limit": self.limits["dl"],
            "up_rate_limit": self.limits["up"],
            "connection_status": "connected",
            "dht_nodes": 300,
        }

    def server_state(self):
        return {"free_space_on_disk": 900 * 1024**3, "queued_io_jobs": 0}

    def speed_limits_mode(self):
        return self.alt

    def toggle_speed_limits_mode(self):
        self.alt = not self.alt
        self.actions.append(("toggleAlt", self.alt))

    def set_global_limits(self, download=None, upload=None):
        if download is not None:
            self.limits["dl"] = download
        if upload is not None:
            self.limits["up"] = upload
        self.actions.append(("globalLimits", download, upload))

    def set_torrent_limits(self, hashes, download=None, upload=None):
        self.actions.append(("torrentLimits", hashes, download, upload))

    def preferences(self):
        return dict(self.prefs)

    def main_log(self, warnings_only=True, last_known_id=-1):
        rows = [{"id": 1, "type": 4, "timestamp": 1, "message": "tracker warning"}]
        if not warnings_only:
            rows.insert(0, {"id": 0, "type": 1, "timestamp": 0, "message": "started"})
        return rows


class FakeMedia:
    def __init__(self, qbit):
        self.qbit = qbit
        self.prowlarr = None
        self.radarr = types.SimpleNamespace(name="Radarr", get=lambda *a, **k: [])
        self.sonarr = types.SimpleNamespace(name="Sonarr", get=lambda *a, **k: [])
        self.cfg = {}

    fail_link = False

    def download_index(self, strict=False):
        if self.fail_link:
            raise RuntimeError("Sonarr's queue could not be read")
        return {
            LINKED: {
                "kind": "movie",
                "title": "Dune: Part Two",
                "queue_id": 7,
                "authority": "Radarr",
                "status": "downloading",
            }
        }

    def download_known(self, download_id):
        return download_id in {LINKED, SEEDING}


@pytest.fixture
def log():
    return CapturingLog("voice")


@pytest.fixture
def rig(log):
    """A live toolkit over the fake client, every torrent tool loaded."""
    qbit = FakeQbit()
    dispatch = types.SimpleNamespace(
        dry_run=False, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    tk = assistant.Toolkit(dispatch, log, media=FakeMedia(qbit))
    tk.load([s.name for s in assistant.REGISTRY if "torrents" in s.needs])
    return tk, qbit, dispatch


def test_list_torrents_links_media_filters_and_pages(rig):
    tk, qbit, _ = rig
    out = tk.call("list_torrents", {"state": "all", "limit": 2})
    assert out["ok"] and out["count"] == 3 and out["next_offset"] == 2
    rest = tk.call("list_torrents", {"state": "all", "limit": 2, "offset": 2})
    assert len(rest["torrents"]) == 1 and rest["next_offset"] is None
    # Newest first, and the linked one names its movie, not its release.
    assert [r["hash"] for r in out["torrents"]] == [SEEDING, LINKED]
    dune = out["torrents"][1]
    assert dune["media"] == "Dune: Part Two" and dune["owner"] == "Radarr"
    assert (
        dune["percent"] == 40
        and dune["down_kbps"] == 30 * 1024
        and dune["eta_min"] == 30
    )
    assert out["torrents"][0]["eta_min"] is None, "8640000 is qBittorrent's infinity"
    by_name = tk.call("list_torrents", {"name": "arrival"})
    assert by_name["count"] == 1 and by_name["torrents"][0]["hash"] == SEEDING
    assert tk.call("list_torrents", {"state": "downloading"})["count"] == 1
    assert tk.call("list_torrents", {"category": "radarr"})["count"] == 2
    assert not tk.call("list_torrents", {"state": "flying"})["ok"]


def test_torrent_details_hides_tracker_urls_and_lists_files(rig):
    tk, _, _ = rig
    out = tk.call("torrent_details", {"hash": LINKED})
    assert out["ok"] and out["media"] == "Dune: Part Two" and out["seeds"] == 40
    assert out["trackers"] == [{"status": 2, "msg": "", "peers": 9}], (
        "DHT row dropped, URL gone"
    )
    assert "tracker.example" not in str(out)
    assert out["files"][0]["name"] == "Dune.mkv" and out["file_count"] == 2
    assert not tk.call("torrent_details", {"hash": "nope"})["ok"]


def test_pause_resume_and_the_harmless_controls_report_the_state_after(rig):
    tk, qbit, _ = rig
    out = tk.call("pause_torrent", {"hashes": [LINKED]})
    assert out["ok"] and qbit.actions[-1] == ("stop", [LINKED])
    assert out["torrents"] == [
        {"hash": LINKED, "name": qbit.rows[0]["name"], "state": "stoppedDL"}
    ]
    out = tk.call("resume_torrent", {"hashes": ["all"]})
    assert (
        out["ok"] and qbit.actions[-1] == ("start", "all") and len(out["torrents"]) == 3
    )
    assert not tk.call("pause_torrent", {"hashes": []})["ok"]
    assert not tk.call("pause_torrent", {"hashes": ["zzz"]})["ok"]
    assert (
        tk.call("recheck_torrent", {"hash": LINKED})["ok"]
        and qbit.actions[-1][0] == "recheck"
    )
    assert (
        tk.call("reannounce_torrent", {"hash": LINKED})["ok"]
        and qbit.actions[-1][0] == "reannounce"
    )
    assert (
        tk.call("force_start", {"hash": LINKED})["ok"]
        and qbit.actions[-1][0] == "forceStart"
    )
    assert tk.call("set_torrent_priority", {"hash": LINKED, "position": "top"})["ok"]
    assert qbit.actions[-1] == ("topPrio", [LINKED])
    assert not tk.call(
        "set_torrent_priority", {"hash": LINKED, "position": "sideways"}
    )["ok"]
    # Nothing above touched identity, location or existence of the linked torrent.
    assert not any(a[0] == "delete" for a in qbit.actions)


def test_delete_torrent_refuses_a_linked_torrent_and_gates_an_orphan(
    rig, log, monkeypatch
):
    tk, qbit, dispatch = rig
    # An unread queue refuses: it cannot pass as "not linked".
    tk.ctx.media.fail_link = True
    unsure = tk.call("delete_torrent", {"hash": ORPHAN, "delete_files": True})
    assert not unsure["ok"] and "could not read" in unsure["error"]
    assert not tk.call("orphan_torrents", {})["ok"]
    tk.ctx.media.fail_link = False
    refused = tk.call("delete_torrent", {"hash": LINKED, "delete_files": True})
    assert (
        not refused["ok"]
        and "Radarr" in refused["error"]
        and "resolve_queue_item" in refused["error"]
    )
    assert log.find("tool_refused")[-1]["reason"] == "linked"
    # An orphan: asked first, with the size the user is about to lose.
    asked = tk.call("delete_torrent", {"hash": ORPHAN, "delete_files": True})
    assert (
        not asked["ok"]
        and "2.0 GB" in asked["acknowledgment"]
        and "Some.Thing" in asked["acknowledgment"]
    )
    assert not any(a[0] == "delete" for a in qbit.actions)
    # Same turn: refused. Later turn: done.
    assert not tk.call("delete_torrent", {"hash": ORPHAN, "delete_files": True})["ok"]
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    done = tk.call("delete_torrent", {"hash": ORPHAN, "delete_files": True})
    assert (
        done["ok"]
        and done["files_erased"]
        and qbit.actions[-1] == ("delete", [ORPHAN], True)
    )
    assert not tk.call("delete_torrent", {"hash": ORPHAN})["ok"], "gone now"
    # The keep-files question reads differently.
    dispatch.utterance = types.SimpleNamespace(turn="aa0003", asked="")
    kept = tk.call("delete_torrent", {"hash": SEEDING, "delete_files": False})
    assert "keeping its files" in kept["acknowledgment"]
    monkeypatch.setattr(confirm, "ASK_TTL_S", -1)
    dispatch.utterance = types.SimpleNamespace(turn="aa0004", asked="yes")
    assert not tk.call("delete_torrent", {"hash": SEEDING, "delete_files": False})[
        "ok"
    ], "stale ask re-asks"


def test_transfer_info_and_speed_limits(rig):
    tk, qbit, _ = rig
    info = tk.call("transfer_info", {})
    assert (
        info["ok"] and info["down_kbps"] == 30 * 1024 and info["free_space_gb"] == 900.0
    )
    assert info["alternative_limits"] is False and info["connection"] == "connected"
    out = tk.call("set_speed_limits", {"download_kbps": 5000, "alternative": True})
    assert (
        out["ok"]
        and out["down_limit_kbps"] == 5000
        and out["alternative_limits"] is True
    )
    assert ("globalLimits", 5000 * 1024, None) in qbit.actions and (
        "toggleAlt",
        True,
    ) in qbit.actions
    # Already on: no second toggle.
    n = len(qbit.actions)
    tk.call("set_speed_limits", {"alternative": True})
    assert not any(a[0] == "toggleAlt" for a in qbit.actions[n:])
    per = tk.call("set_speed_limits", {"hash": SEEDING, "upload_kbps": 0})
    assert per["ok"] and qbit.actions[-1] == ("torrentLimits", [SEEDING], None, 0)
    assert not tk.call("set_speed_limits", {})["ok"]
    assert not tk.call("set_speed_limits", {"download_kbps": -1})["ok"]
    assert not tk.call("set_speed_limits", {"download_kbps": True})["ok"]
    assert not tk.call("set_speed_limits", {"hash": SEEDING, "alternative": True})["ok"]
    assert not tk.call("set_speed_limits", {"alternative": "false"})["ok"], (
        "a string is not a switch"
    )
    assert not tk.call("set_speed_limits", {"hash": "bad", "upload_kbps": 1})["ok"]


def test_seeding_orphans_vpn_and_log(rig, monkeypatch):
    tk, qbit, _ = rig
    seed = tk.call("seeding_report", {})
    assert seed["ok"] and seed["count"] == 1 and seed["torrents"][0]["ratio"] == 1.7
    assert (
        seed["torrents"][0]["ratio_limit"] == "global"
        and seed["torrents"][0]["time_limit_min"] == "none"
    )
    assert seed["global_policy"] == {"max_ratio": 2.0, "max_seeding_minutes": None}
    assert seed["total_uploaded_gb"] == 13.0
    orphans = tk.call("orphan_torrents", {})
    assert orphans["ok"] and [t["hash"] for t in orphans["torrents"]] == [ORPHAN]
    monkeypatch.setattr(
        media_proton,
        "read_proton_port_state",
        lambda path=None, now=None: {"status": "ok", "port": 51820, "state": "active"},
    )
    vpn = tk.call("vpn_status", {})
    assert vpn["ok"] and vpn["interface"] == "ProtonVPN" and vpn["ports_agree"] is True
    qbit.prefs["listen_port"] = 6881
    assert tk.call("vpn_status", {})["ports_agree"] is False
    # A stale Proton reading never agrees, and an unreadable log is an answer.
    qbit.prefs["listen_port"] = 51820
    monkeypatch.setattr(
        media_proton,
        "read_proton_port_state",
        lambda path=None, now=None: {"status": "ok", "port": 51820, "state": "stale"},
    )
    assert tk.call("vpn_status", {})["ports_agree"] is False
    monkeypatch.setattr(
        media_proton,
        "read_proton_port_state",
        lambda path=None, now=None: (_ for _ in ()).throw(
            RuntimeError("log unreadable")
        ),
    )
    out = tk.call("vpn_status", {})
    assert out["ok"] and out["proton"]["state"] == "unreadable"
    lines = tk.call("qbit_log", {})
    assert (
        lines["ok"]
        and lines["count"] == 1
        and lines["lines"][0]["msg"] == "tracker warning"
    )
    assert tk.call("qbit_log", {"everything": True, "limit": 1})["count"] == 2


def test_dry_run_and_service_gating(log):
    dispatch = types.SimpleNamespace(
        dry_run=True, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    qbit = FakeQbit()
    tk = assistant.Toolkit(dispatch, log, media=FakeMedia(qbit))
    tk.load(["pause_torrent", "delete_torrent", "set_speed_limits"])
    assert tk.call("pause_torrent", {"hashes": ["all"]})["dry_run"] and not qbit.actions
    assert (
        tk.call("delete_torrent", {"hash": ORPHAN})["dry_run"] and len(qbit.rows) == 3
    )
    assert (
        tk.call("set_speed_limits", {"download_kbps": 1})["dry_run"]
        and not qbit.actions
    )
    # No qBittorrent client: none of these are offered at all.
    no_qbit = FakeMedia(None)
    assert not any(
        "torrents" in assistant.REGISTRY.get(n).needs
        for n in assistant.Toolkit(dispatch, log, media=no_qbit).offered
    )
