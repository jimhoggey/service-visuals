# Spec: Background images for the Timer tile (v1.24.0)

**Status:** shipped in v1.24.0. Added in verification: a soft dark halo behind the digits whenever a background image is in use — on a busy bright upload the light digits were otherwise marginal, worst in ring style, and raising the default dim would have muddied every image instead. **Owner:** orchestrator. **Implementers:** two Sonnet agents (backend, frontend), one reviewer.

## Why

A countdown or clock on a flat black vignette is functional and dull. The
operator wants to drop in their series artwork — one image, or a few that
cycle — so the screen looks like it belongs to the service. Nothing else
about the timer changes.

**This is opt-in.** With no images chosen the output is exactly what it is
today, to the pixel. That is a hard requirement, not an aspiration.

Applies to **both** Timer modes (countdown and clock) and all styles.

## Behaviour

A new **Background** group in the Timer form, below Accent colour, visible in
both modes:

| Control | Details |
|---|---|
| Image strip | Thumbnails of the images in the current set, in order, each with a small **×** to remove. Plus an **+ ADD IMAGE** button (file picker, accepts PNG/JPG/WebP). Empty state reads `No images — plain dark background.` |
| SECONDS PER IMAGE | Number input, 2–120, default **10**. Only shown when 2+ images are in the set. Hint: `Each image holds this long, then the next one shows.` |
| DIM | Range slider 0–80 %, default **45**. Hint: `Darkens the image so the numbers stay readable.` |
| BLUR | Checkbox, default **off**. Hint: `Softens a busy photo.` |

Images uploaded once are **kept** in `~/.service-visuals/backgrounds/` and
offered again next time — a church reuses the same series artwork for weeks.
The **+ ADD IMAGE** button opens a small picker listing what is already
stored (click to add to the set) plus an upload control. Removing an image
from the *set* (the ×) does not delete it from storage; the picker has its
own delete for that.

Order is upload/add order. Reordering is out of scope.

The preview canvas shows the **first** image in the set with the current dim
and blur applied, so the operator sees the real opening frame.

## Display rules (renderer AND preview must agree)

- Each image is **cover-fit** to 1920×1080: scale so it fills the frame, crop
  the overflow, centred. Never letterbox, never distort.
- **Blur** (when on): Gaussian radius **18 px** at 1920×1080. In the 960-wide
  preview canvas use `ctx.filter = "blur(9px)"` — half, to match.
- **Dim**: composite the image toward black at `bg_dim/100`. Renderer:
  `Image.blend(img, black, dim)`. Preview: draw image, then fill black at
  `globalAlpha = dim`.
- Order: cover-fit → blur → dim. (Blurring after dimming washes out.)
- The style's track (ring track, bar track) is painted **on top of** the
  finished background, exactly as it is on the vignette today.
- Digits, ring, bar, accent and warn colours are all unchanged.

## Cycling — hard cut, and why

With 2+ images, the visible image is `images[int(t / bg_seconds) % n]`, a
**clean cut** — no crossfade.

This is deliberate. The whole timer renderer is fast because a composed frame
is cached per displayed second (`bases`) and reused; a crossfade makes every
frame inside the transition unique, which is what made milliseconds cost 30
fps. A cut keeps the background constant within each interval, so the cache
keeps working and **cycling costs essentially nothing**. Input fps stays
exactly as `_input_fps` returns today.

Implement by making the plate the frame is built on a function of the frame
index, and adding the plate index to the cache key:

- Build `plates`: one 1920×1080 RGB image per background image, each already
  cover-fit + blurred + dimmed **and with the style track painted on** —
  i.e. each plate is exactly what the single `bg` is today. With no images,
  `plates = [<today's vignette+track>]` — a one-element list, so the existing
  path is the n=1 case and nothing else changes.
- `plate_index(i) = 0` when `len(plates) == 1`, else
  `int((i / fps) / bg_seconds) % len(plates)`.
- `bases` key gains the plate index; the LRU cap rises from 16 to
  `16 * min(4, len(plates))` so a cycling timer does not thrash.

## API contract (type stays `"timer"`)

Four optional keys, valid in **both** modes:

```json
{"type": "timer", "options": {
  "backgrounds": ["a1b2c3d4e5f60718", "..."],
  "bg_seconds": 10,
  "bg_dim": 45,
  "bg_blur": false
}}
```

- `backgrounds`: list, 0–10 items, each matching `^[a-f0-9]{16}$`. Default
  `[]`. Unknown id → *"One of the background images is missing — remove it
  and add it again."* More than 10 → *"A timer can use up to 10 background
  images."*
- `bg_seconds`: `_int_field` 2–120, default 10, label `Seconds per image`.
- `bg_dim`: `_int_field` 0–80, default 45, label `Dim`.
- `bg_blur`: bool, default false → *"Blur must be true or false."*
- Omitting all four behaves exactly as today.

## Background library endpoints

Stored under `BACKGROUNDS_DIR = ~/.service-visuals/backgrounds/`, one
`<id>.png` per image, cover-fit to 1920×1080 at upload so the renderer never
re-fits. Re-encode through Pillow on upload (strips anything that is not a
real image), same as `/api/upload-bg`.

| Route | Behaviour |
|---|---|
| `POST /api/backgrounds` | multipart `image` → `{"id": "<16 hex>"}`. Rejects non-images with the existing wording. If the library already holds 40, refuse: *"You have 40 background images saved — delete some before adding more."* |
| `GET /api/backgrounds` | `{"images": [{"id", "added"}]}`, newest first. |
| `GET /api/backgrounds/<id>` | the PNG (404 if unknown). Validate the id against the regex and resolve inside `BACKGROUNDS_DIR` — same containment check as `_safe_upload_path`. |
| `DELETE /api/backgrounds/<id>` | `{"ok": true}`; deleting one still referenced by a saved form is fine, the render just errors with the message above. |

## Also in this release: rename "Hold at 0:00"

Operators have asked what it means. Use these exact strings so front and back
match:

- HTML label: `KEEP 0:00 ON SCREEN FOR (SECONDS)`
- Hint (`timer-hold-hint` default): `After the countdown ends the video stays on 0:00 this long. 0 to 30 seconds.`
- JS validation message: `"Keep 0:00 on screen" must be 0 to 30 seconds.`
- `app.py` `_int_field` label: `Keep 0:00 on screen (seconds)`

The option key stays `hold_seconds` — wire format is unchanged.

## Files & ownership (edit ONLY your own files)

**Backend agent** — `render/timer.py`, `app.py`, `scripts/smoke.py`, `README.md`.
- `render/timer.py`: a `_plates(options, style, accent)` helper returning the
  list described above (reusing the existing track-painting code so the
  no-image plate is byte-identical to today's `bg`); `plate_index`; cache-key
  and LRU changes in **both** `render_timer` and `_render_clock`. Add
  `prepare_background(path, dim, blur)` as a pure image function so smoke can
  test cover-fit/dim/blur without rendering a video.
- `app.py`: the four option validations in both mode branches, the four
  routes, `BACKGROUNDS_DIR`, and the `hold_seconds` label change. Analytics:
  you may add **one** prop `bg` with value `"none"`, `"one"` or `"many"` —
  never a filename, never a count of anything else.
- `scripts/smoke.py`: (a) `prepare_background` cover-fits a 100×400 and a
  400×100 test image to exactly 1920×1080 without distortion (check a known
  edge pixel), dims toward black, and blurs; (b) a 6 s countdown with two
  generated background images renders and passes `verify()`; (c) validation
  accepts the four keys, rejects a bad id, 11 images, `bg_dim` 90; (d) the
  existing no-background checks still pass.
- `README.md`: one sentence in the Timer bullet.

**Frontend agent** — `static/index.html`, `static/app.js`, `static/style.css`.
- The Background group and the add-image picker (a small inline panel, not a
  modal — reuse existing `.panel`/`.chip` styling; no new dependencies).
- `readTimer()` / `validateTimer()` / `timerPayload()` carry the four keys in
  both modes; validation messages match the backend's exactly.
- Preview: draw the first image cover-fit with blur and dim per the display
  rules, then the existing style drawing on top. Handle image load
  asynchronously and redraw when it arrives — the preview must not flicker
  back to black on every keystroke (cache the loaded `Image` by id).
- The "Hold at 0:00" rename.

## Do not

- Do not change output when `backgrounds` is empty — the existing countdown
  and clock smoke renders must stay byte-identical.
- Do not add a crossfade, a Ken Burns pan, or reordering. Cut only.
- Do not touch the spinner, QR, motion-bg or scoreboard tiles.
- Do not add dependencies. Do not bump `version.py` or tag.
- Do not commit any image the user uploads (`~/.service-visuals/` is outside
  the repo; keep it that way).

## Done means

- `SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py` passes.
- `.venv/bin/python -m pyflakes *.py render/*.py scripts/*.py` is clean.
- Byte-identical proof for a no-background 6 s countdown in all three styles
  (`git show HEAD:render/timer.py` + `encoder.py` + `fonts.py` into a temp
  package, render old vs new, extract frames, `cmp`).
- A 5-minute classic countdown with 3 cycling images renders in under 90 s.
