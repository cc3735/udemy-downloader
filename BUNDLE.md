# udemy-downloader — bundle README (cc3735 fork)

This fork extends [Puyodead1/udemy-downloader](https://github.com/Puyodead1/udemy-downloader) with:

1. A **Dockerized runtime** that mounts the user's existing Widevine L3
   CDM (the same `.wvd` that `nsfw-rippers/hornyadventures-ripper-py`
   uses).
2. A **sidecar key fetcher** (`scripts/get_udemy_keys.py`) that performs
   the Stage 3 license exchange from
   [`Repos/WIDEVINE-DECRYPT-PLAYBOOK.md`](../WIDEVINE-DECRYPT-PLAYBOOK.md)
   and populates `keyfile.json` for you.  Upstream expects you to do
   this manually; the sidecar lets `udl-rip <course-url>` be a single
   end-to-end command.
3. **PowerShell helpers** (`udl-*`, 13 aliases) that auto-load in PS5 + PS7 per
   [`Repos/POWERSHELL-HELPERS.md`](../POWERSHELL-HELPERS.md).
4. A **sequential batch driver** (`udl-rip-batch`) that walks many courses
   one after another, skipping ones already on disk.

Upstream README is at [`README.md`](README.md); it covers the original
CLI surface.  This file documents the bundle-specific bits.

> **Deep dives** (start here for cold-pickup):
> - [`docs/WORKFLOW.md`](docs/WORKFLOW.md) — the master narrative: file
>   locations, two-phase pipeline, every helper's purpose, common pitfalls
> - [`docs/DRM.md`](docs/DRM.md) — Widevine reverse-engineering history,
>   license URL anatomy, the JWT freshness gotcha
> - [`docs/BATCH.md`](docs/BATCH.md) — multi-course rip recipe

---

## Single-command flow

```powershell
udl-rip 'https://www.udemy.com/course/<slug>/'
```

What that does, under the hood:

1. **Phase 1 — bulk key fetch.**  `get_udemy_keys.py --bulk` enumerates
   every encrypted lecture in the course via Udemy's
   `/api-2.0/courses/{id}/subscriber-curriculum-items/` endpoint, fetches
   each DASH manifest, parses the Widevine PSSH, drives the CDM at
   `/cdm/widevine.wvd` to mint a license challenge, POSTs it to Udemy's
   Widevine license endpoint, and appends each resulting `(KID, KEY)`
   pair to `keyfile.json`.
2. **Phase 2 — download + decrypt.**  Upstream `python main.py -c <url>`
   downloads every lecture's video + audio tracks via aria2c + yt-dlp,
   then muxes them with ffmpeg's `-decryption_key` (KID:KEY now present
   in `keyfile.json`).  Final files land under
   `J:\V\2026\udemy\<course-name>\<chapter>_<chapter-name>\<lecture>_<lecture-title>.mp4`.

You provide a URL.  Everything else is automatic.

---

## One-time setup

1. **Bearer token.**  Open Udemy in a browser, F12 → Network → any
   `api-2.0/...` request → copy the `Authorization: Bearer <token>` value
   (the part after "Bearer ").  Then either:

   ```powershell
   # Option A: file (recommended for autopilot use)
   Set-Content -Path config\bearer.txt -Value '<your-token-here>' -NoNewline

   # Option B: env var (set in config\.env)
   # UDEMY_BEARER=<your-token-here>
   ```

2. **CDM mount.**  The default `docker-compose.yml` mounts the user's
   existing HA-ripper `.wvd` at `C:\Users\023du\Google\sdk_gphone64_x86_64\28926\1971036799\google_sdk_gphone64_x86_64_17.0.0_e12384ad_28926_l3.wvd`.
   If you've moved the CDM, set `UDL_WVD_PATH_HOST` in `config\.env`.

3. **License endpoint reconnaissance.**  The sidecar ships with
   `LICENSE_URL_TPL = None`.  You need to fill it in once (see
   "License URL reconnaissance" below).

4. **Build:**

   ```powershell
   udl-start         # docker compose up -d --build
   udl-auth-check    # confirms CDM mount + pywidevine can load it + bearer is present
   ```

---

## License URL reconnaissance (one-time)

Required because the sidecar can't auto-derive Udemy's Widevine license
POST URL.

Easiest path (uses the `chrome-devtools` MCP per your global
`CLAUDE.md`):

1. `chrome-debug-start`
2. Log into Udemy in the Chrome window that opened.
3. Navigate to a DRM-protected course you own; press play on any lecture.
4. F12 → Network tab → filter `widevine` / `license` / `drm` /
   `media_license`.
5. Look for a `POST` request with `Content-Type: application/octet-stream`
   and a ~2-3 KB binary request body.  That's the Widevine challenge.
6. Right-click → Copy URL.  This is your `LICENSE_URL_TPL`.
7. Copy the request headers too (especially anything `udemy-*` or
   `x-*`).  If the header set differs from what
   `scripts/get_udemy_keys.py:_udemy_session` already provides, add the
   missing ones.

Pin the values into `scripts/get_udemy_keys.py`:

```python
LICENSE_URL_TPL = "https://www.udemy.com/api-2.0/.../widevine-license/{asset_id}/"
SERVICE_CERT_URL = None   # set only if Udemy fetches a cert.do or similar first
```

You can test without editing the source by passing
`--license-url '<url>'` to `udl-keys`.

---

## Output layout

```
J:\V\2026\udemy\
  <course-name-or-id>\
    1_introduction\
      1_welcome.mp4
      2_what-youll-learn.mp4
      ...
    2_setup\
      ...
```

This is upstream's native chapter/lecture naming convention.  Pass
`-IdAsCourseName` to `udl-rip` if you want `<course-id>/` instead of
`<course-name>/` (shorter paths, useful for large courses with long
titles).

---

## Commands

| Command | What it does |
|---|---|
| `udl-start` | `docker compose up -d --build` |
| `udl-stop` | `docker compose down` |
| `udl-logs` | Follow container logs |
| `udl-status` | Container running? keys cached? bearer present? output dir count? |
| `udl-auth-check` | CDM mount intact + pywidevine can load it + bearer reaches the container |
| `udl-rip <url>` | **The main command.**  Bulk-fetch keys → download + decrypt the whole course. |
| `udl-keys ...` | Run the sidecar directly (passes args through to `get_udemy_keys.py`).  Useful for debugging, `--scan-out`, or `--watch` mode. |

---

## Troubleshooting

**`udl-rip` Phase 1 errors with "LICENSE_URL_TPL is not configured"** —
you haven't done the Stage F reconnaissance yet.  See the section
above; pin the URL into `scripts/get_udemy_keys.py` (or pass
`--license-url` to `udl-keys` for a one-off test).

**`udl-auth-check` says `CDM_MOUNT_MISSING`** — the `.wvd` path on the
host doesn't exist.  Either:
- The CDM was moved (set `UDL_WVD_PATH_HOST` in `config\.env`).
- The HA-ripper was never set up on this machine (you'd need to
  extract a CDM first per
  [`Repos/WIDEVINE-DECRYPT-PLAYBOOK.md`](../WIDEVINE-DECRYPT-PLAYBOOK.md)
  Stage 0).

**`udl-auth-check` says `NO_BEARER`** — drop your token into
`config\bearer.txt` (no quotes, no newline) or set `UDEMY_BEARER=...`
in `config\.env`.

**License POST returns HTTP 401** — bearer is stale; grab a fresh one
from Udemy DevTools (Bearer tokens rotate; check token expiry).

**License POST returns HTTP 403 with empty body** — bearer is valid
but the user doesn't have authorization for this asset (subscription
tier, geo, paywall).  Confirm you can play the lecture in the browser
first.

**`pywidevine` returns no CONTENT keys** — the license response was
valid but the keys are SIGNING-only.  This usually means Udemy enforces
L1 on that specific course (most are L3-compatible, but some
partner-restricted ones aren't).  Not fixable without a real rooted
phone.

**ffmpeg muxes successfully but VLC shows green tiles** — wrong key.
Re-check: the KID `extract_kid()` returned matches the KID stored in
keyfile.json.  Most often this means the bulk-fetch took a key from a
different track and the mux step picked the wrong KID-key pairing.

---

## Cross-references

- Family-wide Widevine playbook: [`Repos/WIDEVINE-DECRYPT-PLAYBOOK.md`](../WIDEVINE-DECRYPT-PLAYBOOK.md)
- HA-ripper reference implementation (the sidecar mirrors this):
  [`nsfw-rippers/hornyadventures-ripper-py/scripts/decrypt_all.py`](../nsfw-rippers/hornyadventures-ripper-py/scripts/decrypt_all.py)
- PowerShell auto-loader convention: [`Repos/POWERSHELL-HELPERS.md`](../POWERSHELL-HELPERS.md)
