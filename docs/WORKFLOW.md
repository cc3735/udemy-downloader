# Workflow — udemy-downloader bundle

Everything you need to operate the bundle: where the moving parts live,
how the two-phase pipeline works, what the commands do, and how to
recover when something breaks.  Written so that a future session (or a
fresh AI agent) can pick up the project cold and be productive within
five minutes.

> For the **Widevine reverse-engineering history + license-URL
> anatomy**, see [`DRM.md`](DRM.md).
> For **multi-course batch usage**, see [`BATCH.md`](BATCH.md).
> For the **family-wide DRM playbook** (Stage 0 CDM extraction, etc.),
> see [`../../WIDEVINE-DECRYPT-PLAYBOOK.md`](../../WIDEVINE-DECRYPT-PLAYBOOK.md).

---

## 1. Mental model

The bundle wraps upstream [`Puyodead1/udemy-downloader`](https://github.com/Puyodead1/udemy-downloader)
with a Dockerized chassis + a Widevine sidecar.  One command,
`udl-rip <course-url>`, does the whole rip:

```
udl-rip <course-url>
  │
  ├── Phase 1 — scripts/get_udemy_keys.py --bulk
  │     ├─ curl_cffi Session with chrome120 impersonation
  │     │     └─ visit("www") → CF clearance cookies
  │     ├─ GET /api-2.0/courses/<slug>/                    → course_id
  │     ├─ GET /api-2.0/courses/<id>/subscriber-curriculum-items/
  │     │     └─ paginated; emits every lecture
  │     │
  │     │     for each lecture flagged DRM (has media_license_token):
  │     │     ├─ GET /api-2.0/users/me/subscribed-courses/<cid>/lectures/<lid>/
  │     │     │     └─ FRESH media_license_token (JWT)   ← curriculum's is cached!
  │     │     ├─ GET <mpd>                                → <ContentProtection>/<cenc:pssh>
  │     │     ├─ pywidevine.Cdm.from_device(.wvd)
  │     │     │     ├─ open() → session id
  │     │     │     ├─ get_license_challenge(PSSH)        → protobuf bytes
  │     │     │     ├─ POST media-license-server/validate-auth-token
  │     │     │     │       ?drm_type=widevine&auth_token=<JWT>
  │     │     │     │       Content-Type: application/octet-stream
  │     │     │     │       Origin: https://www.udemy.com
  │     │     │     │       Referer: https://www.udemy.com/course/<slug>/learn/lecture/<lid>
  │     │     │     │       body: <challenge>
  │     │     │     ├─ parse_license(<response>)          → license object
  │     │     │     └─ get_keys() → CONTENT keys (KID:KEY)
  │     │     └─ keyfile.json[KID_hex] = KEY_hex          (direct write — bind-mounted)
  │     │
  │     └─ "bulk fetch done: DRM_lectures=N non_DRM=M new_keys=K total_keys=T"
  │
  └── Phase 2 — upstream main.py -c <course-url> -b <bearer>
        ├─ N_m3u8DL-RE / aria2c fetches encrypted segments
        │     → <lecture>.encrypted.mp4 + <lecture>.encrypted.m4a (CMAF, tenc preserved)
        ├─ utils.extract_kid(<file>)                       → KID hex
        ├─ keys_dict = json.load(keyfile.json)
        ├─ video_key = keys_dict[video_kid]
        ├─ audio_key = keys_dict[audio_kid]
        └─ mux_process(... audio_key, video_key)
              └─ ffmpeg -decryption_key <key> -decryption_kid <kid>
                    → muxed clean .mp4 at
                       J:\Knowledge\udemy\<course>\<chapter>\<lecture>.mp4
```

Two independent containers per course (Phase 1 then Phase 2), one
shared on-disk `keyfile.json`, one shared `.wvd` mount.  Non-DRM
courses skip Phase 1's license POSTs entirely (Phase 1 still walks
the curriculum but emits 0 keys).

---

## 2. Where everything lives

| What | Host path | Container path | Notes |
|---|---|---|---|
| CDM `.wvd` | `C:\Users\023du\Google\sdk_gphone64_x86_64\28926\1971036799\google_sdk_gphone64_x86_64_17.0.0_e12384ad_28926_l3.wvd` | `/cdm/widevine.wvd` (`:ro`) | Same `.wvd` `nsfw-rippers/hornyadventures-ripper-py` uses.  L3, KeyDive-extracted, ~3.3 KB |
| Bearer token | `config\bearer.txt` | `/app/config/bearer.txt` | Plain text, **no trailing newline**.  87 chars typical |
| Browser cookies | `config\cookies.txt` | `/app/config/cookies.txt` | Netscape format.  Source of truth when running `udl-bearer-from-cookies` |
| Bundle env | `config\.env` | (compose-resolved) | Optional `UDEMY_BEARER=…` override; `UDL_OUT_HOST=…` to relocate output |
| Key cache | `keyfile.json` | `/app/keyfile.json` (`:rw`) | `{kid_hex: key_hex}` JSON, accumulates across rips |
| Output root | `J:\Knowledge\udemy\` | `/app/out_dir` (`:rw`) | Course tree lands here.  Override via `UDL_OUT_HOST` |
| Helpers | `scripts\UdemyDownloader.ps1` | n/a | Auto-loaded via `.ps-autoload` marker in `scripts\` |
| Compose | `docker-compose.yml` | n/a | The single place all mounts are declared |
| Sidecar | `scripts\get_udemy_keys.py` | `/app/scripts/get_udemy_keys.py` (`:ro` bind-mounted) | Hot-reloaded — edit, re-run, no rebuild |

**Output layout**:

```
J:\Knowledge\udemy\
  <course-published-title>\
    01 - <chapter title>\
      001 <lecture title>.mp4
      002 <lecture title>.mp4
      …
    02 - <chapter title>\
      …
```

Upstream's native naming convention; we don't override it.

---

## 3. The two phases in code

### Phase 1 — `scripts/get_udemy_keys.py`

Entry point: `bulk_fetch_for_course(cfg, course_url)`.

Key functions to know:

| Function | Role |
|---|---|
| `_udemy_session(bearer)` | Build a CF-clearing session (uses upstream's `Session` class from `main.py` for curl_cffi + chrome120) |
| `resolve_course_id(sess, portal, slug)` | Course slug → numeric ID via the course-detail endpoint |
| `iter_curriculum(sess, portal, course_id)` | Paginates `/subscriber-curriculum-items/`, yields lecture dicts |
| `fetch_fresh_lecture_asset(sess, course_id, lecture_id)` | Re-fetches a single lecture's asset — gets a **fresh** `media_license_token` JWT.  Critical: the curriculum endpoint caches its JWTs and they expire fast |
| `extract_dash_mpd_url(asset)` | Walks `media_sources` for the `application/dash+xml` URL |
| `fetch_mpd_pssh(sess, mpd_url)` | GETs the MPD, parses `<cenc:pssh>` under the Widevine `<ContentProtection>` |
| `request_license(sess, cfg, auth_token, challenge, referer)` | POSTs the challenge to `media-license-server/validate-auth-token?auth_token=<JWT>` |
| `fetch_keys_for_pssh(cdm, sess, cfg, ...)` | Wraps a full pywidevine session: challenge → POST → parse → keys |
| `load_keyfile(path)` / `save_keyfile(path, keys)` | JSON I/O.  `save` is a direct write (the bind-mounted file can't be atomically renamed; see Pitfalls) |

### Phase 2 — upstream `main.py`

Run via:
```
python main.py -c <course-url> -b <bearer> -cd 1
```

Key code paths (refs into `main.py`):
- `class Session` — curl_cffi-backed HTTP wrapper with `visit()` preflight (line ~1165)
- `class UdemyAuth.authenticate(bearer_token=...)` — sets `Authorization: Bearer <token>` + `X-Udemy-Authorization: Bearer <token>` (line ~1232)
- `_extract_media_sources` — pulls DASH `.mpd` URL from asset (line ~678)
- `_extract_mpd` — yt-dlp parses MPD and returns DASH formats list (line ~784)
- `extract_kid` (from `utils.py`) — reads the encrypted MP4 box, returns KID hex
- KID → `keyfile.json` lookup → ffmpeg `-decryption_key` mux (line ~1366–1410)

---

## 4. PS helper command surface

13 aliases, all auto-load via `.ps-autoload` marker in `scripts\` (works in
PS5.1 and PS7 identically per `Repos\POWERSHELL-HELPERS.md`).

### Rip
| Alias | Use when |
|---|---|
| `udl-rip <url>` | Primary command.  Single course, foreground, default `-ConcurrentDownloads 1` for politeness |
| `udl-rip-bg <url>` | Same but detached.  Pair with `udl-watch` / `udl-logs` |
| `udl-rip-batch <urls...>` / `-File <path>` | Sequential multi-course rip.  Skips already-downloaded courses |
| `udl-keys <args>` | Sidecar-only (`--bulk` / `--scan-out` / `--watch`).  For partial fixes or debugging |

### Discovery
| Alias | Use when |
|---|---|
| `udl-list` / `udl-list -All` | List enrolled courses with progress + DRM column |
| `udl-check-drm <urls...>` | Per-URL DRM verdict + media-source types |

### Observe
| Alias | Use when |
|---|---|
| `udl-status` | Snapshot: container state + key cache size + bearer present + per-course disk count |
| `udl-watch` | Live polling: file count + disk delta + last log line every 10s.  Ctrl+C to stop |
| `udl-logs` | Tail any running rip container's full log |

### Lifecycle / Auth
| Alias | Use when |
|---|---|
| `udl-start` | First run, or after Dockerfile/requirements change.  Just builds the image |
| `udl-stop` | Clean leftover containers (kill the batch mid-flight) |
| `udl-auth-check` | "Is the CDM still mounted?  Does pywidevine load it?  Does the bearer reach `/app`?" |
| `udl-bearer-from-cookies` | After dropping a fresh `config\cookies.txt`.  Extracts the `access_token` into `config\bearer.txt` |

---

## 5. Typical workflows

### Day-1 setup
```powershell
# Drop your CDM .wvd at the path in docker-compose.yml (or override
# via UDL_WVD_PATH_HOST in config\.env), then:

udl-start                            # build the image
udl-bearer-from-cookies              # extract token from cookies.txt
udl-auth-check                       # confirm CDM + bearer + pywidevine
udl-list                             # see your enrolled courses
udl-check-drm 'https://...'          # know what kind of course you're hitting
udl-rip 'https://...'                # rip one course end-to-end
```

### Refresh after the bearer expires
The Udemy access token rotates.  When `udl-rip` Phase 2 starts logging
`HTTP 401` / `Unauthorized`:
```powershell
# Re-export cookies.txt from Chrome (e.g. via a Netscape cookie exporter
# extension), drop it at config\cookies.txt, then:
udl-bearer-from-cookies
udl-auth-check
# … and re-run the failed udl-rip command.
```

### Watching a big rip from a separate window
```powershell
# Window A:
udl-rip-bg 'https://www.udemy.com/course/big-one/'

# Window B:
udl-watch                            # or `udl-status` for snapshots
```

### Recovering from a half-ripped course
The bundle is idempotent at the lecture level: an existing `<lecture>.mp4`
is skipped on rerun.  Just re-run the same `udl-rip <url>` and it
resumes.  For keys specifically:
```powershell
udl-keys --scan-out                  # find encrypted files with no key
udl-keys --course-url '<url>' --bulk # re-fetch keys, then re-run udl-rip
```

### Batch over many courses
See [`BATCH.md`](BATCH.md).

---

## 6. What we actually built (audit trail)

For posterity / re-entering this project cold:

1. **2026-06-02** — Forked `Puyodead1/udemy-downloader` → `cc3735/udemy-downloader`.  Stood up the Docker chassis matching the HA-ripper pattern.  Wrote the first cut of `scripts/get_udemy_keys.py` with `LICENSE_URL_TPL = None` pending Stage F.
2. **2026-06-07 (Stage F)** — Captured `config/udemy-recon3.har` while playing a DRM lecture (`beginners-guide-to-technical-analysis`).  Parser surfaced the license endpoint: `media-license-server/validate-auth-token?drm_type=widevine&auth_token=<JWT>`.  Pinned into `LICENSE_URL_TPL`.
3. **2026-06-07 (three follow-up fixes)**:
   - Switched the sidecar to use upstream's `Session` (curl_cffi + chrome120) instead of plain `requests` — fixed CF 403.
   - Re-fetched each lecture's `media_license_token` via the per-lecture endpoint instead of using the curriculum-cached one — fixed `401 Token expired`.
   - Direct-write `keyfile.json` instead of tmp+rename — fixed `EBUSY` on bind-mount.
4. **2026-06-07 (helper expansion)** — Added `udl-watch`, `udl-rip-bg`, `udl-bearer-from-cookies`, `udl-list`, `udl-check-drm`, plus the bearer-source shell wrap in `udl-rip` / `udl-rip-bg`.
5. **2026-06-07 (pilot)** — `beginners-guide-to-technical-analysis`: 50 DRM lectures / 7.7 GB / ~52 min wall-clock.  Cleanly muxed, no failures.

---

## 7. Common pitfalls (with diagnosis + fix)

### "Token expired" on every license POST
**Symptom**: Phase 1 logs `HTTP 401: {"error":"Unauthorized","message":"Token expired"}` for every DRM lecture.  Keys count stays at 0.

**Diagnosis**: Using `media_license_token` from `/subscriber-curriculum-items/`.  That endpoint caches its JWTs for several minutes — they're expired by the time we POST.

**Fix**: Already in the sidecar (`fetch_fresh_lecture_asset`).  If you ever rip the sidecar apart, make sure each DRM lecture's JWT comes from `/users/me/subscribed-courses/<cid>/lectures/<lid>/?fields[asset]=media_license_token` immediately before the POST.

### `EBUSY` when saving `keyfile.json`
**Symptom**: `OSError: [Errno 16] Device or resource busy: '/app/keyfile.json.tmp' -> '/app/keyfile.json'`

**Diagnosis**: `keyfile.json` is bind-mounted from the host.  Linux refuses `rename(2)` over a bind mount.

**Fix**: Use direct `path.write_text(...)` instead of tmp+rename.  Already in `save_keyfile`.

### Phase 2 errors `No bearer token was provided`
**Symptom**: `udl-rip` Phase 1 succeeds, Phase 2 immediately errors out with `authenticate: No bearer token was provided, and no browser for cookie extraction was specified.`

**Diagnosis**: upstream `main.py` does NOT auto-read `config\bearer.txt`.  It needs `-b <token>` or `$UDEMY_BEARER` or `--browser <name>`.

**Fix**: Both `udl-rip` and `udl-rip-bg` now wrap the `python main.py` call in `sh -c "BEARER=${UDEMY_BEARER:-$(cat /app/config/bearer.txt)} && python main.py … -b $BEARER"`.

### Cloudflare 403 on any plain `requests` call
**Symptom**: Sidecar's `_get(...)` returns 403 with a CF challenge HTML body.

**Diagnosis**: Bare `requests` has a vanilla TLS fingerprint that Cloudflare flags.  Even `--remote-allow-origins=*` doesn't help because CF blocks at the TLS layer, not by Origin.

**Fix**: Use upstream's `Session` class (curl_cffi.requests with `impersonate="chrome120"`).  Sidecar imports it via `from main import Session as _UpstreamSession`.

### Stage F failures
**Symptom**: HAR has zero POSTs matching `widevine` / `license` / `drm`; HAR has no `application/octet-stream` POST; only `.m3u8` manifest URLs (no `.mpd`).

**Diagnosis**: The captured course wasn't DRM-protected.  Some Udemy courses are plain HLS — `course_is_drmed: false` on the asset.

**Fix**: Capture a HAR from an actually-DRM course.  Verify with `udl-check-drm <url>` first.

### "No video lectures" / DRM `??` from `udl-list`
**Symptom**: A course shows `??` in the DRM column.

**Diagnosis**: Either the course was de-listed, the user lost access, or the first lecture isn't a video asset (e.g. a quiz).

**Fix**: Try `udl-check-drm <full-course-url>` directly — it returns more detail.  If it returns `(could not resolve)`, the course needs the auth header set or has been removed.

---

## 8. Audit / extend

To add a new DRM site to this same pipeline:

1. Capture a HAR of a DRM lecture playing.
2. Run `python scripts/parse_recon_har.py` against it — surfaces license POSTs + auth headers.
3. Pin `LICENSE_URL_TPL` + adjust `request_license` headers in `scripts/get_udemy_keys.py`.
4. If the new site uses a different DRM provider (PallyCon vs BuyDRM KeyOS vs Axinom), check whether a service certificate is needed and wire `SERVICE_CERT_URL` if so.
5. Adjust `bulk_fetch_for_course` to enumerate the new site's per-asset structure.

See [`DRM.md`](DRM.md) for the Udemy-specific details and
[`../../WIDEVINE-DECRYPT-PLAYBOOK.md`](../../WIDEVINE-DECRYPT-PLAYBOOK.md) for the
site-agnostic playbook.

---

## 9. Cross-references

- [`DRM.md`](DRM.md) — Udemy-specific reverse-engineering + license-URL anatomy
- [`BATCH.md`](BATCH.md) — `udl-rip-batch` recipe + the 11-course audit trail
- [`../BUNDLE.md`](../BUNDLE.md) — short quick-start
- [`../README.md`](../README.md) — upstream README, untouched for clean rebase
- [`../../WIDEVINE-DECRYPT-PLAYBOOK.md`](../../WIDEVINE-DECRYPT-PLAYBOOK.md) — the
  family-wide DRM playbook (Stage 0 CDM extraction lives here)
- [`../../POWERSHELL-HELPERS.md`](../../POWERSHELL-HELPERS.md) — full
  cross-family helper inventory
- [`../../nsfw-rippers/hornyadventures-ripper-py/scripts/decrypt_all.py`](../../nsfw-rippers/hornyadventures-ripper-py/scripts/decrypt_all.py) — the original reference implementation this sidecar is patterned after
