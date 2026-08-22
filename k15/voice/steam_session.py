"""A headless signed-in Steam session: install-by-voice and download status
over IClientCommService, the same first-party surface the Steam mobile app's
"remote install" uses. Its own failure domain - auth, tokens, a live account -
so its own file (README's module-extraction rule (a)).

WHY this exists and nothing else does it: the couch deliberately refuses to
install games (Dispatch answers NOTINSTALLED, because steam -applaunch on an
uninstalled appid pops a controller-needing dialog on the TV). ClientComm queues
the install on the PC's client with NO dialog and NO controller - the one path
that reopens that door. Download status ("how far along?") rides the same
channel, so CDP is never needed for it.

THE ONE LOAD-BEARING SAFETY RULE (documented account-flagging, DoctorMcKay's
forum): enroll under the WebBrowser platform with a realistic User-Agent, NEVER
MobileApp. The mobile app is the QR *scanner*, never the QR *target*, and Steam
flags a MobileApp+QR login as compromised. WebBrowser is also exactly the
identity the store website's own remote-install runs as, so this box's traffic
is indistinguishable from that sanctioned feature.

Everything is REST with an access_token - no CM websocket, no Steam libraries,
proven by three community impls (one pure-stdlib Python) and the store site's
own JS. It works over plain requests. Undocumented-but-stable, so the mutating
calls VERIFY rather than trust: an install returns an empty 200 whether or not
it queued, so success is confirmed by re-reading GetClientAppList, and failures
surface as an X-eresult response HEADER, not a JSON body. Verification over
reports, the house rule.

CONFIRMED LIVE 2026-08-14 against the real account: the mint, the session list
(the gaming PC comes back by machine_name) and the app list with download
progress all work from this box, driven by a WEB-audience token - which settles
the one question the design rested on. The app-list field names were guessed
wrong before that (see app_list); everything here now matches what Steam
actually sends. All the parsing still reads defensively so a shape drift
degrades to a spoken "couldn't reach Steam", never a crash.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                        # noqa: E402

API = "https://api.steampowered.com"
LOGIN = "https://login.steampowered.com"        # the transfer-login host
COMMUNITY = "https://steamcommunity.com"
STORE = "https://store.steampowered.com"
# ClientComm GETs carry origin= like the store site's own calls do. Steam
# inspects it (node-steam-session 1.9.4 added the mobile equivalent for exactly
# this reason), and our traffic should look like the sanctioned feature it is.
ORIGIN = STORE
# A realistic desktop-Chrome UA - the point is NOT to look like a bot library's
# default (the flagged fingerprint). Bump occasionally; it only needs to read as
# an ordinary browser.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# EAuthTokenPlatformType - WebBrowser is 2. MobileApp (3) is the flagged one;
# it is named here only so the choice is legible, never used.
PLATFORM_WEBBROWSER = 2

log = cglib.make_log("steam")


def jwt_exp(token):
    """The `exp` (unix seconds) out of a JWT without verifying it - we minted
    nothing, we just want to know when to re-mint. 0 if unreadable. Public:
    doctor.py reads the refresh token's expiry with it rather than a copy."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)        # restore base64 padding
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


class SteamSession:
    """One long-lived account session. Construct with secrets + a log; holds a
    requests.Session (browser UA) and a cached access token. available() is
    false without a refresh token, so the whole lane self-gates on the secret
    exactly like the Steam Web API key does - no config bool, no migration."""

    def __init__(self, secrets, log=log, machine_name=None):
        self.secrets = secrets
        self.log = log
        self.steamid = str(secrets.get("steamId64", ""))
        self._refresh = secrets.get("steamRefreshToken")
        # Which client to act on when several are signed in. Empty = the first
        # session Steam lists, which is right for the common single-PC case;
        # set config.steamMachineName if you're also signed in on another PC
        # that could be listed first (else install could target the wrong box).
        self.machine = machine_name or None
        self._access = None
        self._access_exp = 0
        self._sess = None

    def available(self):
        return bool(cglib.real_key(self._refresh) and self.steamid.isdigit())

    # -- HTTP seams (tests swap these to feed canned JSON) --------------------

    def _session(self):
        import requests
        if self._sess is None:
            self._sess = requests.Session()
            self._sess.headers["User-Agent"] = UA
        return self._sess

    def _get(self, method, params, timeout=20):
        """GET a ClientComm/Auth method. Returns (json_or_None, eresult) where
        eresult is the X-eresult header (1 == OK) - the mutating calls answer
        with an empty 200 and put the real outcome there."""
        r = self._session().get(f"{API}/{method}/", params=params, timeout=timeout)
        r = self._retry_401(r, method, "params", params, timeout)
        return self._parse(r)

    def _post(self, method, data, timeout=20):
        r = self._session().post(f"{API}/{method}/", data=data, timeout=timeout)
        r = self._retry_401(r, method, "data", data, timeout)
        return self._parse(r)

    def _login_post(self, url, fields, headers=None):
        """POST multipart to the login/transfer hosts, returning
        (json_or_None, cookies). The transfer responses carry nothing useful in
        the BODY - the Set-Cookie is the payload - so cookies are a first-class
        return value here, unlike _post. One seam, so a test drives the whole
        transfer flow offline (this module's testing contract)."""
        r = self._session().post(
            url, files={k: (None, str(v)) for k, v in fields.items()},
            headers=headers or {}, timeout=20)
        try:
            body = r.json()
        except ValueError:
            body = None
        return body, r.cookies.get_dict()

    def _retry_401(self, r, method, key, payload, timeout):
        """access_token() re-mints on TIME (exp check); this covers the other
        case - a mid-life server-side revocation 401s a token that hasn't hit
        its exp. Re-mint once and retry. Skipped when the call carried no token
        (the auth endpoints), so the mint itself can't recurse."""
        if getattr(r, "status_code", 200) != 401 or "access_token" not in payload:
            return r
        self._access = None
        self._access_exp = 0
        payload = {**payload, "access_token": self.access_token()}
        fn = self._session().get if key == "params" else self._session().post
        return fn(f"{API}/{method}/", timeout=timeout, **{key: payload})

    @staticmethod
    def _parse(r):
        eresult = r.headers.get("X-eresult")
        try:
            body = r.json()
        except ValueError:
            body = None
        return body, eresult

    # -- tokens ---------------------------------------------------------------

    def access_token(self):
        """A ~24h access token, minted from the refresh token and cached until
        just before it expires. Re-mints on demand; raises if there is no
        refresh token (the caller gates on available())."""
        if not cglib.real_key(self._refresh):
            raise RuntimeError("no Steam refresh token - run: python steam_session.py enroll")
        if self._access and time.time() < self._access_exp - 120:
            return self._access
        tok = self._mint()
        if not tok:
            raise RuntimeError("could not mint an access token - the refresh "
                               "token may be expired or revoked; re-enroll")
        self._access = tok
        self._access_exp = jwt_exp(tok) or (time.time() + 23 * 3600)
        return tok

    def _mint(self):
        """Refresh token -> ~24h access token, via the web TRANSFER-LOGIN flow.
        Returns None when it cannot (never raises on a bad answer).

        NOT GenerateAccessTokenForApp, which is the obvious call and the wrong
        one: Valve gated it to MOBILE-audience tokens, so our WebBrowser
        enrollment (aud=[web,renew,derive]) gets a 200 with an empty body and
        X-eresult 15 AccessDenied - measured 2026-08-14, and node-steam-session
        documents the same as of 2025-04-30. This is what the store website
        itself does instead: hand the refresh token to /jwt/finalizelogin as a
        nonce, POST each transfer_info URL it names, and the steamLoginSecure
        cookie that comes back IS the access token, as "<steamid>||<token>".

        Repeatable with the SAME stored refresh token and NON-ROTATING - that
        is what makes it safe to run unattended. (renewRefreshToken rotates and
        invalidates the old token; a partial failure there would strand a
        headless box until someone re-scanned a QR. We never call it.)
        """
        import secrets as _secrets
        from urllib.parse import unquote
        try:
            body, _ = self._login_post(
                f"{LOGIN}/jwt/finalizelogin",
                {"nonce": self._refresh,
                 "sessionid": _secrets.token_hex(12),   # 24 hex, as the site sends
                 "redir": f"{COMMUNITY}/login/home/?goto="},
                headers={"Origin": COMMUNITY, "Referer": COMMUNITY + "/"})
        except Exception as e:
            self.log.warn("token_mint_failed", stage="finalizelogin", err=str(e))
            return None
        transfers = ((body or {}).get("transfer_info") or [])
        if not transfers:
            self.log.warn("token_mint_failed", stage="finalizelogin",
                          err="no transfer_info (refresh token rejected?)")
            return None
        # Any ONE transfer host that answers with the cookie is enough; they are
        # the same session on different Steam domains. Keep trying on failure -
        # a single unreachable host must not cost the whole mint.
        for t in transfers:
            url = t.get("url")
            if not url:
                continue
            try:
                _, cookies = self._login_post(
                    url, {"steamID": self.steamid, **(t.get("params") or {})})
            except Exception as e:
                self.log.warn("token_transfer_failed", url=str(url)[:60], err=str(e))
                continue
            raw = cookies.get("steamLoginSecure")
            if raw and "||" in unquote(raw):
                return unquote(raw).split("||", 1)[1]
        self.log.warn("token_mint_failed", stage="transfer",
                      err="no steamLoginSecure cookie from any transfer host")
        return None

    def token_expiry(self):
        """Unix seconds the REFRESH token dies (0 if none/unreadable) - doctor
        warns as it nears, since only a human (a re-scan) fixes that."""
        return jwt_exp(self._refresh) if cglib.real_key(self._refresh) else 0

    # -- ClientComm -----------------------------------------------------------

    def sessions(self):
        """Every logged-in client on the account (GetAllClientLogonInfo). Each:
        {instanceid, machine_name, os_name}. Empty when the PC's client is
        offline - which is the honest "the PC is asleep" answer, not an error.
        Instanceids CHURN on every client login, so callers re-fetch before
        acting rather than caching one."""
        body, _ = self._get("IClientCommService/GetAllClientLogonInfo/v1",
                           {"access_token": self.access_token(), "origin": ORIGIN})
        out = []
        for s in ((body or {}).get("response", {}) or {}).get("sessions", []) or []:
            iid = s.get("client_instanceid")
            if iid:
                out.append({"instanceid": str(iid),
                            "machine_name": s.get("machine_name", ""),
                            "os_name": s.get("os_name", "")})
        return out

    def _target(self, machine_name=None):
        """Pick the client to act on. Prefers a machine-name match (the call's,
        else the configured self.machine) so install/status hit the gaming PC
        and not another signed-in box Steam happens to list first; falls back to
        the first session. None when the PC is offline - the caller turns that
        into a spoken 'the PC is asleep'."""
        ses = self.sessions()
        if not ses:
            return None
        machine_name = machine_name or self.machine
        if machine_name:
            for s in ses:
                if s["machine_name"].lower() == machine_name.lower():
                    return s
        return ses[0]

    def app_list(self, changing_only=False):
        """GetClientAppList for the target client, normalized to
        {appid: {name, installed, changing, paused, downloaded, total, queue}}.
        The truth source that VERIFIES a queued install actually took.

        SHAPE, confirmed live 2026-08-14 (it was guessed before, and guessed
        wrong): the name is 'app', NOT 'app_name'. Byte counts arrive as
        STRINGS. queue_position is -1 for "not queued", not absent. And the two
        calls answer differently - the plain list is every known app with only
        {app, app_type, appid, bytes_required, changing, queue_position,
        running}, while filters=changing adds the progress fields
        (bytes_downloaded/bytes_to_download, installed, bytes_staged). So
        anything needing progress must pass changing_only."""
        tgt = self._target()
        if not tgt:
            return {}
        params = {"access_token": self.access_token(), "origin": ORIGIN,
                  "client_instanceid": tgt["instanceid"],
                  "fields": "games", "include_client_info": "true"}
        if changing_only:
            params["filters"] = "changing"
        body, _ = self._get("IClientCommService/GetClientAppList/v1", params)
        out = {}
        for a in ((body or {}).get("response", {}) or {}).get("apps", []) or []:
            appid = a.get("appid")
            if not appid:
                continue
            total = int(a.get("bytes_to_download", 0) or 0)
            done = int(a.get("bytes_downloaded", 0) or 0)
            queue = a.get("queue_position")
            out[int(appid)] = {
                "name": a.get("app") or a.get("app_name") or "",
                "installed": bool(a.get("installed")),
                "changing": bool(a.get("changing")),
                "paused": bool(a.get("download_paused")),
                "downloaded": done, "total": total,
                # -1 is Steam's "not in the queue"; None speaks better.
                "queue": None if queue in (None, -1) else queue}
        return out

    def install(self, appid, machine_name=None):
        """Queue an install on the PC's client. Returns a small dict the tool
        speaks. VERIFIED, not trusted: InstallClientApp answers an empty 200
        even when nothing queued (stale instanceid, client just went offline),
        so this re-reads GetClientAppList and reports what actually changed;
        an X-eresult != 1 is surfaced as the failure it is. Honors the module's
        no-crash promise: a token that no longer mints (revoked/expired) or a
        network error becomes a truthful result, not a raised exception."""
        appid = int(appid)
        # One try around the WHOLE flow (target pick, the POST, the verify
        # re-read), so the docstring's no-crash promise holds at every hop -
        # a blip after _target would otherwise raise out of the middle.
        try:
            tgt = self._target(machine_name)
            if not tgt:
                return {"ok": False, "error": "the gaming PC isn't online in Steam "
                        "right now, so there's nothing to install to"}
            _, eresult = self._post("IClientCommService/InstallClientApp/v1",
                                   {"access_token": self.access_token(),
                                    "appid": appid,
                                    "client_instanceid": tgt["instanceid"]})
            if eresult not in (None, "1"):          # 1 == EResult.OK
                self.log.warn("install_failed", appid=appid, eresult=eresult)
                return {"ok": False, "error": f"Steam refused the install (code {eresult})"}
            # Confirm it really queued - the empty 200 is not proof.
            time.sleep(1.5)
            app = self.app_list(changing_only=True).get(appid, {})
        except Exception as e:
            self.log.error("install_error", appid=appid, err=str(e))
            return {"ok": False, "error": "couldn't reach Steam - the account "
                    "session may need re-enrolling"}
        # Appearing in the CHANGING list at all is the proof: that filter is
        # exactly "apps mid-install/update", so presence beats any byte
        # arithmetic (a just-queued app has no bytes_downloaded yet).
        queued = bool(app)
        self.log("install_queued", appid=appid, machine=tgt["machine_name"],
                 verified=queued)
        return {"ok": True, "detail": "queued the install on the gaming PC",
                "verified": bool(queued)}

    def download_status(self, machine_name=None):
        """What's downloading and how far - the list_games 'downloading' source.
        Only apps mid-change, most-complete first."""
        apps = self.app_list(changing_only=True)
        rows = []
        for appid, a in apps.items():
            if not a["changing"] and a["total"] <= a["downloaded"]:
                continue
            pct = (round(100 * a["downloaded"] / a["total"]) if a["total"] else None)
            rows.append({"appid": appid, "name": a["name"], "percent": pct,
                         "paused": a["paused"], "queue": a["queue"]})
        rows.sort(key=lambda r: -(r["percent"] or 0))
        return rows

    # -- one-time enrollment (interactive; run on the K15 once) ---------------

    def enroll(self):
        """QR login under the WebBrowser platform, then persist the refresh
        token to secrets.json. The mobile Steam app scans the printed QR; on
        approval the poll returns the tokens. Run once:
            python steam_session.py enroll
        Re-run only when the token dies (a password change, a "deauthorize all
        devices", or ~200 days) - the same 30-second scan."""
        body, _ = self._post(
            "IAuthenticationService/BeginAuthSessionViaQR/v1",
            {"device_friendly_name": "slopstation couch (K15)",
             "platform_type": PLATFORM_WEBBROWSER,
             "website_id": "Community"})
        resp = (body or {}).get("response", {}) or {}
        client_id = resp.get("client_id")
        request_id = resp.get("request_id")
        challenge = resp.get("challenge_url")
        interval = float(resp.get("interval", 5) or 5)
        if not (client_id and request_id and challenge):
            print("enrollment could not start - Steam returned no challenge. "
                  "Check the machine's clock and network.")
            return 1
        _print_qr(challenge)
        print("\nScan the QR above with the Steam MOBILE APP (Steam Guard -> "
              "scan), then approve. Waiting...\n")
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(interval)
            body, _ = self._post(
                "IAuthenticationService/PollAuthSessionStatus/v1",
                {"client_id": client_id, "request_id": request_id})
            r = (body or {}).get("response", {}) or {}
            refresh = r.get("refresh_token")
            if refresh:
                self._persist_refresh(refresh)
                print("Enrolled. The refresh token is saved to secrets.json.")
                return 0
            if r.get("had_remote_interaction"):
                print("  (approved on the phone - finishing...)")
        print("timed out waiting for the scan - run enroll again.")
        return 1

    def _persist_refresh(self, refresh):
        """Write the token into secrets.json, preserving everything else. The
        value is a credential - never log it (events.scrub would redact it, but
        it never goes near an event here anyway)."""
        path = cglib.SECRETS
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError:                     # no file yet - a fresh rig
            data = {}
        except ValueError:
            # A PRESENT-but-unparseable secrets.json aborts: proceeding with {}
            # would os.replace the whole file with just this token, destroying
            # every other secret to save one. Fix the JSON, re-scan (30 s).
            print(f"REFUSING to write: {path} exists but is not valid JSON - "
                  "fix it, then run enroll again.")
            raise SystemExit(1)
        data["steamRefreshToken"] = refresh
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        self._refresh = refresh
        self._access = None
        self.log("enrolled", steamid=self.steamid)


def _print_qr(text):
    """ASCII QR if the optional `qrcode` lib is present; otherwise the raw URL
    plus how to turn it into a scannable code. Optional so the pin stays light -
    enrollment is rare and a human is at the keyboard for it."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        print("(install `qrcode` for an inline QR, or make a QR of this URL)")
        print("  " + text)


def _cli(argv):
    secrets = cglib.load_secrets()
    s = SteamSession(secrets)
    cmd = argv[0] if argv else "status"
    if cmd == "enroll":
        return s.enroll()
    if not s.available():
        print("no Steam session - set steamId64 and run: python steam_session.py enroll")
        return 1
    if cmd == "token":
        # The one-line health answer, for doctor: can this refresh token
        # actually MINT? An unexpired token is not a working one - the whole
        # lane was green-on-paper and dead in practice for a day because the
        # only check was a JWT exp read (2026-08-14). Exit 0 == usable.
        try:
            t = s.access_token()
            hours = (jwt_exp(t) - time.time()) / 3600
            print(f"OK access token minted, good for {hours:.0f}h")
            return 0
        except Exception as e:
            print(f"FAIL {e}")
            return 1
    if cmd == "sessions":
        print(json.dumps(s.sessions(), indent=2))
    elif cmd == "downloads":
        print(json.dumps(s.download_status(), indent=2))
    elif cmd == "install" and len(argv) > 1:
        print(json.dumps(s.install(int(argv[1])), indent=2))
    else:
        print("usage: steam_session.py enroll | token | sessions | downloads "
              "| install <appid>")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
