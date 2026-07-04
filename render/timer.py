"""Countdown timer renderer.

Renders a MM:SS (or H:MM:SS) countdown to an H.264 MP4 through FrameEncoder.
Three styles:

  classic - huge digits only
  ring    - digits inside a circular accent arc that depletes clockwise
  bar     - digits above a bottom progress bar that shrinks toward the left

Render-speed strategy (a 5-minute ring timer must finish in well under
3 minutes):

  * the vignette background (plus the style's static track) is built ONCE;
  * the digits block is re-rendered only when the displayed second changes;
  * per-frame work is one full-frame copy plus the ring/bar overlay paste;
  * frames are fed at a low input fps (1-10) and ffmpeg duplicates them up
    to the constant 30 fps output.

Anti-aliasing: the ring arc and the bar capsule are drawn on supersampled
single-channel masks and box-downsampled with Image.reduce(), then used as
paste masks for a solid accent tile.  That keeps edges smooth without ever
redrawing the full 1920x1080 canvas at high resolution.
"""

import math
import os
import re

from PIL import Image, ImageDraw

from . import fonts
from .encoder import FrameEncoder, WIDTH, HEIGHT, export_path

# ---- shared visual language -------------------------------------------------
BG_BASE = (14, 16, 19)           # #0e1013
BG_EDGE = (7, 8, 10)             # #07080a  (vignette edges)
TRACK = (35, 38, 43)             # #23262b  (inactive ring/bar track)
DIGITS_COLOR = (242, 240, 235)   # #f2f0eb
DEFAULT_ACCENT = (232, 180, 79)  # #e8b44f

STYLES = ("classic", "ring", "bar")

# ---- ring geometry -----------------------------------------------------------
RING_CX, RING_CY = WIDTH // 2, HEIGHT // 2
RING_RADIUS = 400        # centerline radius
RING_THICKNESS = 26
_RING_PAD = 6
_RING_TILE = 2 * (RING_RADIUS + RING_THICKNESS // 2 + _RING_PAD)   # 838
_RING_ORIGIN = (RING_CX - _RING_TILE // 2, RING_CY - _RING_TILE // 2)
_RING_SS = 3             # supersample factor for the arc mask

# ---- bar geometry ------------------------------------------------------------
BAR_MARGIN = 140
BAR_TOP = 944
BAR_HEIGHT = 16
BAR_WIDTH = WIDTH - 2 * BAR_MARGIN   # 1640
_BAR_SS = 4

_DIGITS_PAD = 8          # transparent padding around the digits block


# ---- defensive option parsing ------------------------------------------------

def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_hex(value, default):
    """'#rrggbb' -> (r, g, b); anything malformed falls back to default."""
    if isinstance(value, str):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
        if m:
            v = m.group(1)
            return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    return default


# ---- background (built once, shared by every render) -------------------------

_bg_cache = None


def _background():
    """1920x1080 #0e1013 base with a radial vignette to #07080a at the edges.

    The radial mask is computed per-pixel on a small grid and upscaled,
    which is visually identical and ~100x cheaper than full resolution.
    """
    global _bg_cache
    if _bg_cache is None:
        sw, sh = 320, 180
        cx, cy = (sw - 1) / 2.0, (sh - 1) / 2.0
        max_d = math.hypot(cx, cy)
        vals = bytearray(sw * sh)
        i = 0
        for y in range(sh):
            dy = y - cy
            for x in range(sw):
                f = math.hypot(x - cx, dy) / max_d
                vals[i] = int(255 * (f ** 1.8))
                i += 1
        mask = Image.frombytes("L", (sw, sh), bytes(vals))
        mask = mask.resize((WIDTH, HEIGHT), Image.BILINEAR)
        base = Image.new("RGB", (WIDTH, HEIGHT), BG_BASE)
        edge = Image.new("RGB", (WIDTH, HEIGHT), BG_EDGE)
        _bg_cache = Image.composite(edge, base, mask)
    return _bg_cache


# ---- ring / bar masks ---------------------------------------------------------

def _ring_mask(frac):
    """Anti-aliased L mask (tile-sized) of the remaining arc.

    Arc is anchored at 12 o'clock and sweeps clockwise by 360*frac degrees
    (PIL angles start at 3 o'clock and increase clockwise in screen coords,
    so 12 o'clock is 270).  frac=1.0 yields the full circle (used for the
    track too).
    """
    s = _RING_SS
    size = _RING_TILE * s
    m = Image.new("L", (size, size), 0)
    frac = max(0.0, min(1.0, frac))
    extent = 360.0 * frac
    if extent > 0.05:
        d = ImageDraw.Draw(m)
        c = size / 2.0
        outer = (RING_RADIUS + RING_THICKNESS / 2.0) * s
        bbox = [c - outer, c - outer, c + outer, c + outer]
        if extent >= 359.95:
            d.arc(bbox, 0, 360, fill=255, width=RING_THICKNESS * s)
        else:
            d.arc(bbox, 270.0, 270.0 + extent, fill=255,
                  width=RING_THICKNESS * s)
    return m.reduce(s)


def _draw_capsule(draw, x0, y0, x1, y1, fill):
    """Filled rectangle with fully rounded (semicircular) ends."""
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    r = min(w, h) / 2.0
    draw.ellipse([x0, y0, x0 + 2 * r, y1], fill=fill)
    draw.ellipse([x1 - 2 * r, y0, x1, y1], fill=fill)
    if w > 2 * r:
        draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)


def _bar_mask(frac):
    """Anti-aliased L mask of the remaining bar fill, anchored left."""
    s = _BAR_SS
    m = Image.new("L", (BAR_WIDTH * s, BAR_HEIGHT * s), 0)
    frac = max(0.0, min(1.0, frac))
    w = int(round(frac * BAR_WIDTH * s))
    if w > 0:
        d = ImageDraw.Draw(m)
        _draw_capsule(d, 0, 0, w, BAR_HEIGHT * s, 255)
    return m.reduce(s)


# ---- digits ------------------------------------------------------------------

def _digits_metrics(size):
    """Font + fixed-width slot metrics so the time string never jitters.

    slot   = widest digit advance (every digit is centered in a slot this
             wide); colon slot is ~55% of that.
    Vertical placement uses one shared baseline for every character, so
    nothing moves vertically either.
    """
    font = fonts.load("digits", size)
    slot = max(font.getlength(ch) for ch in "0123456789")
    ascent, _descent = font.getmetrics()
    # getbbox y-values are relative to the line top (default 'la' anchor);
    # the baseline sits at +ascent from the line top.
    _x0, y0, _x1, y1 = font.getbbox("0123456789:")
    return {
        "font": font,
        "slot": slot,
        "colon": slot * 0.55,
        "baseline_off": ascent - y0,   # baseline, measured from glyph top
        "glyph_h": y1 - y0,
    }


def _text_width(text, met):
    return sum(met["colon"] if ch == ":" else met["slot"] for ch in text)


def _render_digits(text, color, met):
    """RGBA block of the time string, one character per fixed-width slot."""
    w = int(math.ceil(_text_width(text, met))) + 2 * _DIGITS_PAD
    h = int(math.ceil(met["glyph_h"])) + 2 * _DIGITS_PAD
    block = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(block)
    baseline = _DIGITS_PAD + met["baseline_off"]
    x = float(_DIGITS_PAD)
    for ch in text:
        cw = met["colon"] if ch == ":" else met["slot"]
        # 'ms' anchor: horizontally centered in the slot, on the shared
        # baseline -> zero jitter in either axis.
        d.text((x + cw / 2.0, baseline), ch, font=met["font"],
               fill=color + (255,), anchor="ms")
        x += cw
    return block


def _format_remaining(rem, total):
    if total >= 3600:
        return "{0}:{1:02d}:{2:02d}".format(
            rem // 3600, (rem % 3600) // 60, rem % 60)
    return "{0}:{1:02d}".format(rem // 60, rem % 60)


def _classic_font_size(initial_text):
    """Auto-size classic digits: fit ~1600px wide, capped at 400px."""
    ref = 200
    met = _digits_metrics(ref)
    w = _text_width(initial_text, met)
    if w <= 0:
        return 400
    fit = int(ref * 1600.0 / w)
    return max(60, min(400, fit))


# ---- fps economy ---------------------------------------------------------------

def _input_fps(style, total):
    if style == "classic":
        return 1           # digits change once per second; nothing else moves
    if total <= 600:
        return 10
    if total <= 1800:
        return 4
    return 2


# ---- main entry point -----------------------------------------------------------

def render_timer(options, progress_cb):
    """Render a countdown timer MP4; returns the output filename basename.

    options: minutes, seconds (total 5..7200 s), style classic|ring|bar,
             accent '#rrggbb', warn_last10 bool, hold_seconds 0..30.
    Counts down from the total to 0:00, then holds at 0:00 for hold_seconds.
    """
    options = options or {}
    if progress_cb is None:
        progress_cb = lambda pct: None  # noqa: E731

    minutes = _to_int(options.get("minutes"), 0)
    seconds = _to_int(options.get("seconds"), 0)
    total = max(5, min(7200, minutes * 60 + seconds))
    style = str(options.get("style", "classic")).lower()
    if style not in STYLES:
        style = "classic"
    accent = _parse_hex(options.get("accent"), DEFAULT_ACCENT)
    warn_last10 = bool(options.get("warn_last10", True))
    hold = max(0, min(30, _to_int(options.get("hold_seconds"), 5)))

    fps = _input_fps(style, total)
    total_frames = (total + hold) * fps

    # Static layers: vignette + this style's track, built once.
    bg = _background().copy()
    accent_tile = None
    if style == "ring":
        bg.paste(Image.new("RGB", (_RING_TILE, _RING_TILE), TRACK),
                 _RING_ORIGIN, _ring_mask(1.0))
        accent_tile = Image.new("RGB", (_RING_TILE, _RING_TILE), accent)
    elif style == "bar":
        bg.paste(Image.new("RGB", (BAR_WIDTH, BAR_HEIGHT), TRACK),
                 (BAR_MARGIN, BAR_TOP), _bar_mask(1.0))
        accent_tile = Image.new("RGB", (BAR_WIDTH, BAR_HEIGHT), accent)

    initial_text = _format_remaining(total, total)
    if style == "ring":
        size, digits_cy = 190, RING_CY
    elif style == "bar":
        size, digits_cy = 330, 500       # slightly above center
    else:
        size, digits_cy = _classic_font_size(initial_text), HEIGHT // 2
    met = _digits_metrics(size)

    out_path = export_path(
        "timer", "{0}m{1:02d}s_{2}".format(total // 60, total % 60, style))

    cached_key = None      # (text, color) of the base currently cached
    cached_base = None     # background + digits for that second
    last_pct = -1
    with FrameEncoder(out_path, input_fps=fps) as enc:
        for i in range(total_frames):
            t = i / float(fps)
            elapsed = int(t)
            rem = total - elapsed if elapsed < total else 0
            text = _format_remaining(rem, total)
            color = accent if (warn_last10 and rem <= 10) else DIGITS_COLOR
            key = (text, color)
            if key != cached_key:
                block = _render_digits(text, color, met)
                base = bg.copy()
                base.paste(block,
                           (WIDTH // 2 - block.width // 2,
                            digits_cy - block.height // 2),
                           block)
                cached_base, cached_key = base, key

            if style == "classic":
                frame = cached_base          # nothing animates within a second
            else:
                frame = cached_base.copy()
                frac = max(0.0, 1.0 - t / float(total))
                if frac > 0.0:
                    if style == "ring":
                        frame.paste(accent_tile, _RING_ORIGIN,
                                    _ring_mask(frac))
                    else:
                        frame.paste(accent_tile, (BAR_MARGIN, BAR_TOP),
                                    _bar_mask(frac))
            enc.add_frame(frame)

            pct = (i + 1) * 100 // total_frames
            if pct != last_pct:
                progress_cb(pct)
                last_pct = pct

    return os.path.basename(out_path)
