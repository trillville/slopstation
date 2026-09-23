"""Test assistant prompts, tool validation, and provider configuration."""

import asyncio
import re
import time
import types

import pytest

from helpers import CapturingLog, fake_dispatch, seed_lock, toolkit_impls
from slopstation import gamepc, sessionlock, statefile
from slopstation.agent.dispatch import Dispatch
from slopstation.agent.llm import assistant, backends, confirm, prompts
from slopstation.agent.speech import tool_schemas
from slopstation.agent.tools import library, steamstore

CFG_MIN = {
    "tvComPort": "COMX",
    "tvGamingCmd": "hdmi4",
    "voice": {
        "volumeStep": 2,
        "volumeMax": 40,
        "assistantWebSearch": False,
        "assistantSearchMaxUses": 2,
        "location": {"city": "", "region": "", "country": "", "timezone": ""},
        "inputs": {"apple tv": "hdmi1", "gaming": "hdmi4"},
    },
}

# Server-side search on, with a location the providers pass through.
VOICE_ON = {
    **CFG_MIN["voice"],
    "assistantWebSearch": True,
    "location": {"city": "Portland", "region": "", "country": "US", "timezone": ""},
}

INSTALLED = 892970  # Valheim
INSTALLED_AT = 1756000000
OWNED_ONLY = 413150  # Stardew Valley
UNKNOWN = 999999999  # Not in the index.

INDEX = {
    "refreshed": "2026-08-22T00:00:00",
    "installed": [
        {
            "appid": INSTALLED,
            "name": "Valheim",
            "state": 4,
            "size": 1,
            "lastPlayed": 1700000000,
            "updated": INSTALLED_AT,
        },
        {
            "appid": 1245620,
            "name": "ELDEN RING",
            "state": 4,
            "size": 1,
            "lastPlayed": 0,
        },
    ],
    "owned": {
        str(INSTALLED): {
            "hours": 12.0,
            "hours2w": 0,
            "last": 1700000000,
            "name": "Valheim",
        },
        str(OWNED_ONLY): {
            "hours": 3.0,
            "hours2w": 0,
            "last": 0,
            "name": "Stardew Valley",
        },
    },
    "collections": [{"name": "Co-op", "id": "uc-abc"}],
}


def flat(text):
    """Prose with its line breaks collapsed, so an assertion is about the
    words and not where the wrap fell. str() of a list reprs its strings, so
    the escaped newlines go too."""
    return re.sub(r"\s+", " ", str(text).replace(r"\n", " "))


def boom(_):
    raise ValueError("kaboom")


def recording_nav(seen):
    """A Dispatch.nav double: records (kind, arg), answers ok."""

    def nav(kind, arg=None, turn=None):
        seen.append((kind, arg))
        return types.SimpleNamespace(ok=True, detail="showing")

    return nav


class FakeOperations:
    def __init__(self):
        self.tracked = []
        self.acknowledged = None
        self.active_rows = []
        self.observed = []
        self.delivered = []

    def track_steam_install(self, appid, title, turn=None, verified=False):
        self.tracked.append((appid, title, turn, verified))
        return {"id": "op-test"}

    def track_external(
        self,
        kind,
        authority,
        external_ref,
        title,
        turn=None,
        detail="",
        metadata=None,
        work_id=None,
    ):
        self.tracked.append(
            (kind, authority, external_ref, title, turn, detail, metadata)
        )
        return {"id": "op-media"}

    def for_assistant(self, scope, limit=10, offset=0, acknowledge=False):
        self.acknowledged = acknowledge
        self.limit = limit
        return [{"id": "op-test", "state": "RUNNING", "title": "Stardew"}], 1

    def observe(self, operation_id, state, progress, detail):
        self.observed.append((operation_id, state, progress, detail))
        return {
            "id": operation_id,
            "state": state,
            "progress": progress,
            "detail": detail,
        }

    def active(self, kind=None):
        return [r for r in self.active_rows if kind is None or r.get("kind") == kind]

    def mark_delivered(self, operation_id):
        self.delivered.append(operation_id)


class FakeMedia:
    def __init__(self):
        self.requests = []

    def library(self, kind, catalog_id):
        self.requests.append(("library", kind, catalog_id))
        return {
            "kind": kind,
            "catalog_id": catalog_id,
            "in_library": True,
            "title": "Breaking Bad" if kind == "series" else "Dune",
            "year": 2008 if kind == "series" else 2021,
            "seasons": [{"season": 2, "have": 13, "aired": 13}],
        }

    def request_movie(self, tmdb_id, preset):
        self.requests.append(("movie", tmdb_id, preset))
        return {
            "ok": True,
            "kind": "movie_acquisition",
            "authority": "radarr",
            "external_ref": "31",
            "title": "Dune",
            "catalog_id": tmdb_id,
            "preset": preset,
            "profile": "Movie HD",
            "already_available": False,
        }

    def request_series(self, tvdb_id, preset, seasons, episodes=None):
        self.requests.append(("series", tvdb_id, preset, seasons, episodes))
        return {
            "ok": True,
            "kind": "series_acquisition",
            "authority": "sonarr",
            "external_ref": "41",
            "title": "Breaking Bad",
            "catalog_id": tvdb_id,
            "preset": preset,
            "profile": "Series HD",
            "seasons": seasons,
            "already_available": False,
        }

    def delete_movie(self, tmdb_id, command_ids):
        self.requests.append(("delete_movie", tmdb_id, command_ids))
        return {"ok": True, "title": "Dune", "removed": True}

    def episodes_in_seasons(self, tvdb_id, seasons):
        return [201]

    def episodes_in_scope(self, tvdb_id, episodes):
        self.requests.append(("episodes_in_scope", tvdb_id, episodes))
        return [413]

    def delete_series(self, tvdb_id, seasons, all_seasons, command_ids, **_):
        self.requests.append(
            ("delete_series", tvdb_id, seasons, all_seasons, command_ids)
        )
        return {"ok": True, "title": "Breaking Bad", "removed": True}


class RaisingSteam:
    """A revoked token: install answers not ok, as the real one does when the
    token no longer mints, and a read raises."""

    def install(self, a):
        return {"ok": False, "error": "couldn't reach Steam just now"}

    def download_status(self):
        raise RuntimeError("token revoked")


@pytest.fixture
def catalog():
    """The fixture index, written under this test's runtime home."""
    statefile.write(library.library_file(), INDEX)
    return library.load()


@pytest.fixture
def dispatch(catalog, log):
    """A dry-run Dispatch over the fixture catalog."""
    return Dispatch(CFG_MIN, log, dry_run=True)


@pytest.fixture
def live_dispatch(catalog, log):
    """A live Dispatch: the tools take their real path, against the fakes."""
    return Dispatch(CFG_MIN, log, dry_run=False)


@pytest.fixture
def impls(dispatch, log):
    """The base tool set: no operations store, no steam, no media."""
    return toolkit_impls(dispatch, log)


@pytest.fixture
def fake_operations():
    return FakeOperations()


@pytest.fixture
def fake_media():
    return FakeMedia()


@pytest.fixture
def fake_steam():
    """An enrolled account session whose install queues silently."""
    return types.SimpleNamespace(
        install=lambda a: {"ok": True, "detail": "queued"},
        download_status=lambda: [],
    )


@pytest.fixture
def media_impls(dispatch, log, fake_operations, fake_media):
    """Dry-run tools with the operations store and the media boundary."""
    return toolkit_impls(dispatch, log, operations=fake_operations, media=fake_media)


@pytest.fixture
def live_media(live_dispatch, log, fake_operations, fake_media):
    """Live tools over the media fakes, inside utterance fa1100."""
    live_dispatch.begin_utterance("fa1100", "get dune in 1080p")
    return toolkit_impls(
        live_dispatch, log, operations=fake_operations, media=fake_media
    )


# -- the system prompt ---------------------------------------------------------


def test_system_instruction_carries_the_catalog_and_the_voice_rules(catalog):
    si = assistant.system_instruction(CFG_MIN)
    assert "CATALOG" in si and str(INSTALLED) in si
    # The prompt never names a tool the process may not offer: the tool rule
    # travels with the tool, so it is absent when the media service is.
    assert "find_media" not in si
    # Dynamic tail: the date and the clock.
    assert time.strftime("%Y-%m-%d") in si
    # The clock is the LAST line, so every token before it is a stable prefix.
    assert re.search(r"It is \d\d:\d\d on [\d-]+ local time" + r"\.$", si.strip())
    assert si.index("CATALOG (") < si.rindex("It is ")
    # A config that says "inputs": null still gets a prompt, with no input names.
    nulled = {**CFG_MIN, "voice": {**CFG_MIN["voice"], "inputs": None}}
    assert "none configured" in assistant.system_instruction(nulled)
    # A date with no zone drifts toward UTC and dates briefs tomorrow; an empty
    # location is a real deployment shape and must still say the day is local.
    zoned = {
        **CFG_MIN["voice"],
        "location": {**CFG_MIN["voice"]["location"], "timezone": "America/Los_Angeles"},
    }
    si_tz = assistant.system_instruction({**CFG_MIN, "voice": zoned})
    assert f"{time.strftime('%Y-%m-%d')} in America/Los_Angeles" in flat(si_tz)
    assert "apple tv" in flat(si) and "Switching the TV to 'gaming'" in flat(si)


def test_catalog_dates_the_rows_the_pc_stamped(catalog):
    """Catalog rows include Steam's update date when available."""
    rows = {line.split("|")[0]: line for line in library.catalog_lines()}
    day = time.strftime("%Y-%m-%d", time.localtime(INSTALLED_AT))
    assert rows[str(INSTALLED)].split("|")[6] == f"inst:{day}"
    # Missing update times keep the bare token.
    assert rows["1245620"].split("|")[6] == "inst"
    assert rows[str(OWNED_ONLY)].split("|")[6] == "notinst"


# -- the base tools ------------------------------------------------------------


def test_launch_game_refuses_an_appid_outside_the_catalog(impls):
    r = impls["launch_game"]({"appid": UNKNOWN})
    assert not r["ok"] and "not in the catalog" in r["error"]
    r = impls["launch_game"]({"appid": INSTALLED})
    assert r["ok"] and "dry-run" in r["detail"], r


def test_volume_and_session_route_actions_and_refuse_the_malformed(impls):
    # Two tools where there was one: volume never interrupts the TV, session
    # always does, so they carry different risk and separate descriptions.
    assert impls["volume"]({"action": "up"})["ok"]
    assert impls["volume"]({"action": "set", "level": 30})["ok"]
    assert not impls["volume"]({"action": "self_destruct"})["ok"]
    r = impls["volume"]({"action": "set"})  # no level -> refused
    assert not r["ok"] and "level" in r["error"]
    assert impls["session"]({"action": "end_session"})["ok"]
    assert impls["session"]({"action": "switch_input", "input": "apple tv"})["ok"]
    assert not impls["session"]({"action": "volume_up"})["ok"]
    assert assistant.REGISTRY.get("session").risk == "act"


def test_stop_listening_is_refused_with_nothing_to_stop(dispatch, log, impls):
    # stop_listening with nothing to stop (REPL, probes): refused, not faked.
    r = impls["stop_listening"]({})
    assert not r["ok"] and "nothing is listening" in r["error"], r
    stops = []
    simpls = toolkit_impls(dispatch, log, on_stop_listening=lambda: stops.append(1))
    r = simpls["stop_listening"]({})
    assert r["ok"] and stops == [1], (r, stops)


def test_quit_game_asks_first_and_acts_on_a_later_yes(monkeypatch, live_dispatch, log):
    """quit_game asks first like every other write: a question on the first
    call, the act on a later turn, nothing on the same turn."""
    from slopstation.agent.dispatch import Result

    quit = []
    monkeypatch.setattr(
        live_dispatch,
        "quit_game",
        lambda appid, turn=None: quit.append(appid) or Result(True, "ok", "quit"),
    )
    impls = toolkit_impls(live_dispatch, log)
    live_dispatch.begin_utterance("aa0001", "quit valheim")
    asked = impls["quit_game"]({"appid": INSTALLED})
    assert not asked["ok"] and asked["acknowledgment"] == "Quit Valheim?" and quit == []
    assert not impls["quit_game"]({"appid": INSTALLED})["ok"] and quit == []
    live_dispatch.begin_utterance("aa0002", "yes")
    assert impls["quit_game"]({"appid": INSTALLED}) == {"ok": True, "detail": "quit"}
    assert quit == [INSTALLED]


def test_quit_game_on_a_dry_run_previews_and_never_asks(dispatch, log):
    """A dry run previews before the gate: the first call is the preview;
    nothing is asked or dispatched."""
    impls = toolkit_impls(dispatch, log)
    dispatch.begin_utterance("aa0001", "quit valheim")
    out = impls["quit_game"]({"appid": INSTALLED})
    assert out["dry_run"] and out["detail"] == "would quit Valheim"
    assert "acknowledgment" not in out and log.find("dry_run_would")


def test_a_room_refusal_is_an_error_and_says_busy(monkeypatch, dispatch, impls):
    """Every toolset spells a failure `error`; the rig tools add `busy` when a
    live session refused, so the model says "already running". A success keeps
    its receipt."""
    from slopstation.agent.dispatch import Result

    monkeypatch.setattr(
        dispatch,
        "play_game",
        lambda appid, turn=None: Result(False, "busy", "a session is live"),
    )
    r = impls["launch_game"]({"appid": INSTALLED})
    assert r == {"ok": False, "error": "a session is live", "busy": True}
    monkeypatch.setattr(
        dispatch,
        "play_game",
        lambda appid, turn=None: Result(False, "fail", "the PC is asleep"),
    )
    r = impls["launch_game"]({"appid": INSTALLED})
    assert r == {"ok": False, "error": "the PC is asleep"} and "detail" not in r
    monkeypatch.setattr(
        dispatch, "play_game", lambda appid, turn=None: Result(True, "ok", "launched")
    )
    assert impls["launch_game"]({"appid": INSTALLED}) == {
        "ok": True,
        "detail": "launched",
    }


def test_now_playing_reports_whether_the_rig_is_busy(impls):
    # now_playing answers both halves: what the PC runs, and whether the rig is
    # busy. Mid-launch the PC says 0, and only session_active stops that
    # reading as idle.
    r = impls["get_now_playing"]({})
    assert r["ok"]  # dry-run path
    assert r["session_active"] is False, r
    seed_lock(0)  # a launch owns the rig
    r = impls["get_now_playing"]({})
    assert r["ok"] and r["session_active"] is True, r
    seed_lock(sessionlock.LOCK_STALE_S + 60)  # stale = free, same as ever
    assert impls["get_now_playing"]({})["session_active"] is False


def test_now_playing_tells_a_launch_from_a_live_session(
    live_dispatch, log, monkeypatch
):
    # The lock is held from the chord to the end of the session; READY on the
    # PC is what separates "starting" from "up".
    impls = toolkit_impls(live_dispatch, log)
    monkeypatch.setattr(gamepc, "playing", lambda: "0")
    seed_lock(0)
    monkeypatch.setattr(gamepc, "status", lambda: "NOTREADY")
    r = impls["get_now_playing"]({})
    assert r["session_active"] is True and r["launching"] is True, r
    monkeypatch.setattr(gamepc, "status", lambda: "ab12cd")
    assert impls["get_now_playing"]({})["launching"] is False
    monkeypatch.setattr(gamepc, "status", lambda: (_ for _ in ()).throw(OSError()))
    assert impls["get_now_playing"]({})["launching"] is True, "unreachable = not up"


def test_game_details_names_installed_and_owned_only_games(impls):
    r = impls["get_game_details"]({"appid": INSTALLED})
    assert r["ok"] and r["name"] == "Valheim" and r["installed"]
    # Owned-but-not-installed must still come back named.
    r = impls["get_game_details"]({"appid": OWNED_ONLY})
    assert r["ok"] and r["name"] and not r["installed"], r


# -- the operations ledger -----------------------------------------------------


def test_list_operations_acknowledges_only_on_a_live_recent_read(
    dispatch, live_dispatch, log, fake_operations
):
    oimpls = toolkit_impls(dispatch, log, operations=fake_operations)
    r = oimpls["list_operations"]({"scope": "active"})
    assert r["ok"] and r["operations"][0]["state"] == "RUNNING"
    assert fake_operations.acknowledged is False
    assert oimpls["list_operations"]({"scope": "recent"})["ok"]
    assert fake_operations.acknowledged is False  # a dry run eats no bulletin
    live_ops = toolkit_impls(live_dispatch, log, operations=fake_operations)
    assert live_ops["list_operations"]({"scope": "recent"})["ok"]
    assert fake_operations.acknowledged is True
    assert not oimpls["list_operations"]({"scope": "nope"})["ok"]


# -- the media tools -----------------------------------------------------------


def test_request_movie_previews_dry_and_tracks_the_turn_live(
    media_impls, live_media, fake_media, fake_operations
):
    preview = media_impls["request_movie"]({"tmdb_id": 438631, "preset": "1080p"})
    assert preview["dry_run"] and fake_media.requests == []
    requested = live_media["request_movie"]({"tmdb_id": 438631, "preset": "1080p"})
    assert requested["operation_id"] == "op-media"
    assert fake_media.requests[-1] == ("movie", 438631, "1080p")
    assert fake_operations.tracked[-1][4] == "fa1100"


def test_request_series_needs_a_season_scope(live_media, fake_media, fake_operations):
    series_requested = live_media["request_series"]({"tvdb_id": 81189, "seasons": [2]})
    assert series_requested["operation_id"] == "op-media"
    assert series_requested["all_seasons"] is False
    assert fake_media.requests[-1] == ("series", 81189, "default", [2], None)
    assert series_requested["acknowledgment"] == (
        "Requested Breaking Bad, season 2, using the default quality profile. "
        "Sonarr is searching in the background."
    )
    before = list(fake_media.requests)
    unscoped = live_media["request_series"]({"tvdb_id": 81189})
    assert not unscoped["ok"]
    assert unscoped["clarification"] == (
        "Which season would you like, or should I download all seasons?"
    )
    assert fake_media.requests == before
    mixed = live_media["request_series"](
        {"tvdb_id": 81189, "seasons": [1], "all_seasons": True}
    )
    assert not mixed["ok"] and fake_media.requests == before
    empty_scope = live_media["request_series"](
        {"tvdb_id": 81189, "seasons": [], "all_seasons": True}
    )
    assert empty_scope["all_seasons"] is True
    assert fake_media.requests[-1] == ("series", 81189, "default", None, None)
    all_requested = live_media["request_series"](
        {"tvdb_id": 81189, "preset": "2160p", "all_seasons": True}
    )
    assert all_requested["acknowledgment"] == (
        "Requested Breaking Bad, all normal seasons, in 2160p. "
        "Sonarr is searching in the background."
    )
    assert all_requested["all_seasons"] is True
    assert fake_media.requests[-1] == ("series", 81189, "2160p", None, None)
    assert fake_operations.tracked[-1][6]["all_seasons"] is True


def test_request_series_takes_individual_episodes(live_media, fake_media):
    """A list of episodes is requested as those episodes, never widened to
    their seasons."""
    requested = live_media["request_series"](
        {
            "tvdb_id": 75805,
            "episodes": [
                {"season": 10, "episode": 4},
                {"season": 4, "episode": 13},
                {"season": 4, "episode": 13},
            ],
        }
    )
    assert requested["ok"]
    assert fake_media.requests[-1] == (
        "series",
        75805,
        "default",
        None,
        [(4, 13), (10, 4)],
    )
    assert requested["acknowledgment"] == (
        "Requested Breaking Bad, season 4 episode 13 and season 10 episode 4, "
        "using the default quality profile. Sonarr is searching in the background."
    )


def test_delete_media_validates_its_scope(live_media):
    for bad in ([0], [-1], [True], "2", 2, [2, "3"], (2,)):
        assert (
            "positive integers"
            in live_media["delete_media"](
                {"kind": "series", "catalog_id": 81189, "seasons": bad}
            )["error"]
        ), bad
    # The model sends every schema field, so an episode delete arrives with
    # seasons: [] beside it; an empty list is no scope, not a second one.
    asked = live_media["delete_media"](
        {
            "kind": "series",
            "catalog_id": 81189,
            "seasons": [],
            "episodes": [{"season": 1, "episode": 2}],
            "all_seasons": False,
        }
    )
    assert "acknowledgment" in asked, asked
    assert (
        "episodes"
        in live_media["delete_media"](
            {"kind": "series", "catalog_id": 81189, "seasons": []}
        )["error"]
    )
    assert (
        "integer"
        in live_media["delete_media"]({"kind": "movie", "catalog_id": "dune"})["error"]
    )
    for bad in ([{"season": 1}], [{"season": 0, "episode": 1}], [[1, 2]], ["S01E02"]):
        assert (
            "episodes must be"
            in live_media["delete_media"](
                {"kind": "series", "catalog_id": 81189, "episodes": bad}
            )["error"]
        ), bad
    both = live_media["delete_media"](
        {
            "kind": "series",
            "catalog_id": 81189,
            "seasons": [1],
            "episodes": [{"season": 1, "episode": 2}],
        }
    )
    assert "not more than one" in both["error"]
    assert (
        "only a series"
        in live_media["delete_media"](
            {
                "kind": "movie",
                "catalog_id": 949,
                "episodes": [{"season": 1, "episode": 2}],
            }
        )["error"]
    )
    assert (
        "episodes"
        in live_media["delete_media"]({"kind": "series", "catalog_id": 81189})["error"]
    )


def test_delete_media_needs_a_confirmation_from_a_later_turn(
    monkeypatch, live_dispatch, live_media, fake_media, fake_operations, log
):
    # A download of the very season is in flight: the delete cancels it too.
    fake_operations.active_rows.append(
        {
            "id": "op-andor",
            "kind": "series_acquisition",
            "progress": {"phase": "downloading"},
            "metadata": {"catalog_id": 81189, "seasons": [2], "command_ids": [77]},
        }
    )
    ask = {"kind": "series", "catalog_id": 81189, "seasons": [2]}
    asked = live_media["delete_media"](dict(ask))
    assert not asked["ok"] and "Breaking Bad 2008, season 2" in asked["acknowledgment"]
    # a repeat inside the same turn is refused, so the model cannot self-confirm
    assert not live_media["delete_media"](dict(ask))["ok"]
    assert not fake_media.requests[-1][0].startswith("delete")
    assert log.find("tool_refused")[-1]["reason"] == "unconfirmed"
    live_dispatch.begin_utterance("fa1101", "yes")
    deleted = live_media["delete_media"](dict(ask))
    assert deleted["ok"]
    # a question the user declined must not stay armed
    monkeypatch.setattr(confirm, "ASK_TTL_S", -1)
    live_media["delete_media"](dict(ask))
    live_dispatch.begin_utterance("fa1102", "later")
    assert not live_media["delete_media"](dict(ask))["ok"]
    assert ("delete_series", 81189, [2], False, [77]) in fake_media.requests
    assert deleted["operations_canceled"] == ["op-andor"]
    assert fake_operations.observed[-1][1] == "CANCELED"
    assert fake_operations.delivered == ["op-andor"]


# -- data lane: list_games / search_store routing + the kill switch ------------


def test_list_games_and_search_store_refuse_a_bad_ask(impls):
    r = impls["list_games"]({"source": "nope"})
    assert not r["ok"] and "unknown source" in r["error"], r
    r = impls["search_store"]({})  # neither term nor tags
    assert not r["ok"] and ("term" in r["error"] or "genre" in r["error"]), r


def test_install_game_is_always_offered_and_tracks_the_turn(
    dispatch, live_dispatch, log, impls, fake_steam, fake_operations
):
    # install_game is offered with or without the account session: without one
    # it navigates to the game page so the controller can finish the job.
    assert "install_game" in impls
    with_steam = toolkit_impls(dispatch, log, steam=fake_steam)
    rr = with_steam["install_game"]({"appid": UNKNOWN})
    assert not rr["ok"] and "not in the catalog" in rr["error"], rr
    live_dispatch.begin_utterance("4c1d0e", "install stardew valley")
    tracked = toolkit_impls(
        live_dispatch, log, steam=fake_steam, operations=fake_operations
    )
    rr = tracked["install_game"]({"appid": OWNED_ONLY})
    assert rr["ok"] and rr["operation_id"] == "op-test", rr
    assert fake_operations.tracked[-1][2] == "4c1d0e"


# -- Tool errors ---------------------------------------------------------------


def test_a_dead_token_falls_through_to_the_tv_path(
    monkeypatch, dispatch, live_dispatch, log, fake_steam
):
    # A revoked token still has available()==True, then the steam call raises;
    # the tool must answer, not break the turn.
    rimpls = toolkit_impls(live_dispatch, log, steam=RaisingSteam())
    monkeypatch.setattr(library, "installed_name", lambda a: None)  # -> steam.install
    navd = []
    monkeypatch.setattr(live_dispatch, "nav", recording_nav(navd))
    inst = rimpls["install_game"]({"appid": INSTALLED})
    assert toolkit_impls(dispatch, log, steam=fake_steam)["install_game"](
        {"appid": INSTALLED}
    )["dry_run"]
    # A dead token must not end the request: it falls through to the TV path.
    # With no session the page comes up with the session nav starts, and the
    # receipt says so instead of claiming the page is on the TV already.
    assert inst["ok"] and "press Install" in inst["detail"], inst
    assert "showing" in inst["detail"] and "on the TV now" not in inst["detail"]
    assert navd == [("details", INSTALLED)], navd
    seed_lock(10)  # a live session: the page is on the TV at once
    inst = rimpls["install_game"]({"appid": INSTALLED})
    assert inst["ok"] and "on the TV now" in inst["detail"], inst
    seed_lock(None)
    dl = rimpls["download_status"]({})
    assert not dl["ok"] and "Steam" in dl["error"], dl
    assert {"install_fallback", "download_status_error"} <= set(log.events())


def running(impls, log):
    """A Toolkit running these impls, all loaded, in registry order."""
    tk = assistant.Toolkit(fake_dispatch(), log)
    tk.impls, tk.loaded = impls, [n for n in assistant.REGISTRY.names() if n in impls]
    return tk


def test_stop_listening_ends_the_turn_with_no_second_llm_turn(log):
    results = []
    tools = running({"stop_listening": lambda _: {"ok": True, "end_turn": True}}, log)
    schema = tool_schemas.pipecat_schemas(tools)[0]

    class Params:
        arguments = {}
        pipeline_worker = None

        async def result_callback(self, out, *, properties=None):
            results.append((out, properties))

    asyncio.run(schema.handler(Params()))
    out, props = results[0]
    assert out["ok"] and props is not None and props.run_llm is False, results
    assert props.on_context_updated is None, "nothing is spoken to a closing mic"


def test_an_acknowledgment_is_spoken_without_a_second_llm_turn(log):
    spoken = []
    receipt_result = []
    tools = running(
        {
            "request_series": lambda _: {
                "ok": True,
                "acknowledgment": "Requested Andor, season 1, in 2160p.",
            }
        },
        log,
    )
    receipt_schema = tool_schemas.pipecat_schemas(tools)[0]

    class ReceiptWorker:
        async def queue_frame(self, frame):
            spoken.append(frame)

    class ReceiptParams:
        arguments = {"tvdb_id": 393189, "seasons": [1]}
        pipeline_worker = ReceiptWorker()

        async def result_callback(self, out, *, properties=None):
            receipt_result.append((out, properties))

    async def exercise_receipt():
        await receipt_schema.handler(ReceiptParams())
        await receipt_result[0][1].on_context_updated()

    asyncio.run(exercise_receipt())
    assert receipt_result[0][1].run_llm is False
    assert spoken[0].text == "Requested Andor, season 1, in 2160p."
    assert spoken[0].append_to_context is True


# -- every tool call is RECORDED, including the ones that raise ----------------


def test_every_tool_call_is_recorded_including_the_raisers(monkeypatch):
    # A tool-calling llm span traces as output:null; Toolkit.call is where the
    # tool and its args are recorded.
    tlog = CapturingLog("voice")
    calls = {"n": 0}

    def spy(kind, query, status=None):
        calls["n"] += 1

    monkeypatch.setattr(assistant.sentry, "tool_span", spy)
    tools = running(
        {
            "get_now_playing": lambda a: {"ok": True, "game": "Hades"},
            "launch_game": boom,
        },
        tlog,
    )
    schemas = tool_schemas.pipecat_schemas(tools)

    answered = []

    class P2:
        arguments = {"appid": 1145360}

        async def result_callback(self, out):
            answered.append(out)

    for s in schemas:
        asyncio.run(s.handler(P2()))
    # Order follows the registry, not the impls dict, so key by tool name.
    rec = {r["tool"]: r for r in tlog.records if r.get("event") == "tool_call"}
    assert set(rec) == {"get_now_playing", "launch_game"}, rec  # the raiser too
    assert rec["get_now_playing"]["ok"] is True
    assert rec["launch_game"]["ok"] is False, "a raising impl must still record"
    assert "1145360" in rec["launch_game"]["args"]  # args carried, for search terms
    assert calls["n"] == 2, "tool_span not called per tool"
    # The raiser still answers through result_callback, so the turn never hangs.
    assert [o["ok"] for o in answered] == [False, True], answered


# -- nav tool: target->kind remap + catalog guard ------------------------------


def test_nav_remaps_targets_and_guards_the_catalog(monkeypatch, dispatch, log):
    seen = []
    monkeypatch.setattr(dispatch, "nav", recording_nav(seen))
    navimpls = toolkit_impls(dispatch, log)
    assert navimpls["nav"]({"target": "game_page", "appid": INSTALLED})["ok"]
    assert navimpls["nav"]({"target": "store_page", "appid": INSTALLED})["ok"]
    assert navimpls["nav"]({"target": "downloads"})["ok"]
    assert seen == [
        ("details", INSTALLED),
        ("store", INSTALLED),
        ("downloads", None),
    ], seen
    assert not navimpls["nav"]({"target": "bogus"})["ok"]
    # An unowned appid: the library page is refused, the store page is not -
    # with the install dialog needing a button press, that IS the install path.
    assert not navimpls["nav"]({"target": "game_page", "appid": UNKNOWN})["ok"]
    assert navimpls["nav"]({"target": "store_page", "appid": 1478500})["ok"]
    assert seen[-1] == ("store", 1478500), seen[-1]
    assert not navimpls["nav"]({"target": "store_page", "appid": 0})["ok"]


def test_nav_reaches_the_newer_pages_search_and_allowed_urls(
    monkeypatch, dispatch, log
):
    seen = []
    monkeypatch.setattr(dispatch, "nav", recording_nav(seen))
    navimpls = toolkit_impls(dispatch, log)
    nav = navimpls["nav"]
    for target in ("friends", "settings", "screenshots", "wishlist"):
        assert nav({"target": target})["ok"]
    assert seen[-4:] == [
        (t, None) for t in ("friends", "settings", "screenshots", "wishlist")
    ]
    # Per-game pages take any appid; the tool maps its names onto the verb's.
    assert nav({"target": "dlc", "appid": 1478500})["ok"] and seen[-1] == (
        "dlc",
        1478500,
    )
    assert nav({"target": "community_hub", "appid": 1478500})["ok"]
    assert seen[-1] == ("hub", 1478500)
    assert nav({"target": "verify_files", "appid": INSTALLED})["ok"]
    assert seen[-1] == ("validate", INSTALLED)
    assert not nav({"target": "workshop"})["ok"], "a per-game page needs the appid"
    # news is both: the feed without an appid, one game's with.
    assert nav({"target": "news"})["ok"] and seen[-1] == ("news", None)
    assert nav({"target": "news", "appid": INSTALLED})["ok"]
    assert seen[-1] == ("news", INSTALLED)
    # search builds a store URL with the words encoded, trimmed to fit the
    # PC's URL length when the encoding blows up.
    assert nav({"target": "search", "query": "co-op roguelike & friends"})["ok"]
    assert seen[-1] == (
        "url",
        "https://store.steampowered.com/search/?term=co-op+roguelike+%26+friends",
    )
    assert nav({"target": "search", "query": "'" * 120})["ok"]
    assert len(seen[-1][1]) <= len("https://store.steampowered.com/") + 300
    assert not nav({"target": "search", "query": " "})["ok"]
    # The two front pages are pages too.
    assert nav({"target": "web", "url": "https://steamcommunity.com/"})["ok"]
    # web takes the two Steam hosts and nothing else.
    assert nav({"target": "web", "url": "https://steamcommunity.com/id/someone/"})["ok"]
    assert seen[-1] == ("url", "https://steamcommunity.com/id/someone/")
    r = nav({"target": "web", "url": "https://example.com/store.steampowered.com/"})
    assert not r["ok"] and "store.steampowered.com" in r["error"]
    assert not nav({"target": "web", "url": "http://store.steampowered.com/"})["ok"]
    assert not nav({"target": "web", "url": "javascript:alert(1)"})["ok"]


def test_nav_resolves_a_collection_by_name_and_lists_them_on_a_miss(
    monkeypatch, dispatch, log
):
    # Collections by name, with the miss handing back the real list (STT turns
    # "mech" into "neck").
    seen = []
    monkeypatch.setattr(dispatch, "nav", recording_nav(seen))
    monkeypatch.setattr(
        library,
        "load",
        lambda: {
            "collections": [
                {"name": "mech", "id": "uc-m1"},
                {"name": "RPG", "id": "uc-r1"},
            ]
        },
    )
    navimpls = toolkit_impls(dispatch, log)
    r = navimpls["nav"]({"target": "collection", "collection": "mech"})
    assert r["ok"] and seen[-1] == ("collection", "uc-m1"), (r, seen[-1])
    r = navimpls["nav"]({"target": "collection", "collection": "neck"})
    assert not r["ok"] and set(r["collections"]) == {"mech", "RPG"}, r


# -- list_games routing + get_game_details hltb-fallback, fetchers mocked ------


def test_list_games_routes_each_source_to_its_fetcher(monkeypatch, impls):
    monkeypatch.setattr(
        steamstore,
        "load_deals",
        lambda: {
            "specials": [{"appid": 1, "name": "S"}],
            "wishlist_on_sale": [{"appid": 2, "name": "W"}],
        },
    )
    monkeypatch.setattr(
        steamstore, "fetch_trending", lambda: [{"appid": 3, "name": "T", "rank": 1}]
    )
    monkeypatch.setattr(
        steamstore,
        "fetch_recently_played",
        lambda: [{"appid": 4, "name": "R", "hours2w": 2.0}],
    )
    assert impls["list_games"]({"source": "specials"})["games"][0]["name"] == "S"
    assert (
        impls["list_games"]({"source": "wishlist_on_sale"})["games"][0]["name"] == "W"
    )
    assert impls["list_games"]({"source": "trending"})["games"][0]["name"] == "T"
    assert impls["list_games"]({"source": "recently_played"})["games"][0]["name"] == "R"


def test_game_details_resolves_a_missing_name_from_the_store(monkeypatch, impls):
    # hltb for a game with no catalog name resolves the name from the store.
    hltb_calls = []
    monkeypatch.setattr(
        steamstore,
        "store_items",
        lambda a: {a[0]: {"name": "Some Unowned Game"}},
    )
    monkeypatch.setattr(
        steamstore, "fetch_hltb", lambda name: hltb_calls.append(name) or {"main": 20}
    )
    r = impls["get_game_details"]({"appid": 424242, "facets": ["hltb"]})
    assert (
        r["ok"]
        and r.get("hltb") == {"main": 20}
        and hltb_calls == ["Some Unowned Game"]
    ), r
    # Every facet ask resolves a missing name, not just hltb: nameless review
    # payloads made the model match results to titles from memory.
    monkeypatch.setattr(
        steamstore, "fetch_reviews", lambda a: {"desc": "Very Positive"}
    )
    r = impls["get_game_details"]({"appid": 424242, "facets": ["reviews"]})
    assert r["ok"] and r["name"] == "Some Unowned Game", r


def test_server_side_search_follows_the_knob(catalog):
    # Server-side search: knob off -> absent everywhere; knob on -> each
    # provider's native entry next to, not instead of, the tools.
    voice_off = CFG_MIN["voice"]
    assert assistant.server_tools(voice_off, "anthropic") == []
    assert assistant.server_tools(voice_off, "openai") == []
    assert prompts.WEB_SEARCH_RULE not in assistant.system_instruction(CFG_MIN)
    (aw,) = assistant.server_tools(VOICE_ON, "anthropic")
    (ow,) = assistant.server_tools(VOICE_ON, "openai")
    assert aw["type"] == "web_search_20250305" and aw["max_uses"] == 2
    assert ow["type"] == "web_search" and ow["search_context_size"] == "low"
    assert (
        aw["user_location"]
        == ow["user_location"]
        == {"type": "approximate", "city": "Portland", "country": "US"}
    )
    bare = {
        **VOICE_ON,
        "location": {"city": "", "region": "", "country": "", "timezone": ""},
    }
    assert "user_location" not in assistant.server_tools(bare, "openai")[0]
    si_on = assistant.system_instruction({**CFG_MIN, "voice": VOICE_ON})
    # Voice replies omit citations and search narration.
    assert prompts.WEB_SEARCH_RULE in si_on and prompts.WEB_SEARCH_VOICE_RULE in si_on


def test_anthropic_backend_resumes_a_paused_turn(monkeypatch, log):
    # Resuming a turn keeps the partial assistant response.
    b = backends.AnthropicBackend(
        {"anthropicApiKey": "x" * 24}, "claude-haiku-4-5", voice=VOICE_ON
    )
    script = [
        types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="server_tool_use"),
                types.SimpleNamespace(type="text", text="Checking."),
            ],
            stop_reason="pause_turn",
            usage=None,
        ),
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="June 2026.")],
            stop_reason="end_turn",
            usage=None,
        ),
    ]
    calls = []
    monkeypatch.setattr(
        b,
        "client",
        types.SimpleNamespace(
            messages=types.SimpleNamespace(
                create=lambda **kw: (calls.append(kw), script.pop(0))[1]
            )
        ),
    )
    out = b.turn("sys", "when did the dlc ship", running({}, log))
    assert out == "Checking. June 2026.", out
    assert len(calls) == 2 and not script
    assert calls[0]["tools"][-1]["type"] == "web_search_20250305"
    assert calls[1]["messages"][-1]["role"] == "assistant"  # partial re-sent


# -- pipecat constructions with dummy keys -------------------------------------


def test_make_llm_builds_both_providers_from_dummy_keys(catalog):
    # Through the PRODUCTION _make_llm: a local copy can pass a dict for
    # `reasoning`, which only live inference rejects.
    from slopstation.agent.speech import session

    si = assistant.system_instruction(CFG_MIN)
    dummy = {"anthropicApiKey": "x" * 24, "openaiApiKey": "x" * 24}
    voice_a = {
        **CFG_MIN["voice"],
        "assistantProvider": "anthropic",
        "assistantModelAnthropic": "claude-haiku-4-5",
        "assistantModelOpenai": "gpt-5.6-luna",
    }
    session._make_llm(voice_a, dummy, si)
    voice_o = {
        **voice_a,
        "assistantProvider": "openai",
        "assistantModelOpenai": "gpt-5.6-luna",
        "assistantReasoningEffort": "low",
    }
    llm_o = session._make_llm(voice_o, dummy, si)
    # The inference path's model_dump() call, which a plain dict would fail.
    assert llm_o._settings.reasoning.model_dump(exclude_none=True) == {
        "effort": "low"
    }, llm_o._settings.reasoning


def test_the_sdk_clients_carry_deadlines():
    # The SDK clients carry explicit deadlines: without them a stalled
    # provider stream outlives mcp.py's 280 s forwarding budget.
    real = backends.AnthropicBackend({"anthropicApiKey": "a" * 64}, "claude-test")
    assert real.client.timeout == backends.LLM_TIMEOUT_S
    assert real.client.max_retries == backends.LLM_MAX_RETRIES
