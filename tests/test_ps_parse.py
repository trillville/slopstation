"""Guard: the gaming-PC scripts parse, and the literals Dispatch.ps1 cannot
share (it is dependency-free) stay equal to their one home in
CouchGaming.common.ps1 - marker paths, the nav-collection charset, the
emitter-owned key list, the event timestamp format, the vdf root regex -
plus the Set-Turn-after-guards order and the deploy set.
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PC = ROOT / "gaming-pc"
MEDIA_START = ROOT / "media" / "Start-Media.ps1"
DISPATCH = PC / "Dispatch.ps1"
COMMON = PC / "CouchGaming.common.ps1"
NAV = PC / "Nav-BigPicture.ps1"

# Prints one 'FILE|LINE|MESSAGE' per parse error, then 'PARSED <n>'.
PARSE_PS = r"""
$files = @(Get-ChildItem '{pc}\*.ps1') + @(Get-Item '{media}')
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
    script = PARSE_PS.format(pc=str(PC), media=str(MEDIA_START))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        enc,
    ]
    # 2-3s in CI, 0.8s locally - but a runner stalled powershell's start past
    # the timeout once (2026-08-31), which failed the suite and SKIPPED that
    # commit's deploy. Retry once: a cold-start stall does not repeat, and a
    # real hang still trips twice and fails.
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            break
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "|" in ln]
    parsed = [ln for ln in lines if ln.startswith("PARSED ")]
    for e in errors:
        print("  PARSE ERROR", e)
    assert parsed, f"parser run produced no summary: {r.stderr[:400]}"
    n = int(parsed[0].split()[1])
    assert n >= 11, f"only {n} scripts parsed - path bug, not a real pass"
    assert not errors, f"{len(errors)} parse error(s) in gaming-pc/*.ps1"
    return n


def dispatch_markers(text):
    # $launchMarker = 'C:\...\launch-app'
    return dict(re.findall(r"^\$(launch|nav|stop)Marker\s*=\s*'([^']+)'", text, re.M))


def common_markers(text):
    # StateDir = 'C:\...'   then   $CG.LaunchMarker = Join-Path $CG.StateDir 'launch-app'
    state = re.search(r"StateDir\s*=\s*'([^']+)'", text)
    assert state, "common.ps1: no $CG.StateDir literal"
    leafs = re.findall(
        r"^\$CG\.(\w+)Marker\s*=\s*Join-Path \$CG\.StateDir '([^']+)'", text, re.M
    )
    return {name.lower(): state.group(1) + "\\" + leaf for name, leaf in leafs}


def verb_arms(text):
    """Dispatch's switch arms as (pattern, [code lines]) - from each verb
    pattern line to the next one (or `default`), comment lines dropped."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*'\^[^']+'", ln)]
    end = next(i for i, ln in enumerate(lines) if re.match(r"^\s*default\s", ln))
    arms = []
    for a, b in zip(starts, starts[1:] + [end], strict=True):
        code = [ln for ln in lines[a:b] if not ln.strip().startswith("#")]
        arms.append((lines[a].strip(), code))
    return arms


def name_list(text, var, where):
    """The single-quoted names in a `$var = @( ... )` array literal."""
    m = re.search(r"\$" + var + r"\s*=\s*@\(([^)]*)\)", text, re.S)
    assert m, f"{where}: no ${var} list"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def owned(text, fn):
    body = text[text.index(f"function {fn}") :]
    m = re.search(r"\$owned\s*=\s*@\(([^)]*)\)", body)
    assert m, f"no $owned list in {fn}"
    return re.findall(r"'(\w+)'", m.group(1))


def test_ps_parse():
    if not shutil.which("powershell"):
        print("SKIP - powershell not on PATH")
        return
    dispatch, common, nav = read(DISPATCH), read(COMMON), read(NAV)

    # 1. Every script parses.
    parse_all()

    # 2. Marker paths: Dispatch's literals == common.ps1's $CG values.
    dm, cm = dispatch_markers(dispatch), common_markers(common)
    assert set(dm) == {"launch", "nav", "stop"}, (
        f"Dispatch marker literals: {sorted(dm)}"
    )
    assert {"launch", "nav", "stop"} <= set(cm), f"common.ps1 markers: {sorted(cm)}"
    for k in ("launch", "nav", "stop"):
        assert dm[k] == cm[k], (
            f"{k} marker drift: Dispatch {dm[k]!r} vs common {cm[k]!r}"
        )
    # ready/turn too - Dispatch reads READY and writes the turn to these.
    for var, key in (("ready", "ready"), ("turnFile", "turn")):
        lit = re.search(r"^\$" + var + r"\s*=\s*'([^']+)'", dispatch, re.M).group(1)
        assert lit == cm[key], (
            f"{key} marker drift: Dispatch {lit!r} vs common {cm[key]!r}"
        )

    # 3. Nav-collection charset: the verb pattern and the task's re-validation.
    d = re.search(r"\^nav collection \((\[[^\]]+\]\{\d+,\d+\})\)", dispatch)
    t = re.search(r"'collection'\s*\{[^\n]*-match\s+'\^(\[[^\]]+\]\{\d+,\d+\})\$'", nav)
    assert d and t, f"charset not found: dispatch={bool(d)} nav={bool(t)}"
    assert d.group(1) == t.group(1), (
        f"collection charset drift: {d.group(1)} vs {t.group(1)}"
    )

    # 4. Set-Turn after the guards, before the task start: every early
    # `break` (a refusal) precedes it, and Start-CgTask follows it.
    arms = verb_arms(dispatch)
    assert len(arms) == 13, f"expected 13 verb arms, got {len(arms)}"
    ordered = 0
    for pat, code in arms:
        st = next(
            (i for i, ln in enumerate(code) if re.search(r"\bSet-Turn\b", ln)), None
        )
        sc = next(
            (i for i, ln in enumerate(code) if re.search(r"\bStart-CgTask\b", ln)), None
        )
        if st is None or sc is None:
            continue
        assert st < sc, f"Set-Turn after Start-CgTask in arm {pat}"
        late = [
            i
            for i, ln in enumerate(code[:-1])
            if re.search(r"\bbreak\b", ln) and i > st
        ]
        assert not late, (
            f"a refusal (break) after Set-Turn in arm {pat}: a refused verb would stamp the turn"
        )
        ordered += 1
    # enter, exit, launch, nav x3, stop
    assert ordered == 7, f"expected 7 turn-bearing task arms, got {ordered}"

    # 5. Emitter-owned keys: Dispatch's Write-Event == common's Write-CgEvent.
    a, b = owned(dispatch, "Write-Event"), owned(common, "Write-CgEvent")
    assert a == b, f"owned-key drift: Dispatch {a} vs common {b}"

    # 6. The ts both emitters write is what the collector's timestamp parser reads and
    #    events.emit produces; the vdf root regex is one text in both resolvers.
    ts = [
        re.search(
            r"ToUniversalTime\(\)\.ToString\('([^']+)'\)",
            text[text.index(f"function {fn}") :],
        ).group(1)
        for text, fn in ((dispatch, "Write-Event"), (common, "Write-CgEvent"))
    ]
    assert ts[0] == ts[1] == "yyyy-MM-ddTHH:mm:ss.fffZ", f"ts format drift: {ts}"
    vdf = [
        re.search(r"""-match '([^']*"path"[^']*)'""", t).group(1)
        for t in (dispatch, common)
    ]
    assert vdf[0] == vdf[1], f"vdf root regex drift: {vdf}"

    # 7. The deploy set covers every script. Deploy.ps1 aborts on a LISTED
    #    file that is missing, but silently ignores one that is not listed, so
    #    a new script would deploy green and simply be absent on the PC. It
    #    does not copy itself. Doctor.ps1 keeps its own list and must check
    #    everything that ships.
    on_disk = {p.name for p in PC.glob("*.ps1")} - {"Deploy.ps1"}
    shipped = name_list(read(PC / "Deploy.ps1"), "scripts", "Deploy.ps1")
    assert shipped == on_disk, (
        f"Deploy.ps1 ships {len(shipped)} of {len(on_disk)} scripts: "
        f"never copied {sorted(on_disk - shipped)}, "
        f"listed but absent {sorted(shipped - on_disk)}"
    )
    checked = name_list(read(PC / "Doctor.ps1"), "files", "Doctor.ps1")
    assert on_disk <= checked, (
        f"Doctor.ps1 $files does not check {sorted(on_disk - checked)}"
    )
