# Scoreboard (points board) — Design

**Date:** 2026-08-15
**Status:** Approved (user chose: reusable boards, harvest digits from the image, numbers only)

## Problem

The youth "GROUP POINTS" screen is regenerated in ChatGPT every week just to change
six numbers. Slow, and it introduces errors (team names drift, layout changes).

## Solution

A fifth visual: upload the existing points image ONCE, the app OCRs it, and every
week after you open the saved board, type the new numbers, and export a PNG that is
**pixel-identical to the original except for the digits you changed**.

Not AI image generation — that would redraw the whole image and drift. This is a
surgical local edit: erase just the old number, redraw the new one in the same
place, same colour, same size, using the *same font* (harvested from the image).

## OCR: OS-native, per platform

Matches the approach proven in the user's Runsheet Pilot app (~1 MB of deps, no
model files, no system binary, excellent accuracy):

| Platform | Package | Engine | Coords returned |
|---|---|---|---|
| macOS | `ocrmac` | Apple Vision | normalised 0..1, **bottom-left** origin |
| Windows | `winocr` | Windows.Media.Ocr | **pixel**, top-left, line→words |
| other | — | none | raise `OCRUnavailable` with plain advice |

Both are normalised to **top-left PIXEL rects**. On Windows use WORD-level boxes
(`line["words"][i]["bounding_rect"]`), not line boxes, so "350" is its own box even
if it sits near other text. Getting the macOS y-flip backwards is silent and hard to
spot — `y_top = (1 - y - h) * H` — so it gets a dedicated test.

Only tokens that are **entirely digits** become editable boxes (user's choice:
numbers only).

## Digit harvesting (the fidelity trick)

The board already contains the original font's digits (0,2,3,4,5,6,7 in the sample).
So we never guess a font:

1. Per numeric box, build an **ink mask**: pixels close to the sampled ink colour.
2. Segment digits by **column projection** over that mask (runs of ink columns).
   If the segment count matches the OCR text length, each glyph is labelled by its
   character and stored as an RGBA bitmap (alpha = ink coverage, so anti-aliasing is
   preserved).
3. Record the median inter-digit gap and the glyph baseline/cap-height.

Rendering a new value composites the harvested glyph bitmaps with that gap.
**Fallback:** any digit never seen anywhere on the board is drawn with the closest
bundled system font, scaled to the harvested cap-height and filled with the ink
colour. So it always works, and is pixel-perfect whenever the digit exists.

## Erase (must not smear texture)

Flat-fill would leave a patch on a textured/paper card. Instead:

1. Dilate the ink mask a few px (catches anti-aliased edges).
2. For each masked pixel, fill from the nearest **unmasked** pixel scanning
   vertically (a mini content-aware fill), falling back to the box's median
   background colour.

Non-glyph pixels are never touched, so card texture, borders and shadows survive.

## Board storage (reusable)

`~/.service-visuals/boards/<id>/` — outside the app bundle, so boards survive
updates:

```
source.png        the original upload (never modified)
board.json        {id, name, created, updated, width, height,
                   boxes:[{id, text, x, y, w, h, ink, bg, style,
                           glyph_top, cap_height, ink_cx, gap, gaps,
                           safe_x0, safe_x1}],
                   values:{box_id: "400"}}
glyphs/<style>/<char>.png   harvested digit bitmaps
```

Glyphs are filed per STYLE (a bucket of ink colour + cap height), not per
character alone: a board can hold several sizes and colours of number — the
sample has three tiny podium digits as well as six big pink scores — and a
bitmap keyed on the character alone would let the first box seen supply the
digits for every other one. A glyph is only borrowed across styles when the
ink colours are close, and is always recoloured to the target box's ink.

## API

- `POST /api/board/analyse` — multipart `image` + `name` → `{board_id, name, width, height, boxes[]}`
- `GET  /api/board/list` → `[{id, name, updated, box_count}]`
- `GET  /api/board/<id>` → full board (boxes + current values)
- `GET  /api/board/<id>/source.png` → original image (for the UI overlay)
- `POST /api/board/<id>/values` — `{values:{box_id:"400"}}` → persists
- `POST /api/board/<id>/preview` → PNG of the board with current values
- `POST /api/board/<id>/export` → renders to `exports/` → `{filename}`
- `DELETE /api/board/<id>`

All validated: board ids are `[a-z0-9]{12}`, values must be 1–6 digits, paths are
realpath-contained. Upload re-encoded through Pillow (as `/api/upload-bg` does).

## UI

New tile **SCOREBOARD**. The view has two states:

- **No board open:** drop/choose an image + a list of saved boards to reopen.
- **Board open:** the image at scale with an accent rectangle over every detected
  number. Click one → inline numeric input → value updates → debounced server
  re-render refreshes the preview. Then **EXPORT PNG**, which lands in `exports/`
  and offers the standard *Show in Explorer / Reveal in Finder* + *Make another*.

Board name is editable and saved. Values persist, so next week is: open board, type
six numbers, export.

## Packaging

- `requirements.txt`: `ocrmac>=1.0.1; sys_platform == "darwin"`,
  `winocr>=0.0.15; sys_platform == "win32"`
- `build.yml`: `--collect-all ocrmac` (mac job), `--collect-all winocr` (windows job)
- Smoke test must NOT require OCR (CI/Linux): it exercises render/erase/harvest with
  a synthetic board whose boxes are supplied directly.

## Out of scope

Editing team names (numbers only, per the user), multi-page boards, and automatic
ProPresenter import.
