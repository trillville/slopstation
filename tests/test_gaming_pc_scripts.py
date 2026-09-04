"""Parse gaming-PC scripts and verify their shared constants stay aligned."""

import base64
import re
import shutil
import subprocess

import pytest

import helpers

ROOT = helpers.REPO
PC = ROOT / "gaming-pc"
MEDIA_START = ROOT / "media" / "Start-Media.ps1"
DISPATCH = PC / "Dispatch.ps1"
COMMON = PC / "CouchGaming.common.ps1"
NAV = PC / "Nav-BigPicture.ps1"
DEPLOY = PC / "Deploy.ps1"
CONFIG_EXAMPLE = PC / "config.example.psd1"
# Run from a checkout, never shipped: like Deploy.ps1.
LOCAL_ONLY = {"Deploy.ps1", "Install.ps1"}

# Prints one 'FILE|LINE|MESSAGE' per parse error, then 'PARSED <n>'.
PARSE_PS = r"""
$files = @(Get-ChildItem '{pc}\*.ps1') + @(Get-Item '{media}') + @(Get-ChildItem '{root}\*.ps1')
foreach ($f in $files) {{
  $errs = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs) | Out-Null
  foreach ($e in $errs) {{ '{{0}}|{{1}}|{{2}}' -f $f.Name, $e.Extent.StartLineNumber, $e.Message }}
}}
'PARSED ' + $files.Count
"""


def read(p):
    return p.read_text(encoding="utf-8")


def powershell(script, timeout=120):
    """Run a snippet in Windows PowerShell; stdout lines, stripped."""
    if not shutil.which("powershell"):
        pytest.skip("powershell not on PATH")
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        enc,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def config_keys(common):
    """The keys common.ps1 requires of config.psd1, name -> PowerShell type."""
    m = re.search(r"\$script:CgConfigKeys\s*=\s*@\{([^}]*)\}", common)
    assert m, "common.ps1: no $script:CgConfigKeys table"
    return dict(re.findall(r"(\w+)\s*=\s*\[(\w+)\]", m.group(1)))


def task_table(common):
    """$CG.Tasks rows as dicts of their single-quoted fields."""
    m = re.search(r"\$CG\.Tasks\s*=\s*@\((.*?)\n\)", common, re.S)
    assert m, "common.ps1: no $CG.Tasks table"
    rows = re.findall(r"@\{([^}]*)\}", m.group(1))
    return [dict(re.findall(r"(\w+)\s*=\s*'([^']*)'", r)) for r in rows]


def parse_all():
    script = PARSE_PS.format(pc=str(PC), media=str(MEDIA_START), root=str(ROOT))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        enc,
    ]
    # Retry once: a cold-start stall does not repeat; a real hang trips twice.
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
    assert parsed, f"parser run produced no summary: {r.stderr[:400]}"
    n = int(parsed[0].split()[1])
    assert n >= 11, f"only {n} scripts parsed - path bug, not a real pass"
    assert not errors, (
        f"{len(errors)} parse error(s) in gaming-pc/*.ps1:\n" + "\n".join(errors)
    )
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


def test_gaming_pc_scripts_parse():
    if not shutil.which("powershell"):
        pytest.skip("powershell not on PATH")
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
    # test_turn owns the arm counts.
    for pat, code in verb_arms(dispatch):
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
    on_disk = {p.name for p in PC.glob("*.ps1")} - LOCAL_ONLY
    on_disk.add(CONFIG_EXAMPLE.name)
    shipped = name_list(read(DEPLOY), "scripts", "Deploy.ps1")
    assert shipped == on_disk, (
        f"Deploy.ps1 ships {len(shipped)} of {len(on_disk)} files: "
        f"never copied {sorted(on_disk - shipped)}, "
        f"listed but absent {sorted(shipped - on_disk)}"
    )
    assert "config.psd1" not in shipped, "the live config is never deployed over"
    checked = name_list(read(PC / "Doctor.ps1"), "files", "Doctor.ps1")
    assert on_disk <= checked, (
        f"Doctor.ps1 $files does not check {sorted(on_disk - checked)}"
    )


def test_config_example_is_what_common_requires():
    """Every key common.ps1 validates is in the example, with the right type,
    and nothing else is: the example is the whole per-installation surface."""
    keys = config_keys(read(COMMON))
    assert keys == {
        "PuckName": "string",
        "PuckHwId": "string",
        "TvEdid": "string",
        "TvHeight": "int",
    }
    # The same read as common.ps1's Import-CgConfig.
    parse = (
        "$e = $null; $ast = [System.Management.Automation.Language.Parser]"
        f"::ParseFile('{CONFIG_EXAMPLE}', [ref]$null, [ref]$e); "
        "$c = $ast.Find({ $args[0] -is "
        "[System.Management.Automation.Language.HashtableAst] }, $false)"
        ".SafeGetValue(); "
        "foreach ($k in $c.Keys) { '{0}={1}' -f $k, $c[$k].GetType().Name }"
    )
    got = dict(ln.split("=", 1) for ln in powershell(parse))
    want = {k: {"string": "String", "int": "Int32"}[t] for k, t in keys.items()}
    assert got == want


def test_common_loads_config_beside_it(tmp_path):
    """The library reads config.psd1 from its own directory into $CG. A
    missing, mistyped, blank or still-placeholder file stops the dot-source
    with a message naming the file or the key."""
    shutil.copy(COMMON, tmp_path / "CouchGaming.common.ps1")
    lib = tmp_path / "CouchGaming.common.ps1"
    config = tmp_path / "config.psd1"
    load = (
        f"try {{ . '{lib}'; 'LOADED ' + $CG.TvHeight + ' ' + $CG.TvEdid + ' ' + "
        "(($CG.Tasks | ForEach-Object { $_.Name }) -join ',') } "
        "catch { 'STOPPED ' + $_.Exception.Message }"
    )
    config.write_text(
        "@{ PuckName = 'p'; PuckHwId = 'VID_1&PID_2'; TvEdid = 'TVNAME'; TvHeight = 2160 }"
    )
    out = powershell(load)
    assert out == [
        "LOADED 2160 TVNAME Enter,Exit,ForceOfficeAtLogon,WakeSafety,LaunchGame,Nav,StopGame"
    ], out

    config.unlink()
    out = powershell(load)
    assert out[0].startswith("STOPPED") and "config.psd1" in out[0], out

    stops = {
        "TvHeight": "@{ PuckName = 'p'; PuckHwId = 'h'; TvEdid = 'e'; TvHeight = '2160' }",
        "PuckName": "@{ PuckName = ''; PuckHwId = 'h'; TvEdid = 'e'; TvHeight = 2160 }",
    }
    # The example as shipped is not a valid live file: its placeholder stops.
    stops["TvEdid"] = CONFIG_EXAMPLE.read_text()
    assert "<" in stops["TvEdid"], "config.example.psd1 has no placeholder"
    for key, text in stops.items():
        config.write_text(text)
        out = powershell(load)
        assert out[0].startswith("STOPPED") and key in out[0], (key, out)


def test_deploy_ships_the_example_and_keeps_the_live_config(tmp_path):
    """Deploy.ps1 into an empty directory carries config.example.psd1 and
    leaves a config.psd1 already there untouched (CI's checkout cannot
    supply one, so the live file must survive every deploy)."""
    if not shutil.which("powershell"):
        pytest.skip("powershell not on PATH")
    dest = tmp_path / "CouchGaming"
    dest.mkdir()
    live = "@{ Sentinel = 1 }"
    (dest / "config.psd1").write_text(live)
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(DEPLOY),
            "-Dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (dest / "config.psd1").read_text() == live
    assert (dest / "config.example.psd1").exists()
    assert (dest / "CouchGaming.common.ps1").exists()
    assert (dest / "build-id").exists()


def test_task_table_is_the_task_contract():
    """$CG.Tasks names every task another script starts, stops or waits on,
    every script it names ships, and its task path is the literal Dispatch.ps1
    and Deploy.ps1 use (neither loads the library)."""
    common = read(COMMON)
    rows = task_table(common)
    names = {r["Name"] for r in rows}
    assert len(rows) == 7 and len(names) == 7, rows
    shipped = name_list(read(DEPLOY), "scripts", "Deploy.ps1")
    assert {r["Script"] for r in rows} <= shipped
    assert {r["Trigger"] for r in rows} <= {"none", "logon", "wake"}

    # Every row carries its execution limit; only logon rows carry a delay.
    # The values reproduce the tasks as they were registered on the live PC.
    duration = re.compile(r"^PT\d+[HMS]$")
    for r in rows:
        assert duration.match(r.get("TimeLimit", "")), r
        assert ("Delay" in r) == (r["Trigger"] == "logon"), r
        if "Delay" in r:
            assert duration.match(r["Delay"]), r
    by_name = {r["Name"]: r for r in rows}
    assert by_name["ForceOfficeAtLogon"]["Delay"] == "PT20S"
    assert by_name["ForceOfficeAtLogon"]["TimeLimit"] == "PT72H"
    assert {r["TimeLimit"] for r in rows if r["Name"] != "ForceOfficeAtLogon"} == {
        "PT5M"
    }
    # Install.ps1 and Doctor.ps1 read both fields from the table rather than
    # carrying a constant of their own.
    install, doctor = read(PC / "Install.ps1"), read(PC / "Doctor.ps1")
    for text, where in ((install, "Install.ps1"), (doctor, "Doctor.ps1")):
        assert "$t.TimeLimit" in text and "$t.Delay" in text, where
    assert "New-TimeSpan" not in install, "Install.ps1 hard-codes a time limit"

    used = set()
    for p in PC.glob("*.ps1"):
        used |= set(
            re.findall(
                r"(?:Start-CgTask|Stop-CgTask|Test-CgTaskRunning) '(\w+)'", read(p)
            )
        )
    assert used <= names, f"tasks used but not in the table: {sorted(used - names)}"

    path = re.search(r"^\$CG\.TaskPath\s*=\s*'([^']+)'", common, re.M).group(1)
    assert f'"{path}$Name"' in read(DISPATCH), "Dispatch.ps1 task path drifted"
    assert f"-TaskPath '{path}'" in read(DEPLOY), "Deploy.ps1 task path drifted"
