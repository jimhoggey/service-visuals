# Spec: batch downloads

**Status:** in progress. **Owner:** orchestrator. **Implementers:** backend
Sonnet agent, frontend Sonnet agent (disjoint files, below).

## Why

"Paste in a batch of URLs and it goes through and downloads them all
individually" — mostly MP3s. Today the tile does one link at a time, which
means babysitting twenty exports by hand.

One job walks the list; the queue already serialises jobs, so nothing else
in the app changes. Off by default: with BATCH unticked the tile behaves
exactly as it does today, including its single-line field.

## Owner's decisions (do not revisit)

- **3 at a time.** Not one, not six.
- **A jittered gap between items in a lane: 8–20 s, random per item.**
  A fixed interval is itself a machine signature; the jitter is the point.
- Per-item results, skip-already-downloaded, and retry-failed-only are in.
  A mid-batch stop button is **out**.

## Behaviour

A **BATCH** checkbox sits under FORMAT (`timer`-style `.check-row`,
`id="download-batch"`, default off). Hint: `Paste several links, one per
line — they download three at a time.`

| Batch off | Batch on |
|---|---|
| Today's single-line `#download-url`, unchanged. | `#download-url` hidden, a `<textarea id="download-urls">` in its place, 6 rows. |
| Button reads `DOWNLOAD`. | Button reads `DOWNLOAD 12` — the live count of valid links. `DOWNLOAD` when the box is empty. |
| Hint as today. | `12 links found.` / `Line 4 isn't a YouTube link.` (first offending line only) |

Blank lines ignored; duplicates removed silently (keep first occurrence);
surrounding whitespace stripped. The set caps at **50**.

While running, the status line reads `DOWNLOADING 7 OF 20…` and the bar
tracks completed items, not bytes. `SAVED` on completion as today.

The done panel gains a results list, newest task last, in input order:

```
17 saved, 2 failed, 1 already there

  ✓  Song One.mp3
  ✓  Song Two.mp3
  ↷  Song Three.mp3 — already in your exports folder
  ✗  youtu.be/xyz — That video is age-restricted, so it can't be downloaded.
```

A failed row shows the **link as pasted** and the same plain-English
sentence the single-link path shows. When at least one row failed, a
`RETRY FAILED (2)` button appears beside SHOW FILE; it re-submits a batch
containing only the failed links.

## API contract

`POST /api/render` `{"type": "download", "options": {...}}`:

```json
{"urls": ["https://youtu.be/a", "https://youtu.be/b"], "format": "mp3"}
```

- `validate_download_options` accepts **either** `url` (a string, exactly as
  today) **or** `urls` (a list). It always returns `{"urls": [...],
  "format": ...}` — one downstream shape, so the single-link path is just a
  one-element batch. A payload with neither → today's URL error.
- Each entry validated exactly as `url` is today (same host list, same
  500-char cap, same message). A bad entry in a list →
  *"Line {n} isn't a YouTube link — paste one YouTube link per line."*
  (n is 1-based over the **submitted list**, not the raw textarea.)
- More than 50 → *"A batch can hold up to 50 links at once."*
- Empty list → today's URL error.

## The job

`download_video(options, progress_cb)` returns a **dict** now:

```python
{"filename": "<basename of the last saved file, or None>",
 "items": [{"link": str,            # exactly as pasted
            "state": "saved" | "skipped" | "failed",
            "filename": str | None,
            "message": str | None,  # the operator sentence, failures only
            "reason": str | None,   # ERROR_REASONS word, failures only
            "seconds": float}]}     # that item's own wall time, 1 dp
```

- Order matches the submitted list even though work is concurrent.
- `BATCH_LANES = 3`, `BATCH_GAP = (8, 20)`. A lane sleeps
  `random.uniform(*BATCH_GAP)` **before every item except its first**.
  A one-item batch therefore never sleeps — the single-link path keeps
  today's timing exactly.
- Use `concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_LANES)`.
  `_run_once` is already a pure subprocess call; keep it that way.
- `ensure_tools` / `update_ytdlp` run **once** before the pool starts,
  never per item.
- Progress: `progress_cb(int(done * 100 / total))` as each item settles.
  When tools had to be fetched, keep today's 0–30 / 30–100 split.
- One failing item must never abort the batch.
- The 15 s / 45 s bot-check retry stays **per item**.

**Skip already downloaded.** Add `--no-overwrites`. Determine
**empirically** (run it twice against the same link) what yt-dlp prints on
the second run and how `--print after_move:filepath` behaves, then parse
that to mark the item `skipped` with its existing filename. Report exactly
what you observed. Do NOT use `--download-archive`: it records ids, so a
file the operator deleted would be skipped forever.

`jobs.py` must carry the extra through: if a renderer returns a dict, take
`filename` from it and expose the remaining keys in the job's `info()`.
A renderer returning a string behaves exactly as today (every other tile).

## Analytics

One `export` event **per saved item**, with that item's own `seconds` —
otherwise a 20-song batch would report as one 3-minute "render" and poison
`render_seconds`. One `download_failed` per failed item, `reason` as today.
Both gain `batch` = `"one"` when the batch held a single link, else
`"many"` — never the count, never a link, never a title.

Skipped items emit nothing.

## Files & ownership (edit ONLY your own files)

**Backend agent** — `downloader.py`, `validation.py`, `app.py`, `jobs.py`,
`scripts/smoke.py`, `README.md` (one clause).

Smoke stays **offline** — no network, no binaries, and it must not sleep:
(a) validation accepts `url` alone, `urls` alone, normalises both to
`urls`, rejects 51 links, a bad line (exact message, correct line number),
an empty list, and de-dupes/strips blanks; (b) `BATCH_LANES == 3` and
`BATCH_GAP` is a 2-tuple inside 5–30 s; (c) a batch of 3 with `_run_once`
monkeypatched returns items in submitted order with the right states, and
does NOT sleep for a one-item batch (patch `time.sleep` and assert it was
never called); (d) `jobs.py` passes a dict renderer's extra keys through to
`info()` and a string renderer still behaves as today; (e) the per-item
analytics: a fake batch result yields one `export` per saved item and one
`download_failed` per failure, each carrying `batch`.

**Frontend agent** — `static/index.html`, `static/js/download.js`,
`static/style.css`.
- The BATCH checkbox, the textarea, the live count on the button, the
  results list, and RETRY FAILED.
- `readDownload()` returns `urls` (always an array) and `batch`;
  `downloadPayload()` sends `urls`. Client-side validation mirrors the
  backend's messages verbatim.
- The results list comes from the job's `items` (poll response). Rows are
  `✓` saved / `↷` skipped / `✗` failed; escape everything (a video title
  is untrusted text — use `textContent`, never `innerHTML`).
- Keep the existing single-link behaviour byte-for-byte when BATCH is off,
  including the DOWNLOAD ANOTHER / lastDoneUrl reset.

## Do not

- No playlist expansion, no cancel button, no per-item progress bars.
- No cookies, no login, no other sites.
- Never put a link, title or filename in an analytics prop.
- Do not `git stash`/`reset`/`checkout`, do not commit, do not bump
  `version.py` or edit `whatsnew.py`. Never port 8765.

## Done means

`pyflakes` clean; `SERVICE_VISUALS_STATS=0 .venv/bin/python
scripts/smoke.py` green and still fast (no real sleeping);
`golden.py --check` unaffected. Orchestrator verifies a real 3-link MP3
batch through the UI, including one deliberately bad link and a re-run to
prove the skip.
