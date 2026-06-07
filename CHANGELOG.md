# udemy-downloader (cc3735 fork) -- bundle changelog

Per-bundle changelog.  Mirrors the family's `CHANGELOG.md` style: every
code change with non-trivial effect lands here, with a `Why:` line on
non-obvious fixes.

Upstream changes (merged in from `upstream/master`) are NOT mirrored
here -- see `git log upstream/master..HEAD` for the local diff.


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
