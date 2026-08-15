"""Scoreboard — surgically re-number an existing points graphic.

The youth "GROUP POINTS" board is regenerated from scratch every week just to
change six numbers, which is slow and lets the layout drift. Instead the user
uploads their board ONCE; we OCR it, remember where every purely-numeric text
box lives, and save it as a reusable board under ~/.service-visuals/boards.
Each week they open the board, type new numbers, and export a PNG that is
pixel-identical to the original except for the digits they changed.

This is a local edit, not image generation. Three things make it convincing:

* OCR is OS-native (Apple Vision on macOS, Windows.Media.Ocr on Windows), so
  there are no model files to ship. Vision returns NORMALISED, BOTTOM-LEFT
  rects; getting that y-flip backwards mirrors every box and fails silently,
  so `_from_vision_rect` is deliberately tiny and separately tested.
* We never guess a font. The board already contains the original font's
  digits, so each numeric box is segmented by column projection and every
  glyph is harvested as an RGBA bitmap whose ALPHA is the ink coverage —
  anti-aliasing and all. Re-rendering a value composites those same bitmaps.
  A digit that appears nowhere on the board (say "9" on a board of 350/240/...)
  falls back to a bundled font scaled to the harvested cap height.
* Erasing is content-aware, not a flat rectangle fill: only ink pixels (plus a
  few px of dilation to catch the anti-aliased halo) are replaced, each from
  the nearest unmasked pixel scanning vertically. Card texture, gradients,
  rounded edges and shadows survive untouched.

Public surface used by app.py: OCRUnavailable, BoardError, BOARDS_DIR,
detect_numbers, create_board, load_board, list_boards, save_values,
delete_board, render_board, export_board.
"""

import json
import logging
import math
import os
import platform
import re
import shutil
import tempfile
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFilter

from . import fonts
from .encoder import export_path

# Pillow >= 9.1 moved resampling filters into an enum; keep 3.9-safe access.
_RESAMPLING = getattr(Image, "Resampling", Image)
_LANCZOS = _RESAMPLING.LANCZOS

# Boards live outside the app bundle (like the AI config) so they survive an
# update, and honour SERVICE_VISUALS_CONFIG so tests can isolate themselves.
CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
BOARDS_DIR = os.path.join(CONFIG_DIR, "boards")

BOARD_ID_RE = re.compile(r"^[a-z0-9]{12}$")
VALUE_RE = re.compile(r"^[0-9]{1,6}$")
# Deliberately not str.isdigit(), which is True for Arabic-Indic digits
# ("٣٥٠") and superscripts ("³") — neither of which the values API accepts
# nor we can harvest a glyph for. Used where a token's LENGTH isn't fixed yet
# (Windows word boxes are merged before the 1-6 digit rule applies).
ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")

MAX_NAME_LEN = 60
MAX_VALUE_LEN = 6
MAX_BOXES = 60                   # a points board with more is not a points board

_LOG = logging.getLogger(__name__)

# board.json is a read-modify-write, and Flask serves requests threaded, so
# two tabs saving the same board race each other. One lock for all boards:
# writes are milliseconds and there are never many boards.
_BOARD_LOCK = threading.RLock()

# --- colour / mask tuning -------------------------------------------------
# Ink coverage is the pixel's position along the background->ink colour axis,
# so a half-covered anti-aliased pixel scores ~0.5. Two thresholds: a strict
# one for "this is glyph body" (segmentation + harvesting) and a loose one for
# "this is glyph or its halo" (erasing, where leftovers are very visible).
CORE_COVER = 0.50
EDGE_COVER = 0.10
ERASE_DILATE = 3                 # px grown around the erase mask
BOX_PAD = 8                      # px of slack around the OCR rect
QUANT = 16                       # colour histogram bucket size
MIN_SEGMENT_PIXELS = 8           # ignore specks when column-segmenting
DEFAULT_GAP_RATIO = 0.12         # inter-digit gap when it can't be measured
MIN_CAP_RATIO = 0.40             # harvest sanity: ink must fill the box
MAX_CAP_HEIGHT = 512             # clamp before allocating a fallback canvas

# Harvested glyphs are filed per STYLE, not per character: two boxes only
# share a bitmap when their ink colour and size agree, so a small white "1"
# from a podium graphic can never be reused as a big pink score digit.
STYLE_COLOUR_STEP = 24
STYLE_SIZE_STEP = 8
STYLE_INK_TOLERANCE = 96         # max L1 ink distance for borrowing a glyph
_STYLE_RE = re.compile(r"^s([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})"
                       r"h([0-9a-f]{2})$")


class BoardError(Exception):
    """Carries a plain-English message safe to show the user."""


class OCRUnavailable(BoardError):
    """Raised when this platform has no OCR engine we can use.

    A BoardError subclass so that any route catching BoardError degrades to a
    plain-English 400 rather than a traceback, while routes that want to say
    something more specific can still catch it first.
    """


# ---------------------------------------------------------------- OCR

def _from_vision_rect(x, y, w, h, width, height):
    """Apple Vision rect (normalised 0..1, BOTTOM-left origin) -> top-left px.

    Vision measures y UP from the bottom of the image, Pillow measures it DOWN
    from the top, so the top edge of the box is (1 - y - h) * height. Flipping
    this the wrong way still produces plausible-looking boxes — they are just
    mirrored vertically — which is why it is factored out and tested.
    """
    x0 = int(round(x * width))
    x1 = int(round((x + w) * width))
    y0 = int(round((1.0 - y - h) * height))
    y1 = int(round((1.0 - y) * height))
    return x0, y0, x1 - x0, y1 - y0


def _clip_rect(x, y, w, h, width, height):
    """Clamp a pixel rect into the image; returns None if nothing is left."""
    x0 = max(0, min(int(x), width))
    y0 = max(0, min(int(y), height))
    x1 = max(0, min(int(x) + int(w), width))
    y1 = max(0, min(int(y) + int(h), height))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _detect_macos(img):
    try:
        from ocrmac import ocrmac
    except ImportError as exc:
        # Log the real ImportError: "could not be loaded" is also what a
        # packaging mistake looks like, and the app is built --windowed so a
        # bare message is all the user can report back.
        _LOG.exception("ocrmac could not be imported")
        raise OCRUnavailable(
            "OCR isn't available on this system. The macOS text recogniser "
            "could not be loaded ({0!r}).".format(exc)) from exc
    except Exception as exc:
        _LOG.exception("ocrmac failed to initialise")
        raise OCRUnavailable(
            "OCR isn't available on this system. The macOS text recogniser "
            "would not start ({0!r}).".format(exc)) from exc

    width, height = img.size
    fd, path = tempfile.mkstemp(prefix="sv-board-", suffix=".png")
    os.close(fd)
    try:
        img.save(path, format="PNG")
        try:
            results = ocrmac.OCR(path, recognition_level="accurate").recognize()
        except Exception as exc:
            raise OCRUnavailable(
                "OCR isn't available on this system. macOS text recognition "
                "failed ({0}).".format(exc))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    boxes = []
    for item in results or []:
        try:
            text, _confidence, rect = item[0], item[1], item[2]
            x, y, w, h = rect
        except Exception:
            continue
        text = str(text).strip()
        # The module's own contract, not str.isdigit(): a token the values
        # API could never accept must never become an editable box.
        if not VALUE_RE.match(text):
            continue
        px, py, pw, ph = _from_vision_rect(x, y, w, h, width, height)
        rect = _clip_rect(px, py, pw, ph, width, height)
        if rect:
            rect["text"] = text
            boxes.append(rect)
    return boxes


def _detect_windows(img):
    try:
        import winocr
    except ImportError as exc:
        # Narrow, chained and logged on purpose: on Windows this same sentence
        # otherwise covers "winrt wasn't bundled by PyInstaller", "a PyWinRT
        # major broke winocr" and "this PC has no OCR", and only the last is
        # the user's problem. The logged ModuleNotFoundError names which.
        _LOG.exception("winocr could not be imported")
        raise OCRUnavailable(
            "OCR isn't available on this system. The Windows text recogniser "
            "could not be loaded ({0!r}).".format(exc)) from exc
    except Exception as exc:
        _LOG.exception("winocr failed to initialise")
        raise OCRUnavailable(
            "OCR isn't available on this system. The Windows text recogniser "
            "would not start ({0!r}).".format(exc)) from exc

    width, height = img.size

    # Windows.Media.Ocr refuses anything past OcrEngine.max_image_dimension.
    # The platform is fine and the image is fine — it is just too big — so
    # this is a BoardError, not "your system can't do OCR".
    try:
        limit = int(getattr(winocr.OcrEngine, "max_image_dimension", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit and max(width, height) > limit:
        raise BoardError(
            "That image is {0}px across, which is bigger than Windows text "
            "recognition can read ({1}px). Export the board at a smaller "
            "size and try again.".format(max(width, height), limit))

    try:
        result = winocr.recognize_pil_sync(img, "en")
    except Exception as exc:
        raise OCRUnavailable(
            "OCR isn't available on this system. Windows text recognition "
            "failed ({0}).".format(exc))

    _reject_tilt((result or {}).get("text_angle"))

    boxes = []
    # Word-level boxes, never line-level: a line box would swallow "350" plus
    # whatever sits beside it, and we need the number on its own.
    for line in (result or {}).get("lines", []) or []:
        for word in (line or {}).get("words", []) or []:
            text = str((word or {}).get("text", "")).strip()
            # Length is checked AFTER merging: a tracked "3 5 0" arrives as
            # three one-character words.
            if not ASCII_DIGITS_RE.match(text):
                continue
            r = (word or {}).get("bounding_rect") or {}
            try:
                rect = _clip_rect(round(r["x"]), round(r["y"]),
                                  round(r["width"]), round(r["height"]),
                                  width, height)
            except (KeyError, TypeError, ValueError):
                continue
            if rect:
                rect["text"] = text
                boxes.append(rect)
    return [b for b in _merge_word_boxes(boxes) if VALUE_RE.match(b["text"])]


# Windows de-skews before recognising, so anything past a fraction of a degree
# means the rects belong to a rotated frame, not to the pixels we would edit.
MAX_TEXT_ANGLE = 0.2


def _reject_tilt(angle):
    """Refuse a tilted capture rather than paint digits onto the wrong pixels.

    `OcrResult.text_angle` is the rotation Windows detected and internally
    corrected for; the word rects it returns are in that DE-SKEWED frame. On a
    1656px board a 3 degree tilt displaces a box by ~40px, so we would erase a
    clean patch of card, draw the new number there, and leave the old one
    showing — while reporting success.
    """
    if angle is None:
        return
    try:
        angle = float(angle)
    except (TypeError, ValueError):
        return
    if abs(angle) > MAX_TEXT_ANGLE:
        raise BoardError(
            "That picture of the board is slightly rotated, so the numbers "
            "can't be replaced accurately. Upload a straight export or "
            "screenshot of the board rather than a photo.")


def _joins(a, b):
    """Do two word boxes belong to the same number?"""
    top = max(a["y"], b["y"])
    bottom = min(a["y"] + a["h"], b["y"] + b["h"])
    overlap = bottom - top
    if overlap <= 0.6 * min(a["h"], b["h"]):
        return False
    gap = b["x"] - (a["x"] + a["w"])
    return -a["w"] < gap < 0.6 * max(a["h"], b["h"])


def _merge_word_boxes(boxes):
    """Rebuild numbers that Windows split, and drop stray label digits.

    Windows.Media.Ocr breaks words on horizontal whitespace, so a score set
    with letter-spacing comes back as "3", "5", "0" — three boxes where macOS
    gives one, and typing into any of them would composite three glyphs over
    its neighbours. Joining adjacent digit boxes on the same line restores the
    number and is a no-op when OCR already grouped them. Boxes far shorter
    than the board's typical number (the "1" in "TEAM 1") are then dropped.
    """
    if not boxes:
        return boxes
    merged = []
    for box in _reading_order(boxes):
        if merged and _joins(merged[-1], box):
            prev = merged[-1]
            x0 = min(prev["x"], box["x"])
            y0 = min(prev["y"], box["y"])
            x1 = max(prev["x"] + prev["w"], box["x"] + box["w"])
            y1 = max(prev["y"] + prev["h"], box["y"] + box["h"])
            merged[-1] = {"text": prev["text"] + box["text"],
                          "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        else:
            merged.append(dict(box))

    typical = _median([b["h"] for b in merged]) or 0
    return [b for b in merged if b["h"] >= typical * 0.5]


def _require_rect(box, width, height):
    """Validate a caller-supplied box rect; returns the clipped rect."""
    try:
        rect = _clip_rect(box["x"], box["y"], box["w"], box["h"], width, height)
    except (KeyError, TypeError, ValueError):
        raise BoardError("A number's position was given in the wrong form.")
    if rect is None:
        raise BoardError("A number's position falls outside the image.")
    if not VALUE_RE.match(str(box.get("text", "")).strip()):
        raise BoardError(
            "Only numbers of 1 to {0} digits can be made editable."
            .format(MAX_VALUE_LEN))
    return rect


def _reading_order(boxes):
    """Sort top-to-bottom then left-to-right, so box ids follow the layout."""
    if not boxes:
        return boxes
    heights = sorted(b["h"] for b in boxes)
    row_h = max(1.0, heights[len(heights) // 2] * 1.2)
    return sorted(boxes, key=lambda b: (int(b["y"] / row_h), b["x"]))


def detect_numbers(pil_image):
    """Find every purely-numeric text box.

    Returns [{"text", "x", "y", "w", "h"}] as top-left PIXEL rects, in reading
    order. Raises OCRUnavailable where no OS text recogniser exists.
    """
    img = pil_image.convert("RGB")
    system = platform.system()
    if system == "Darwin":
        boxes = _detect_macos(img)
    elif system == "Windows":
        boxes = _detect_windows(img)
    else:
        raise OCRUnavailable(
            "OCR isn't available on this system. Scoreboards need macOS or "
            "Windows text recognition.")
    return _reading_order(_drop_decorative(boxes))


# Decorative digits printed INTO the artwork — the sample board's trophy has a
# podium with 1/2/3 on it — are real text and Vision reads them at full
# confidence. They are not scores, and making them editable gives the operator
# three bogus boxes to mis-click on a busy Friday night. Scores are the large
# numerals, so keep only boxes within a fraction of the tallest. Measured on the
# user's real board: podium digits 32-39px vs scores 87-92px, so 0.6 cleanly
# takes 9 boxes to 6. Only applied when there is a clear size split, so a board
# whose numbers are all one size keeps every one of them.
DECORATIVE_RATIO = 0.6


def _drop_decorative(boxes):
    if len(boxes) < 2:
        return boxes
    tallest = max(b["h"] for b in boxes)
    kept = [b for b in boxes if b["h"] >= DECORATIVE_RATIO * tallest]
    return kept if kept else boxes


# ---------------------------------------------------------------- colour

def _quantise(px):
    half = QUANT // 2
    return (px[0] // QUANT * QUANT + half,
            px[1] // QUANT * QUANT + half,
            px[2] // QUANT * QUANT + half)


def _sample_colours(pixels):
    """Return (ink_rgb, bg_rgb) for a cropped box.

    BG is simply the most common colour. INK is scored on distance from BG
    times how much of it there is, with distance then saturation breaking
    ties. Distance alone is not enough: a handful of near-black pixels from a
    caption clipped into the crop out-scores thousands of hot-pink ones purely
    because black is further from cream, and the erase then wipes the real
    digits. Both colours are refined by averaging the real (unquantised)
    pixels in their bucket, so we keep the exact colour, not a bucket centre.
    """
    if not pixels:
        return (0, 0, 0), (255, 255, 255)

    counts = {}
    for px in pixels:
        key = _quantise(px)
        counts[key] = counts.get(key, 0) + 1

    bg_key = max(counts, key=lambda k: counts[k])
    floor = max(6, int(len(pixels) * 0.004))

    def score(key, count):
        dist = (abs(key[0] - bg_key[0]) + abs(key[1] - bg_key[1])
                + abs(key[2] - bg_key[2]))
        sat = max(key) - min(key)
        return (dist * count, dist, sat)

    ink_key = bg_key
    best = (-1, -1, -1)
    for key, count in counts.items():
        if key == bg_key or count < floor:
            continue
        s = score(key, count)
        if s > best:
            best, ink_key = s, key

    def refine(key):
        rs = gs = bs = n = 0
        for px in pixels:
            if _quantise(px) == key:
                rs += px[0]
                gs += px[1]
                bs += px[2]
                n += 1
        if not n:
            return key
        return (int(round(rs / n)), int(round(gs / n)), int(round(bs / n)))

    return refine(ink_key), refine(bg_key)


def _coverage(pixels, ink, bg):
    """Per-pixel ink coverage 0..255 (a bytearray parallel to `pixels`).

    Coverage is where the pixel falls along the bg->ink colour axis, so an
    anti-aliased edge pixel scores partially and harvested glyphs keep their
    soft edges. Pixels far OFF that axis (the dark team name sharing the crop
    with a pink number) are rejected outright by the residual test.
    """
    out = bytearray(len(pixels))
    dr = ink[0] - bg[0]
    dg = ink[1] - bg[1]
    db = ink[2] - bg[2]
    denom = float(dr * dr + dg * dg + db * db)
    if denom < 1.0:
        return out
    resid_max = max(40.0, 0.35 * math.sqrt(denom))
    resid_max2 = resid_max * resid_max
    for i, px in enumerate(pixels):
        vr = px[0] - bg[0]
        vg = px[1] - bg[1]
        vb = px[2] - bg[2]
        t = (vr * dr + vg * dg + vb * db) / denom
        if t <= 0.0:
            continue
        if t > 1.0:
            t = 1.0
        rr = vr - t * dr
        rg = vg - t * dg
        rb = vb - t * db
        if rr * rr + rg * rg + rb * rb > resid_max2:
            continue
        out[i] = int(t * 255.0)
    return out


# ---------------------------------------------------------------- geometry

def _crop_box(img, box):
    """Padded crop around a box; returns (x0, y0, w, h, pixels)."""
    width, height = img.size
    pad_x = BOX_PAD
    pad_y = max(BOX_PAD, int(round(box["h"] * 0.18)))
    x0 = max(0, box["x"] - pad_x)
    y0 = max(0, box["y"] - pad_y)
    x1 = min(width, box["x"] + box["w"] + pad_x)
    y1 = min(height, box["y"] + box["h"] + pad_y)
    crop = img.crop((x0, y0, x1, y1))
    return x0, y0, crop.width, crop.height, list(crop.getdata())


def _tight_pixels(img, box):
    """Pixels of the OCR rect ITSELF, with no padding.

    Colour sampling uses this and nothing else. The padded crop routinely
    reaches the dark "POINTS" caption under a score, and a caption further
    from the card colour than the number is would be sampled as the ink —
    after which the erase wipes the digits and the harvest records a 5px
    cap height. Masking still uses the padded crop, to catch the halo.
    """
    crop = img.crop((box["x"], box["y"],
                     box["x"] + box["w"], box["y"] + box["h"]))
    return list(crop.getdata())


def _column_segments(cover, w, h, threshold):
    """Runs of columns containing ink, plus the ink's row extent.

    Returns (segments, top, bottom) where segments are (col_start, col_end)
    inclusive. Column projection is enough here because these are short,
    upright, well-spaced numerals — the digits never overlap horizontally.
    """
    cutoff = int(threshold * 255)
    col_counts = [0] * w
    top, bottom = None, None
    for y in range(h):
        row = y * w
        for x in range(w):
            if cover[row + x] >= cutoff:
                col_counts[x] += 1
                if top is None:
                    top = y
                bottom = y
    if top is None:
        return [], None, None

    segments = []
    start = None
    for x in range(w):
        if col_counts[x]:
            if start is None:
                start = x
        elif start is not None:
            segments.append((start, x - 1))
            start = None
    if start is not None:
        segments.append((start, w - 1))

    segments = [s for s in segments
                if sum(col_counts[s[0]:s[1] + 1]) >= MIN_SEGMENT_PIXELS]
    return segments, top, bottom


def _limit_rows(cover, w, h, lo, hi):
    """Blank coverage outside a row window, in place.

    The padded crop reaches whatever sits just above the number — on the
    user's own board that is a pink rule under the team name, the SAME colour
    as the digits, so the residual test cannot reject it. Two rows of that
    rule at the top of the crop stretched the measured cap height from 85 to
    104 and lifted the baseline with it. Rows are the cheap, honest guard:
    the number lives inside its OCR rect, so nothing else may count.
    """
    lo = max(0, min(h, int(lo)))
    hi = max(lo, min(h, int(hi)))
    if lo:
        cover[:lo * w] = bytes(lo * w)
    if hi < h:
        cover[hi * w:] = bytes((h - hi) * w)
    return cover


def _rect_rows(y0, h, top, height, slack):
    """Crop-relative row window for an absolute band, with slack for the halo."""
    slack = max(2, int(round(height * slack)))
    return (int(top) - slack - y0, int(top + height) + slack - y0)


def _widen_segments(cover, w, h, segments, threshold):
    """Grow each segment out to the last column holding any ink at all.

    Segmentation runs at the strict CORE threshold so two digits can never
    merge — but that also throws away every glyph's outermost anti-aliased
    column, and the whole point of harvesting is that alpha IS the ink
    coverage. Widening afterwards puts the soft edge back; each side stops
    short of its neighbour, so segments still cannot touch.
    """
    if not segments:
        return segments
    cutoff = int(threshold * 255)
    inked = [False] * w
    for y in range(h):
        base = y * w
        for x in range(w):
            if cover[base + x] >= cutoff:
                inked[x] = True

    out = []
    for i, (c0, c1) in enumerate(segments):
        left_stop = out[-1][1] + 1 if out else 0
        while c0 > left_stop and inked[c0 - 1]:
            c0 -= 1
        right_stop = segments[i + 1][0] - 1 if i + 1 < len(segments) else w - 1
        while c1 < right_stop and inked[c1 + 1]:
            c1 += 1
        out.append((c0, c1))
    return out


def _safe_span(img, box, ink_x0, ink_x1, top, cap_height, bg, ink):
    """How far either side of the number we may draw before hitting artwork.

    A 4-digit score in a 3-digit slot has to go somewhere. Clamping only
    against the IMAGE lets it run off the card and over the illustration
    beside it, so we walk outwards from the existing digits while every row of
    the glyph band still matches the card, and stop at the first column that
    doesn't. `_draw_value` scales the number down to whatever this leaves.
    """
    y0 = max(0, int(top))
    y1 = min(img.height, y0 + max(1, int(cap_height)))
    if y1 <= y0:
        return box["x"], box["x"] + box["w"]

    reach = max(box["w"], cap_height * 4)
    lo = max(0, ink_x0 - reach)
    hi = min(img.width, ink_x1 + reach)
    band = list(img.crop((lo, y0, hi, y1)).getdata())
    bw = hi - lo
    bh = y1 - y0
    # Tolerance rides on how far the ink is from the card, so a textured or
    # gently graded card isn't mistaken for the edge of one.
    spread = (abs(ink[0] - bg[0]) + abs(ink[1] - bg[1]) + abs(ink[2] - bg[2]))
    tol = max(45, min(120, int(spread * 0.25)))

    def clean(col):
        for r in range(bh):
            px = band[r * bw + col]
            if (abs(px[0] - bg[0]) + abs(px[1] - bg[1])
                    + abs(px[2] - bg[2])) > tol:
                return False
        return True

    left = ink_x0
    col = ink_x0 - lo - 1
    while col >= 0 and clean(col):
        left -= 1
        col -= 1
    right = ink_x1 + 1
    col = right - lo
    while col < bw and clean(col):
        right += 1
        col += 1
    return left, right


def _style_key(ink, cap_height):
    """Bucket a box by ink colour and size — the folder its glyphs live in.

    Harvested bitmaps used to be filed by character alone and shared across
    the whole board, so three tiny podium digits could win the race and supply
    the "1" for every big pink score. Keying on appearance means a glyph is
    only ever reused where it actually belongs.
    """
    return "s{0:02x}{1:02x}{2:02x}h{3:02x}".format(
        min(255, max(0, int(ink[0])) // STYLE_COLOUR_STEP),
        min(255, max(0, int(ink[1])) // STYLE_COLOUR_STEP),
        min(255, max(0, int(ink[2])) // STYLE_COLOUR_STEP),
        min(255, max(0, int(cap_height)) // STYLE_SIZE_STEP))


def _harvested_styles(glyph_dir):
    """(style, ink, cap_height) for every style folder under glyph_dir."""
    out = []
    try:
        names = os.listdir(glyph_dir)
    except OSError:
        return out
    for name in sorted(names):
        m = _STYLE_RE.match(name)
        if not m or not os.path.isdir(os.path.join(glyph_dir, name)):
            continue
        r, g, b, cap = (int(m.group(i), 16) for i in (1, 2, 3, 4))
        half = STYLE_COLOUR_STEP // 2
        out.append((name,
                    (r * STYLE_COLOUR_STEP + half,
                     g * STYLE_COLOUR_STEP + half,
                     b * STYLE_COLOUR_STEP + half),
                    cap * STYLE_SIZE_STEP + STYLE_SIZE_STEP // 2))
    return out


def _median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    if n % 2:
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2.0


# ---------------------------------------------------------------- harvest

def _harvest_box(img, box, index, glyph_dir):
    """Analyse one numeric box: colours, glyph metrics, and glyph bitmaps.

    Returns the box record for board.json. Saving a glyph is first-wins
    WITHIN A STYLE: the earliest box of a given ink colour and size supplies
    that style's characters, and later boxes do not overwrite them.
    """
    x0, y0, w, h, pixels = _crop_box(img, box)
    ink, bg = _sample_colours(_tight_pixels(img, box))
    cover = _coverage(pixels, ink, bg)
    _limit_rows(cover, w, h, *_rect_rows(y0, h, box["y"], box["h"], 0.10))
    segments, top, bottom = _column_segments(cover, w, h, CORE_COVER)
    segments = _widen_segments(cover, w, h, segments, EDGE_COVER)

    record = {
        "id": "b{0}".format(index),
        "text": box["text"],
        "x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"],
        "ink": list(ink), "bg": list(bg),
    }

    cap_height = (bottom - top + 1) if top is not None else 0
    # A cap height far short of the box means the ink axis found something
    # other than the number (a caption edge, a shadow). Better to keep the
    # box editable on coarse metrics than to persist a 5px "digit" forever.
    if not segments or top is None or cap_height < box["h"] * MIN_CAP_RATIO:
        record["glyph_top"] = box["y"]
        record["cap_height"] = box["h"]
        record["gap"] = max(1, int(round(box["h"] * DEFAULT_GAP_RATIO)))
        record["gaps"] = []
        record["ink_cx"] = box["x"] + box["w"] / 2.0
        record["style"] = _style_key(ink, box["h"])
        record["safe_x0"] = box["x"]
        record["safe_x1"] = box["x"] + box["w"]
        return record

    ink_x0 = x0 + segments[0][0]
    ink_x1 = x0 + segments[-1][1]
    record["glyph_top"] = y0 + top
    record["cap_height"] = cap_height
    record["ink_cx"] = (ink_x0 + ink_x1 + 1) / 2.0
    record["style"] = _style_key(ink, cap_height)

    gaps = [segments[i + 1][0] - segments[i][1] - 1
            for i in range(len(segments) - 1)]
    gap = _median(gaps)
    if gap is None or gap < 0:
        gap = cap_height * DEFAULT_GAP_RATIO
    record["gap"] = int(round(gap))
    # Keep the individual gaps too, not just their median: a value that
    # reuses a digit PAIR from the original can then be laid out on the
    # spacing actually measured there instead of drifting a pixel.
    record["gaps"] = [max(0, g) for g in gaps]

    safe_lo, safe_hi = _safe_span(img, box, ink_x0, ink_x1,
                                  y0 + top, cap_height, bg, ink)
    record["safe_x0"] = safe_lo
    record["safe_x1"] = safe_hi

    # Only label glyphs when the segmentation agrees with what OCR read;
    # otherwise we would file the wrong bitmap under a character forever.
    if len(segments) == len(box["text"]):
        style_dir = os.path.join(glyph_dir, record["style"])
        os.makedirs(style_dir, exist_ok=True)
        for (c0, c1), char in zip(segments, box["text"]):
            path = os.path.join(style_dir, "{0}.png".format(char))
            if os.path.exists(path):
                continue
            gw = c1 - c0 + 1
            glyph = Image.new("RGBA", (gw, cap_height), tuple(ink) + (0,))
            alpha = Image.frombytes(
                "L", (gw, cap_height),
                _rows(cover, w, c0, c1, top, cap_height))
            glyph.putalpha(alpha)
            glyph.save(path, format="PNG")
    return record


def _rows(cover, w, c0, c1, top, rows):
    """Flatten a rectangle out of the coverage buffer (row-major)."""
    out = bytearray()
    for y in range(rows):
        base = (top + y) * w
        out.extend(cover[base + c0:base + c1 + 1])
    return bytes(out)


# ---------------------------------------------------------------- storage

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _board_dir(board_id):
    """Realpath-contained directory for a board id."""
    # fullmatch, not match: "$" also matches just before a trailing newline,
    # so "aaaaaaaaaaaa\n" would otherwise pass this last line of defence.
    if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
        raise BoardError("That board link isn't valid.")
    root = os.path.realpath(BOARDS_DIR)
    path = os.path.realpath(os.path.join(root, board_id))
    # Strictly BELOW the root — the boards root itself must never be the
    # target, or delete_board would rmtree every saved board at once.
    if not path.startswith(root + os.sep):
        raise BoardError("That board link isn't valid.")
    return path


def _clean_name(name):
    name = str(name or "").strip()
    name = re.sub(r"\s+", " ", name)[:MAX_NAME_LEN]
    return name or "Points board"


def _write_board(board):
    """Replace board.json atomically, via a temp file unique to this write.

    A shared "board.json.tmp" is not safe: two saves at once truncate each
    other's file, and the first os.replace renames the inode the second is
    still writing into — leaving trailing garbage in the LIVE board.json,
    which loses the board, its glyphs and its values for good.
    """
    directory = _board_dir(board["id"])
    path = os.path.join(directory, "board.json")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="board.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(board, f)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        raise BoardError(
            "That board couldn't be saved ({0}). Check there is free disk "
            "space and try again.".format(exc.strerror or exc))
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_board(board_id):
    """Load a saved board. BoardError if it is missing or unreadable."""
    path = os.path.join(_board_dir(board_id), "board.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            board = json.load(f)
    except (OSError, ValueError):
        raise BoardError("That board couldn't be opened — it may have been "
                         "deleted.")
    if not isinstance(board, dict) or not board.get("boxes"):
        raise BoardError("That board file is damaged. Upload the image again "
                         "to rebuild it.")
    board["id"] = board_id
    return board


def list_boards():
    """Saved boards, newest first."""
    out = []
    try:
        names = os.listdir(BOARDS_DIR)
    except OSError:
        return out
    for name in names:
        if not BOARD_ID_RE.fullmatch(name):
            continue
        try:
            board = load_board(name)
        except BoardError:
            continue
        out.append({
            "id": name,
            "name": board.get("name") or "Points board",
            "updated": board.get("updated") or board.get("created") or "",
            "box_count": len(board.get("boxes") or []),
        })
    out.sort(key=lambda b: b["updated"], reverse=True)
    return out


def create_board(pil_image, name, boxes=None):
    """OCR an uploaded board image, harvest its digits, and save it.

    `boxes` lets a caller supply the numeric rects directly, skipping OCR. The
    app never does — but the smoke test must run on Linux CI, where no OS text
    recogniser exists, and everything worth testing (harvest, erase, compose)
    happens after detection anyway.
    """
    img = pil_image.convert("RGB")
    if boxes is None:
        boxes = detect_numbers(img)
    else:
        boxes = _reading_order(
            [dict(b, **_require_rect(b, img.width, img.height)) for b in boxes])
    if not boxes:
        raise BoardError(
            "No numbers were found in that image. Make sure the picture is "
            "the full points board and the numbers are clear and upright.")
    if len(boxes) > MAX_BOXES:
        raise BoardError(
            "That image has {0} separate numbers on it, more than a points "
            "board should need ({1} at most). Crop it to just the board and "
            "try again.".format(len(boxes), MAX_BOXES))

    board_id = uuid.uuid4().hex[:12]
    directory = _board_dir(board_id)
    glyph_dir = os.path.join(directory, "glyphs")
    # Everything after the mkdir is cleaned up on ANY failure: board.json is
    # written last, and a directory without it is invisible to list_boards —
    # so a half-built board would leak the full-size upload forever with no
    # way for the user to find or delete it.
    try:
        os.makedirs(glyph_dir, exist_ok=True)
        img.save(os.path.join(directory, "source.png"), format="PNG")

        records = [_harvest_box(img, box, i, glyph_dir)
                   for i, box in enumerate(boxes)]
        stamp = _now()
        board = {
            "id": board_id,
            "name": _clean_name(name),
            "created": stamp,
            "updated": stamp,
            "width": img.width,
            "height": img.height,
            "boxes": records,
            # Defence in depth for boards written by an older build: a value
            # the values API could never accept must not be seeded here.
            "values": {r["id"]: r["text"] for r in records
                       if VALUE_RE.match(r["text"])},
        }
        _write_board(board)
    except BoardError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except (OSError, MemoryError):
        shutil.rmtree(directory, ignore_errors=True)
        raise BoardError(
            "That board couldn't be saved. Check there is free disk space "
            "and try again.")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return board


def save_values(board_id, values_dict, name=None):
    """Persist new numbers (and optionally a new name). Returns the board.

    Load, modify and write happen under one lock: the UI saves on a 400 ms
    debounce and two tabs on the same board overlap trivially, and an
    unguarded read-modify-write loses whichever save finished first.
    Renaming rides along so a save is ONE write, not two.
    """
    if not isinstance(values_dict, dict):
        raise BoardError("The new numbers weren't sent in the expected form.")

    with _BOARD_LOCK:
        board = load_board(board_id)
        known = {box["id"] for box in board["boxes"]}
        updated = dict(board.get("values") or {})
        for key, raw in values_dict.items():
            if key not in known:
                raise BoardError("This board has no number called '{0}'."
                                 .format(key))
            value = str(raw).strip()
            if not VALUE_RE.match(value):
                raise BoardError(
                    "'{0}' isn't a valid score — use 1 to {1} digits, numbers "
                    "only.".format(value, MAX_VALUE_LEN))
            updated[key] = value

        board["values"] = updated
        if name is not None:
            board["name"] = _clean_name(name)
        board["updated"] = _now()
        _write_board(board)
        return board


def rename_board(board_id, name):
    """Rename a saved board. Returns the updated board."""
    with _BOARD_LOCK:
        board = load_board(board_id)
        board["name"] = _clean_name(name)
        board["updated"] = _now()
        _write_board(board)
        return board


def delete_board(board_id):
    """Remove a board and everything harvested from it."""
    directory = _board_dir(board_id)
    if not os.path.isdir(directory):
        raise BoardError("That board couldn't be found — it may already have "
                         "been deleted.")
    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------- erase

def _dilate(mask, w, h, radius):
    """Grow a 0/1 mask by `radius` px (separable: rows, then columns)."""
    if radius < 1:
        return mask
    wide = bytearray(len(mask))
    for y in range(h):
        base = y * w
        row = mask[base:base + w]
        for x in range(w):
            if row[x]:
                lo = max(0, x - radius)
                hi = min(w, x + radius + 1)
                for i in range(lo, hi):
                    wide[base + i] = 1
    out = bytearray(len(mask))
    for x in range(w):
        for y in range(h):
            if wide[y * w + x]:
                lo = max(0, y - radius)
                hi = min(h, y + radius + 1)
                for j in range(lo, hi):
                    out[j * w + x] = 1
    return out


def _erase(img, box):
    """Remove a box's digits without disturbing anything else.

    Every masked pixel is replaced by the nearest UNMASKED pixel found by
    scanning vertically (up and down), which reproduces the card's local
    gradient, texture and any border it sits on. Flat-filling the whole rect
    would leave an obvious patch. BG is only the last resort, for a column
    with no clean pixel at all.
    """
    x0, y0, w, h, pixels = _crop_box(img, box)
    ink = tuple(box["ink"])
    bg = tuple(box["bg"])
    cover = _coverage(pixels, ink, bg)

    # Only the number's own band may be touched. Without this the erase eats
    # into whatever shares the ink colour just above it — the pink rule under
    # the team name on the user's board sits two rows inside the crop.
    cap = box.get("cap_height")
    if isinstance(cap, (int, float)) and cap > 0:
        window = _rect_rows(y0, h, box.get("glyph_top", box["y"]), cap, 0.10)
    else:
        window = _rect_rows(y0, h, box["y"], box["h"], 0.10)
    _limit_rows(cover, w, h, *window)

    cutoff = int(EDGE_COVER * 255)
    mask = bytearray(1 if c >= cutoff else 0 for c in cover)
    if not any(mask):
        return
    mask = _dilate(mask, w, h, ERASE_DILATE)
    # Dilation must not reach back out of the band either.
    _limit_rows(mask, w, h, *window)

    out = list(pixels)
    for x in range(w):
        # Nearest clean row above / below each row in this column.
        up = [None] * h
        last = None
        for y in range(h):
            if not mask[y * w + x]:
                last = y
            up[y] = last
        down = [None] * h
        last = None
        for y in range(h - 1, -1, -1):
            if not mask[y * w + x]:
                last = y
            down[y] = last
        for y in range(h):
            i = y * w + x
            if not mask[i]:
                continue
            a, b = up[y], down[y]
            if a is None and b is None:
                out[i] = bg
            elif a is None:
                out[i] = pixels[b * w + x]
            elif b is None:
                out[i] = pixels[a * w + x]
            else:
                out[i] = pixels[(a if (y - a) <= (b - y) else b) * w + x]

    patch = Image.new("RGB", (w, h))
    patch.putdata(out)
    img.paste(patch, (x0, y0))


# ---------------------------------------------------------------- compose

DIGIT_SET = "0123456789"


# The bundled fallback has ONE weight, and a board can use anything. Measured
# at cap height 86 the fallback's stroke is 18px, against 25px for Arial Black,
# 31px for Avenir Next Heavy, but only 9-12px for Helvetica Neue / Avenir Next
# Regular or Menlo. So a fixed weight is wrong in both directions — too spindly
# next to a chunky poster face (the reported "the 8 looks off"), too fat beside
# a light one. The fallback is therefore re-weighted to the measured stroke of
# whatever digits the board DID supply: dilated when it is too thin, eroded
# when it is too bold, before being fitted to the cap height, so height and
# width are unchanged either way.
MAX_FONT_GROW = 14
MAX_FONT_SHRINK = 8


def _stroke_width(mask):
    """Median horizontal ink-run length — a robust proxy for stroke weight."""
    px = mask.load()
    runs = []
    for y in range(mask.height):
        run = 0
        for x in range(mask.width):
            if px[x, y] > 128:
                run += 1
            elif run:
                runs.append(run)
                run = 0
        if run:
            runs.append(run)
    if not runs:
        return 0.0
    runs.sort()
    return float(runs[len(runs) // 2])


def _font_mask(char, cap_height, grow):
    """Alpha mask for `char` at `cap_height`, re-weighted `grow` steps first.

    Positive `grow` dilates (bolder), negative erodes (lighter).

    The reference height is the whole digit SET's band, never this
    character's own ink height. A flat-topped "1", "4" or "7" is genuinely
    shorter than a round "0" — scaling each fallback so its own ink fills the
    cap height stretches the flat ones and breaks the shared top line. So the
    set is rendered once to find the band, and the character is cropped out of
    that band, keeping its true position within it. The dilation is applied to
    BOTH canvases so the band grows with the glyph and alignment is preserved.
    """
    cap_height = max(1, min(int(cap_height), MAX_CAP_HEIGHT))
    size = max(8, min(int(cap_height * 3), 600))
    font = fonts.load("digits", size)
    extent = font.getbbox(DIGIT_SET)
    pad = size + 2 * abs(grow)
    canvas_size = (max(1, extent[2] + pad), max(1, extent[3] + pad))

    every = Image.new("L", canvas_size, 0)
    ImageDraw.Draw(every).text((0, 0), DIGIT_SET, font=font, fill=255)
    one = Image.new("L", canvas_size, 0)
    ImageDraw.Draw(one).text((0, 0), char, font=font, fill=255)
    kernel = ImageFilter.MaxFilter(3) if grow > 0 else ImageFilter.MinFilter(3)
    for _ in range(abs(grow)):
        every = every.filter(kernel)
        one = one.filter(kernel)

    band = every.getbbox()
    box = one.getbbox()
    if not band or not box:
        # Eroded away entirely — this glyph cannot go that light.
        return None
    mask = one.crop((box[0], band[1], box[2], band[3]))
    scale = cap_height / float(max(1, mask.height))
    width = max(1, int(round(mask.width * scale)))
    return mask.resize((width, cap_height), _LANCZOS)


def _font_glyph(char, cap_height, ink, target_stroke=None):
    """Draw a digit the board never showed us, matched to the harvested band
    and — when we know what its neighbours weigh — to their stroke too."""
    if not target_stroke:
        best = _font_mask(char, cap_height, 0)
        if best is None:
            return None
    else:
        # Stroke rises monotonically with `grow`, so the error is unimodal:
        # walk light -> bold and stop once it starts getting worse again.
        best = None
        best_err = None
        for grow in range(-MAX_FONT_SHRINK, MAX_FONT_GROW + 1):
            mask = _font_mask(char, cap_height, grow)
            if mask is None:
                continue                 # eroded to nothing; try a heavier step
            err = abs(_stroke_width(mask) - float(target_stroke))
            if best_err is None or err < best_err:
                best, best_err = mask, err
            elif best is not None:
                break
        if best is None:
            best = _font_mask(char, cap_height, 0)
        if best is None:
            return None
    glyph = Image.new("RGBA", best.size, tuple(ink) + (0,))
    glyph.putalpha(best)
    return glyph


def _tint(glyph, ink):
    """Repaint a glyph's RGB with this box's ink, keeping its alpha.

    A harvested bitmap carries its donor box's colour in RGB. Without this, a
    borrowed glyph drags the donor's palette with it — a white podium "1" in
    the middle of a pink score reads as a gap.
    """
    solid = Image.new("RGBA", glyph.size, tuple(ink) + (0,))
    solid.putalpha(glyph.getchannel("A"))
    return solid


def _load_glyph(glyph_dir, styles, style, char, cap_height, ink):
    """Best harvested bitmap for `char`, recoloured and scaled; else a font.

    Preference order: this box's own style, then the nearest style whose ink
    is close enough to borrow from, then a pre-cluster board's flat layout,
    then the bundled font. Everything is retinted to the target ink, so no
    path can leak another card's colour.
    """
    paths = []
    if style:
        paths.append(os.path.join(glyph_dir, style, "{0}.png".format(char)))
    near = []
    for name, other_ink, other_cap in styles:
        if name == style:
            continue
        dist = (abs(other_ink[0] - ink[0]) + abs(other_ink[1] - ink[1])
                + abs(other_ink[2] - ink[2]))
        if dist > STYLE_INK_TOLERANCE:
            continue
        near.append((dist, abs(other_cap - cap_height), name))
    near.sort()
    paths.extend(os.path.join(glyph_dir, name, "{0}.png".format(char))
                 for _d, _c, name in near)
    paths.append(os.path.join(glyph_dir, "{0}.png".format(char)))

    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            glyph = Image.open(path).convert("RGBA")
        except Exception:
            continue
        if glyph.height <= 0:
            continue
        glyph = _tint(glyph, ink)
        if abs(glyph.height - cap_height) > 1:
            scale = cap_height / float(glyph.height)
            glyph = glyph.resize(
                (max(1, int(round(glyph.width * scale))),
                 max(1, int(cap_height))), _LANCZOS)
        return glyph
    # Nothing harvested for this character — weigh its neighbours so the
    # bundled font can be emboldened to match rather than looking spindly
    # beside them.
    return _font_glyph(char, cap_height, ink,
                       _harvested_stroke(paths, cap_height))


_STROKE_CACHE = {}


def _harvested_stroke(paths, cap_height):
    """Median stroke of whatever glyphs DO exist alongside a missing one.

    `paths` is the same preference-ordered list _load_glyph just tried, so we
    measure siblings in the box's own style first. Scaled to the target cap
    height, because a glyph borrowed from a smaller style is thinner in
    absolute pixels but the same weight relative to its height.
    """
    dirs = []
    for path in paths:
        folder = os.path.dirname(path)
        if folder not in dirs:
            dirs.append(folder)
    for folder in dirs:
        key = (folder, int(cap_height))
        if key in _STROKE_CACHE:
            if _STROKE_CACHE[key]:
                return _STROKE_CACHE[key]
            continue
        widths = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".png") or len(name) != 5:
                continue
            try:
                sibling = Image.open(os.path.join(folder, name)).convert("RGBA")
            except Exception:
                continue
            if sibling.height <= 0:
                continue
            alpha = sibling.split()[3]
            if sibling.height != cap_height:
                scale = cap_height / float(sibling.height)
                alpha = alpha.resize(
                    (max(1, int(round(sibling.width * scale))),
                     max(1, int(cap_height))), _LANCZOS)
            width = _stroke_width(alpha)
            if width > 0:
                widths.append(width)
        widths.sort()
        result = widths[len(widths) // 2] if widths else 0.0
        _STROKE_CACHE[key] = result
        if result:
            return result
    return 0.0


def _pair_gaps(box, value, fallback, widths=None):
    """Spacing for each digit pair, reusing what the board actually measured.

    Re-laying every digit on one median gap shifts the inner digits by a pixel
    against the original; where the new value keeps a pair the board already
    had ("35" in "350" -> "351"), the gap measured there is exact.

    For a pair the board never showed us, an ink-to-ink gap is the wrong thing
    to copy: it depends on how wide the LEFT digit is. A "1" is narrow and
    carries a lot of side bearing, so the gap measured in "12" is far wider
    than the one in "34" — reusing it spaced an unseen pair visibly apart.
    Scoreboards almost always use tabular figures, so what is actually constant
    is the PITCH (glyph width + gap). When we know the glyph widths we derive
    the gap from the median pitch instead, and only fall back to the measured
    median gap when we cannot.
    """
    text = str(box.get("text") or "")
    measured = box.get("gaps") or []
    lookup = {}
    for i in range(min(len(measured), max(0, len(text) - 1))):
        lookup.setdefault(text[i:i + 2], measured[i])

    pitch = None
    if widths:
        pitches = [widths[text[i]] + measured[i]
                   for i in range(min(len(measured), max(0, len(text) - 1)))
                   if text[i] in widths]
        if pitches:
            pitches.sort()
            pitch = pitches[len(pitches) // 2]

    gaps = []
    for i in range(max(0, len(value) - 1)):
        pair = value[i:i + 2]
        if pair in lookup:
            gaps.append(int(round(lookup[pair])))
            continue
        if pitch is not None and value[i] in widths:
            gaps.append(max(0, int(round(pitch - widths[value[i]]))))
            continue
        gaps.append(int(round(fallback)))
    return gaps


def _draw_span(img, box):
    """The columns this box may draw into — its card, not the whole image."""
    lo = box.get("safe_x0")
    hi = box.get("safe_x1")
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return 0, img.width
    lo = max(0, lo)
    hi = min(img.width, hi)
    if hi - lo < 2:
        return 0, img.width
    return lo, hi


def _draw_value(img, glyph_dir, box, value):
    """Composite `value` where the old number was: same baseline, same centre.

    A longer value grows evenly to both sides, and is scaled down if it would
    otherwise leave the card — clamping only against the image edge let a
    4-digit score paint straight over the artwork beside it.
    """
    cap_height = max(1, int(box.get("cap_height") or box["h"]))
    ink = tuple(box.get("ink") or (0, 0, 0))
    gap = int(box.get("gap") or round(cap_height * DEFAULT_GAP_RATIO))
    style = box.get("style") or ""
    styles = _harvested_styles(glyph_dir)
    # Widths of every digit involved, at the box's own cap height, so the
    # pitch estimate above can reason about narrow digits like "1".
    widths = {}
    for ch in set(str(box.get("text") or "")) | set(value):
        measure = _load_glyph(glyph_dir, styles, style, ch, cap_height, ink)
        if measure is not None:
            widths[ch] = measure.width
    gaps = _pair_gaps(box, value, gap, widths)

    def compose(cap, scale):
        drawn = [g for g in (_load_glyph(glyph_dir, styles, style, ch, cap,
                                         ink)
                             for ch in value) if g is not None]
        spacing = [max(0, int(round(g * scale))) for g in gaps]
        width = sum(g.width for g in drawn)
        width += sum(spacing[:max(0, len(drawn) - 1)])
        return drawn, spacing, width

    glyphs, spacing, total = compose(cap_height, 1.0)
    if not glyphs:
        return

    lo, hi = _draw_span(img, box)
    room = hi - lo
    cap = cap_height
    if 0 < room < total:
        shrink = room / float(total)
        cap = max(4, int(cap_height * shrink))
        glyphs, spacing, total = compose(cap, shrink)
        if not glyphs:
            return

    centre = float(box.get("ink_cx", box["x"] + box["w"] / 2.0))
    # Shrinking keeps the baseline, not the top, so a scaled number still
    # sits on the line the rest of the board uses.
    top = int(round(box.get("glyph_top", box["y"]))) + (cap_height - cap)

    x = int(round(centre - total / 2.0))
    if total <= room:
        x = max(lo, min(x, hi - total))
    x = max(0, min(x, img.width - total)) if total <= img.width else 0

    for i, glyph in enumerate(glyphs):
        img.paste(glyph, (x, top), glyph)
        x += glyph.width + (spacing[i] if i < len(spacing) else 0)


# ---------------------------------------------------------------- render

def _as_board(board):
    """Accept either a board id or an already-loaded board dict.

    app.py holds only the id (it comes straight off the URL) while the smoke
    test and internal callers already have the dict; taking both means neither
    side has to load the board twice or guess which form is wanted.
    """
    if isinstance(board, str):
        return load_board(board)
    if isinstance(board, dict) and board.get("boxes"):
        return board
    raise BoardError("That board is empty — upload the image again.")


def render_board(board):
    """The board's source image with its current values applied (RGB)."""
    board_dict = _as_board(board)
    directory = _board_dir(board_dict.get("id"))
    glyph_dir = os.path.join(directory, "glyphs")
    try:
        img = Image.open(os.path.join(directory, "source.png")).convert("RGB")
    except Exception:
        raise BoardError("The original board image is missing. Upload it "
                         "again to rebuild this board.")

    values = board_dict.get("values") or {}
    for box in board_dict["boxes"]:
        value = str(values.get(box["id"], box["text"])).strip()
        if not value or value == box["text"]:
            continue
        if not VALUE_RE.match(value):
            raise BoardError(
                "'{0}' isn't a valid score — use 1 to {1} digits, numbers "
                "only.".format(value, MAX_VALUE_LEN))
        _erase(img, box)
        _draw_value(img, glyph_dir, box, value)
    return img


def export_board(board):
    """Render the board and save it to exports/. Returns the filename."""
    board_dict = _as_board(board)
    img = render_board(board_dict)
    out_path = export_path("board", board_dict.get("name") or "points",
                           ext=".png")
    img.save(out_path, format="PNG")
    return os.path.basename(out_path)
