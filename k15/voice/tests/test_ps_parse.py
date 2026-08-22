"""Guard: the gaming-PC scripts parse, and the literals Dispatch.ps1 cannot
share (it is dependency-free) stay equal to their one home in
CouchGaming.common.ps1 - marker paths, the nav-collection charset, the
emitter-owned key list, the event timestamp format, the vdf root regex -
plus the Set-Turn-after-guards order. Stdlib only, so it runs on system
python as well as in the venv. Run:
    .venv\\Scripts\\python tests\\test_ps_parse.py
"""
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

PC = Path(__file__).resolve().parents[3] / "gaming-pc"
DISPATCH = PC / "Dispatch.ps1"
COMMON = PC / "CouchGaming.common.ps1"
NAV = PC / "Nav-BigPicture.ps1"

# Prints one 'FILE|LINE|MESSAGE' per parse error, then 'PARSED <n>'.
PARSE_PS = r"""
$files = Get-ChildItem '{pc}\*.ps1'
foreach ($f in $files) {{
  $errs = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs) | Out-Null
  foreach ($e in $errs) {{ '{{0}}|{{1}}|{{2}}' -f $f.Name, $e.Extent.StartLineNumber, $e.Message }}
}}
'PARSED ' + $files.Count
"""


def read(p):
    return p.read_text(encoding="utf-8")


def parse_all():
    script = PARSE_PS.format(pc=str(PC))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc],
                       capture_output=True, text=True, timeout=120)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "|" in ln]
    parsed = [ln for ln in lines if ln.startswith("PARSED ")]
    for e in errors:
        print("  PARSE ERROR", e)
    assert parsed, f"parser run produced no summary: {r.stderr[:400]}"
    n = int(parsed[0].split()[1])
    assert n >= 10, f"only {n} scripts parsed - path bug, not a real pass"
    assert not errors, f"{len(errors)} parse error(s) in gaming-pc/*.ps1"
    return n


def dispatch_markers(text):
    # $launchMarker = 'C:\...\launch-app'
    return dict(re.findall(r"^\$(launch|nav|stop)Marker\s*=\s*'([^']+)'", text, re.M))


def common_markers(text):
    # StateDir = 'C:\...'   then   $CG.LaunchMarker = Join-Path $CG.StateDir 'launch-app'
    state = re.search(r"StateDir\s*=\s*'([^']+)'", text)
    assert state, "common.ps1: no $CG.StateDir literal"
    leafs = re.findall(r"^\$CG\.(\w+)Marker\s*=\s*Join-Path \$CG\.StateDir '([^']+)'", text, re.M)
    return {name.lower(): state.group(1) + "\\" + leaf for name, leaf in leafs}


def verb_arms(text):
    """Dispatch's switch arms as (pattern, [code lines]) - from each verb
    pattern line to the next one (or `default`), comment lines dropped."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*'\^[^']+'", ln)]
    end = next(i for i, ln in enumerate(lines) if re.match(r"^\s*default\s", ln))
    arms = []
    for a, b in zip(starts, starts[1:] + [end]):
        code = [ln for ln in lines[a:b] if not ln.strip().startswith("#")]
        arms.append((lines[a].strip(), code))
    return arms


def owned(text, fn):
    body = text[text.index(f"function {fn}"):]
    m = re.search(r"\$owned\s*=\s*@\(([^)]*)\)", body)
    assert m, f"no $owned list in {fn}"
    return re.findall(r"'(\w+)'", m.group(1))


def main():
    if not shutil.which("powershell"):
        print("SKIP - powershell not on PATH")
        return
    dispatch, common, nav = read(DISPATCH), read(COMMON), read(NAV)

    # 1. Every script parses.
    n = parse_all()
    print(f"  parse: {n} gaming-pc scripts, no errors")

    # 2. Marker paths: Dispatch's literals == common.ps1's $CG values.
    dm, cm = dispatch_markers(dispatch), common_markers(common)
    assert set(dm) == {"launch", "nav", "stop"}, f"Dispatch marker literals: {sorted(dm)}"
    assert {"launch", "nav", "stop"} <= set(cm), f"common.ps1 markers: {sorted(cm)}"
    for k in ("launch", "nav", "stop"):
        assert dm[k] == cm[k], f"{k} marker drift: Dispatch {dm[k]!r} vs common {cm[k]!r}"
    # ready/turn too - Dispatch reads READY and writes the turn to these.
    for var, key in (("ready", "ready"), ("turnFile", "turn")):
        lit = re.search(r"^\$" + var + r"\s*=\s*'([^']+)'", dispatch, re.M).group(1)
        assert lit == cm[key], f"{key} marker drift: Dispatch {lit!r} vs common {cm[key]!r}"
    print(f"  markers: launch/nav/stop/ready/turn paths equal across the two files")

    # 3. Nav-collection charset: the verb pattern and the task's re-validation.
    d = re.search(r"\^nav collection \((\[[^\]]+\]\{\d+,\d+\})\)", dispatch)
    t = re.search(r"'collection'\s*\{[^\n]*-match\s+'\^(\[[^\]]+\]\{\d+,\d+\})\$'", nav)
    assert d and t, f"charset not found: dispatch={bool(d)} nav={bool(t)}"
    assert d.group(1) == t.group(1), f"collection charset drift: {d.group(1)} vs {t.group(1)}"
    print(f"  nav collection charset: {d.group(1)} in both")

    # 4. Set-Turn after the guards, before the task start: every early
    # `break` (a refusal) precedes it, and Start-CgTask follows it.
    arms = verb_arms(dispatch)
    assert len(arms) == 13, f"expected 13 verb arms, got {len(arms)}"
    ordered = 0
    for pat, code in arms:
        st = next((i for i, ln in enumerate(code) if re.search(r"\bSet-Turn\b", ln)), None)
        sc = next((i for i, ln in enumerate(code) if re.search(r"\bStart-CgTask\b", ln)), None)
        if st is None or sc is None:
            continue
        assert st < sc, f"Set-Turn after Start-CgTask in arm {pat}"
        late = [i for i, ln in enumerate(code[:-1]) if re.search(r"\bbreak\b", ln) and i > st]
        assert not late, f"a refusal (break) after Set-Turn in arm {pat}: a refused verb would stamp the turn"
        ordered += 1
    # enter, exit, launch, nav x3, stop
    assert ordered == 7, f"expected 7 turn-bearing task arms, got {ordered}"
    print(f"  order: guards, then Set-Turn, then Start-CgTask in all {ordered} mutating arms")

    # 5. Emitter-owned keys: Dispatch's Write-Event == common's Write-CgEvent.
    a, b = owned(dispatch, "Write-Event"), owned(common, "Write-CgEvent")
    assert a == b, f"owned-key drift: Dispatch {a} vs common {b}"
    print(f"  owned keys: {a}")

    # 6. The ts both emitters write is what Alloy's timestamp stage parses and
    #    events.emit produces; the vdf root regex is one text in both resolvers.
    ts = [re.search(r"ToUniversalTime\(\)\.ToString\('([^']+)'\)",
                    text[text.index(f"function {fn}"):]).group(1)
          for text, fn in ((dispatch, "Write-Event"), (common, "Write-CgEvent"))]
    assert ts[0] == ts[1] == "yyyy-MM-ddTHH:mm:ss.fffZ", f"ts format drift: {ts}"
    vdf = [re.search(r"""-match '([^']*"path"[^']*)'""", t).group(1)
           for t in (dispatch, common)]
    assert vdf[0] == vdf[1], f"vdf root regex drift: {vdf}"
    print(f"  ts format {ts[0]!r} and vdf regex {vdf[0]!r} in both")

    print("OK - ps parse: every script parses; markers, charset, turn order, owned keys, "
          "ts format, vdf regex agree")


if __name__ == "__main__":
    main()
