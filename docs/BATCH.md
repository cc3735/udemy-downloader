# Batch — ripping many courses sequentially

The `udl-rip-batch` helper lets you queue an arbitrary number of Udemy
course URLs and have them ripped one-after-another with no manual
shepherding.  Designed for "set it up, walk away" sessions over many
hours.

> For the single-command rip flow, see [`WORKFLOW.md`](WORKFLOW.md).
> For the underlying DRM mechanics, see [`DRM.md`](DRM.md).

---

## Why sequential, not parallel

Two reasons:

1. **Politeness toward Udemy.**  Each course's Phase 2 already uses
   `-cd 1` (one segment at a time) by default.  Adding parallel
   *courses* on top would multiply the load on Udemy's CDNs + license
   server.  We want to look like one user binge-watching, not a fleet.
2. **CDM serialization.**  The Widevine L3 CDM has a single open
   `Session` per process.  Phase 1's `cdm.open() / get_keys() / close()`
   is fast (~1s per lecture) but doesn't multi-thread cleanly.  Serial
   batch keeps one ripper container alive at a time.

If you ever want to break that rule (e.g. you want to fan out across
multiple CDMs on different machines), `udl-rip-batch` is just a thin
PS loop — easy to fork or invoke `udl-rip-bg` directly in a custom
script.

---

## Usage

### Multi-URL inline
```powershell
udl-rip-batch `
  'https://www.udemy.com/course/foo/' `
  'https://www.udemy.com/course/bar/' `
  'https://www.udemy.com/course/baz/'
```

### URL file
```powershell
# urls.txt — one URL per line, # for comments, blank lines OK
udl-rip-batch -File C:\path\to\urls.txt
```

Format:
```text
# Math foundations
https://www.udemy.com/course/calculus1/
https://www.udemy.com/course/calculus-2/
https://www.udemy.com/course/calculus-3/

# AWS
https://www.udemy.com/course/aws-certified-solutions-architect-associate-saa-c03/
```

### Dry-run (DRM check only, no rip)
```powershell
udl-rip-batch -DryRun 'https://...' 'https://...'
# Per-URL: prints DRM status + media-source types.  No rip.  Useful
# before committing to a long batch.
```

### Custom concurrency
```powershell
udl-rip-batch -ConcurrentDownloads 2 -File urls.txt
# -cd 2 inside each course's main.py.  Default is -cd 1.
```

### Skip already-downloaded courses
Idempotent by design.  If `J:\Knowledge\udemy\<slug>\` exists and contains
at least one `.mp4`, the batch logs `SKIP (already ripped): <slug>` and
moves on.  Re-running the same batch is safe.

---

## Recovery from a partial batch

The batch is sequential — if course N fails (e.g. transient `udl-rip`
error), courses N+1…end still get attempted.  To re-try the failed
course later:

```powershell
udl-rip 'https://...'                   # single-course rerun, foreground
```

To resume the batch from a specific point (skipping fully-downloaded
courses, picking up at the first incomplete one):

```powershell
udl-rip-batch -File urls.txt            # same batch, the SKIP logic
                                        # will fast-forward through
                                        # completed entries
```

If a course is partially downloaded (some lectures present, some
missing), `udl-rip` resumes at the next missing lecture — upstream
`main.py` checks for existing files per lecture before downloading.

---

## Disk-cap discipline

Each batch run can land **dozens of GB**.  Some rules of thumb (based
on the 2026-06-07 pilot):

- **Average per-lecture (1080p, DRM)**: ~150 MB
- **Average per-lecture (1080p, non-DRM HLS)**: ~80-200 MB (more variance)
- **Pilot baseline**: 50 lectures = 7.7 GB
- **Watch out**: courses with 500+ lectures (e.g. `the-ai-engineer-course-complete-ai-engineer-bootcamp` has 672) can hit 50-100 GB.

Before committing to a big batch:

```powershell
udl-rip-batch -DryRun -File urls.txt    # confirm DRM mix + that all URLs resolve
udl-list                                # eyeball any 0%-progress entries
Get-PSDrive J | Select Used, Free       # confirm room
```

---

## The 2026-06-07 11-course batch (audit trail)

Initial state: only `beginners-guide-to-technical-analysis` (the
pilot) was on disk.  Batch list:

```
aws-certified-solutions-architect-associate-saa-c03    (non-DRM)
calculus1                                              (non-DRM)
llm-engineering-master-ai-and-large-language-models    (DRM)
ai-influencer-make-money-online-with-social-media-fakes(DRM)
statistics-probability                                 (probed at batch time)
calculus-2                                             (probed at batch time)
calculus-3                                             (probed at batch time)
linear-algebra-course                                  (probed at batch time)
aws-certified-advanced-networking-specialty-ans        (probed at batch time)
google-certified-architect-developer-engineer-data-devops (probed at batch time)
the-ai-engineer-course-complete-ai-engineer-bootcamp   (non-DRM, 672 items — deferred to last)
```

Execution order: user-submitted order, with the 672-item AI Engineer
Bootcamp deferred to the END so it can be `udl-stop`'d cleanly if disk
cap becomes a concern without losing the other 10 courses.

Use this as a template for future big batches: smaller / fully-DRM
ones early, "monster" courses last.

---

## See also

- [`WORKFLOW.md`](WORKFLOW.md) — the bundle's overall pipeline
- [`DRM.md`](DRM.md) — how each DRM course's keys get acquired
- [`../scripts/UdemyDownloader.ps1`](../scripts/UdemyDownloader.ps1) — `Invoke-UdlRipBatch` source
- [`../../POWERSHELL-HELPERS.md`](../../POWERSHELL-HELPERS.md) — full helper inventory
