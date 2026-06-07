# DRM — Udemy Widevine pipeline, license URL anatomy, reverse-engineering log

Everything we learned about Udemy's DRM stack, how we reverse-engineered
it (Stage F of `Repos/WIDEVINE-DECRYPT-PLAYBOOK.md`), and how to run the
license exchange end to end.  If you're reading this cold, start at
**Overview** and go straight through.

> **Porting this to another DRM site?** This doc is Udemy-specific (real
> URLs, real JWT payloads, real failure modes).  For a site-agnostic
> playbook (Stage 0 CDM extraction → Stage 4 mp4decrypt, plus a porting
> checklist), see
> [`../../WIDEVINE-DECRYPT-PLAYBOOK.md`](../../WIDEVINE-DECRYPT-PLAYBOOK.md).
> The HA-ripper writeup at
> [`../../nsfw-rippers/hornyadventures-ripper-py/docs/DRM.md`](../../nsfw-rippers/hornyadventures-ripper-py/docs/DRM.md)
> is the sibling document for that bundle.

---

## Overview

Udemy delivers DRM-protected lectures as **CMAF DASH** (separate video
+ audio CMAF tracks) with **Widevine + PlayReady + FairPlay** CENC.
Each lecture has its own AES-128 content key.  Encrypted segments live
on `dash-enc-cdn77.udemycdn.com`; the license server lives on
`www.udemy.com/media-license-server/validate-auth-token`.

We don't have to talk to the underlying DRM provider directly — Udemy
proxies the license exchange through their own API and signs each
request with a short-lived JWT (`media_license_token`) that's
per-asset, per-user.  Our sidecar treats that JWT as the auth token
for the license POST.

Mental model:

```
Udemy curriculum API ──► Sidecar (Phase 1)
                          │
                          ├─ Re-fetch lecture detail → fresh media_license_token (JWT)
                          ├─ GET DASH manifest      → <cenc:pssh>
                          │
                          │  pywidevine + L3 .wvd (KeyDive-extracted)
                          │
                          └─ POST validate-auth-token?auth_token=<JWT>
                                ├─ body: Widevine challenge
                                └─ response: Widevine license

                          parse_license → CONTENT keys (KID:KEY)
                                          │
                          keyfile.json ───┘
                          │
upstream main.py (Phase 2)│
                          ├─ N_m3u8DL-RE / aria2c → encrypted .mp4 + .m4a
                          ├─ extract_kid(file)    → KID hex
                          ├─ keyfile.json[KID]    → KEY hex
                          └─ ffmpeg -decryption_key  → muxed clean .mp4
```

---

## What Udemy actually uses

| Layer | Component | Notes |
|---|---|---|
| API backend | `www.udemy.com/api-2.0/...` | Django; Bearer or session-cookie auth |
| CDN (encrypted) | `dash-enc-cdn77.udemycdn.com` | Token-signed CMAF segments + `.mpd` manifests |
| CDN (plain HLS) | `www.udemy.com/assets/<id>/files/...` + `hls-c.udemycdn.com` | Non-DRM courses; plain `.m3u8` + MPEG-TS |
| DRM provider | **BuyDRM KeyOS** | Identified via PSSH content data: protobuf payload contains the literal string `buydrmkeyos`.  Udemy proxies through `/media-license-server/`; we never talk to BuyDRM directly |
| License endpoint | `https://www.udemy.com/media-license-server/validate-auth-token` | Single endpoint for Widevine + PlayReady (via `?drm_type=` query parameter) |
| DRM systems advertised | Widevine + PlayReady + FairPlay | All three in the same MPD; we only handle Widevine |
| Container | CMAF (fragmented MP4) | `ftyp` + `moov` (with `tenc` + `pssh`) + `sidx` + `moof`/`mdat` |
| Encryption scheme | `cenc` | AES-128-CTR full-sample encryption |
| Browser player | shaka-player (4.4.x) | Stripped fingerprint in `udemy-recon3.har` |

---

## License endpoint anatomy (pinned 2026-06-07)

```
POST https://www.udemy.com/media-license-server/validate-auth-token
     ?drm_type=widevine
     &auth_token=<per-asset JWT>

Headers:
  Content-Type: application/octet-stream
  Accept:       */*
  Origin:       https://www.udemy.com
  Referer:      https://www.udemy.com/course/<slug>/learn/lecture/<lecture_id>

Body:
  raw Widevine challenge bytes  (protobuf; starts with 0x08 0x01 0x12 …)

Response:
  200, application/octet-stream, raw Widevine license bytes  (~615 bytes typical)
```

The JWT in `auth_token=` is the **only** per-user authentication on this
endpoint — there's no `Authorization: Bearer` header on the license
POST.  All you need is:

1. A fresh JWT (extracted from the lecture-detail endpoint, see below),
2. A CF-cleared session (curl_cffi chrome120 + visit() preflight),
3. A Widevine challenge built from the lecture's PSSH.

---

## The `media_license_token` JWT

ES256-signed (`{"alg":"ES256","typ":"JWT"}`).  Payload claims:

```
{
  "course_id":   <int>,
  "user_id":     <int>,
  "user_agent":  "<browser UA string>",
  "iat":         <unix epoch>,
  "exp":         <unix epoch>          (~5-15 minutes after iat)
}
```

**Where you get a JWT**:

- **CACHED** (don't use): `/api-2.0/courses/<id>/subscriber-curriculum-items/?fields[asset]=...,media_license_token`
  — included in every asset, but this endpoint sends `caching_intent: True` and Udemy caches the response per-CDN-edge.  The JWTs you get out are usually minutes old → expire mid-batch.
- **FRESH** (use this): `/api-2.0/users/me/subscribed-courses/<course_id>/lectures/<lecture_id>/?fields[lecture]=asset&fields[asset]=media_license_token`
  — mints a new JWT per call.  This is what Udemy's web player does on every play.

The sidecar's `fetch_fresh_lecture_asset` (see `scripts/get_udemy_keys.py`) does the fresh fetch for every lecture immediately before the license POST.

---

## Why curl_cffi + chrome120

Bare `requests` hits Cloudflare 403 on every `udemy.com` call.  Even
with `--remote-allow-origins=*` on the test Chrome instance, the issue
is the TLS fingerprint — Cloudflare flags Python's default cipher
ordering + ALPN preferences as bot traffic.

Upstream `main.py` already solved this with [`curl_cffi`](https://github.com/yifeikong/curl_cffi)
configured to impersonate Chrome 120 (`Session.__init__` uses
`requests2.Session(impersonate="chrome120")`).  The sidecar imports
upstream's `Session` class directly:

```python
from main import Session as _UpstreamSession
sess = _UpstreamSession()
sess._set_auth_headers(bearer)
sess.visit("www")                # /api-2.0/visits/current/?... → CF cookies
```

The `visit("www")` preflight is critical — it hits
`/api-2.0/visits/current/` (a lightweight endpoint that always
returns JSON) and lets CF set its clearance cookies for the session.
Without it, subsequent API calls return CF challenge HTML.

---

## How we figured all of this out (Stage F log)

Following the playbook's "Stage 1 reconnaissance" workflow:

1. **First HAR** (`config/udemy-recon.har`, 64 MB) — captured while
   playing **a non-DRM course** by accident.  Zero `application/octet-stream`
   POSTs, only `.m3u8` HLS.  False start, but valuable: confirmed how
   curriculum + asset endpoints are shaped, and that the bearer is
   stripped from Chrome HAR exports (so we couldn't lift the token
   directly).
2. **Second HAR** (`config/udemy-recon2.har`, 7.8 MB) — also non-DRM.
   Confirmed `course_is_drmed: false` for `the-ai-engineer-course-complete-ai-engineer-bootcamp`.
   Built `scripts/check_drm.py` to triage DRM status without re-recording HARs.
3. **Third HAR** (`config/udemy-recon3.har`, 20 MB) — DRM course
   (`beginners-guide-to-technical-analysis`).  Found:
   - DASH manifest at `dash-enc-cdn77.udemycdn.com/cmaf/<asset_id>/...stream.mpd`
   - Two POSTs to `media-license-server/validate-auth-token` — first a
     2-byte preflight, then a 4263-byte real challenge
   - The real challenge body starts with `\x08\x01\x12\x89` — Widevine
     protobuf wire format
   - The JWT in `auth_token=` decoded to show `course_id`, `user_id`,
     `user_agent`, `iat`, `exp` claims
   - Response was 200, 615 bytes, `application/octet-stream` — the raw
     Widevine license

`scripts/parse_recon_har.py` is the parser we built to grovel through
HARs; it scans for license POSTs, octet-stream POSTs, DRM-hint URLs,
DASH manifest URLs, and the `course_is_drmed` flag in lecture details.
Keep it around — it's the tool you'd use to revalidate the pinned URL
if Udemy ever changes the endpoint.

---

## Three failures during Stage F (and the fixes)

### 1. CF 403 on the license POST
**Symptom**: First `bulk_fetch_for_course` run errored on `_get(...)` calls — Cloudflare returned a challenge HTML.

**Diagnosis**: Sidecar was using bare `requests`.

**Fix**: Imported `Session` from upstream `main.py` (curl_cffi + chrome120 + `visit()` preflight).  See `_udemy_session()` in `scripts/get_udemy_keys.py`.

### 2. "Token expired" on every license POST
**Symptom**: All 50 license POSTs returned `401: {"error":"Unauthorized","message":"Token expired","statusCode":401}` despite minting JWTs fresh inside the same script.

**Diagnosis**: We were reading `media_license_token` from the curriculum-items response, which is cached.

**Fix**: Added `fetch_fresh_lecture_asset(sess, course_id, lecture_id)` and call it immediately before each license POST.

### 3. `keyfile.json` save crashed
**Symptom**: After the first successful key landed, `save_keyfile` raised `OSError: [Errno 16] Device or resource busy: '/app/keyfile.json.tmp' -> '/app/keyfile.json'`.

**Diagnosis**: `keyfile.json` is bind-mounted from the host into the container.  Linux refuses `rename(2)` over a bind mount.

**Fix**: Direct `path.write_text(json.dumps(...))`.  Brief race window is acceptable; the sidecar is the only writer.

---

## Diagnostic recipes

### Decode a `media_license_token` JWT

```python
import base64, json
jwt = "eyJhbGciOi…"
header, payload, sig = jwt.split(".")
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
print(json.loads(b64d(payload)))
# {'course_id': 66383, 'user_id': 20507830, 'user_agent': '...', 'iat': ..., 'exp': ...}
```

### Confirm a specific lecture is DRM

```powershell
udl-check-drm 'https://www.udemy.com/course/<slug>/'
# DRM column = True → media_license_token is present
```

Or inline:
```python
import requests
from main import Session
sess = Session(); sess._set_auth_headers(open("config/bearer.txt").read().strip()); sess.visit("www")
r = sess._get(f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{cid}/lectures/{lid}/?fields[asset]=course_is_drmed,media_license_token,media_sources")
print(r.json()["asset"]["course_is_drmed"])  # True / False
```

### Verify a (KID, KEY) pair decrypts a specific encrypted MP4

```bash
mp4decrypt --key <kid_hex>:<key_hex> <file>.encrypted.mp4 <file>.test.mp4
ffprobe <file>.test.mp4   # should report valid codec parameters
vlc <file>.test.mp4       # plays cleanly = correct pair
```

### Dump every URL Udemy hits while you click a lecture

```powershell
# Capture a fresh HAR via Chrome DevTools, save to config\udemy-recon-N.har
python scripts\parse_recon_har.py
# Will use the newest config\udemy-recon*.har by mtime.
```

---

## Cross-references

- [`WORKFLOW.md`](WORKFLOW.md) — the bundle's overall workflow + helper map
- [`BATCH.md`](BATCH.md) — multi-course rip recipe
- [`../scripts/get_udemy_keys.py`](../scripts/get_udemy_keys.py) — sidecar source (the doc above maps 1:1 to its functions)
- [`../scripts/parse_recon_har.py`](../scripts/parse_recon_har.py) — Stage F HAR parser (re-use to revalidate if Udemy changes the endpoint)
- [`../scripts/check_drm.py`](../scripts/check_drm.py) — DRM probe used by `udl-check-drm`
- [`../../WIDEVINE-DECRYPT-PLAYBOOK.md`](../../WIDEVINE-DECRYPT-PLAYBOOK.md) — site-agnostic Widevine playbook
- [`../../nsfw-rippers/hornyadventures-ripper-py/docs/DRM.md`](../../nsfw-rippers/hornyadventures-ripper-py/docs/DRM.md) — the sibling HA writeup
