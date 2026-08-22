"""Blind test: doctor's voice checks run end to end and keep their WARN-only
posture. check_voice is twelve concerns in a row and had no test: this
drives it against a config with and without a voice section, counting
reports, and proves it never FAILs (voice is an overlay - a broken voice
lane must not redden the chain doctor) and never raises. Filesystem and
process-list reads only; the one subprocess it can spawn (the Steam mint
probe) needs a refresh token, which is forced absent here. Run:
    .venv\\Scripts\\python tests\\test_doctor.py
"""
import _bootstrap  # noqa: F401
import io
import json
from contextlib import redirect_stdout

import cglib
import doctor


def run(cfg):
    before = dict(doctor._counts)
    out = io.StringIO()
    with redirect_stdout(out):
        doctor.check_voice(cfg)
    delta = {k: doctor._counts[k] - before[k] for k in before}
    return delta, out.getvalue()


def main():
    cglib.load_secrets = lambda: {}                 # no token: no mint probe, no network

    # No voice section: one WARN, nothing else, no exception.
    delta, out = run({"tvComPort": "COMX"})
    assert delta == {"PASS": 0, "WARN": 1, "FAIL": 0}, (delta, out)
    assert "no voice section" in out

    # The example config (every key present): every check runs, reports
    # something, and nothing is a FAIL - the posture the docstring promises.
    cfg = json.loads((cglib.BASE / "config.example.json").read_text(encoding="utf-8-sig"))
    delta, out = run(cfg)
    assert delta["FAIL"] == 0, out
    # The unconditional checks report on any checkout; the others (deals,
    # jobs, the token) are silent when their files are absent, by design.
    for name in ("voice keys", "voice venv", "voice library", "worker CLI", "voice agent"):
        assert name in out, f"{name!r} never reported:\n{out}"
    assert delta["PASS"] + delta["WARN"] >= 5, (delta, out)

    # A missing required voice key is said, as a WARN, and the rest still run.
    cfg2 = {**cfg, "voice": {k: v for k, v in cfg["voice"].items() if k != "wakeModel"}}
    delta, out = run(cfg2)
    assert delta["FAIL"] == 0 and "missing voice.wakeModel" in out, out
    print("OK - doctor: check_voice runs end to end, WARN-only, never raises")


if __name__ == "__main__":
    main()
