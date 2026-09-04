"""Install games and read download status through a signed-in Steam session.

Enroll as WebBrowser, not MobileApp. Installation is verified because a
successful HTTP response does not guarantee that Steam queued the request.
"""

import base64
import json
import sys
import time

from slopstation import config, logbook, paths, statefile

API = "https://api.steampowered.com"
LOGIN = "https://login.steampowered.com"  # the transfer-login host
COMMUNITY = "https://steamcommunity.com"
STORE = "https://store.steampowered.com"
# Steam inspects origin= on ClientComm GETs.
ORIGIN = STORE
# Must not read as a bot library's default UA - that is the flagged
# fingerprint. Bump occasionally.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# EAuthTokenPlatformType - WebBrowser is 2; MobileApp (3) is the flagged one.
PLATFORM_WEBBROWSER = 2
# Delay between GET retries.
_RETRY_BACKOFF_S = 0.3

log = logbook.logger("steam")


def _jwt_exp(token):
    """The `exp` (unix seconds) out of a JWT, unverified. 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


class SteamSession:
    """One long-lived account session: a requests.Session (browser UA) plus a
    cached access token. The lane self-gates on available()."""

    def __init__(self, secrets, log=log, machine_name=None):
        self.secrets = secrets
        self.log = log
        self.steamid = str(secrets.get("steamId64", ""))
        self._refresh = secrets.get("steamRefreshToken")
        # Empty = the first session Steam lists; set config.steamMachineName
        # when another signed-in PC could be listed first, else install can
        # target the wrong box.
        self.machine = machine_name or None
        self._access = None
        self._access_exp = 0.0
        self._sess = None

    def available(self):
        return bool(config.real_key(self._refresh) and self.steamid.isdigit())

    # -- HTTP seams (tests swap these to feed canned JSON) --------------------

    def _session(self):
        if self._sess is None:
            import requests

            self._sess = requests.Session()
            self._sess.headers["User-Agent"] = UA
        return self._sess

    def _get(self, method, params, timeout=20, tries=3):
        """Run a GET request, retrying connection errors only."""
        for attempt in range(1, tries + 1):
            try:
                r = self._session().get(
                    f"{API}/{method}/", params=params, timeout=timeout
                )
                break
            except OSError as e:
                if attempt == tries:
                    raise
                self.log(
                    "steam_read_retried",
                    method=method,
                    attempt=attempt,
                    err=str(e),
                )
                time.sleep(_RETRY_BACKOFF_S * attempt)
        r = self._retry_401(r, method, "params", params, timeout)
        return self._parse(r)

    def _post(self, method, data, timeout=20):
        r = self._session().post(f"{API}/{method}/", data=data, timeout=timeout)
        r = self._retry_401(r, method, "data", data, timeout)
        return self._parse(r)

    def _login_post(self, url, fields, headers=None):
        """POST multipart to the login/transfer hosts -> (json_or_None,
        cookies). Transfer responses carry nothing in the body; the Set-Cookie
        is the payload."""
        r = self._session().post(
            url,
            files={k: (None, str(v)) for k, v in fields.items()},
            headers=headers or {},
            timeout=20,
        )
        try:
            body = r.json()
        except ValueError:
            body = None
        return body, r.cookies.get_dict()

    def _retry_401(self, r, method, key, payload, timeout):
        """A mid-life server-side revocation 401s a token that hasn't hit its
        exp (access_token() only re-mints on time). Re-mint once and retry.
        Skipped when the call carried no token, so the mint can't recurse."""
        if getattr(r, "status_code", 200) != 401 or "access_token" not in payload:
            return r
        self._access = None
        self._access_exp = 0.0
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
        just before it expires. Raises if there is no refresh token."""
        if not config.real_key(self._refresh):
            raise RuntimeError(
                "no Steam refresh token - run: python -m slopstation.agent.tools.steam_session enroll"
            )
        if self._access and time.time() < self._access_exp - 120:
            return self._access
        tok = self._mint()
        if not tok:
            raise RuntimeError(
                "could not mint an access token - the refresh "
                "token may be expired or revoked; re-enroll"
            )
        self._access = tok
        self._access_exp = _jwt_exp(tok) or (time.time() + 23 * 3600)
        return tok

    def _mint(self):
        """Refresh token -> ~24h access token via the web TRANSFER-LOGIN flow:
        refresh token to /jwt/finalizelogin as a nonce, POST each transfer_info
        URL it names, and the steamLoginSecure cookie that comes back IS the
        access token, as "<steamid>||<token>". Returns None on failure.

        NOT GenerateAccessTokenForApp: Valve gated it to MOBILE-audience
        tokens, so this WebBrowser enrollment (aud=[web,renew,derive]) gets an
        empty 200 with X-eresult 15 AccessDenied.

        Non-rotating and repeatable with the same stored refresh token, which
        is what makes it safe unattended. renewRefreshToken rotates and would
        strand a headless box on partial failure; never call it.
        """
        import secrets as _secrets
        from urllib.parse import unquote

        try:
            body, _ = self._login_post(
                f"{LOGIN}/jwt/finalizelogin",
                {
                    "nonce": self._refresh,
                    "sessionid": _secrets.token_hex(12),  # 24 hex, as the site sends
                    "redir": f"{COMMUNITY}/login/home/?goto=",
                },
                headers={"Origin": COMMUNITY, "Referer": COMMUNITY + "/"},
            )
        except Exception as e:
            self.log.warn("token_mint_failed", stage="finalizelogin", err=str(e))
            return None
        transfers = (body or {}).get("transfer_info") or []
        if not transfers:
            self.log.warn(
                "token_mint_failed",
                stage="finalizelogin",
                err="no transfer_info (refresh token rejected?)",
            )
            return None
        # Any one transfer host that answers with the cookie is enough - same
        # session on different Steam domains - so keep trying on failure.
        for t in transfers:
            url = t.get("url")
            if not url:
                continue
            try:
                _, cookies = self._login_post(
                    url, {"steamID": self.steamid, **(t.get("params") or {})}
                )
            except Exception as e:
                self.log.warn("token_transfer_failed", url=str(url)[:60], err=str(e))
                continue
            raw = cookies.get("steamLoginSecure")
            if raw and "||" in unquote(raw):
                return unquote(raw).split("||", 1)[1]
        self.log.warn(
            "token_mint_failed",
            stage="transfer",
            err="no steamLoginSecure cookie from any transfer host",
        )
        return None

    def token_expiry(self):
        """Unix seconds the REFRESH token dies (0 if none/unreadable). doctor
        warns as it nears; only a re-scan fixes it."""
        return _jwt_exp(self._refresh) if config.real_key(self._refresh) else 0

    # -- ClientComm -----------------------------------------------------------

    def sessions(self):
        """Logged-in clients (GetAllClientLogonInfo), each {instanceid,
        machine_name, os_name}. Empty when the PC's client is offline - not an
        error. Instanceids CHURN on every client login; never cache one."""
        body, _ = self._get(
            "IClientCommService/GetAllClientLogonInfo/v1",
            {"access_token": self.access_token(), "origin": ORIGIN},
        )
        out = []
        for s in ((body or {}).get("response", {}) or {}).get("sessions", []) or []:
            iid = s.get("client_instanceid")
            if iid:
                out.append(
                    {
                        "instanceid": str(iid),
                        "machine_name": s.get("machine_name", ""),
                        "os_name": s.get("os_name", ""),
                    }
                )
        return out

    def _target(self):
        """Pick the configured client, or the first when none is configured."""
        ses = self.sessions()
        if not ses:
            return None
        if self.machine:
            for s in ses:
                if s["machine_name"].lower() == self.machine.lower():
                    return s
            return None
        return ses[0]

    def client_online(self):
        return self._target() is not None

    def app_list(self):
        """Return installation and download status for changing apps."""
        tgt = self._target()
        if not tgt:
            return {}
        body, _ = self._get(
            "IClientCommService/GetClientAppList/v1",
            {
                "access_token": self.access_token(),
                "origin": ORIGIN,
                "client_instanceid": tgt["instanceid"],
                "fields": "games",
                "include_client_info": "true",
                "filters": "changing",
            },
        )
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
                "downloaded": done,
                "total": total,
                # -1 is Steam's "not in the queue"; None speaks better.
                "queue": None if queue in (None, -1) else queue,
            }
        return out

    def install(self, appid):
        """Queue an install on the PC's client -> a small dict the tool speaks.
        InstallClientApp answers an empty 200 even when nothing queued (stale
        instanceid, client just went offline), so this re-reads
        GetClientAppList; X-eresult != 1 is a failure. Never raises."""
        appid = int(appid)
        # One try around the whole flow so no hop can raise out of the middle.
        try:
            tgt = self._target()
            if not tgt:
                return {
                    "ok": False,
                    "error": "the gaming PC isn't online in Steam "
                    "right now, so there's nothing to install to",
                }
            _, eresult = self._post(
                "IClientCommService/InstallClientApp/v1",
                {
                    "access_token": self.access_token(),
                    "appid": appid,
                    "client_instanceid": tgt["instanceid"],
                },
            )
            if eresult not in (None, "1"):  # 1 == EResult.OK
                self.log.warn("install_failed", appid=appid, eresult=eresult)
                return {
                    "ok": False,
                    "error": f"Steam refused the install (code {eresult})",
                }
            time.sleep(1.5)
            app = self.app_list().get(appid, {})
        except Exception as e:
            self.log.error("install_error", appid=appid, err=str(e))
            return {
                "ok": False,
                "error": "couldn't reach Steam just now, so the install was not queued",
            }
        # Presence in the CHANGING list is the proof; a just-queued app has no
        # bytes yet, so byte arithmetic would not do.
        queued = bool(app)
        self.log(
            "install_queued", appid=appid, machine=tgt["machine_name"], verified=queued
        )
        return {
            "ok": True,
            "detail": "queued the install on the gaming PC",
            "verified": bool(queued),
        }

    def download_status(self):
        """Apps mid-change, most-complete first - the list_games 'downloading'
        source."""
        rows = []
        for appid, a in self.app_list().items():
            if not a["changing"] and a["total"] <= a["downloaded"]:
                continue
            pct = round(100 * a["downloaded"] / a["total"]) if a["total"] else None
            if pct == 100 and a["installed"]:
                continue
            phase = (
                "finalizing"
                if pct == 100
                else "paused"
                if a["paused"]
                else "queued"
                if a["queue"] is not None and not a["downloaded"]
                else "downloading"
            )
            rows.append(
                {
                    "appid": appid,
                    "name": a["name"],
                    "percent": pct,
                    "paused": a["paused"],
                    "queue": a["queue"],
                    "phase": phase,
                }
            )
        rows.sort(key=lambda r: -(r["percent"] or 0))
        return rows

    # -- one-time enrollment (interactive; run on the K15 once) ---------------

    def enroll(self):
        """QR login under the WebBrowser platform, then persist the refresh
        token to secrets.json; the mobile Steam app scans the printed QR.

            python -m slopstation.agent.tools.steam_session enroll

        Re-run only when the token dies: password change, "deauthorize all
        devices", or ~200 days."""
        body, _ = self._post(
            "IAuthenticationService/BeginAuthSessionViaQR/v1",
            {
                "device_friendly_name": "slopstation couch (K15)",
                "platform_type": PLATFORM_WEBBROWSER,
                "website_id": "Community",
            },
        )
        resp = (body or {}).get("response", {}) or {}
        client_id = resp.get("client_id")
        request_id = resp.get("request_id")
        challenge = resp.get("challenge_url")
        interval = float(resp.get("interval", 5) or 5)
        if not (client_id and request_id and challenge):
            print(
                "enrollment could not start - Steam returned no challenge. "
                "Check the machine's clock and network."
            )
            return 1
        _print_qr(challenge)
        print(
            "\nScan the QR above with the Steam MOBILE APP (Steam Guard -> "
            "scan), then approve. Waiting...\n"
        )
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(interval)
            body, _ = self._post(
                "IAuthenticationService/PollAuthSessionStatus/v1",
                {"client_id": client_id, "request_id": request_id},
            )
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
        value is a credential - never log it."""
        path = paths.secrets_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError:  # no file yet - a fresh rig
            data = {}
        except ValueError:
            # A present-but-unparseable secrets.json aborts: proceeding with {}
            # would os.replace the whole file with just this token.
            print(
                f"REFUSING to write: {path} exists but is not valid JSON - "
                "fix it, then run enroll again."
            )
            raise SystemExit(1) from None
        data["steamRefreshToken"] = refresh
        statefile.write(path, data, indent=2)
        self._refresh = refresh
        self._access = None
        self.log("enrolled", steamid=self.steamid)


def _print_qr(text):
    """ASCII QR if the optional `qrcode` lib is present, else the raw URL."""
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
    secrets = config.secrets()
    s = SteamSession(secrets)
    cmd = argv[0] if argv else "status"
    if cmd == "enroll":
        return s.enroll()
    if not s.available():
        print(
            "no Steam session - set steamId64 and run: python -m slopstation.agent.tools.steam_session enroll"
        )
        return 1
    if cmd == "token":
        # doctor's health check. Mint rather than read exp: an unexpired token
        # is not a working one. Exit 0 == usable.
        try:
            t = s.access_token()
            hours = (_jwt_exp(t) - time.time()) / 3600
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
        print(
            "usage: python -m slopstation.agent.tools.steam_session enroll | token | sessions | downloads "
            "| install <appid>"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
