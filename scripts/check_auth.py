"""Auth probe for the Udemy downloader (udl).

Reads `config/bearer.txt`.  Tries JWT decode first (some Udemy bearer
tokens are JWTs with a usable `exp`); falls back to a file-mtime + 30-
day assumed TTL otherwise (Udemy session cookies tend to last ~30 days
when refreshed via the browser session, then need re-extraction via
`udl-bearer-from-cookies`).

Emits a single-line AuthStatus JSON on stdout.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
# udemy-downloader sits directly under Repos\, not under a category
# folder, so we go up one fewer level than the nsfw-rippers scripts do.
REPOS_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPOS_ROOT))

from _shared.auth import AuthStatus, decode_jwt_exp  # noqa: E402


BUNDLE = "udl"
BEARER_PATH = HERE.parents[1] / "config" / "bearer.txt"
ASSUMED_TTL_DAYS = 30


def main() -> int:
    status = AuthStatus(bundle=BUNDLE, ok=False, source="jwt-exp")
    if not BEARER_PATH.exists():
        status.note = "missing config/bearer.txt"
        status.emit()
        return 1
    token = BEARER_PATH.read_text(encoding="utf-8", errors="replace").strip()
    if not token:
        status.note = "bearer.txt is empty"
        status.emit()
        return 1

    exp = decode_jwt_exp(token)
    if exp is not None:
        now = int(time.time())
        status.expires_at = exp
        secs_left = exp - now
        if secs_left <= 0:
            status.note = "JWT expired"
            status.emit()
            return 1
        status.ok = True
        if secs_left < 600:
            status.note = "<10m left"
            status.emit()
            return 2
        status.emit()
        return 0

    # Opaque bearer -- fall back to mtime + assumed TTL.
    status.source = "file-mtime"
    mtime = int(BEARER_PATH.stat().st_mtime)
    assumed_exp = mtime + ASSUMED_TTL_DAYS * 86400
    secs_left = assumed_exp - int(time.time())
    age_days = (int(time.time()) - mtime) // 86400
    status.expires_at = assumed_exp
    if secs_left <= 0:
        status.note = f"opaque bearer: {age_days}d old; assumed TTL exceeded"
        status.emit()
        return 1
    status.ok = True
    status.note = f"opaque bearer: {age_days}d old (assumed {ASSUMED_TTL_DAYS}d TTL)"
    if secs_left < 86400:
        status.note += " -- under 24h left"
        status.emit()
        return 2
    status.emit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
