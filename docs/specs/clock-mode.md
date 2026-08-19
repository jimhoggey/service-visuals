# Spec: Clock mode for the Timer tile

**Status:** shipped in v1.22.0 (ring digits fit to the ring's inner diameter, capped at 190 — an addition found in verification). **Owner:** orchestrator. **Implementers:** two Sonnet agents (backend, frontend), one reviewer.

## Why

The MC says "put the clock on the screen — registrations for Summit go live at
8 o'clock on the dot". The operator wants a video that shows a wall clock
starting at a time THEY choose (e.g. `7:59:50 PM`), ticking forward in real
time, rolling over to `8:00:00 PM` on screen, and continuing for as long as the
clip runs. Optionally with milliseconds for drama on the ten-second run-in.

It is not a countdown, but it lives under the existing **TIMER** tile as a
second **mode**, sharing its styles, accent, background and export flow.

## Behaviour (what the operator sees)

Timer view gets a **MODE** segmented control at the top of the form:
`COUNTDOWN` (default, everything exactly as today) | `CLOCK`.

In **CLOCK** mode the form shows:

| Control | Details |
|---|---|
| Style | `CLASSIC` and `RING` only. `BAR` is hidden in clock mode (a bar depletes; a clock doesn't). If BAR was selected when switching to clock, fall back to CLASSIC. |
| Start time | Three number inputs `HOURS (0-23)` : `MINUTES (0-59)` : `SECONDS (0-59)`, laid out like the existing duration row. Default **19 : 59 : 50**. A hint under it reads e.g. `Shows as 7:59:50 PM` (updates live from the format choice). |
| Clip length | Number input `CLIP LENGTH (SECONDS)`, 5–1800 (30 minutes), default **30**. Chip presets: `15 S`, `30 S`, `1 MIN`, `5 MIN`. |
| Format | Segmented `12-HOUR` \| `24-HOUR`, default 12-hour. |
| Show seconds | Checkbox, default **on**. |
| Show milliseconds | Checkbox, default **off**. Ticking it also ticks/locks *Show seconds* (you can't have millis without seconds). Hint under it: `Renders at 30 fps — longer to export`. |
| Accent colour | Same swatches as countdown. In clock mode accent colours the ring (ring style) and the AM/PM tag; digits stay light. |
| Hidden | Duration group, "Warn colour in final 10 seconds", "Hold at 0:00". |

Preview canvas shows the **first frame** in the chosen format/style, so the
operator sees exactly what the clip opens with. Estimate line follows the
real frame count (see fps below).

The MP4 filename is `clock_1959-50_30s_classic_<stamp>.mp4`.

The view title becomes **Timer** (it is no longer only a countdown); the home
tile description becomes: *"Countdown for pre-service, transitions and games — or a
live clock counting to the moment. Classic, ring or bar."*

## Display rules (renderer AND preview must agree)

Let `T` = start + elapsed, wrapping at 24 h (`23:59:55` + 10 s shows `00:00:05`).

- **24-hour:** `HH:MM` / `HH:MM:SS` / `HH:MM:SS.mmm`, hours zero-padded (`07:59:50`, `20:00:00`).
- **12-hour:** `H:MM` / `H:MM:SS` / `H:MM:SS.mmm`, hours NOT padded (`7:59:50`, `12:00:00`), plus a small tag `AM`/`PM`. Midnight hour shows `12`, noon shows `12 PM`.
- Milliseconds are three digits (`.000`–`.999`) computed exactly from the frame index: `ms = round(i * 1000 / input_fps)`; never derived from wall time.
- Digits use the existing fixed-width slot system (`_digits_metrics` / `digitMetrics`) so nothing jitters: every digit is centred in a slot as wide as the widest digit; `:` slot 55 %; the `.` slot 40 %.
- **Millis are drawn smaller** — 55 % of the main digit size, sharing the main baseline, in the same light colour.
- **AM/PM tag** — 28 % of the main digit size, in the accent colour, placed after the last digit with a gap of one colon-slot, sitting on the main baseline. Fixed-width (`AM`/`PM` measured, take the wider) so switching doesn't shift the clock.
- Colour: digits `DIGITS_COLOR`; no warn colour in clock mode.
- **Classic:** whole time string (incl. millis and tag) auto-sized to fit ~1600 px wide, cap 400 px, vertically centred — reuse `_classic_font_size` logic on the FULL string width.
- **Ring:** digits 190 px like the countdown ring, centred. The ring is a **seconds hand**: accent arc from 12 o'clock clockwise, `frac = (sec + ms/1000) / 60`, so it fills over each minute and resets on the minute — completing exactly at `8:00:00`. Track behind it as today.

## Frame rate

- `show_millis` **off**: input fps `1` (classic) or `10` (ring), output `TIMER_OUTPUT_FPS` (15) — identical economics to the countdown; per-second digit bases cached like `base_for`.
- `show_millis` **on**: input fps `30`, output fps `30` (pass `output_fps=30`). Every frame is unique; skip the base cache.

## API contract (type stays `"timer"`)

```json
{"type": "timer", "options": {
  "mode": "clock",
  "start": "19:59:50",
  "duration_seconds": 30,
  "format": "12h",
  "show_seconds": true,
  "show_millis": false,
  "style": "classic",
  "accent": "#e8b44f"
}}
```

- `mode` optional; missing or `"countdown"` → existing behaviour untouched, existing validation untouched.
- `mode: "clock"` validation (`app.py`, plain-English messages like the rest):
  - `start`: string `HH:MM:SS`, `00`–`23` / `00`–`59` / `00`–`59` → *"Start time must look like 19:59:50 (24-hour, hours 0-23)."*
  - `duration_seconds`: `_int_field` 5..1800 → *"Clip length must be a whole number between 5 and 1800 seconds."*
  - `format`: `"12h"` \| `"24h"` (default `"12h"`).
  - `show_seconds`, `show_millis`: bool (defaults `true` / `false`); if `show_millis` is true, `show_seconds` is forced true.
  - `style`: `"classic"` \| `"ring"` (`"bar"` → *"Bar style isn't available for the clock — choose classic or ring."*)
  - `accent`: `_accent_field`.
  - Countdown-only keys (`minutes`, `seconds`, `warn_last10`, `hold_seconds`) are ignored in clock mode.
  - Return the normalised dict with `mode: "clock"` so the renderer never guesses.

## Files & ownership (agents edit ONLY their own files)

**Backend agent** — `render/timer.py`, `app.py`, `scripts/smoke.py`, `README.md`.
- `render/timer.py`: `render_timer()` branches on `options.get("mode")`. Put clock rendering in a new function `_render_clock(options, progress_cb)` in the same module; share `_background`, `_digits_metrics`, `_render_digits` (extend it or add a sibling that handles the smaller millis and the tag — do NOT change countdown output by a single pixel). Add `format_clock_time(total_ms, fmt, show_seconds, show_millis) -> (main_text, tag)` as a pure function for tests.
- `app.py`: `validate_timer_options` dispatches on `mode`.
- `scripts/smoke.py`: (a) pure checks of `format_clock_time` — midnight wrap, 12 h midnight/noon, millis exactness, seconds hidden; (b) render a 6 s clock `classic/12h/millis on` and a 6 s `ring/24h/millis off` and pass both through the existing `verify()` helper (duration ≈ 6 s, 1920×1080); (c) `validate_timer_options` rejects bad `start`, `bar` style, `duration_seconds` out of range, and still accepts a countdown payload unchanged. Follow the existing `check()` style.
- `README.md`: extend the Timer bullet with one sentence about clock mode.

**Frontend agent** — `static/index.html`, `static/app.js`, `static/style.css`.
- Mode segmented control (reuse `.seg-group` / `.seg` from the spinner form).
- Clock controls as in the table; hide/show groups via `hidden` on the group fieldsets; keep IDs prefixed `timer-clock-…`.
- `readTimer()` gains `mode` and the clock fields; `validateTimer()` covers clock fields with the same messages as the backend; `timerPayload()` sends the API contract above (only clock keys in clock mode, only countdown keys in countdown mode).
- Preview: implement the display rules above on the canvas (2× scale, `PW`×`PH`); reuse `digitMetrics` / `drawClock` — extend rather than duplicate.
- Estimate: `frames = duration × (millis ? 30 : (ring ? 10 : 1))`, `sec = frames / 30`.
- The style picker must hide the BAR card in clock mode and re-show it in countdown mode; if bar was checked, check classic.
- Presets chips for clip length; hours/minutes/seconds inputs clamp like the duration row.

## Do not

- Do not change countdown rendering, validation, or preview output in any way (the smoke suite's existing timer checks must pass unchanged).
- Do not add dependencies.
- Do not bump the version or tag — the orchestrator does that.
- Do not touch files outside your ownership list. If you believe you must, stop and say so in your report instead.

## Done means

- `SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py` passes with the new checks.
- A 30 s clock renders in well under a minute on this Mac with millis on.
- The UI switches modes cleanly and the preview matches the render's first frame.
