# Service Visuals — Design

**Date:** 2026-07-04
**Status:** Approved for implementation (autonomous session; decisions made per user's stated goal)

## Purpose

A localhost web app for a church tech volunteer to quickly generate simple motion
visuals and export them as MP4 files for import into ProPresenter. First two visuals:
a countdown **timer** and a decision **spinner wheel**. Priorities: mega functional,
fast to operate under pressure, dark modern UI, zero cloud dependencies.

## Decisions (with reasoning)

| Decision | Choice | Why |
|---|---|---|
| Backend | Python 3.9 + Flask | User asked for "mainly Python", localhost. Flask is the smallest thing that works. |
| Frame rendering | Pillow (PIL) | Pure-Python drawing, no native toolchain, deterministic. |
| MP4 encoding | Bundled static ffmpeg via `imageio-ffmpeg` pip package | **ffmpeg is not installed on this machine.** The pip package ships a static binary, so the app has zero system dependencies beyond Python. Frames are piped raw RGB → libx264. |
| Output format | H.264 MP4, `yuv420p`, 1920×1080, 30 fps output, `+faststart` | Maximum ProPresenter compatibility. |
| Frontend | Vanilla HTML/CSS/JS, single page, no build step | Offline-capable, trivially maintainable by a volunteer team. |
| Fonts (renders) | Helvetica Neue / Avenir Next / Menlo from macOS system paths, with fallback chain | Always present on macOS, no font downloads. Digits drawn in fixed-width slots to avoid jitter. |
| Job model | In-process queue, one render worker thread, polled via JSON API | Rendering is CPU-bound; serializing avoids thrash. Polling is simpler than SSE and good enough. |
| Port | 8765 (configurable via `PORT` env) | Port 5000 collides with macOS AirPlay Receiver. |

### Alternatives considered

1. **MoviePy / matplotlib animation** — heavier deps, slower, less control. Rejected.
2. **Browser-side rendering (canvas + MediaRecorder → webm)** — no server needed, but
   webm/vp9 is second-class in ProPresenter and H.264 encoding in-browser is unreliable.
   Rejected; MP4 via ffmpeg is the compatibility-safe path.
3. **Require Homebrew ffmpeg** — extra install step, breaks "clone and run". Rejected in
   favor of the bundled static binary.

## Architecture

```
app.py                # Flask app: routes, validation, wiring
jobs.py               # JobManager: queue, worker thread, progress, status
render/
  encoder.py          # FrameEncoder: pipes RGB frames to bundled ffmpeg → MP4
  fonts.py            # system font discovery + cached truetype loading
  timer.py            # render_timer(opts, encoder_factory, progress_cb) -> filename
  spinner.py          # render_spinner(opts, encoder_factory, progress_cb) -> filename
static/
  index.html          # single page: tile chooser → config panel → export/progress
  style.css
  app.js              # live canvas preview, form logic, render/poll/download
exports/              # output MP4s (gitignored)
run.sh                # creates venv if missing, installs deps, starts server
```

### Data flow

UI form → `POST /api/render {type, options}` → validate → job queued →
worker thread calls renderer → renderer draws frames (Pillow) → `FrameEncoder`
pipes them to ffmpeg → MP4 lands in `exports/` → UI polls `GET /api/jobs/<id>`
(progress %) → done → UI shows Download + Reveal in Finder buttons.

## API contract

- `GET /` → UI.
- `POST /api/render` body `{"type": "timer"|"spinner", "options": {...}}` →
  `202 {"job_id": "..."}` or `400 {"error": "human-readable message"}`.
- `GET /api/jobs/<id>` → `{"status": "queued"|"rendering"|"done"|"error",
  "progress": 0-100, "filename": str|null, "error": str|null, "queue_position": int}`.
- `GET /exports/<filename>` → the MP4 (download).
- `POST /api/reveal` body `{"filename": "..."}` → runs `open -R exports/<filename>`
  (macOS Finder reveal; validated against directory traversal).

### Timer options

```json
{
  "minutes": 5, "seconds": 0,        // total 5s .. 120min
  "style": "classic"|"ring"|"bar",
  "accent": "#e8b44f",               // hex color for ring/bar/accents
  "warn_last10": true,               // digits shift to accent color in final 10s
  "hold_seconds": 5                  // hold at 00:00 at the end (0..30)
}
```

- **classic**: huge centered digits, subtle vignette background.
- **ring**: digits inside a circular progress ring that depletes clockwise.
- **bar**: digits with a thin progress bar along the bottom.
- Digits drawn in fixed-width character slots (no horizontal jitter).
- MM:SS display; timers ≥ 60 min display H:MM:SS.
- Render economy: background layer cached once; digits re-drawn once per second;
  ring/bar drawn per frame. Input fps: classic = 1; ring/bar = 10 (duration ≤ 10 min),
  4 (≤ 30 min), else 2. Output always 30 fps (ffmpeg duplicates frames).

### Spinner options

```json
{
  "entries": ["Alice", "Bob", ...],   // 2..20 non-empty strings
  "mode": "random"|"rigged",
  "winner": "Bob",                    // required iff mode == "rigged"
  "accent": "#e8b44f"
}
```

- Wheel with equal segments, auto palette (dark-friendly hues), labels auto-sized
  and contrast-checked. Fixed pointer at top.
- Animation: ~1s anticipation wind-up, ~7s spin with cubic ease-out
  (4–6 full revolutions), lands on winner, winner card + highlight, ~4s hold.
  30 fps throughout (~360 frames).
- **random**: winner chosen with `random.choice` server-side, then same path as rigged.
- **rigged**: final rotation computed so the pointer lands inside the winner's segment,
  with random jitter within the segment so the result doesn't look centered/staged.
- Wheel drawn once at 2× supersample, rotated per frame, downsampled (quality + speed).

## Frontend design intent

Aesthetic: **broadcast control room** — dark charcoal surfaces, one warm amber accent,
utilitarian grid, big obvious affordances usable in a dark tech booth under time
pressure. Typography from local system fonts with character (Avenir Next for display,
Menlo for numerals) — no webfont downloads, works offline. Distinctive feature: a
**live canvas preview** of the configured visual (approximate, not frame-accurate)
so the operator sees what they'll get *before* spending render time. Flow:

1. Home: two large tiles — TIMER and SPINNER (room for future tiles).
2. Config panel: options form + live preview + filename preview.
3. Export: progress bar with % and stage; on completion, Download / Reveal in
   Finder / Render another. Errors shown inline with plain-English messages.

## Error handling

- All `POST /api/render` inputs validated server-side (types, ranges, entry counts,
  hex colors); 400 with plain-English message; UI shows it inline.
- Renderer exceptions caught by the worker → job status `error` with message;
  partial output file deleted.
- ffmpeg process failure (nonzero exit) surfaces stderr tail in the job error.
- Filename sanitization for both export naming and the reveal endpoint.

## Testing / verification

- `pytest`-less by design (tiny app); instead a `scripts/smoke.py` that renders a
  short timer (each style) and a spinner headlessly, then decodes each MP4 with the
  bundled ffmpeg and asserts codec/h264, yuv420p, 1920×1080, expected duration ±0.5 s.
- End-to-end browser verification via the running server: configure → export →
  poll → download for both visuals.

## Out of scope (v1)

- Automatic ProPresenter import (planned later; manual drag-in for now).
- Alpha-channel exports (ProRes 4444), audio tracks, custom backgrounds/logos,
  additional visual types. The tile grid and renderer registry leave room for these.
