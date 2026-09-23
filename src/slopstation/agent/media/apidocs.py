"""Fetch each service's own API documentation live and slice it by topic.

Nothing here is hand-written and nothing rots: Radarr, Sonarr and Prowlarr
serve their OpenAPI documents, qBittorrent's reference is its wiki page. A
fetched document is cached on disk for a day. The slice is what the model
reads before it parameterises a passthrough call.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request

from slopstation import paths

CACHE_S = 24 * 3600
MAX_CHARS = 7000
FETCH_TIMEOUT_S = 20
# The arr documents are about a megabyte; anything past this is not one.
MAX_DOC_BYTES = 4 * 1024 * 1024

# For the arr apps the live path is the app's own; the fallback is the same
# document as published by the project, for when the app does not serve it.
SOURCES: dict[str, dict] = {
    "radarr": {
        "kind": "openapi",
        "live": "{base}/docs/v3/openapi.json",
        "fallback": "https://raw.githubusercontent.com/Radarr/Radarr/develop/"
        "src/Radarr.Api.V3/openapi.json",
    },
    "sonarr": {
        "kind": "openapi",
        "live": "{base}/docs/v3/openapi.json",
        "fallback": "https://raw.githubusercontent.com/Sonarr/Sonarr/develop/"
        "src/Sonarr.Api.V3/openapi.json",
    },
    "prowlarr": {
        "kind": "openapi",
        "live": "{base}/docs/v1/openapi.json",
        "fallback": "https://raw.githubusercontent.com/Prowlarr/Prowlarr/develop/"
        "src/Prowlarr.Api.V1/openapi.json",
    },
    "qbittorrent": {
        "kind": "markdown",
        "live": "https://raw.githubusercontent.com/wiki/qbittorrent/qBittorrent/"
        "WebUI-API-(qBittorrent-5.0).md",
    },
}


def cache_file(service: str) -> paths.pathlib.Path:
    return paths.state(f"apidocs-{service}.txt")


def _urlopen(url: str) -> str:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        raw = r.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise ValueError("document too large")
    return raw.decode("utf-8", "replace")


def fetch(service: str, base_url: str | None = None, opener=_urlopen, now=time.time):
    """The document text, from the day cache or the live sources in order."""
    src = SOURCES[service]
    f = cache_file(service)
    try:
        if now() - f.stat().st_mtime < CACHE_S:
            return f.read_text(encoding="utf-8")
    except OSError:
        pass
    urls = []
    if "{base}" in src["live"]:
        if base_url:
            urls.append(src["live"].format(base=base_url.rstrip("/")))
    else:
        urls.append(src["live"])
    if src.get("fallback"):
        urls.append(src["fallback"])
    last = None
    for url in urls:
        try:
            text = opener(url)
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
        ) as e:
            last = e
            continue
        if src["kind"] == "openapi":
            try:
                json.loads(text)
            except ValueError as e:
                last = e
                continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return text
    # Every source failed: a stale copy beats no answer.
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        pass
    raise RuntimeError(f"could not fetch {service} documentation: {last}")


def _deref(schema, spec, depth=0):
    """Resolve $ref one level at a time; a schema for a tool call needs field
    names and types, not the whole component tree."""
    if depth > 3 or not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        target = spec.get("components", {}).get("schemas", {}).get(name, {})
        return _deref(target, spec, depth + 1)
    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        return {
            k: (v.get("type") or ("ref" if "$ref" in v else "?"))
            for k, v in schema["properties"].items()
        }
    if schema.get("type") == "array":
        return [_deref(schema.get("items", {}), spec, depth + 1)]
    return schema.get("type", schema)


def slice_openapi(text: str, topic: str) -> dict:
    spec = json.loads(text)
    want = topic.lower().strip("/ ")
    out: dict[str, dict] = {}
    for path, methods in sorted(spec.get("paths", {}).items()):
        if want and want not in path.lower():
            continue
        entry = {}
        for method, op in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "DELETE"):
                continue
            params = [
                f"{p.get('name')}:{p.get('in')}" + ("!" if p.get("required") else "")
                for p in op.get("parameters", [])
            ]
            row: dict = {}
            if params:
                row["params"] = params
            body = (
                op.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if body:
                row["body"] = _deref(body, spec)
            if op.get("summary"):
                row["summary"] = op["summary"]
            entry[method.upper()] = row
        if entry:
            out[path] = entry
    return out


def slice_markdown(text: str, topic: str) -> list[str]:
    """The sections (heading to next heading) whose heading or body mention
    the topic, the heading match first."""
    want = topic.lower().strip()
    # Split on headings outside fenced code, where a `# comment` is code.
    sections: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"#{1,4} ", line) and sections:
            sections.append(line)
        elif sections:
            sections[-1] += line
        else:
            sections.append(line)
    by_heading = [s for s in sections if want in s.split("\n", 1)[0].lower()]
    by_body = [s for s in sections if s not in by_heading and want in s.lower()]
    return by_heading + by_body


def describe(service: str, topic: str, base_url: str | None = None, opener=_urlopen):
    """The documentation slice for one topic, capped for the model."""
    text = fetch(service, base_url, opener)
    if SOURCES[service]["kind"] == "openapi":
        paths_found = slice_openapi(text, topic)
        body = json.dumps(paths_found, separators=(",", ":"))
        result: dict = {"service": service, "topic": topic, "paths": len(paths_found)}
    else:
        sections = slice_markdown(text, topic)
        body = "\n\n".join(sections)
        result = {"service": service, "topic": topic, "sections": len(sections)}
    if len(body) > MAX_CHARS:
        result["truncated"] = True
        result["detail"] = "narrow the topic to see the rest"
        body = body[:MAX_CHARS]
    result["doc"] = body
    return result
