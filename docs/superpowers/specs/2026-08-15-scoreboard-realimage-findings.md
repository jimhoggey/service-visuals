# Scoreboard — findings from the user's REAL board

Validated against the actual `Grouppoints.png` (1672×941), not the mock. Both of these
must be handled in `render/scoreboard.py`.

## 1. Decorative digits are detected as editable numbers — MUST filter by size

Apple Vision found **9** numeric tokens, not 6. The extras are the **trophy podium
graphic**, which has `1`, `2`, `3` printed on it:

| token | box height | what it is |
|---|---|---|
| 2, 1, 3 | 31–39 px | podium graphic — must NOT be editable |
| 350, 365, 240, 250, 265, 270 | 87–92 px | the actual point values |

The user explicitly chose "numbers only… fewer things to mis-click", so showing three
bogus boxes over a trophy is a real UX failure.

**Fix (verified on the real image):** keep only boxes whose height is
`>= 0.6 * max(box heights)`. That cleanly took 9 → 6 here. Rationale: on a scoreboard
the point values are the large numerals; decorative digits in artwork are much smaller.
Confidence is a weaker signal (the podium `3` scored 0.50, but the `1` and `2` scored
1.00), so size is the reliable discriminator.

Expose the dropped boxes behind a "show all detected numbers" toggle rather than
discarding them, in case a genuine value is unusually small.

## 2. Glyph alpha must be ink COVERAGE, not binary

A binary mask (alpha 255 where the pixel is near the ink colour) produces visibly
jagged digit edges next to the original anti-aliased ones. Harvested glyphs must store
**partial alpha** derived from how close each pixel is to the ink colour — e.g. map the
colour distance across the ink→background range onto 255→0 — so the recomposed number
has the same soft edges as its neighbours.

Same applies to the erase mask: dilate for the fill, but feather nothing — only fully
replace pixels that were fully ink, and blend partial ones.

## Confirmed good (no action needed)

- **Erase is clean.** The cards look textured but are near-flat: per-channel stdev
  1.5 / 7.3 / 3.9. The nearest-unmasked-pixel vertical fill leaves no visible patch.
- **Column-projection segmentation is exact.** `"350"` → 3 runs at (8,66), (75,133),
  (141,202) — widths 59/59/62, gaps 9 and 8 px. Median gap ≈ 8 px.
- **Harvest covers most digits.** From the six values we get `0,2,3,4,5,6,7`.
  Only `1`, `8`, `9` need the system-font fallback on this particular board.
- Sampled ink `(225, 29, 168)`, background `(248, 233, 216)`.
