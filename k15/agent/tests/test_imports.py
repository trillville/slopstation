"""Guard: every module imports with no config.json and no hardware, and every
`module.attr` written in the code resolves - pyflakes cannot see a moved
attribute, and the chord lane's haptic path swallows the AttributeError it
would raise. Run:
    .venv\\Scripts\\python tests\\test_imports.py
"""
import ast
import importlib
import sys
import types

import _bootstrap  # noqa: F401

K15 = _bootstrap.K15
AGENT = _bootstrap.AGENT

# Hardware libs the chord lane imports at top level; stubbed so the walk runs
# on any box.
for name in ("hid", "serial"):
    sys.modules.setdefault(name, types.ModuleType(name))

def modules():
    """(path, import name) for everything this walk imports. The agent half
    comes from the package walk, never rglob: agent/.venv is ~11.6k files."""
    for p in sorted(K15.glob("*.py")) + _bootstrap.agent_modules():
        yield p, _bootstrap.modname(p)


def walked():
    """Every file whose module.attr references are checked: the modules plus
    bench/, which imports them but is not imported here."""
    for p, _ in modules():
        yield p
    yield from sorted((AGENT / "bench").glob("*.py"))


def attr_refs(path, ours):
    """(module, attr, lineno) for every `alias.attr` where alias is an imported
    module, and for every `from <our module> import name`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                # `import urllib.request` binds the name `urllib`; the
                # attribute walk then resolves `.request` on the package.
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    aliases[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                sub = f"{node.module}.{a.name}"
                if sub in ours:
                    # `from agent.tools import library` binds a MODULE. Without
                    # this the name is not an alias and every `library.X` below
                    # goes unchecked.
                    aliases[a.asname or a.name] = sub
                elif node.module in ours:
                    yield node.module, a.name, node.lineno
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases):
            yield aliases[node.value.id], node.attr, node.lineno


def main():
    # Config must not be read at import: the fixture goes away for this walk.
    import cglib
    cglib.use_config(None)
    real = cglib.load_config
    cglib.load_config = lambda: (_ for _ in ()).throw(FileNotFoundError("config.json"))
    loaded = {}
    try:
        for p, name in modules():
            loaded[name] = importlib.import_module(name)
    finally:
        cglib.load_config = real
        cglib.use_config(_bootstrap.CONFIG)
    print(f"  import: {len(loaded)} modules, no config.json, hid/serial stubbed")

    bad = []
    for p in walked():
        for mod, attr, line in attr_refs(p, set(loaded)):
            target = loaded.get(mod) or sys.modules.get(mod)
            if target is None or mod in ("hid", "serial"):
                continue                    # third-party or stubbed: not ours to check
            if not hasattr(target, attr):
                bad.append(f"{p.relative_to(K15)}:{line} {mod}.{attr}")
    for b in bad:
        print("FAIL", b)
    assert not bad, f"{len(bad)} unresolved module attribute(s)"
    print("OK - imports: every module loads without config or hardware; every "
          "module.attr and from-import resolves (bench included)")


if __name__ == "__main__":
    main()
