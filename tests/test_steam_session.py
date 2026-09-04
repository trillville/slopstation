"""Test Steam account sessions with mocked HTTP requests."""

import base64
import json
import time
import types
from urllib.parse import quote

import pytest

from helpers import CapturingLog
from slopstation import paths
from slopstation.agent.tools import steam_session as ss

STEAMID = "76561190000"
TOKEN_SECRET = "REFRESH_TOKEN_SECRET_VALUE_123"


def make_jwt(exp):
    """A JWT whose only readable claim is exp - all _jwt_exp needs."""
    p = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


def two_clients():
    """What GetAllClientLogonInfo lists: a laptop first, the rig second."""
    return [
        {"client_instanceid": "999", "machine_name": "LAPTOP", "os_name": "Windows"},
        {
            "client_instanceid": "111",
            "machine_name": "GAMING-PC",
            "os_name": "Windows",
        },
    ]


@pytest.fixture
def log():
    return CapturingLog("steam")


@pytest.fixture
def refresh():
    return make_jwt(time.time() + 200 * 86400)  # ~200-day refresh token


@pytest.fixture
def seams(monkeypatch):
    """The HTTP seams, routed by method, and the state a test turns: what the
    client list and the app list answer, and InstallClientApp's X-eresult.
    Every request lands on posts/gets/logins. Steam's pacing sleeps are
    skipped."""
    minted = [make_jwt(time.time() + 24 * 3600), make_jwt(time.time() + 25 * 3600)]
    state = {"sessions": [], "apps": [], "install_eresult": "1"}
    posts, gets, logins = [], [], []

    def fake_login_post(url, fields, headers=None):
        """finalizelogin hands back transfer hosts, and each host answers with
        the steamLoginSecure cookie that IS the access token, shaped
        '<steamid>||<token>'."""
        logins.append((url, dict(fields)))
        if "finalizelogin" in url:
            return {
                "transfer_info": [
                    {
                        "url": "https://store.steampowered.com/login/settoken",
                        "params": {"nonce": "n1", "auth": "a1"},
                    }
                ]
            }, {}
        return None, {"steamLoginSecure": quote(f"{STEAMID}||{minted.pop(0)}")}

    def fake_post(method, data, timeout=20):
        posts.append((method, dict(data)))
        if "InstallClientApp" in method:
            return {}, state["install_eresult"]  # the empty 200
        if "BeginAuthSessionViaQR" in method:
            return {
                "response": {
                    "client_id": "c1",
                    "request_id": "r1",
                    "challenge_url": "steam://qr",
                    "interval": 0.01,
                }
            }, "1"
        if "PollAuthSessionStatus" in method:
            return {"response": {"refresh_token": TOKEN_SECRET}}, "1"
        return None, None

    def fake_get(method, params, timeout=20):
        gets.append((method, dict(params)))
        if "GetAllClientLogonInfo" in method:
            return {"response": {"sessions": state["sessions"]}}, "1"
        if "GetClientAppList" in method:
            apps = state["apps"]
            # Honour filters=changing like the real service: only that call
            # carries the progress fields, and the install verify leans on it.
            if params.get("filters") == "changing":
                apps = [a for a in apps if a.get("changing")]
            return {"response": {"apps": apps}}, "1"
        return None, None

    monkeypatch.setattr(time, "sleep", lambda n: None)
    return types.SimpleNamespace(
        state=state,
        posts=posts,
        gets=gets,
        logins=logins,
        post=fake_post,
        get=fake_get,
        login_post=fake_login_post,
    )


@pytest.fixture
def make_session(seams, log, refresh, monkeypatch):
    """A session on the seams. `token` is the stored refresh token;
    `machine_name` pins the target."""

    def make(token=refresh, machine_name=None):
        s = ss.SteamSession(
            {"steamId64": STEAMID, "steamRefreshToken": token},
            log,
            machine_name=machine_name,
        )
        monkeypatch.setattr(s, "_post", seams.post)
        monkeypatch.setattr(s, "_get", seams.get)
        monkeypatch.setattr(s, "_login_post", seams.login_post)
        return s

    return make


@pytest.fixture
def session(make_session):
    """No machine name pinned."""
    return make_session()


@pytest.fixture
def pinned(make_session, monkeypatch):
    """Pinned to the rig by machine name, its token pre-minted."""
    s = make_session(machine_name="GAMING-PC")
    monkeypatch.setattr(s, "_access", "tok")  # skip minting
    monkeypatch.setattr(s, "_access_exp", time.time() + 3600)
    return s


def test_gating_and_token_expiry(log, refresh):
    s = ss.SteamSession({"steamId64": STEAMID, "steamRefreshToken": refresh}, log)
    assert s.available()
    assert (
        ss.SteamSession(
            {"steamId64": STEAMID, "steamRefreshToken": "..."}, log
        ).available()
        is False
    )
    assert (
        ss.SteamSession(
            {"steamId64": "", "steamRefreshToken": refresh}, log
        ).available()
        is False
    )
    assert s.token_expiry() > time.time() + 190 * 86400


def test_token_mint_cache_and_remint_on_expiry(session, seams, monkeypatch):
    # The mint is the transfer-login flow: GenerateAccessTokenForApp is gated
    # to mobile-audience tokens and answers ours with eresult 15 AccessDenied.
    t1 = session.access_token()
    session.access_token()  # cached, no 2nd mint

    def n_mints():
        return sum("finalizelogin" in u for u, _ in seams.logins)

    assert n_mints() == 1, seams.logins
    assert session._refresh == seams.logins[0][1]["nonce"], (
        "the refresh token rides as nonce"
    )
    monkeypatch.setattr(session, "_access_exp", time.time())  # force expiry
    t2 = session.access_token()
    assert t2 != t1 and n_mints() == 2, seams.logins
    assert "GenerateAccessTokenForApp" not in json.dumps(seams.posts), (
        "the gated endpoint must never be called again"
    )


def test_sessions_and_target_pick_by_machine_name(session, pinned, seams):
    seams.state["sessions"] = two_clients()
    assert [x["instanceid"] for x in session.sessions()] == ["999", "111"]
    assert session._target()["instanceid"] == "999"  # no name -> first
    # A configured machine name pins the target, so install/status can't land
    # on another signed-in box Steam happens to list first.
    assert pinned._target()["instanceid"] == "111"  # self.machine wins over first
    assert pinned.client_online()
    seams.state["sessions"] = two_clients()[:1]  # only the laptop remains
    assert pinned._target() is None and not pinned.client_online(), (
        "a configured target must not fall through to another signed-in client"
    )


def test_401_remints_once_and_retries(log, refresh, monkeypatch):
    """A revoked token is refreshed once before retrying."""

    def resp(code):
        return types.SimpleNamespace(
            status_code=code,
            headers={"X-eresult": "1"},
            json=lambda: {"response": {"sessions": []}},
        )

    hits = {"n": 0}

    class FakeSess:
        headers = {}

        def get(self, url, params=None, timeout=20):
            hits["n"] += 1
            return resp(401 if hits["n"] == 1 else 200)  # 401 once, then OK

        def post(self, url, data=None, timeout=20):  # the re-mint POST
            return resp(200)

    s = ss.SteamSession({"steamId64": STEAMID, "steamRefreshToken": refresh}, log)
    monkeypatch.setattr(s, "_sess", FakeSess())
    monkeypatch.setattr(s, "_access", "stale")  # cached, unexpired
    monkeypatch.setattr(s, "_access_exp", time.time() + 3600)
    monkeypatch.setattr(s, "access_token", lambda: "fresh")  # re-mint: a new token
    body, _ = s._get(
        "IClientCommService/GetAllClientLogonInfo/v1", {"access_token": "stale"}
    )
    assert hits["n"] == 2, "expected one 401 then a retry"  # retried exactly once
    assert body is not None


def test_install_is_verified_via_the_changing_list_on_the_pinned_target(
    pinned, seams, log
):
    seams.state["sessions"] = two_clients()
    seams.state["apps"] = [
        {
            "appid": 570,
            "app": "Dota",
            "changing": True,
            "bytes_to_download": "100",
            "bytes_downloaded": "10",
        }
    ]
    r = pinned.install(570)
    assert r["ok"] and r["verified"] is True, r
    inst = [d for m, d in seams.posts if "InstallClientApp" in m][-1]
    assert inst["appid"] == 570 and inst["client_instanceid"] == "111", inst
    assert "install_queued" in log.events()


def test_install_never_seen_changing_is_ok_but_not_verified(session, seams):
    # The empty 200 is not proof, so absence from the changing list reports
    # verified=False rather than success.
    seams.state["sessions"] = two_clients()
    seams.state["apps"] = [
        {
            "appid": 570,
            "app": "Dota",
            "installed": True,
            "changing": False,
            "bytes_to_download": "0",
            "bytes_downloaded": "0",
        }
    ]
    r = session.install(570)
    assert r["ok"] and r["verified"] is False, r


def test_install_refused_surfaces_the_eresult(session, seams):
    # An X-eresult != 1 is surfaced, not swallowed.
    seams.state["sessions"] = two_clients()
    seams.state["install_eresult"] = "15"  # e.g. AccessDenied
    r = session.install(570)
    assert not r["ok"] and "15" in r["error"], r


def test_install_with_the_pc_offline_is_an_honest_asleep(session, seams):
    seams.state["sessions"] = []
    r = session.install(570)
    assert not r["ok"] and "online" in r["error"], r
    assert not any("InstallClientApp" in m for m, _ in seams.posts), "never called"


def test_download_status_lists_changing_apps_most_complete_first(session, seams):
    seams.state["sessions"] = [{"client_instanceid": "111", "machine_name": "pc"}]
    seams.state["apps"] = [
        {
            "appid": 570,
            "app": "Dota",
            "changing": True,
            "bytes_to_download": "100",
            "bytes_downloaded": "50",
            "download_paused": False,
        },
        {
            "appid": 20,
            "app": "Nearly",
            "changing": True,
            "bytes_to_download": "100",
            "bytes_downloaded": "90",
            "download_paused": False,
        },
        {
            "appid": 30,
            "app": "Finalizing",
            "changing": True,
            "bytes_to_download": "100",
            "bytes_downloaded": "100",
            "download_paused": False,
            "installed": False,
        },
        {
            "appid": 40,
            "app": "Installed",
            "changing": True,
            "bytes_to_download": "100",
            "bytes_downloaded": "100",
            "download_paused": False,
            "installed": True,
        },
        {
            "appid": 730,
            "app": "Idle",
            "changing": False,
            "bytes_to_download": "0",
            "bytes_downloaded": "0",
        },
    ]
    ds = session.download_status()
    assert [d["appid"] for d in ds] == [30, 20, 570], ds
    assert ds[0]["phase"] == "finalizing"
    assert ds[1] == {
        "appid": 20,
        "name": "Nearly",
        "percent": 90,
        "paused": False,
        "queue": None,
        "phase": "downloading",
    }, ds[1]


@pytest.fixture
def enrolling(make_session, monkeypatch):
    """A session with no token yet, its QR going nowhere near the console."""
    monkeypatch.setattr(ss, "_print_qr", lambda text: None)
    return make_session(token=None)


def test_enroll_persists_the_token_and_never_logs_it(enrolling, log):
    # secrets.json resolves under this test's home (conftest).
    secrets = paths.secrets_file()
    secrets.write_text(json.dumps({"deepgramApiKey": "keep-me", "steamId64": STEAMID}))
    assert enrolling.enroll() == 0
    saved = json.loads(secrets.read_text())
    assert saved["steamRefreshToken"] == TOKEN_SECRET
    assert saved["deepgramApiKey"] == "keep-me"  # other secrets preserved
    # The credential must never reach the log - no event, no field.
    blob = json.dumps([r for r in log.records], default=str)
    assert TOKEN_SECRET not in blob, "enroll leaked the token to the log"
    assert "enrolled" in log.events()


def test_enroll_refuses_to_clobber_a_corrupt_secrets_file(enrolling):
    # Writing {} over it would destroy every other secret to save one token.
    secrets = paths.secrets_file()
    secrets.write_text('{"deepgramApiKey": "keep-me",')  # invalid JSON on disk
    with pytest.raises(SystemExit):
        enrolling.enroll()
    assert "keep-me" in secrets.read_text()  # untouched, recoverable


class _Ok:
    status_code = 200
    headers = {"X-eresult": "1"}

    def json(self):
        return {"response": {"sessions": two_clients()}}


def _flaky(calls, fail_first):
    """Return a session whose first GET requests fail."""

    class Flaky:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            if len(calls) <= fail_first:
                raise OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")
            return _Ok()

        def post(self, url, data=None, timeout=None):
            calls.append(url)
            raise OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")

    return Flaky


def bare_session(log, refresh, monkeypatch, calls, fail_first):
    """Use the real retry logic with a mocked HTTP session."""
    monkeypatch.setattr(ss.time, "sleep", lambda n: None)
    s = ss.SteamSession({"steamId64": STEAMID, "steamRefreshToken": refresh}, log)
    monkeypatch.setattr(s, "access_token", lambda: "tok")
    monkeypatch.setattr(s, "_session", _flaky(calls, fail_first))
    return s


def test_a_dropped_read_is_retried(log, refresh, monkeypatch):
    """GET requests retry transient connection failures."""
    calls = []
    s = bare_session(log, refresh, monkeypatch, calls, fail_first=2)
    assert [c["machine_name"] for c in s.sessions()] == ["LAPTOP", "GAMING-PC"]
    assert len(calls) == 3, calls
    # Log each retry at info level.
    retries = log.find("steam_read_retried")
    assert len(retries) == 2 and retries[0]["level"] == "info", retries


def test_a_read_that_never_connects_still_raises(log, refresh, monkeypatch):
    calls = []
    s = bare_session(log, refresh, monkeypatch, calls, fail_first=99)
    with pytest.raises(OSError):
        s.sessions()
    assert len(calls) == 3, "three tries, then the caller hears about it"


def test_a_post_is_never_retried(log, refresh, monkeypatch):
    """POST requests are not retried."""
    calls = []
    s = bare_session(log, refresh, monkeypatch, calls, fail_first=99)
    with pytest.raises(OSError):
        s._post("IClientCommService/InstallClientApp/v1", {"appid": 1})
    assert len(calls) == 1, calls
