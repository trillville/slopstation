"""Test find_tools: retrieval, loading, and the loaded set reaching each lane."""

import types

import pytest

from helpers import CapturingLog
from slopstation.agent.llm import assistant, backends, toolsearch

# One realistic ask per searchable tool that exists today. Every new
# searchable tool gets a line here, so a keyword list that stops retrieving
# its own tool fails loudly.
ASKS = {
    "delete_media": "delete the movie and erase its files",
    "describe_api": "look up the api documentation for the queue endpoint",
    "radarr_api": "call the radarr api directly",
    "sonarr_api": "make a raw sonarr call",
    "prowlarr_api": "hit the prowlarr api",
    "qbittorrent_api": "call the qbittorrent api",
    "steam_api": "call the steam web api directly",
    "list_torrents": "what torrents are downloading right now",
    "torrent_details": "show me the files and trackers of that torrent",
    "pause_torrent": "pause the dune torrent",
    "resume_torrent": "resume all torrents",
    "recheck_torrent": "force a recheck of that torrent",
    "reannounce_torrent": "reannounce the stalled torrent to its tracker",
    "force_start": "force start that download and skip the queue",
    "set_torrent_priority": "move that torrent to the top of the queue",
    "delete_torrent": "remove the orphan torrent and its files",
    "transfer_info": "what is the current download speed in qbittorrent",
    "set_speed_limits": "throttle the upload speed limit to 500",
    "seeding_report": "what am i seeding and what are the ratios",
    "orphan_torrents": "any orphan torrents that were never imported",
    "vpn_status": "is the vpn up and the port forward working",
    "qbit_log": "show the qbittorrent log for errors",
    "disk_usage": "how much free disk space is left",
    "largest_items": "what takes the most space, the biggest folders",
    "orphan_files": "any leftover or orphan files on the drive",
    "delete_path": "delete that folder from the disk",
    "drive_health": "is the drive ok, any smart warnings",
    "browse_media": "browse the movies i have by genre, recently added",
    "media_details": "what quality is that movie and which episodes are missing",
    "missing_media": "what episodes are missing or not downloaded yet",
    "calendar": "what airs this week, upcoming episodes",
    "search_releases": "do a manual search for releases of that movie",
    "grab_release": "grab that release and download that one",
    "retry_search": "search again for it, the download is stuck",
    "set_monitored": "stop tracking that show, unmonitor it",
    "set_quality_profile": "change the quality profile to 4k",
    "import_queue": "why is the import stuck, show the queue warnings",
    "resolve_queue_item": "remove it from the queue and blocklist that release",
    "manual_import": "force the manual import of that download",
    "media_history": "what was grabbed recently, media history",
    "media_health": "is radarr ok, media stack health and indexer status",
    "movie_collections": "do i have all the alien movies in the collection",
    "search_indexers": "search the indexers for a documentary release",
}


@pytest.fixture
def log():
    return CapturingLog("voice")


@pytest.fixture
def toolkit(log):
    dispatch = types.SimpleNamespace(
        dry_run=True, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    return assistant.Toolkit(
        dispatch, log, operations=object(), media=object(), steam=object()
    )


def test_every_tool_is_found_by_its_own_summary_and_its_ask(toolkit):
    for spec in assistant.REGISTRY:
        hits = [s.name for s, _ in toolsearch.search(toolkit.registry, spec.name)]
        assert spec.name in hits, f"{spec.name} not found by its own name"
        summary = toolsearch.summary(spec)
        hits = [s.name for s, _ in toolsearch.search(toolkit.registry, summary)]
        assert spec.name in hits, (
            f"{spec.name} not in top {toolsearch.TOP_N} for {summary!r}"
        )
    searchable = {s.name for s in assistant.REGISTRY if not s.default}
    assert set(ASKS) == searchable, "every searchable tool needs an ask in ASKS"
    for name, ask in ASKS.items():
        hits = [s.name for s, _ in toolsearch.search(toolkit.registry, ask)]
        assert name in hits, f"{name} not found for {ask!r}: got {hits}"


def test_search_is_deterministic_excludes_loaded_and_has_a_floor(toolkit):
    a = toolsearch.search(toolkit.registry, "volume louder")
    b = toolsearch.search(toolkit.registry, "volume louder")
    assert [s.name for s, _ in a] == [s.name for s, _ in b]
    assert a[0][0].name == "volume"
    assert "volume" not in {
        s.name for s, _ in toolsearch.search(toolkit.registry, "volume", {"volume"})
    }
    assert toolsearch.search(toolkit.registry, "the of and") == []
    assert toolsearch.search(toolkit.registry, "qzxv plonk") == []


def test_find_tools_loads_matches_and_lists_areas_on_a_miss(toolkit, log):
    assert "delete_media" not in toolkit.loaded
    before = len(toolkit.render("openai"))
    out = toolkit.call("find_tools", {"query": "erase the files for that movie"})
    assert out["ok"] and "delete_media" in [r["tool"] for r in out["loaded"]], out
    assert "delete_media" in toolkit.loaded
    assert len(toolkit.render("openai")) == before + len(out["loaded"])
    assert len(toolkit.render("anthropic")) == len(toolkit.render("openai"))
    # Registry order survives loading, so the rendered prefix is stable.
    assert toolkit.loaded == [
        n for n in toolkit.registry.names() if n in toolkit.loaded
    ]
    found = log.find("tools_found")
    assert found and "delete_media" in found[-1]["found"]
    miss = toolkit.call("find_tools", {"query": "qzxv plonk"})
    assert not miss["ok"] and "areas" in miss and log.find("tools_found")[-1]["n"] == 0
    assert not toolkit.call("find_tools", {})["ok"]
    assert not toolkit.call("no_such_tool", {})["ok"]


def test_on_load_fires_once_per_change_with_the_new_schemas(log):
    pushed = []
    dispatch = types.SimpleNamespace(dry_run=True, utterance=None)
    tk = assistant.Toolkit(
        dispatch, log, media=object(), on_load=lambda: pushed.append(1)
    )
    assert tk.load(["delete_media"]) == ["delete_media"] and pushed == [1]
    assert tk.load(["delete_media"]) == [] and pushed == [1], "no change, no push"
    assert tk.load(["not_a_tool"]) == [] and pushed == [1]
    assert [s.name for s in tk.function_schemas(log)][-1] == "delete_media"


def test_the_backends_render_the_loaded_set_on_every_request(monkeypatch, log):
    """A tool found mid-turn is offered on the very next request of that turn,
    on both providers."""
    dispatch = types.SimpleNamespace(
        dry_run=True, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    tk = assistant.Toolkit(dispatch, log, media=object())
    calls = []

    # Anthropic: turn 1 asks find_tools, turn 2 answers.
    b = backends.AnthropicBackend({"anthropicApiKey": "x" * 24}, "m")
    script = [
        types.SimpleNamespace(
            content=[
                types.SimpleNamespace(
                    type="tool_use",
                    id="t1",
                    name="find_tools",
                    input={"query": "delete movie files"},
                )
            ],
            stop_reason="tool_use",
            usage=None,
        ),
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="Delete Dune?")],
            stop_reason="end_turn",
            usage=None,
        ),
    ]
    monkeypatch.setattr(
        b,
        "client",
        types.SimpleNamespace(
            messages=types.SimpleNamespace(
                create=lambda **kw: (calls.append(kw), script.pop(0))[1]
            )
        ),
    )
    assert b.turn("sys", "erase dune", tk) == "Delete Dune?"
    names = [[t["name"] for t in c["tools"]] for c in calls]
    assert "delete_media" not in names[0] and "delete_media" in names[1], names

    # OpenAI, same shape.
    calls.clear()
    tk2 = assistant.Toolkit(dispatch, log, media=object())
    o = backends.OpenAIBackend({"openaiApiKey": "x" * 24}, "m")
    oscript = [
        types.SimpleNamespace(
            id="r1",
            usage=None,
            output=[
                types.SimpleNamespace(
                    type="function_call",
                    call_id="c1",
                    name="find_tools",
                    arguments='{"query": "delete movie files"}',
                )
            ],
            output_text="",
        ),
        types.SimpleNamespace(
            id="r2", usage=None, output=[], output_text="Delete Dune?"
        ),
    ]
    monkeypatch.setattr(
        o,
        "client",
        types.SimpleNamespace(
            responses=types.SimpleNamespace(
                create=lambda **kw: (calls.append(kw), oscript.pop(0))[1]
            )
        ),
    )
    assert o.turn("sys", "erase dune", tk2) == "Delete Dune?"
    names = [[t["name"] for t in c["tools"]] for c in calls]
    assert "delete_media" not in names[0] and "delete_media" in names[1], names


def test_the_prompt_maps_the_areas_from_the_offered_set():
    text = assistant.tools_map()
    assert text.startswith("TOOLS:") and "find_tools reaches" in text
    assert "media (" in text
    # Nothing beyond the defaults offered: no map at all, rather than a lie.
    defaults = [s.name for s in assistant.REGISTRY if s.default]
    assert assistant.tools_map(defaults) == ""
