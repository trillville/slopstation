"""Test the one page shape every listing tool answers with."""

from slopstation.agent.llm import assistant, paging


def test_page_walks_a_local_list_and_a_server_window():
    rows = list(range(7))
    first = paging.page(rows, {"limit": 3}, "items", kind="x")
    assert first == {
        "ok": True,
        "kind": "x",
        "count": 7,
        "offset": 0,
        "items": [0, 1, 2],
        "next_offset": 3,
    }
    middle = paging.page(rows, {"limit": 3, "offset": first["next_offset"]}, "items")
    assert middle["items"] == [3, 4, 5] and middle["next_offset"] == 6
    last = paging.page(rows, {"limit": 3, "offset": 6}, "items")
    assert last["items"] == [6] and last["next_offset"] is None
    assert paging.page(rows, {"offset": 99}, "items")["items"] == []
    # A server window already starts at the offset; the total is the app's.
    window = paging.page([10, 11, 12], {"limit": 2, "offset": 4}, "items", total=9)
    assert window["items"] == [10, 11] and window["next_offset"] == 6
    assert window["count"] == 9 and window["offset"] == 4
    assert (
        paging.page([10, 11], {"limit": 2, "offset": 4}, "items", total=6)[
            "next_offset"
        ]
        is None
    )
    # Malformed and out-of-range asks.
    assert not paging.page(rows, {"limit": "x"}, "items")["ok"]
    assert not paging.page(rows, {"offset": -1}, "items")["ok"]
    assert paging.window({"limit": 999, "offset": 10**9}) == (
        (paging.CAP, paging.OFFSET_MAX),
        None,
    )
    assert paging.window({}, default=5, cap=8) == ((5, 0), None)
    props = paging.properties(cap=25, what="lines")
    assert set(props) == {"limit", "offset"}
    assert props["limit"]["description"] == "lines per page, default 10, at most 25"


def test_every_paged_tool_carries_the_shared_properties():
    for spec in assistant.REGISTRY:
        if spec.paged:
            assert spec.properties["offset"] == paging.properties()["offset"], spec.name
