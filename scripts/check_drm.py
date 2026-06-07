"""DRM status check for a list of Udemy course URLs.

Reuses upstream's Session class (curl_cffi + chrome120 impersonation +
visit preflight to clear Cloudflare) so we don't reinvent the
fingerprint dance.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Bootstrap the module-level logger main.py uses (it's only initialized
# inside main.py's own startup path; importing Session bypasses that).
import main as _udl_main  # type: ignore
_udl_main.logger = logging.getLogger("udl_recon")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from main import Session  # type: ignore  -- upstream's curl_cffi-backed session
from constants import URLS, CURRICULUM_ITEMS_PARAMS  # type: ignore

BEARER = Path("/app/config/bearer.txt").read_text(encoding="utf-8").strip()
COURSES = [u.strip() for u in sys.argv[1:] if u.strip()]


def slug_from_url(url: str) -> str:
    m = re.search(r"/course/([^/?]+)", url)
    if not m:
        raise ValueError(f"Could not parse slug from URL: {url}")
    return m.group(1)


def resolve_course_id(sess: Session, slug: str) -> str | None:
    url = f"https://www.udemy.com/api-2.0/courses/{slug}/?fields[course]=id,title,published_title"
    r = sess._get(url)
    if r.status_code != 200:
        print(f"  (course-detail HTTP {r.status_code}: {r.text[:120]!r})", file=sys.stderr)
        return None
    try:
        return str(r.json().get("id"))
    except Exception:
        return None


def first_video_lecture_id(sess: Session, course_id: str) -> tuple[str | None, str | None]:
    url = URLS.CURRICULUM_ITEMS.format(portal_name="www", course_id=course_id)
    r = sess._get(url, data=CURRICULUM_ITEMS_PARAMS)
    if r.status_code != 200:
        return None, None
    for item in r.json().get("results", []):
        if item.get("_class") == "lecture":
            asset = item.get("asset") or {}
            if asset.get("asset_type") == "Video":
                return str(item.get("id")), item.get("title")
    return None, None


def lecture_drm(sess: Session, course_id: str, lecture_id: str) -> dict:
    url = (
        f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}"
        f"/lectures/{lecture_id}/?fields[lecture]=asset"
        "&fields[asset]=asset_type,course_is_drmed,media_license_token,media_sources,stream_urls"
    )
    r = sess._get(url)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:160]!r}"}
    doc = r.json()
    asset = doc.get("asset") or {}
    media_sources = asset.get("media_sources") or []
    types = sorted({(m.get("type") or "") for m in media_sources})
    return {
        "drmed": asset.get("course_is_drmed"),
        "media_license_token": bool(asset.get("media_license_token")),
        "stream_urls": bool(asset.get("stream_urls")),
        "media_sources_types": types,
    }


def main() -> int:
    if not BEARER:
        print("ERROR: bearer.txt is empty")
        return 2
    sess = Session()
    sess._set_auth_headers(BEARER)
    if not sess.visit("www"):
        print("ERROR: visit preflight failed (Cloudflare bot challenge or auth bad)")
        return 3

    print(f"{'Course':<70} {'DRM':<7} {'license_token':<14} {'stream_urls':<12} {'media_sources types'}")
    print("-" * 160)
    for url in COURSES:
        slug = slug_from_url(url)
        try:
            cid = resolve_course_id(sess, slug)
            if not cid:
                print(f"{slug[:70]:<70} (could not resolve course id)")
                continue
            lid, _ = first_video_lecture_id(sess, cid)
            if not lid:
                print(f"{slug[:70]:<70} (no video lectures)")
                continue
            info = lecture_drm(sess, cid, lid)
            if "error" in info:
                print(f"{slug[:70]:<70} {info['error']}")
                continue
            print(
                f"{slug[:70]:<70} "
                f"{str(info['drmed']):<7} "
                f"{str(info['media_license_token']):<14} "
                f"{str(info['stream_urls']):<12} "
                f"{info['media_sources_types']}"
            )
        except Exception as e:
            print(f"{slug[:70]:<70} ERROR: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
