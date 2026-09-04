# Spec: three QR card styles

**Status:** shipped in v1.30.0. **Owner:** orchestrator. **Implementers:** backend
Sonnet agent, frontend Sonnet agent (disjoint files, below). The backend
used segno's `matrix_iter(verbose=True)` module types for the DOTS
finder/alignment squares (segno 1.6.6). No QR decoder is installed on the
dev Mac, and both Apple Vision (pyobjc) and OpenCV took minutes just to
import on the loaded machine — in the end the owner scanned the three
stills with a phone: all three read. They then asked for a no-ring
variant, so a `ring` bool (default `true`) was added alongside `style`:
off means the still is exactly the composed base and the video repeats
it, with a checkbox "Accent ring around the code" in the Style group.

## Why

"Add three different styles for the QR code — this style with a white
background, another style, and something else, so the user can click
through it." Every style must stay a dark-on-light code with the full
quiet zone: inverted (light-on-dark) codes fail on older scanners, so that
is not one of the three.

**Opt-in.** `style` absent or `"card"` → output byte-identical to today
(the golden harness's `qr` job proves it).

## The three styles

| `style` | Label | Look |
|---|---|---|
| `card` (default) | CARD | Exactly today: white rounded card on the dark vignette (or the uploaded background), square modules, accent heading above, light caption below, breathing accent ring. |
| `light` | LIGHT | The whole frame is an off-white plate — no card. Code drawn straight on the plate, modules `QR_DARK`; heading and caption in `QR_DARK` (gold on white is unreadable); the ring stays accent. **Ignores an uploaded background image** (a code needs a plain light field behind it). |
| `dots` | DOTS | Today's card, but each data module is a filled circle of diameter `module_px` centred in its cell. The three finder patterns (7×7 corners) and the alignment patterns stay square — scanners locate the code by them. Everything else as `card`. |

## Display rules (renderer only — the preview is the renderer's own PNG)

- `light` plate: `LIGHT_BASE = "#f4f2ee"` with a radial vignette to
  `LIGHT_EDGE = "#e6e3dc"` at the edges, built exactly like
  `_vignette_background()` with those two colours (reuse the function via
  a colour-pair parameter — do not duplicate the mask code). The card
  image for `light` is the code + quiet zone on a **transparent** RGBA
  tile (no white rounded rectangle), so the plate shows through; the
  quiet zone is therefore plate colour, which is fine — it is light.
- `light` text colours: heading `QR_DARK`, caption `QR_DARK` at 78 %
  alpha (`(10, 12, 14, 199)`), so the caption reads quieter than the
  heading as it does today.
- `dots`: draw with `ImageDraw.ellipse([x0, y0, x0 + module_px - 1,
  y0 + module_px - 1])`. A module is a finder/alignment module when
  `segno`'s matrix says so — use the matrix iterator that yields module
  types if the installed segno exposes one (`qr.matrix_iter(verbose=True)`
  yields `ModuleType`), else compute the finder squares from position
  (rows/cols 0–6 at the three corners) and skip alignment rounding.
  Say in your report which path you took.
- The ring, position, heading/caption sizes, spacing, animation: unchanged
  for all three.

## API contract

`style` (`"card"` | `"light"` | `"dots"`, default `"card"`) in QR options,
both for `POST /api/render` (`type: "qr"`) and `POST /api/qr-preview` /
`/api/qr-image`. Invalid → *"That QR style is not valid."*
`validate_qr_options` returns it. Analytics: `_counted("qr", …)` gains a
`_qr_props` → `{"style": _one_of(style, QR_STYLES, "card")}`.

## UI

A **Style** group directly under the Link-or-text group in the QR form:
segmented control `qr-style` — `CARD` (checked) | `LIGHT` | `DOTS`, radio
inputs named `qr-style`, ids `qr-style-card` / `qr-style-light` /
`qr-style-dots`. Hint: `Card and dots sit on your background; light fills
the whole screen.` When LIGHT is selected the background-image control's
group is hidden (`hidden` on its fieldset) and shown again for the other
two. Changing the style re-fetches the preview like any other field.

## Files & ownership (edit ONLY your own files)

**Backend agent** — `render/qr.py`, `validation.py`, `app.py`
(`_qr_props` only), `scripts/smoke.py`, `README.md` (one clause).
Smoke, offline, via `render_qr_still(clean, max_width=900)` on
`{"url": "https://example.org", "heading": "GIVE", "caption": "Thanks"}`:
(a) `style` validation — default `card`, `light`/`dots` accepted,
`"neon"` rejected with the exact message; (b) `light`: pixel (5, 5) has
every channel ≥ 200, `card`: pixel (5, 5) has every channel ≤ 40;
(c) `dots`: for one dark data module at a known cell (take the cell at
row 9, column 9 of the matrix if dark, else scan for the first dark
non-finder cell) the pixel at the cell's centre is dark and the pixel at
its top-left corner is light — compute the cell's frame coordinates from
the renderer's own `_module_px`/geometry helpers, don't hard-code; (d) a
`card` render still passes verify() in the existing video check
(unchanged); (e) `golden.py --check` still passes (run it).

**Frontend agent** — `static/index.html`, `static/js/qr.js`,
`static/style.css` (a line or two at most). `readQr` / `qrPayload` carry
`style`; `validateQr` unchanged (a radio can't be invalid); hide/show the
background-image group; drive it in the browser: the preview visibly
changes across the three, and LIGHT hides the background group.

## Do not

- Do not change `card` output by a pixel. Do not touch the ring, the
  animation, the encoder, other tiles, `version.py`, `whatsnew.py`.
- Do not `git stash`, `reset` or `checkout`. Do not commit. No port 8765;
  dev runs use `PORT=8799 SERVICE_VISUALS_STATS=0 SERVICE_VISUALS_CONFIG=<scratch>`.

## Done means

Smoke and pyflakes clean, `golden.py --check` passes, and the orchestrator
renders all three stills, looks at them, and scans each with a phone
camera-equivalent (the `light` and `dots` stills decode with `zbarimg` or
the `pyzbar` path if available on this Mac; if neither is installed, say
so — do not install anything).
