"""Probe Udemy for a specific course's actual ID and published_title.

Useful when upstream main.py's `_extract_course_info` fails with
'Failed to find the course' — the user-typed slug may not match the
real `published_title` Udemy stores in the subscription list.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as _udl_main  # type: ignore
_udl_main.logger = logging.getLogger("probe_course")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from main import Session  # type: ignore
from constants import URLS  # type: ignore


BEARER = Path("/app/config/bearer.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python probe_course.py <slug-or-url> [<slug-or-url> ...]")
        return 2
    sess = Session()
    sess._set_auth_headers(BEARER)
    if not sess.visit("www"):
        print("ERROR: CF preflight failed"); return 3

    # Pull every subscribed course title once
    r = sess._get("https://www.udemy.com/api-2.0/users/me/subscribed-courses/?fields[course]=id,url,title,published_title&page=1&page_size=500")
    if r.status_code != 200:
        print(f"subscribed-courses HTTP {r.status_code}: {r.text[:200]!r}")
        return 4
    sub = r.json().get("results", [])
    print(f"User has {len(sub)} subscribed courses\n")

    for raw in sys.argv[1:]:
        # Extract slug from URL if given
        m = re.search(r'/course/([^/?]+)', raw)
        slug = m.group(1) if m else raw
        print(f"=== Slug entered: '{slug}' ===")

        # 1. Direct course-by-slug
        url = f"https://www.udemy.com/api-2.0/courses/{slug}/?fields[course]=id,title,published_title,url"
        r1 = sess._get(url)
        if r1.status_code == 200:
            d = r1.json()
            print(f"  Direct API:  id={d.get('id')}  title={d.get('title')!r}")
            print(f"               published_title={d.get('published_title')!r}")
            print(f"               url={d.get('url')!r}")
        else:
            print(f"  Direct API HTTP {r1.status_code}: {r1.text[:120]!r}")

        # 2. Subscribed-courses match (the test main.py uses)
        match = None
        for c in sub:
            if c.get("published_title") == slug:
                match = c; break
        if match:
            print(f"  In sub list: id={match.get('id')}  title={match.get('title')!r}  ✓ MATCH")
        else:
            # Approximate match by published_title contains
            candidates = [c for c in sub if slug.lower() in (c.get("published_title") or "").lower()
                           or slug.lower() in (c.get("title") or "").lower()]
            if candidates:
                print(f"  In sub list: NO exact match.  Closest:")
                for c in candidates[:5]:
                    print(f"    id={c.get('id')}  published_title={c.get('published_title')!r}  title={c.get('title')!r}")
            else:
                print(f"  In sub list: NO matches by 'contains'.")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
