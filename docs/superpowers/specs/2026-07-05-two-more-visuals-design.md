# Two More Service Visuals — Design

**Date:** 2026-07-05
**Status:** Approved (autonomous; user delegated the choice and asked to build)

## Why these two

The app's value is *generated motion* visuals that ProPresenter can't trivially
make itself (it handles static text/images natively). Timer = motion; Spinner =
motion + randomness. The two additions fill the same "annoying to make otherwise"
gap and are church-specific:

1. **QR "Scan to…" card** — churches constantly need on-screen QR codes (giving,
   connect cards, event signups). Today that means Canva/Photoshop. Type a URL +
   heading → a clean animated MP4.
2. **Motion background loop** — worship/ambient motion backgrounds are a paid
   commodity (WorshipHouse etc.). A tasteful, seamlessly-looping animated
   background in the church's accent colour fills a real gap.

Both slot into the existing renderer registry, tile grid, config-view, and
live-preview patterns. No architectural change.

## QR visual — `render/qr.py` → `render_qr(options, progress_cb)`

- **Dependency:** `segno` (pure-Python QR, zero transitive deps; packages fine
  with PyInstaller via the import chain).
- **Scannability is non-negotiable:** QR is ALWAYS dark modules on a white
  rounded card with a proper quiet zone (≥4 modules). Never inverted — many
  scanners won't read light-on-dark. The accent colour is used only for the
  heading text and a subtle breathing ring, never the modules.
- **Options** (validated server-side):
  - `url` str, 1..1000 chars, required (the QR payload; any text/URL).
  - `heading` str 0..30, default "" (e.g. "SCAN TO GIVE"), drawn in accent above the card.
  - `caption` str 0..60, default "" (small off-white line under the card).
  - `accent` hex, default #e8b44f.
  - `duration_seconds` int 5..60, default 15.
- **Motion:** SEAMLESS LOOP (frame 0 == frame N; all motion is a function of
  `phase = 2π·f/N`). The card and QR are static (so the code stays scannable);
  only a soft accent ring around the card gently breathes. No fade-in (would
  break the loop — ProPresenter loops the clip).
- Output filename: `export_path("qr", heading or "code")`.

## Motion background — `render/motionbg.py` → `render_motion_bg(options, progress_cb)`

- **Options** (validated):
  - `style` "aurora"|"bokeh"|"waves", default "aurora".
  - `accent` hex, default #e8b44f (a tasteful 2–3 colour dark scheme is derived from it).
  - `duration_seconds` int 5..30, default 12 (the loop length, 30 fps).
- **Seamless loop is the core requirement:** every moving quantity is periodic in
  `phase = 2π·f/N`, so the last frame flows into the first with no jump. The
  self-test verifies this numerically (wrap-around frame diff ≈ adjacent-frame diff).
- **Styles:**
  - `aurora`: soft drifting colour blobs (precomputed radial sprites composited at
    low res, upscaled smooth → soft gradient look, cheap).
  - `bokeh`: floating out-of-focus dots drifting on periodic paths.
  - `waves`: slow horizontal wave bands.
- **Performance:** render at low resolution (≈480×270 for aurora, ≈960×540 for
  bokeh/waves) and upscale to 1920×1080 with smooth resampling. Full-frame every
  frame (no caching possible), but low-res keeps a 12 s loop well under a minute.
- Output filename: `export_path("motionbg", f"{style}_{dur}s")`.

## Integration (shared files, one agent, no parallel conflicts)

- `app.py`: add `validate_qr_options` / `validate_motion_bg_options`, register in
  `VALIDATORS` and `JobManager({...})`, bump `APP_VERSION` to `1.3.0`.
- `static/index.html`: replace the "MORE VISUALS COMING" placeholder with two real
  tiles (QR, Motion BG) + a fresh placeholder; add two config views with preview canvases.
- `static/app.js`: readers/validators/payloads/previews for both, tile+back wiring,
  add both view ids to `VIEWS`, boot-time preview init. QR preview draws a
  representative card (stylised QR glyph, real heading/accent), captioned
  "approximate"; motion-bg preview live-loops the chosen style.
- `requirements.txt`: add `segno`. `.github/workflows/build.yml`: `--collect-all segno`
  on both build steps.
- `scripts/smoke.py`: render both new visuals headlessly and probe each MP4.
- `README.md`: mention the two new visuals.

## Out of scope

Static-image QR export (we render MP4 to match the pipeline), custom fonts/logos,
video/image motion-bg sources, additional styles beyond the three.
