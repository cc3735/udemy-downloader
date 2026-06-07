"""List enrolled courses with progress + DRM annotation.

Walks /api-2.0/users/me/subscribed-courses/ then for each one with
progress > 0 checks the first lecture's `course_is_drmed` flag.
Output sorted by last_accessed_time descending so most-recent shows
first.

Use `--all` to include courses with 0% progress.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as _udl_main  # type: ignore
_udl_main.logger = logging.getLogger("udl_recon")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from main import Session  # type: ignore
from constants import URLS, CURRICULUM_ITEMS_PARAMS  # type: ignore

BEARER = Path("/app/config/bearer.txt").read_text(encoding="utf-8").strip()
INCLUDE_ALL = "--all" in sys.argv

ENROLL_URL = (
    "https://www.udemy.com/api-2.0/users/me/subscribed-courses/"
    "?fields[course]=id,url,title,published_title,completion_ratio,last_accessed_time"
    "&ordering=-last_accessed,-access_time&page=1&page_size=200"
)


def first_lecture_drm(sess: Session, course_id: str) -> bool | None:
    try:
        r = sess._get(URLS.CURRICULUM_ITEMS.format(portal_name="www", course_id=course_id),
                      data=CURRICULUM_ITEMS_PARAMS)
        if r.status_code != 200:
            return None
        for item in r.json().get("results", []):
            if item.get("_class") == "lecture":
                asset = item.get("asset") or {}
                if asset.get("asset_type") == "Video":
                    lid = item.get("id")
                    if not lid:
                        return None
                    url = (
                        f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}"
                        f"/lectures/{lid}/?fields[lecture]=asset"
                        "&fields[asset]=course_is_drmed"
                    )
                    rr = sess._get(url)
                    if rr.status_code == 200:
                        return bool((rr.json().get("asset") or {}).get("course_is_drmed"))
                    return None
    except Exception:
        return None
    return None


def main() -> int:
    sess = Session()
    sess._set_auth_headers(BEARER)
    if not sess.visit("www"):
        print("ERROR: CF preflight failed"); return 3

    r = sess._get(ENROLL_URL)
    if r.status_code != 200:
        print(f"ERROR: HTTP {r.status_code}"); return 4
    results = r.json().get("results", [])
    print(f"\nTotal enrolled: {len(results)}\n")

    rows = []
    for c in results:
        pct = c.get("completion_ratio") or 0
        if not INCLUDE_ALL and pct == 0:
            continue
        rows.append(c)

    print(f"{'%':>5}  {'DRM':<5}  {'Last Accessed':<22}  {'Course URL'}")
    print("-" * 130)
    for c in rows:
        pct = c.get("completion_ratio") or 0
        drmed = first_lecture_drm(sess, str(c.get("id")))
        drm_str = "🔒 YES" if drmed is True else ("no" if drmed is False else "??")
        last = (c.get("last_accessed_time") or "")[:19]
        url = c.get("url") or f"/course/{c.get('published_title')}/"
        full_url = f"https://www.udemy.com{url}"
        print(f"{pct:>4.1f}%  {drm_str:<5}  {last:<22}  {full_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
