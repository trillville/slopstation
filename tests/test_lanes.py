"""The chord lane never imports the agent lane."""

import ast
import pathlib

import helpers

# What the chord lane is made of: the controller, the launch, the devices,
# and the modules every lane shares. A voice failure must not be able to
# take these down, so they import nothing from slopstation.agent, not even
# lazily inside a function.
CHORD_LANE = (
    "chord_listener",
    "couch",
    "gamepc",
    "tv",
    "haptics",
    "events",
    "logbook",
    "config",
    "paths",
    "sessionlock",
    "statefile",
    "supervise",
    "checkin",
)


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_the_chord_lane_imports_nothing_from_the_agent():
    root = pathlib.Path(helpers.REPO) / "src" / "slopstation"
    for name in CHORD_LANE:
        path = root / f"{name}.py"
        assert path.exists(), path
        crossing = sorted(
            m for m in _imports(path) if m.startswith("slopstation.agent")
        )
        assert not crossing, f"{name}.py imports the agent lane: {crossing}"


def test_the_rule_would_catch_a_crossing(tmp_path):
    bad = tmp_path / "x.py"
    bad.write_text("def f():\n    from slopstation.agent.tools import library\n")
    assert any(m.startswith("slopstation.agent") for m in _imports(bad))
