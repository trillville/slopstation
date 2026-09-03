"""Interactive client for Slopstation's authenticated K15 text interface."""

import argparse
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _local_settings():
    url = None
    token = None
    config_path = HERE / "config.json"
    secrets_path = HERE / "secrets.json"
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        text_cfg = cfg.get("textInterface") or {}
        host = str(text_cfg.get("host", "127.0.0.1"))
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        url = f"http://{host}:{int(text_cfg.get('port', 8765))}"
    except (OSError, ValueError, TypeError):
        pass
    try:
        secrets = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
        token = secrets.get("textInterfaceToken")
    except (OSError, ValueError, TypeError):
        pass
    return url, token


def ask(url, token, session, message):
    body = json.dumps({"session": session, "message": message}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/chat",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error")
        except (ValueError, UnicodeDecodeError):
            detail = f"HTTP {e.code}"
        raise RuntimeError(detail) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError("could not reach the K15 text interface") from e
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "assistant request failed"))
    return str(result.get("reply", ""))


def main(argv=None):
    local_url, local_token = _local_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="*")
    parser.add_argument("--url", default=os.environ.get("SLOPSTATION_URL", local_url))
    parser.add_argument(
        "--token", default=os.environ.get("SLOPSTATION_TOKEN", local_token)
    )
    parser.add_argument("--session", default=uuid.uuid4().hex)
    args = parser.parse_args(argv)
    if not (args.url and args.token):
        print("Set SLOPSTATION_URL and SLOPSTATION_TOKEN, or pass --url/--token.")
        return 2

    def one(message):
        try:
            print(ask(args.url, args.token, args.session, message))
            return True
        except RuntimeError as e:
            print(f"error: {e}")
            return False

    if args.message:
        return 0 if one(" ".join(args.message)) else 1
    print(f"Slopstation text interface at {args.url}. Empty line exits.")
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            return 0
        if not one(message):
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
