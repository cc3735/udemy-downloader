# udemy-downloader (cc3735 fork) -- bundle changelog

Per-bundle changelog.  Mirrors the family's `CHANGELOG.md` style: every
code change with non-trivial effect lands here, with a `Why:` line on
non-obvious fixes.

Upstream changes (merged in from `upstream/master`) are NOT mirrored
here -- see `git log upstream/master..HEAD` for the local diff.


## 2026-06-07 -- Docs + batch helper

Long-form documentation + a sequential multi-course rip driver, after
the first DRM pilot (`beginners-guide-to-technical-analysis`, 50
lectures / 7.7 GB) landed cleanly end to end.

### NEW

- `docs/WORKFLOW.md` (NEW): the master narrative.  File locations
  (CDM, bearer, cookies, keyfile, output), two-phase pipeline diagram,
  the full `udl-*` command map with "when to reach for which", common
  pitfalls with symptom → diagnosis → fix.
  *Why:* future sessions need to pick up the project cold without
  re-reading chat history.
- `docs/DRM.md` (NEW): Udemy-specific Widevine deep-dive.  Pinned
  license endpoint anatomy
  (`https://www.udemy.com/media-license-server/validate-auth-token?drm_type=widevine&auth_token=<JWT>`),
  the per-asset JWT structure, why curl_cffi + chrome120 is required,
  the three Stage-F false starts (CF 403, "Token expired", EBUSY).
  Mirrors `nsfw-rippers/hornyadventures-ripper-py/docs/DRM.md` shape.
- `docs/BATCH.md` (NEW): `udl-rip-batch` usage + the 11-course batch
  audit trail.  Disk-cap discipline rules of thumb.
- `Invoke-UdlRipBatch` + `udl-rip-batch` alias in
  `scripts/UdemyDownloader.ps1`: sequential multi-course rip.  Skips
  courses whose output dir already has at least one `.mp4`.  Supports
  inline URLs, `-File <path>`, and `-DryRun` (DRM probe only).
  Brings helper count to 13.

### UPDATED

- `BUNDLE.md`: added a "Deep dives" callout pointing at the new
  `docs/` files.  Bumped helper count to 13.
- `Repos\POWERSHELL-HELPERS.md`: added `udl-rip-batch` to the `udl-*`
  inventory.
- `Repos\WIDEVINE-DECRYPT-PLAYBOOK.md`: appended Udemy as the second
  fully-worked DRM example under "Cross-references".


## 2026-06-02 -- Initial bundle ship

Forked `Puyodead1/udemy-downloader` to `cc3735/udemy-downloader` and
wrapped it with the family's Dockerized chassis so a single command
(`udl-rip <course-url>`) walks the whole course, fetches every key via
the user's existing Widevine L3 CDM, and lands decrypted MP4s under
`J:\V\2026\udemy\`.

### NEW

- `Dockerfile` (overwrites upstream): extends upstream's Python 3.12 +
  ffmpeg + shaka-packager image with `pywidevine`, `pycryptodome`,
  Bento4 `mp4decrypt`, and `libicu-dev`.
  *Why:* the sidecar needs pywidevine; mp4decrypt is kept as a verify
  fallback; libicu-dev is a defensive add for any N_m3u8DL-RE port.
- `docker-compose.yml` (overwrites upstream): bind-mounts the existing
  HA-ripper `.wvd` read-only at `/cdm/widevine.wvd`, makes
  `keyfile.json` `:rw` so the sidecar can populate it, lands output at
  `J:\V\2026\udemy\` to match the family convention.
- `scripts/get_udemy_keys.py` (NEW): sidecar key fetcher.  Three modes:
  `--bulk --course-url` (the one used by `udl-rip`), `--scan-out`
  (fallback for already-downloaded files), `--watch` (polling).
  Imports upstream's `constants.URLS`, `constants.HEADERS`,
  `utils.extract_kid` so endpoint knowledge stays in one place.
- `scripts/UdemyDownloader.ps1` (NEW): `udl-*` helpers (`udl-rip`,
  `udl-keys`, `udl-start`, `udl-stop`, `udl-logs`, `udl-status`,
  `udl-auth-check`).  Auto-loaded via `.ps-autoload` marker.
- `BUNDLE.md` (NEW): bundle-specific README (upstream `README.md`
  stays untouched for clean rebases against `upstream/master`).
- `.gitignore`: added `config/` + `saved/` (upstream already excluded
  `.env`, `keyfile.json`, `out_dir/`, `output/`).

### KNOWN UNFINISHED

- **Stage F: license URL not yet pinned.**  The sidecar ships with
  `LICENSE_URL_TPL = None` -- attempting `udl-rip` will surface a
  clear "license endpoint not configured" error.  Filling in this
  constant requires a one-time chrome-devtools-mcp reconnaissance pass
  (documented in `BUNDLE.md::License URL reconnaissance`).
