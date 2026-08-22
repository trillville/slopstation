"""Blind test: the account session's LOGIC with the HTTP seams mocked - token
mint/cache/refresh, session pick, the empty-200 -> GetClientAppList VERIFY
path, an X-eresult failure surfaced, download-status parse, and enrollment that
persists the token WITHOUT ever logging it. No network. Run:
    .venv\\Scripts\\python tests\\test_steam_session.py
"""
import _bootstrap  # noqa: F401
import base64
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from _bootstrap import freeze_sleep

import cglib
import steam_session as ss


def make_jwt(exp):
    """A JWT whose only readable claim is exp - all jwt_exp needs."""
    p = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


def main():
    log = cglib.CapturingLog("steam")
    refresh = make_jwt(time.time() + 200 * 86400)          # ~200-day refresh token
    s = ss.SteamSession({"steamId64": "76561190000", "steamRefreshToken": refresh}, log)

    # --- gating + token expiry ----------------------------------------------
    assert s.available()
    assert ss.SteamSession({"steamId64": "76561190000",
                            "steamRefreshToken": "..."}, log).available() is False
    assert ss.SteamSession({"steamId64": "", "steamRefreshToken": refresh},
                           log).available() is False
    assert s.token_expiry() > time.time() + 190 * 86400

    # --- seams: route by method; state the tests can turn ---------------------
    minted = [make_jwt(time.time() + 24 * 3600), make_jwt(time.time() + 25 * 3600)]
    state = {"sessions": [], "apps": [], "install_eresult": "1"}
    posts, gets, logins = [], [], []

    def fake_login_post(url, fields, headers=None):
        """The mint, as it really works: finalizelogin hands back transfer
        hosts, and each host answers with the steamLoginSecure cookie that IS
        the access token, shaped '<steamid>||<token>'."""
        logins.append((url, dict(fields)))
        if "finalizelogin" in url:
            return {"transfer_info": [
                {"url": "https://store.steampowered.com/login/settoken",
                 "params": {"nonce": "n1", "auth": "a1"}}]}, {}
        return None, {"steamLoginSecure": quote(f"76561190000||{minted.pop(0)}")}

    def fake_post(method, data, timeout=20):
        posts.append((method, dict(data)))
        if "InstallClientApp" in method:
            return {}, state["install_eresult"]        # the empty 200
        if "BeginAuthSessionViaQR" in method:
            return {"response": {"client_id": "c1", "request_id": "r1",
                                 "challenge_url": "steam://qr", "interval": 0.01}}, "1"
        if "PollAuthSessionStatus" in method:
            return {"response": {"refresh_token": "REFRESH_TOKEN_SECRET_VALUE_123"}}, "1"
        return None, None

    def fake_get(method, params, timeout=20):
        gets.append((method, dict(params)))
        if "GetAllClientLogonInfo" in method:
            return {"response": {"sessions": state["sessions"]}}, "1"
        if "GetClientAppList" in method:
            apps = state["apps"]
            # Honour filters=changing like the real service: it is what the
            # install VERIFY leans on, and only that call carries the progress
            # fields at all (see app_list's shape note).
            if params.get("filters") == "changing":
                apps = [a for a in apps if a.get("changing")]
            return {"response": {"apps": apps}}, "1"
        return None, None

    s._post = fake_post
    s._get = fake_get
    s._login_post = fake_login_post

    # --- token mint + cache + re-mint on expiry ------------------------------
    # The mint is the TRANSFER-LOGIN flow, not GenerateAccessTokenForApp: that
    # endpoint is gated to mobile-audience tokens and answers our web-audience
    # one with eresult 15 AccessDenied (measured 2026-08-14).
    t1 = s.access_token()
    s.access_token()                                        # cached, no 2nd mint
    def n_mints(): return sum("finalizelogin" in u for u, _ in logins)
    assert n_mints() == 1, logins
    assert s._refresh == logins[0][1]["nonce"], "the refresh token rides as nonce"
    s._access_exp = time.time()                             # force expiry
    t2 = s.access_token()
    assert t2 != t1 and n_mints() == 2, logins
    assert "GenerateAccessTokenForApp" not in json.dumps(posts), \
        "the gated endpoint must never be called again"

    # --- sessions + target pick by machine name ------------------------------
    state["sessions"] = [
        {"client_instanceid": "999", "machine_name": "LAPTOP", "os_name": "Windows"},
        {"client_instanceid": "111", "machine_name": "TILLMAN-DESKTOP", "os_name": "Windows"}]
    assert [x["instanceid"] for x in s.sessions()] == ["999", "111"]
    assert s._target("TILLMAN-DESKTOP")["instanceid"] == "111"   # matched, not first
    assert s._target()["instanceid"] == "999"                    # no name -> first
    # A CONFIGURED machine name (self.machine) pins the target so install/status
    # can't land on another signed-in box Steam happens to list first.
    s_pinned = ss.SteamSession({"steamId64": "76561190000", "steamRefreshToken": refresh},
                               log, machine_name="TILLMAN-DESKTOP")
    s_pinned._get, s_pinned._post = fake_get, fake_post
    s_pinned._access, s_pinned._access_exp = "tok", time.time() + 3600   # skip minting
    assert s_pinned._target()["instanceid"] == "111"             # self.machine wins over first

    # --- 401 re-mint: a mid-life revocation 401s a not-yet-expired token; the
    # real _get retries once with a fresh token (mocks replace _get, so this
    # drives _session directly). ---------------------------------------------
    class FakeResp:
        def __init__(self, code): self.status_code = code; self.headers = {"X-eresult": "1"}
        def json(self): return {"response": {"sessions": []}}
    hits = {"n": 0}
    class FakeSess:
        headers = {}
        def get(self, url, params=None, timeout=20):
            hits["n"] += 1
            return FakeResp(401 if hits["n"] == 1 else 200)   # 401 once, then OK
        def post(self, url, data=None, timeout=20):           # the re-mint POST
            return FakeResp(200)
    s401 = ss.SteamSession({"steamId64": "76561190000", "steamRefreshToken": refresh}, log)
    s401._sess = FakeSess()
    s401._access, s401._access_exp = "stale", time.time() + 3600   # cached, unexpired
    s401.access_token = lambda: "fresh"                            # re-mint returns a new token
    body, _ = s401._get("IClientCommService/GetAllClientLogonInfo/v1",
                       {"access_token": "stale"})
    assert hits["n"] == 2, "expected one 401 then a retry"        # retried exactly once
    assert body is not None

    # --- install: verified via GetClientAppList, right instanceid on the wire -
    state["apps"] = [{"appid": 570, "app": "Dota", "changing": True,
                      "bytes_to_download": "100", "bytes_downloaded": "10"}]
    r = s.install(570, machine_name="TILLMAN-DESKTOP")
    assert r["ok"] and r["verified"] is True, r
    inst = [d for m, d in posts if "InstallClientApp" in m][-1]
    assert inst["appid"] == 570 and inst["client_instanceid"] == "111", inst
    assert "install_queued" in log.events()

    # --- install that never showed up as changing -> ok but NOT verified -----
    # The empty 200 is not proof, so "it did not appear in the changing list"
    # is reported as verified=False rather than claimed as success.
    state["apps"] = [{"appid": 570, "app": "Dota", "installed": True,
                      "changing": False, "bytes_to_download": "0", "bytes_downloaded": "0"}]
    r = s.install(570)
    assert r["ok"] and r["verified"] is False, r

    # --- install refused: an X-eresult != 1 is surfaced, not swallowed -------
    state["install_eresult"] = "15"                         # e.g. AccessDenied
    r = s.install(570)
    assert not r["ok"] and "15" in r["error"], r
    state["install_eresult"] = "1"

    # --- install with the PC offline: honest 'asleep', no InstallClientApp ---
    state["sessions"] = []
    before = sum("InstallClientApp" in m for m, _ in posts)
    r = s.install(570)
    assert not r["ok"] and "online" in r["error"], r
    assert sum("InstallClientApp" in m for m, _ in posts) == before   # never called

    # --- download_status: only changing apps, most complete first ------------
    state["sessions"] = [{"client_instanceid": "111", "machine_name": "pc"}]
    state["apps"] = [
        {"appid": 570, "app": "Dota", "changing": True,
         "bytes_to_download": "100", "bytes_downloaded": "50", "download_paused": False},
        {"appid": 20, "app": "Nearly", "changing": True,
         "bytes_to_download": "100", "bytes_downloaded": "90", "download_paused": False},
        {"appid": 730, "app": "Idle", "changing": False,
         "bytes_to_download": "0", "bytes_downloaded": "0"}]
    ds = s.download_status()
    assert [d["appid"] for d in ds] == [20, 570], ds        # 90% before 50%; idle dropped
    assert ds[0] == {"appid": 20, "name": "Nearly", "percent": 90,
                     "paused": False, "queue": None}, ds[0]

    # --- enroll: persists the refresh token, and NEVER logs it ---------------
    tmp = Path(tempfile.mkdtemp()) / "secrets.json"
    tmp.write_text(json.dumps({"deepgramApiKey": "keep-me", "steamId64": "76561190000"}))
    cglib.SECRETS = tmp
    ss._print_qr = lambda text: None                        # no console spam
    log2 = cglib.CapturingLog("steam")
    s2 = ss.SteamSession({"steamId64": "76561190000", "steamRefreshToken": None}, log2)
    s2._post = fake_post
    assert s2.enroll() == 0
    saved = json.loads(tmp.read_text())
    assert saved["steamRefreshToken"] == "REFRESH_TOKEN_SECRET_VALUE_123"
    assert saved["deepgramApiKey"] == "keep-me"             # other secrets preserved
    # The credential must never reach the log - not the event, not any field.
    blob = json.dumps([r for r in log2.records], default=str)
    assert "REFRESH_TOKEN_SECRET_VALUE_123" not in blob, "enroll leaked the token to the log"
    assert "enrolled" in log2.events()

    # --- enroll REFUSES to clobber a present-but-corrupt secrets.json --------
    # (writing {} over it would destroy every other secret to save one token)
    tmp.write_text('{"deepgramApiKey": "keep-me",')     # invalid JSON on disk
    s3 = ss.SteamSession({"steamId64": "76561190000", "steamRefreshToken": None}, log2)
    s3._post = fake_post
    try:
        s3.enroll()
        raise AssertionError("enroll wrote over a corrupt secrets.json")
    except SystemExit:
        pass
    assert "keep-me" in tmp.read_text()                 # untouched, recoverable

    print("OK - steam_session: gate, token mint/cache/refresh, session pick, "
          "install verify + X-eresult + offline, download parse, enroll persists "
          "the token without logging it and refuses a corrupt secrets.json")


if __name__ == "__main__":
    with freeze_sleep():
        main()
