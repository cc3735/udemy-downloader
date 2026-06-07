"""CDM-driven license fetcher for udemy-downloader.

Why this exists
---------------
Upstream `main.py` expects you to populate `keyfile.json` manually
(`{ "<kid_hex>": "<key_hex>", ... }`) before it runs; it does NOT call
any Widevine license server itself.  This sidecar uses the user's
existing Widevine L3 CDM (mounted at /cdm/widevine.wvd by
docker-compose.yml) to perform the Stage 3 license exchange documented
in `Repos/WIDEVINE-DECRYPT-PLAYBOOK.md`, then writes the resulting
KID:KEY pairs into `keyfile.json` so the next `python main.py -c <url>`
run can decrypt everything.

Three modes:

  1. ``--bulk --course-url <url>``  (used by `udl-rip`)
        Enumerates the entire course's lectures via Udemy's
        ``/api-2.0/courses/{id}/subscriber-curriculum-items/`` endpoint,
        finds every encrypted asset's DASH manifest, parses the PSSH,
        performs the license exchange, and appends KID:KEY pairs to
        keyfile.json.

  2. ``--scan-out``
        Fallback: walks `out_dir/<course>/...` for already-downloaded
        encrypted MP4 files and fills any keyfile.json gaps by parsing
        PSSH out of those files directly.

  3. ``--watch``
        Poll mode for autopilot operation alongside a continuously-
        running rip.  Re-runs the chosen mode every N seconds.

Stage F (license endpoint reconnaissance) -- this script ships with
LICENSE_URL_TPL set to a placeholder.  The first time you run it
you'll get a clear "license endpoint not configured" error pointing at
this constant.  Use chrome-devtools-mcp (`chrome-debug-start` ->
play a DRM lecture -> Network tab -> capture the POST URL + headers)
to fill it in.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

import requests

# Run-from-anywhere import shim: the script lives in scripts/ but needs
# to import upstream modules (constants.py, utils.py, mp4parse.py,
# widevine_pssh_data_pb2.py) that live at /app.
_HERE = Path(__file__).resolve().parent
_APP = _HERE.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from constants import HEADERS, URLS, CURRICULUM_ITEMS_PARAMS  # type: ignore
from utils import extract_kid  # type: ignore

# pywidevine is installed by the bundle Dockerfile (NOT in upstream
# requirements.txt; see Dockerfile).
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH


# ---------------------------------------------------------------------
# Stage F constants -- pinned 2026-06-07 from
# config/udemy-recon3.har (beginners-guide-to-technical-analysis DRM
# lecture).  See parse_recon_har.py for the discovery script.
# ---------------------------------------------------------------------

# Udemy's Widevine license endpoint takes a per-asset auth_token JWT
# in the query string.  Each lecture's asset has its own JWT in
# `asset.media_license_token`, so the sidecar fetches that first and
# substitutes it here.
LICENSE_URL_TPL: str = (
    "https://www.udemy.com/media-license-server/validate-auth-token"
    "?drm_type=widevine&auth_token={auth_token}"
)

# No service certificate is required for Udemy's flow (verified in
# the HAR -- no cert.do request precedes the license POST).
SERVICE_CERT_URL: Optional[str] = None

# Default polite delay between license requests (seconds).
DEFAULT_REQUEST_DELAY = 0.6

# Widevine PSSH system UUID -- constant.
WIDEVINE_SYSID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")


# ---------------------------------------------------------------------
# Config + logging
# ---------------------------------------------------------------------

logger = logging.getLogger("get_udemy_keys")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@dataclass
class Config:
    wvd_path: Path
    bearer: str
    keys_file: Path
    out_root: Path
    license_url_tpl: Optional[str] = None
    service_cert_url: Optional[str] = None
    request_delay_sec: float = DEFAULT_REQUEST_DELAY
    portal_name: str = "www"

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        wvd = Path(args.wvd or os.environ.get("UDL_WVD_PATH") or "/cdm/widevine.wvd")
        bearer = args.bearer or os.environ.get("UDEMY_BEARER") or _read_bearer_file()
        keys_file = Path(args.keys_file or os.environ.get("UDL_KEYS_FILE") or "/app/keyfile.json")
        out_root = Path(args.out_root or os.environ.get("UDL_OUT_ROOT") or "/app/out_dir")
        return cls(
            wvd_path=wvd,
            bearer=bearer or "",
            keys_file=keys_file,
            out_root=out_root,
            license_url_tpl=args.license_url or LICENSE_URL_TPL,
            service_cert_url=args.cert_url or SERVICE_CERT_URL,
            request_delay_sec=args.request_delay,
            portal_name="www",
        )


def _read_bearer_file() -> Optional[str]:
    """Fallback: read the token from config/bearer.txt if the env var
    isn't set.  Helps when the user prefers a file over export-on-shell."""
    candidates = [Path("/app/config/bearer.txt"), Path("config/bearer.txt")]
    for path in candidates:
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
    return None


# ---------------------------------------------------------------------
# Keyfile I/O (atomic)
# ---------------------------------------------------------------------

def load_keyfile(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        logger.warning("keyfile.json is not valid JSON; starting fresh")
        return {}
    # Strip the upstream placeholder entry (`{"the key id goes here": ...}`)
    # if present, so the sidecar's dict-update semantics work cleanly.
    return {k: v for k, v in data.items() if re.fullmatch(r"[0-9a-fA-F]{32}", k or "")}


def save_keyfile(path: Path, keys: dict) -> None:
    # keyfile.json is bind-mounted from the host -- can't use the
    # tmp-write + atomic-rename trick (Linux: EBUSY when the target is
    # a bind-mounted file).  Do a direct in-place write; brief race
    # window if another process reads at the same time is acceptable
    # given the sidecar is the only writer.
    path.write_text(json.dumps(keys, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------
# CDM (pywidevine)
# ---------------------------------------------------------------------

def load_cdm(wvd_path: Path) -> Cdm:
    if not wvd_path.exists():
        raise FileNotFoundError(
            f"CDM not found at {wvd_path}. The docker-compose mount likely "
            "failed -- check UDL_WVD_PATH_HOST points at a real .wvd file."
        )
    device = Device.load(str(wvd_path))
    logger.info(f"CDM loaded: system_id={device.system_id} security_level={device.security_level}")
    return Cdm.from_device(device)


# ---------------------------------------------------------------------
# Udemy API enumeration -- uses upstream's Session (curl_cffi +
# chrome120 impersonation + visit() preflight to clear Cloudflare).
# ---------------------------------------------------------------------

# Stub the module-level `logger` upstream's main.py expects.  It's only
# initialized when main.py is run directly; importing Session bypasses
# that path.
import main as _udl_main  # type: ignore
if getattr(_udl_main, "logger", None) is None:
    _udl_main.logger = logging.getLogger("get_udemy_keys.upstream")

from main import Session as _UpstreamSession  # noqa: E402  type: ignore


def _udemy_session(bearer: str) -> "_UpstreamSession":
    """Build a Cloudflare-clearing Udemy session reusing upstream's
    SSL/UA/header stack.  Returns the Session wrapper -- use ._get and
    ._post for HTTP, .visit('www') has already been called."""
    sess = _UpstreamSession()
    if bearer:
        sess._set_auth_headers(bearer)
    if not sess.visit("www"):
        raise RuntimeError(
            "Cloudflare preflight failed -- bearer may be expired or the "
            "curl_cffi fingerprint is no longer accepted.  Refresh "
            "config/bearer.txt and re-run."
        )
    return sess


def extract_portal_and_slug(course_url: str) -> tuple[str, str]:
    """Lifted from main.py's UdemyAuth.extract_course_name. Returns
    ('www', '<slug-or-id>')."""
    m = re.search(
        r"(?i)(?://(?P<portal>.+?).udemy.com/(?:course(?:/draft)*/)?(?P<slug>[a-zA-Z0-9_-]+))",
        course_url,
    )
    if not m:
        raise ValueError(f"Could not parse Udemy course URL: {course_url}")
    return m.group("portal"), m.group("slug")


def resolve_course_id(sess: "_UpstreamSession", portal: str, slug_or_id: str) -> str:
    """If `slug_or_id` is already numeric, return it. Otherwise hit the
    course-by-slug detail endpoint."""
    if slug_or_id.isdigit():
        return slug_or_id
    url = f"https://www.udemy.com/api-2.0/courses/{slug_or_id}/?fields[course]=id"
    r = sess._get(url)
    if r.status_code != 200:
        raise ValueError(
            f"Could not resolve course '{slug_or_id}' (HTTP {r.status_code}): "
            f"{r.text[:200]!r}"
        )
    cid = r.json().get("id")
    if not cid:
        raise ValueError(f"Course detail returned no id for '{slug_or_id}'")
    return str(cid)


def iter_curriculum(sess: "_UpstreamSession", portal: str, course_id: str) -> Iterable[dict]:
    """Walks the paginated /subscriber-curriculum-items/ endpoint and
    yields each `{_class: "lecture", asset: {...}}` entry."""
    next_url = URLS.CURRICULUM_ITEMS.format(portal_name=portal, course_id=course_id)
    params: Optional[dict] = dict(CURRICULUM_ITEMS_PARAMS)
    while next_url:
        r = sess._get(next_url, data=params if "?" not in next_url else None)
        if r.status_code != 200:
            raise RuntimeError(
                f"curriculum-items HTTP {r.status_code}: {r.text[:200]!r}"
            )
        data = r.json()
        for item in data.get("results", []):
            if item.get("_class") == "lecture":
                yield item
        next_url = data.get("next")
        params = None  # only the first request takes the params


def fetch_fresh_lecture_asset(
    sess: "_UpstreamSession", course_id: str, lecture_id: str
) -> Optional[dict]:
    """Re-fetch a lecture's asset via the per-lecture endpoint that
    mints a fresh media_license_token JWT per call (the curriculum
    endpoint serves cached tokens that expire within minutes).  Returns
    the asset dict or None on failure."""
    url = (
        f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/"
        f"{course_id}/lectures/{lecture_id}/?fields[lecture]=asset"
        "&fields[asset]=asset_type,course_is_drmed,media_license_token,"
        "media_sources,stream_urls"
    )
    r = sess._get(url)
    if r.status_code != 200:
        logger.warning(
            f"lecture-detail HTTP {r.status_code} for lecture {lecture_id}"
        )
        return None
    return (r.json() or {}).get("asset") or None


# ---------------------------------------------------------------------
# DASH manifest parsing -- pull the Widevine PSSH out of the MPD's
# <ContentProtection> element.  Beats downloading a segment.
# ---------------------------------------------------------------------

_MPD_NS = {
    "mpd":  "urn:mpeg:dash:schema:mpd:2011",
    "cenc": "urn:mpeg:cenc:2013",
}


def fetch_mpd_pssh(sess: "_UpstreamSession", mpd_url: str) -> Optional[bytes]:
    """GET the MPD, return the base64-decoded Widevine PSSH box bytes
    from the first <cenc:pssh> element under a Widevine-scheme
    ContentProtection.  None if not found."""
    r = sess._get(mpd_url)
    if r.status_code != 200:
        logger.warning(f"MPD fetch HTTP {r.status_code}: {r.text[:200]!r}")
        return None
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        logger.warning(f"could not parse MPD at {mpd_url}: {e}")
        return None
    for cp in root.iter("{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
        scheme = cp.get("schemeIdUri", "").lower()
        if "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" not in scheme:
            continue
        pssh_el = cp.find("cenc:pssh", _MPD_NS)
        if pssh_el is None or not pssh_el.text:
            continue
        return base64.b64decode(pssh_el.text.strip())
    return None


def extract_dash_mpd_url(asset: dict) -> Optional[str]:
    """Walk an asset's `media_sources` list for the
    `application/dash+xml` entry's `src` (the MPD URL)."""
    for source in asset.get("media_sources") or []:
        if source.get("type") == "application/dash+xml":
            return source.get("src")
    return None


# ---------------------------------------------------------------------
# License exchange -- the Stage 3 of the playbook
# ---------------------------------------------------------------------

def fetch_service_cert(sess: "_UpstreamSession", url: str) -> Optional[bytes]:
    try:
        r = sess._get(url)
        if r.status_code != 200:
            logger.warning(f"service cert HTTP {r.status_code}")
            return None
        return r.content
    except Exception as e:
        logger.warning(f"service cert fetch failed: {e}")
        return None


def request_license(
    sess: "_UpstreamSession",
    cfg: Config,
    *,
    auth_token: str,
    challenge: bytes,
    referer: str,
) -> bytes:
    """POST a Widevine challenge to Udemy's license server.

    Udemy's endpoint (verified 2026-06-07):
        POST https://www.udemy.com/media-license-server/validate-auth-token
            ?drm_type=widevine&auth_token=<JWT>
        Content-Type: application/octet-stream
        Origin: https://www.udemy.com
        Referer: <lecture URL the player is on>
        body: raw Widevine challenge protobuf bytes
        -> 200, application/octet-stream, raw Widevine license bytes
    """
    if not cfg.license_url_tpl:
        raise RuntimeError("LICENSE_URL_TPL is empty -- pin it via Stage F.")
    url = cfg.license_url_tpl.format(auth_token=auth_token)
    r = sess._post(
        url,
        data=challenge,
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
            "Origin": "https://www.udemy.com",
            "Referer": referer,
        },
    )
    if r.status_code != 200:
        snippet = r.text[:200] if r.text else "<empty>"
        raise RuntimeError(f"license POST -> HTTP {r.status_code}: {snippet}")
    return r.content


def fetch_keys_for_pssh(
    cdm: Cdm,
    sess: "_UpstreamSession",
    cfg: Config,
    *,
    auth_token: str,
    referer: str,
    pssh_bytes: bytes,
) -> list[tuple[str, str]]:
    """Run one license exchange for one PSSH.  Returns [(kid_hex,
    key_hex), ...] of CONTENT keys."""
    sid = cdm.open()
    try:
        if cfg.service_cert_url:
            cert = fetch_service_cert(sess, cfg.service_cert_url)
            if cert:
                cdm.set_service_certificate(sid, cert)
        pssh_obj = PSSH(pssh_bytes)
        challenge = cdm.get_license_challenge(sid, pssh_obj)
        license_bytes = request_license(
            sess, cfg, auth_token=auth_token, challenge=challenge, referer=referer
        )
        cdm.parse_license(sid, license_bytes)
        out: list[tuple[str, str]] = []
        for k in cdm.get_keys(sid):
            if k.type == "CONTENT":
                out.append((k.kid.hex.lower(), k.key.hex()))
        return out
    finally:
        cdm.close(sid)


# ---------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------

def bulk_fetch_for_course(cfg: Config, course_url: str) -> int:
    """Enumerate the entire course, fetch all missing keys, return the
    count of new (KID, KEY) pairs written."""
    portal, slug_or_id = extract_portal_and_slug(course_url)
    cfg.portal_name = portal
    sess = _udemy_session(cfg.bearer)

    course_id = resolve_course_id(sess, portal, slug_or_id)
    logger.info(f"course_id resolved: {course_id} (portal={portal})")

    cdm = load_cdm(cfg.wvd_path)
    keys = load_keyfile(cfg.keys_file)
    initial_count = len(keys)
    new_count = 0
    drm_seen = 0
    nondrm_seen = 0

    for item in iter_curriculum(sess, portal, course_id):
        asset = item.get("asset") or {}
        asset_id = str(asset.get("id") or "")
        lecture_id = str(item.get("id") or "")
        if not asset_id or not lecture_id:
            continue
        if asset.get("asset_type") != "Video":
            continue
        # The curriculum endpoint caches its media_license_token JWTs
        # for several minutes, so they're usually expired by the time
        # we POST.  Re-fetch via the per-lecture detail endpoint
        # which mints a fresh JWT per call (same code path Udemy's
        # web player uses on play).
        if not asset.get("media_license_token"):
            nondrm_seen += 1
            continue
        fresh = fetch_fresh_lecture_asset(sess, course_id, lecture_id)
        auth_token = (fresh or {}).get("media_license_token") if fresh else None
        if not auth_token:
            logger.warning(f"asset {asset_id}: could not fetch fresh JWT")
            continue
        drm_seen += 1
        # Prefer the fresh asset's media_sources (mpd URL may also be
        # signed/short-lived).
        active_asset = fresh or asset
        mpd_url = extract_dash_mpd_url(active_asset)
        if not mpd_url:
            logger.warning(f"asset {asset_id}: DRM but no DASH manifest in media_sources")
            continue
        pssh = fetch_mpd_pssh(sess, mpd_url)
        if not pssh:
            logger.warning(f"asset {asset_id}: no Widevine PSSH in MPD")
            continue
        referer = (
            f"https://www.udemy.com/course/{slug_or_id if not slug_or_id.isdigit() else course_id}"
            f"/learn/lecture/{lecture_id}"
        )
        try:
            pairs = fetch_keys_for_pssh(
                cdm, sess, cfg,
                auth_token=auth_token,
                referer=referer,
                pssh_bytes=pssh,
            )
        except Exception as e:
            logger.error(f"asset {asset_id}: license exchange failed: {e}")
            continue
        for kid, key in pairs:
            if kid not in keys:
                keys[kid] = key
                new_count += 1
                logger.info(f"+ key for asset {asset_id} (lecture {lecture_id}): KID={kid}")
        save_keyfile(cfg.keys_file, keys)
        time.sleep(cfg.request_delay_sec)

    logger.info(
        f"bulk fetch done: DRM_lectures={drm_seen} non_DRM={nondrm_seen} "
        f"new_keys={new_count} total_keys={len(keys)} (was {initial_count})"
    )
    return new_count


def scan_out_dir(cfg: Config) -> int:
    """Walk cfg.out_root for already-downloaded encrypted MP4s, surface
    any KIDs missing from keyfile.json.  Cannot fetch keys without the
    course URL -- this is a diagnostic helper."""
    if not cfg.out_root.exists():
        logger.error(f"out_root does not exist: {cfg.out_root}")
        return 0
    keys = load_keyfile(cfg.keys_file)
    encrypted_glob = list(cfg.out_root.rglob("*.encrypted.mp4")) + list(cfg.out_root.rglob("*.drm.mp4"))
    logger.info(f"scanning {len(encrypted_glob)} encrypted files under {cfg.out_root}")
    missing = 0
    for mp4 in encrypted_glob:
        try:
            kid = extract_kid(str(mp4))
        except Exception as e:
            logger.warning(f"{mp4}: extract_kid failed: {e}")
            continue
        if not kid:
            continue
        if kid not in keys:
            missing += 1
            logger.warning(f"MISSING key for KID={kid} ({mp4.name})")
    if missing:
        logger.warning(
            f"{missing} files have unknown KIDs.  Re-run --bulk for the "
            "course they came from to populate keyfile.json."
        )
    return 0


def watch_loop(cfg: Config, args: argparse.Namespace) -> None:
    interval = args.watch_interval
    while True:
        try:
            if args.course_url:
                bulk_fetch_for_course(cfg, args.course_url)
            elif args.scan_out:
                scan_out_dir(cfg)
            else:
                logger.warning("--watch requires --course-url or --scan-out")
                return
        except KeyboardInterrupt:
            return
        except Exception:
            logger.exception("watch cycle failed; continuing")
        logger.info(f"sleeping {interval}s")
        time.sleep(interval)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--course-url", help="Full Udemy course URL.  Enables --bulk by default.")
    p.add_argument("--course-id", help="Numeric course id (skip URL parsing).")
    p.add_argument("--bulk", action="store_true", help="Bulk-fetch keys for the whole course.  Default when --course-url is given.")
    p.add_argument("--scan-out", action="store_true", help="Scan existing out_dir/*.encrypted.mp4 files and log missing keys.")
    p.add_argument("--watch", action="store_true", help="Polling mode.")
    p.add_argument("--watch-interval", type=int, default=300, help="Seconds between --watch cycles.")
    p.add_argument("--wvd", help="Path to .wvd file (default: $UDL_WVD_PATH or /cdm/widevine.wvd).")
    p.add_argument("--bearer", help="Udemy Bearer token (default: $UDEMY_BEARER or /app/config/bearer.txt).")
    p.add_argument("--keys-file", help="Path to keyfile.json (default: $UDL_KEYS_FILE).")
    p.add_argument("--out-root", help="Path to course-downloads root (default: $UDL_OUT_ROOT).")
    p.add_argument("--license-url", help="Override LICENSE_URL_TPL.  Use for Stage F testing.")
    p.add_argument("--cert-url", help="Override SERVICE_CERT_URL.")
    p.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY, help="Politeness delay between license POSTs.")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging(args.log_level)
    cfg = Config.from_env(args)

    if not cfg.bearer:
        logger.error(
            "No Udemy bearer token configured.  Set $UDEMY_BEARER, drop "
            "the token into config/bearer.txt, or pass --bearer."
        )
        return 2

    if args.watch:
        watch_loop(cfg, args)
        return 0

    if args.scan_out:
        scan_out_dir(cfg)
        return 0

    course_url = args.course_url
    if not course_url and args.course_id:
        course_url = f"https://www.udemy.com/course/{args.course_id}/"
    if not course_url:
        logger.error("--course-url (or --course-id) is required for bulk mode.")
        return 2

    bulk_fetch_for_course(cfg, course_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
