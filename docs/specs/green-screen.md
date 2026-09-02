# Spec: Green screen background for the Timer tile

**Status:** in progress. **Owner:** orchestrator. **Implementers:** backend
Sonnet agent, frontend Sonnet agent (disjoint files, below).

## Why

The operator wants to put the countdown or clock over their *own* video
background in ProPresenter or an editor. A solid chroma green frame keys out
cleanly; the digits, ring and bar stay. Applies to **both** Timer modes
(countdown and clock) and all styles.

**Opt-in.** With the toggle off, output is byte-identical to today.

## Behaviour

In the existing **Background** group, next to `+ ADD IMAGE`, a small toggle
button `GREEN SCREEN` (`id="timer-bg-green"`, `type="button"`,
`aria-pressed="false"`). Clicking it toggles `aria-pressed`.

| Green on | Green off |
|---|---|
| Button shows pressed (green outline/text, small green swatch dot). | Normal `.btn` look. |
| Image strip, `+ ADD IMAGE`, SECONDS PER IMAGE, DIM, BLUR all hidden. | Exactly today's `applyTimerBg()` rules. |
| `timer-bg-empty` hint shown, text: `Solid green — key it out in your video software to put your own background behind the numbers.` | Hint text `No images — plain dark background.` shown only when the set is empty. |
| Preview canvas is flat `#00ff00`, digits/ring/bar drawn on top as today, **no** digit shadow halo. | Unchanged. |

The chosen image set (`timerBg.ids`) is *kept* in memory while green is on,
so turning green off restores it. Only the payload omits it.

## Display rules (renderer AND preview must agree)

- Plate is a flat `#00ff00` (`GREEN_SCREEN = (0, 255, 0)`) 1920×1080. No
  vignette, no dim, no blur.
- The style's track (ring track, bar track) is painted on top of the green
  exactly as on the vignette — it is part of the graphic the operator keys
  *in*, not out.
- Digits, ring, bar, accent, warn colour, AM/PM tag: unchanged.
- The digit shadow halo (`has_bg`) stays **off** — it would leave a dark
  fringe once keyed. `has_bg` remains `bool(options["backgrounds"])`, which
  is `[]` under green (see API), so no new condition is needed there.

## API contract (type stays `"timer"`)

One optional key, valid in **both** modes: `"green_screen": false`.

- bool, default `false`. Not a bool → *"Green screen must be true or false."*
- When `true`, the normalised options carry `backgrounds: []` regardless of
  what was sent, and the image ids are **not** validated (a stale id in a
  hidden set must not block a green export). `bg_seconds`/`bg_dim`/`bg_blur`
  are still validated and returned as usual.
- Analytics (`_timer_props`): `bg` gains a fourth value `"green"`. Nothing
  else.
- Filename: append `_green` as the last descriptor element in both modes
  (`timer_5m00s_classic_green_<stamp>.mp4`, `timer_..._ms_green_...`,
  `clock_1959-50_30s_ring_green_<stamp>.mp4`).

## Files & ownership (edit ONLY your own files)

**Backend agent** — `render/timer.py`, `app.py`, `scripts/smoke.py`, `README.md`.
- `render/timer.py`: `GREEN_SCREEN` constant; `_plates()` builds
  `[Image.new("RGB", (WIDTH, HEIGHT), GREEN_SCREEN)]` when
  `options.get("green_screen")`, before the track-painting loop. The
  descriptor suffix in both `render_timer` and `_render_clock`. Nothing
  else in the file changes.
- `app.py`: validation in `_timer_background_options` (one place, both
  modes); `_timer_props`.
- `scripts/smoke.py`: (a) `validate_timer_options` with `green_screen: True`
  and a bogus `backgrounds` id returns `green_screen True`, `backgrounds []`,
  in both modes; `"yes"` raises the exact message; omitted → `False`.
  (b) `timer._plates({"green_screen": True}, "ring", (1, 2, 3))` returns one
  plate whose pixel (10, 10) is `(0, 255, 0)`. (c) A 6 s classic countdown
  with `green_screen: True` renders, passes `verify()`, its filename contains
  `_green`, and frame 0 extracted with the bundled ffmpeg (`-frames:v 1` to a
  PNG) has pixel (10, 10) with `R <= 24, G >= 232, B <= 24` — H.264 4:2:0
  shifts pure green slightly; that tolerance is the point of the check.
- `README.md`: one clause in the Timer bullet.

**Frontend agent** — `static/index.html`, `static/app.js`, `static/style.css`.
- The button in `.bg-strip-wrap` after `+ ADD IMAGE`; pressed styling via
  `#timer-bg-green[aria-pressed="true"]` (reuse `.btn` / `.btn-file`; no new
  dependencies).
- `readTimer()`: `greenScreen` from `aria-pressed`; `backgrounds` becomes
  `[]` when green is on (so the preview's `hasBg` and the payload are right
  by construction). `timerPayload()`: `green_screen` in both mode branches.
- `applyTimerBg()`: the show/hide table above. `validateTimerBg()`: skip the
  image-count check under green.
- `drawTimerBackground(ctx, t)`: first line — if `t.greenScreen`, fill
  `#00ff00` over `PW×PH` and return.
- The button click calls `updateTimer()` explicitly (a button click fires no
  form `input`/`change`).

## Do not

- Do not change output when `green_screen` is absent/false — byte-identical.
- Do not add a colour picker, a blue-screen option, or an alpha export.
- Do not bump `version.py`, edit `whatsnew.py`, or tag.

## Done means

- `SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py` passes.
- `.venv/bin/python -m pyflakes *.py render/*.py scripts/*.py` is clean.
- Orchestrator: byte-identical proof for a no-green 6 s countdown in all three
  styles; a real green render viewed as extracted frames (classic, ring,
  clock); the UI toggled in the browser with the preview checked.
