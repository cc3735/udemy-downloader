"""Stage F license-endpoint scan over the latest HAR."""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Pick the most recent udemy-recon*.har in config/.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
HARS = sorted(CONFIG_DIR.glob("udemy-recon*.har"), key=lambda p: p.stat().st_mtime)
if not HARS:
    print("no HAR files matching config/udemy-recon*.har")
    sys.exit(1)
HAR_PATH = HARS[-1]


def short(s, n=120):
    if s is None:
        return ""
    s = s.replace("\n", " ").replace("\r", " ")
    return (s[:n] + "...") if len(s) > n else s


def main() -> int:
    print(f"Parsing {HAR_PATH.name} ({HAR_PATH.stat().st_size:,} bytes)\n")
    har = json.loads(HAR_PATH.read_text(encoding="utf-8"))
    entries = har.get("log", {}).get("entries", [])
    print(f"HAR has {len(entries)} entries\n")

    # Manifest URLs — does this course serve DASH (.mpd) or just HLS (.m3u8)?
    print("=== Manifest URLs (.mpd / .m3u8) ===")
    saw_mpd = False
    for e in entries:
        url = e.get("request", {}).get("url", "")
        path = urlparse(url).path
        if path.lower().endswith((".mpd", ".m3u8")):
            method = e["request"]["method"]
            resp = e.get("response", {})
            mime = resp.get("content", {}).get("mimeType", "")
            print(f"  {method} {short(url, 150)}")
            print(f"     resp={resp.get('status')} mime={mime}")
            if path.lower().endswith(".mpd"):
                saw_mpd = True
    print(f"  -> DASH/.mpd present: {saw_mpd}")
    print()

    # All POST requests, grouped by Content-Type
    posts_by_ctype: dict[str, list[dict]] = {}
    for e in entries:
        req = e.get("request", {})
        if req.get("method") != "POST":
            continue
        ctype = ""
        for h in req.get("headers", []):
            if h.get("name", "").lower() == "content-type":
                ctype = (h.get("value", "") or "").split(";")[0].strip()
                break
        posts_by_ctype.setdefault(ctype or "<none>", []).append(e)
    print("=== POSTs grouped by Content-Type ===")
    for ctype, lst in sorted(posts_by_ctype.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{len(lst):3d}] {ctype}")
    print()

    # Highlight octet-stream + JSON POSTs
    for special_ctype in ("application/octet-stream", "application/json"):
        lst = posts_by_ctype.get(special_ctype, [])
        if not lst:
            continue
        print(f"=== POSTs with Content-Type: {special_ctype} ===")
        for i, e in enumerate(lst[:20], 1):
            req = e["request"]
            url = req["url"]
            print(f"  [{i}] {short(url, 140)}")
            body = req.get("postData", {}) or {}
            text = body.get("text", "") or ""
            enc = body.get("encoding", "")
            if special_ctype == "application/octet-stream":
                # octet-stream body should be base64-encoded by Chrome
                print(f"      body: encoding={enc} text_len={len(text)}")
                if enc == "base64" and text:
                    try:
                        raw = base64.b64decode(text)
                        # Widevine challenge is a protobuf; first byte tag/wire
                        # type roughly: 0x08 / 0x0a / 0x12 are typical.
                        print(f"      body raw: first 8 bytes = {raw[:8].hex()} (total {len(raw)} bytes)")
                    except Exception as ex:
                        print(f"      body decode failed: {ex}")
            elif special_ctype == "application/json":
                # Print the JSON keys at top level + small snippet
                try:
                    doc = json.loads(text)
                    if isinstance(doc, dict):
                        print(f"      json keys: {list(doc.keys())[:10]}")
                        # If keys include 'pssh' / 'init_data' / 'rawLicenseRequestBase64' we know it's Widevine
                        for k in ("pssh", "init_data", "rawLicenseRequestBase64", "wv", "license", "widevine"):
                            if k in doc:
                                v = doc[k]
                                print(f"      JSON contains key {k!r}; value len={len(str(v))[:60]}")
                except json.JSONDecodeError:
                    pass
            resp = e.get("response", {})
            mime = resp.get("content", {}).get("mimeType", "")
            print(f"      resp: {resp.get('status')} mime={mime}")
        print()

    # ALSO: any GET/POST whose URL matches widevine/license/drm/cert
    print("=== Any request URL hinting at DRM (widevine|license|drm|cert|wv|media_license_token) ===")
    drm_re = re.compile(r"(widevine|license|drm\b|media_license_token|cert\.do|fairplay|playready)", re.IGNORECASE)
    drm_count = 0
    for e in entries:
        req = e.get("request", {})
        url = req.get("url", "")
        # restrict to udemy + cdn hosts to filter noise
        host = urlparse(url).hostname or ""
        if "udemy" not in host and "drm" not in host:
            continue
        path = urlparse(url).path
        if drm_re.search(path):
            drm_count += 1
            print(f"  {req['method']} {short(url, 150)}")
            if drm_count > 25:
                print("  (truncated)")
                break
    if drm_count == 0:
        print("  (none)")
    print()

    # Lecture detail responses — read course_is_drmed flag
    print("=== course_is_drmed across captured lecture details ===")
    drmed_seen = []
    for e in entries:
        url = e.get("request", {}).get("url", "")
        if "/users/me/subscribed-courses/" not in url or "/lectures/" not in url:
            continue
        content = e.get("response", {}).get("content", {})
        text = content.get("text", "") or ""
        if content.get("encoding") == "base64":
            try:
                text = base64.b64decode(text).decode("utf-8", "ignore")
            except Exception:
                continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        asset = doc.get("asset") or {}
        drmed = asset.get("course_is_drmed")
        title = doc.get("title") or ""
        media_lic = asset.get("media_license_token")
        has_media_sources = bool(asset.get("media_sources"))
        has_stream_urls = bool(asset.get("stream_urls"))
        drmed_seen.append((drmed, has_media_sources, has_stream_urls, bool(media_lic), title, asset.get("id")))
    for d, ms, su, ml, t, aid in drmed_seen[:10]:
        print(f"  asset_id={aid} drmed={d!r} media_sources={ms} stream_urls={su} media_license_token={ml} title={t!r}")
    if not drmed_seen:
        print("  (no lecture-detail responses found)")
    print()

    # Hosts contacted
    print("=== Top 20 hosts contacted ===")
    hosts = {}
    for e in entries:
        h = urlparse(e.get("request", {}).get("url", "")).hostname or ""
        hosts[h] = hosts.get(h, 0) + 1
    for h, c in sorted(hosts.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  [{c:4d}] {h}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
