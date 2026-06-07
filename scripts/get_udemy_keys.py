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
# Stage F constants -- fill these in via DevTools recon (see module
# docstring above).
# ---------------------------------------------------------------------

# Per-asset Widevine license POST endpoint.  The actual value depends on
# Udemy's player; capture via F12 Network while a DRM lecture plays.
LICENSE_URL_TPL: Optional[str] = None  # e.g. "https://www.udemy.com/api-2.0/.../widevine-license/{asset_id}/"

# Optional service certificate URL (some providers require setting a
# cert on the CDM before generating a challenge; many do not).  Leave
# None until you confirm Udemy needs one.
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(keys, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


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
# Udemy API enumeration -- reuses upstream's URLs + HEADERS
# ---------------------------------------------------------------------

def _udemy_session(bearer: str) -> requests.Session:
    sess = requests.Session()
    headers = dict(HEADERS)  # upstream Android-app headers
    if bearer:
        # Bearer auth slots into a separate header alongside upstream's
        # basic-auth "authorization" (Udemy accepts both; the bearer is
        # what proves user identity for license access).
        headers["x-udemy-bearer"] = bearer
        headers["authorization-bearer"] = f"Bearer {bearer}"
    sess.headers.update(headers)
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


def resolve_course_id(sess: requests.Session, portal: str, slug_or_id: str) -> str:
    """If `slug_or_id` is already numeric, return it. Otherwise look up
    the published_title -> id mapping via the user's subscribed-courses
    list."""
    if slug_or_id.isdigit():
        return slug_or_id
    url = URLS.COURSE_SEARCH.format(portal_name=portal, course_name=slug_or_id)
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    for entry in r.json().get("results", []):
        if entry.get("published_title") == slug_or_id:
            return str(entry["id"])
    raise ValueError(f"Course '{slug_or_id}' not found in user's subscribed courses")


def iter_curriculum(sess: requests.Session, portal: str, course_id: str) -> Iterable[dict]:
    """Walks the paginated /subscriber-curriculum-items/ endpoint and
    yields each `{_class: "lecture", asset: {...}}` entry."""
    next_url = URLS.CURRICULUM_ITEMS.format(portal_name=portal, course_id=course_id)
    params = dict(CURRICULUM_ITEMS_PARAMS)
    while next_url:
        r = sess.get(next_url, params=params if "?" not in next_url else None, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("results", []):
            if item.get("_class") == "lecture":
                yield item
        next_url = data.get("next")
        params = None  # only first request takes the params


# ---------------------------------------------------------------------
# DASH manifest parsing -- pull the Widevine PSSH out of the MPD's
# <ContentProtection> element.  Beats downloading a segment.
# ---------------------------------------------------------------------

_MPD_NS = {
    "mpd":  "urn:mpeg:dash:schema:mpd:2011",
    "cenc": "urn:mpeg:cenc:2013",
}


def fetch_mpd_pssh(sess: requests.Session, mpd_url: str) -> Optional[bytes]:
    """GET the MPD, return the base64-decoded Widevine PSSH box bytes
    from the first <cenc:pssh> element under a Widevine-scheme
    ContentProtection.  None if not found."""
    r = sess.get(mpd_url, timeout=30)
    r.raise_for_status()
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

def fetch_service_cert(sess: requests.Session, url: str) -> Optional[bytes]:
    try:
        r = sess.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        logger.warning(f"service cert fetch failed: {e}")
        return None


def request_license(
    sess: requests.Session,
    cfg: Config,
    asset_id: str,
    challenge: bytes,
) -> bytes:
    if not cfg.license_url_tpl:
        raise RuntimeError(
            "LICENSE_URL_TPL is not configured.  Run the Stage F "
            "reconnaissance (see scripts/get_udemy_keys.py docstring) "
            "and set the constant at the top of this file, OR pass "
            "--license-url '<URL>' explicitly."
        )
    url = cfg.license_url_tpl.format(asset_id=asset_id, course_id="")
    r = sess.post(
        url,
        data=challenge,
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream, application/json",
        },
        timeout=30,
    )
    if r.status_code != 200:
        snippet = r.text[:200] if r.text else "<empty>"
        raise RuntimeError(f"license POST -> HTTP {r.status_code}: {snippet}")
    # Some providers wrap the raw Widevine license bytes in JSON
    # (`{"license": "<b64>"}`).  Detect + unwrap.
    if r.headers.get("Content-Type", "").startswith("application/json"):
        try:
            doc = r.json()
            for k in ("license", "license_data", "wv_license", "result"):
                if k in doc and isinstance(doc[k], str):
                    return base64.b64decode(doc[k])
        except (json.JSONDecodeError, ValueError):
            pass
    return r.content


def fetch_keys_for_pssh(cdm: Cdm, sess: requests.Session, cfg: Config, asset_id: str, pssh_bytes: bytes) -> list[tuple[str, str]]:
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
        license_bytes = request_license(sess, cfg, asset_id, challenge)
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

    for item in iter_curriculum(sess, portal, course_id):
        asset = item.get("asset") or {}
        asset_id = str(asset.get("id") or "")
        if not asset_id:
            continue
        if asset.get("asset_type") != "Video":
            continue
        # Encrypted lectures expose media_sources (DASH); plain video
        # lectures expose stream_urls.  Skip the plain ones.
        if not asset.get("media_sources"):
            logger.debug(f"asset {asset_id} not DRM-protected -- skipping")
            continue
        mpd_url = extract_dash_mpd_url(asset)
        if not mpd_url:
            logger.warning(f"asset {asset_id}: no DASH manifest in media_sources")
            continue
        try:
            pssh = fetch_mpd_pssh(sess, mpd_url)
        except requests.RequestException as e:
            logger.warning(f"asset {asset_id}: MPD fetch failed: {e}")
            continue
        if not pssh:
            logger.warning(f"asset {asset_id}: no Widevine PSSH in MPD")
            continue
        try:
            pairs = fetch_keys_for_pssh(cdm, sess, cfg, asset_id, pssh)
        except Exception as e:
            logger.error(f"asset {asset_id}: license exchange failed: {e}")
            continue
        for kid, key in pairs:
            if kid not in keys:
                keys[kid] = key
                new_count += 1
                logger.info(f"+ key for asset {asset_id}: KID={kid}")
        save_keyfile(cfg.keys_file, keys)
        time.sleep(cfg.request_delay_sec)

    logger.info(
        f"bulk fetch done: {new_count} new keys, {len(keys)} total in keyfile "
        f"(was {initial_count})"
    )
    return new_count


def scan_out_dir(cfg: Config) -> int:
    """Walk cfg.out_root for *.drm.mp4 / *.encrypted.mp4 files whose KID
    isn't in keyfile.json yet, and fill the gaps.

    This mode skips the curriculum API entirely -- useful when keys are
    needed for an already-downloaded course that wasn't completed."""
    if not cfg.out_root.exists():
        logger.error(f"out_root does not exist: {cfg.out_root}")
        return 0
    sess = _udemy_session(cfg.bearer)
    cdm = load_cdm(cfg.wvd_path)
    keys = load_keyfile(cfg.keys_file)
    new_count = 0
    encrypted_glob = list(cfg.out_root.rglob("*.encrypted.mp4")) + list(cfg.out_root.rglob("*.drm.mp4"))
    logger.info(f"scanning {len(encrypted_glob)} encrypted files under {cfg.out_root}")
    for mp4 in encrypted_glob:
        try:
            kid = extract_kid(str(mp4))
        except Exception as e:
            logger.warning(f"{mp4}: extract_kid failed: {e}")
            continue
        if not kid or kid in keys:
            continue
        logger.warning(
            f"{mp4} has KID={kid} but the file-only path can't re-fetch a "
            "license without the asset_id + license URL.  Use --bulk for "
            "courses you can identify by URL."
        )
    save_keyfile(cfg.keys_file, keys)
    return new_count


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
