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
import threading
from collections import OrderedDict

from PIL import Image, ImageDraw

from . import fonts
from .encoder import (FrameEncoder, WIDTH, HEIGHT, encode_parallel,
                      export_path)

TIMER_OUTPUT_FPS = 15   # see the FrameEncoder call in render_timer()

# ---- shared visual language -------------------------------------------------
BG_BASE = (14, 16, 19)           # #0e1013
BG_EDGE = (7, 8, 10)             # #07080a  (vignette edges)
TRACK = (35, 38, 43)             # #23262b  (inactive ring/bar track)
DIGITS_COLOR = (242, 240, 235)   # #f2f0eb
DEFAULT_ACCENT = (232, 180, 79)  # #e8b44f

STYLES = ("classic", "ring", "bar")
# Clock mode has no "depletes to zero" story, so a shrinking bar doesn't
# make sense for it — only classic and ring are offered (app.py enforces
# this at validation time; kept here too so the renderer never guesses).
CLOCK_STYLES = ("classic", "ring")

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

    The arc END is pinned at 12 o'clock and the start edge advances
    clockwise as frac shrinks, so the exposed track gap grows clockwise
    from 12 — i.e. the ring depletes clockwise (PIL angles start at
    3 o'clock and increase clockwise in screen coords, so 12 o'clock is
    270).  frac=1.0 yields the full circle (used for the track too).
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
            d.arc(bbox, 270.0 + (360.0 - extent), 630.0, fill=255,
                  width=RING_THICKNESS * s)
    return m.reduce(s)


def _ring_seconds_mask(frac):
    """Anti-aliased L mask (tile-sized) of the clock's seconds-hand fill.

    Mirror image of _ring_mask: instead of a full ring that DEPLETES
    clockwise from 12 as frac shrinks (countdown), this is an EMPTY ring
    that FILLS clockwise from 12 as frac grows, so at frac=0 (the top of a
    minute) nothing is drawn and at frac=1 (the next top of the minute) the
    circle is complete — a plain "arc from 270 degrees, sweeping clockwise
    by frac*360 degrees" rather than _ring_mask's pinned-at-12 remainder
    arc, so the two never share code paths (and countdown pixels can't be
    touched by a clock-only change).
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
        "dot": slot * 0.40,            # clock-mode millis separator only;
                                        # countdown text never contains "."
        "baseline_off": ascent - y0,   # baseline, measured from glyph top
        "glyph_h": y1 - y0,
    }


def _slot_width(ch, met):
    if ch == ":":
        return met["colon"]
    if ch == ".":
        return met["dot"]
    return met["slot"]


def _text_width(text, met):
    return sum(_slot_width(ch, met) for ch in text)


def _render_digits(text, color, met):
    """RGBA block of the time string, one character per fixed-width slot.

    A leading space occupies (but leaves empty) a full digit slot, so a
    space-padded shorter time keeps the exact same block width and slot
    positions as the initial one. "." (clock-mode millis only) gets its own
    narrower slot; countdown text never contains one, so this is a no-op
    for every existing caller.
    """
    w = int(math.ceil(_text_width(text, met))) + 2 * _DIGITS_PAD
    h = int(math.ceil(met["glyph_h"])) + 2 * _DIGITS_PAD
    block = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(block)
    baseline = _DIGITS_PAD + met["baseline_off"]
    x = float(_DIGITS_PAD)
    for ch in text:
        cw = _slot_width(ch, met)
        # 'ms' anchor: horizontally centered in the slot, on the shared
        # baseline -> zero jitter in either axis.
        if ch != " ":
            d.text((x + cw / 2.0, baseline), ch, font=met["font"],
                   fill=color + (255,), anchor="ms")
        x += cw
    return block


def _format_remaining(rem, total):
    """Format `rem` with field widths fixed by the INITIAL total, zero-padded.

    A 10-minute timer renders "10:00" then "09:59" (not " 9:59"): the string
    is always the same width, so the digits never jitter AND the visible
    glyphs are always dead-centred. The old space-padding kept the block
    width constant but left the visible text half a slot off-centre for
    almost the whole video — clearly visible inside the ring.
    """
    if total >= 3600:
        return "{0}:{1:02d}:{2:02d}".format(
            rem // 3600, (rem % 3600) // 60, rem % 60)
    if total >= 600:
        return "{0:02d}:{1:02d}".format(rem // 60, rem % 60)
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


# ---- clock mode ------------------------------------------------------------
#
# format_clock_time is a pure function (no fonts, no Image) so scripts/smoke
# can exercise midnight/noon wrap-around and millis exactness directly.

CLOCK_MS_SCALE = 0.55    # millis are drawn at 55% of the main digit size
CLOCK_TAG_SCALE = 0.28   # the AM/PM tag at 28%
_DAY_MS = 24 * 60 * 60 * 1000


def format_clock_time(total_ms, fmt, show_seconds, show_millis):
    """Pure clock-mode display formatter — the contract the renderer AND
    the JS preview must both match exactly (see docs/specs/clock-mode.md).

    `total_ms` is milliseconds since midnight; it wraps at 24h so a start
    of 23:59:55 plus 10s elapsed comes back as 00:00:05 (a real clock
    rolling over, not an error). Returns (main_text, tag):

      * `tag` is "AM"/"PM" in 12-hour format, "" in 24-hour format.
      * `main_text` is the digit string, with milliseconds (when shown)
        baked onto the end as ".mmm" — always exactly 4 characters, so a
        caller that needs to draw them smaller can split them off with
        `main_text[-4:]` / `main_text[:-4]` rather than re-deriving them.
    """
    if show_millis:
        show_seconds = True   # can't show millis without seconds
    total_ms = total_ms % _DAY_MS
    total_seconds = total_ms // 1000
    ms = total_ms % 1000
    h = (total_seconds // 3600) % 24
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60

    tag = ""
    if fmt == "12h":
        tag = "AM" if h < 12 else "PM"
        h12 = h % 12
        h_str = str(h12 if h12 else 12)   # midnight/noon both display "12"
    else:
        h_str = "{0:02d}".format(h)

    if show_millis:
        main = "{0}:{1:02d}:{2:02d}.{3:03d}".format(h_str, m, s, ms)
    elif show_seconds:
        main = "{0}:{1:02d}:{2:02d}".format(h_str, m, s)
    else:
        main = "{0}:{1:02d}".format(h_str, m)
    return main, tag


# Inside the ring the time must clear the track on both sides. The ring's
# inner edge is at RING_RADIUS - RING_THICKNESS/2 = 387px from centre; leave
# 36px of air each side.
RING_INNER_FIT = 2 * (RING_RADIUS - RING_THICKNESS // 2) - 72      # 702px
RING_DIGITS_MAX = 190            # the countdown ring's digit size


def _clock_font_size(main_text, show_millis, has_tag, fit_width=1600.0,
                     cap=400):
    """Auto-size clock digits: fit `fit_width` px wide, capped at `cap`.

    Same ref-then-scale approach as _classic_font_size, generalised to the
    clock's smaller millis (55%) and tag (28%) runs, which are drawn at a
    fraction of the main size and so must contribute proportionally less
    to the width being fitted. `main_text` excludes the ".mmm" suffix (the
    caller splits it off, same convention as _render_clock uses to draw).

    The ring style passes its own fit: the countdown ring's fixed 190px is
    right for "5:00" and "10:00", but a 24-hour clock with milliseconds is
    "19:59:50.000" — twelve slots — and at 190px that ran straight through
    the track on both sides. So the ring fits to its inner diameter, still
    capped at 190 so the short strings look exactly like the countdown.
    """
    ref = 200
    met_main = _digits_metrics(ref)
    w = _text_width(main_text, met_main)
    if show_millis:
        met_ms = _digits_metrics(max(1, int(round(ref * CLOCK_MS_SCALE))))
        w += _text_width(".000", met_ms)
    if has_tag:
        tag_size = max(1, int(round(ref * CLOCK_TAG_SCALE)))
        tag_font = fonts.load("digits", tag_size)
        w += met_main["colon"] + max(tag_font.getlength("AM"),
                                     tag_font.getlength("PM"))
    if w <= 0:
        return cap
    fit = int(ref * fit_width / w)
    return max(60, min(cap, fit))


def _render_clock_block(main_text, ms_text, tag, color, tag_color,
                        met_main, met_ms, tag_font, tag_width):
    """RGBA block combining the main digits, an optional smaller millis
    run and an optional AM/PM tag on one shared baseline.

    This is the clock's answer to _render_digits, which only ever draws
    one run at one size and one colour; here up to three runs at three
    sizes share a canvas. The trick is the same 'ms' anchor _render_digits
    uses: every d.text() call is given the SAME baseline y regardless of
    its own font's metrics, so mixing font sizes never misaligns them.
    `ms_text` is "" with millis off; `tag` is "" in 24-hour format.
    `tag_width` is the wider of "AM"/"PM" at the tag's own font size, so
    switching between them is a fixed-width slot — nothing else shifts.
    """
    gap = met_main["colon"] if tag else 0.0
    main_w = _text_width(main_text, met_main)
    ms_w = _text_width(ms_text, met_ms) if ms_text else 0.0
    total_w = main_w + ms_w + (gap + tag_width if tag else 0.0)

    h = int(math.ceil(met_main["glyph_h"])) + 2 * _DIGITS_PAD
    w = int(math.ceil(total_w)) + 2 * _DIGITS_PAD
    block = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(block)
    baseline = _DIGITS_PAD + met_main["baseline_off"]

    x = float(_DIGITS_PAD)
    for ch in main_text:
        cw = _slot_width(ch, met_main)
        if ch != " ":
            d.text((x + cw / 2.0, baseline), ch, font=met_main["font"],
                   fill=color + (255,), anchor="ms")
        x += cw
    for ch in ms_text:
        cw = _slot_width(ch, met_ms)
        if ch != " ":
            d.text((x + cw / 2.0, baseline), ch, font=met_ms["font"],
                   fill=color + (255,), anchor="ms")
        x += cw
    if tag:
        x += gap
        d.text((x + tag_width / 2.0, baseline), tag, font=tag_font,
              fill=tag_color + (255,), anchor="ms")
        x += tag_width
    return block


def _render_clock(options, progress_cb):
    """Render a wall-clock MP4 (mode "clock"); returns the output basename.

    options (already normalised by app.validate_timer_options): start
    "HH:MM:SS", duration_seconds 5..1800, format "12h"|"24h", show_seconds
    / show_millis bool, style "classic"|"ring", accent "#rrggbb". Ticks
    forward from `start` in real time for `duration_seconds`, wrapping at
    24h like a real clock — it is not a countdown, so there is no hold and
    no warn colour.
    """
    options = options or {}
    if progress_cb is None:
        progress_cb = lambda pct: None  # noqa: E731

    start = str(options.get("start", "19:59:50"))
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})", start)
    # app.py already rejects a malformed start before this is ever called;
    # the fallback below only guards a renderer called directly (tests).
    sh, sm, ss = (int(g) for g in m.groups()) if m else (19, 59, 50)
    start_ms = (sh * 3600 + sm * 60 + ss) * 1000

    duration = max(5, min(1800, _to_int(options.get("duration_seconds"), 30)))
    fmt = options.get("format", "12h")
    if fmt not in ("12h", "24h"):
        fmt = "12h"
    show_seconds = bool(options.get("show_seconds", True))
    show_millis = bool(options.get("show_millis", False))
    if show_millis:
        show_seconds = True
    style = str(options.get("style", "classic")).lower()
    if style not in CLOCK_STYLES:
        style = "classic"
    accent = _parse_hex(options.get("accent"), DEFAULT_ACCENT)
    has_tag = fmt == "12h"

    if show_millis:
        fps, out_fps = 30, 30      # every frame unique, see module docstring
    else:
        fps = 1 if style == "classic" else 10
        out_fps = TIMER_OUTPUT_FPS
    total_frames = duration * fps

    # Static layers: vignette + this style's track, built once (identical to
    # the countdown ring/bar setup — the shared _background/_ring_mask are
    # untouched by clock mode).
    bg = _background().copy()
    accent_tile = None
    if style == "ring":
        bg.paste(Image.new("RGB", (_RING_TILE, _RING_TILE), TRACK),
                 _RING_ORIGIN, _ring_mask(1.0))
        accent_tile = Image.new("RGB", (_RING_TILE, _RING_TILE), accent)

    sample_main, _sample_tag = format_clock_time(
        start_ms, fmt, show_seconds, show_millis)
    sample_big = sample_main[:-4] if show_millis else sample_main

    if style == "ring":
        size = _clock_font_size(sample_big, show_millis, has_tag,
                                RING_INNER_FIT, RING_DIGITS_MAX)
        digits_cy = RING_CY
    else:
        size = _clock_font_size(sample_big, show_millis, has_tag)
        digits_cy = HEIGHT // 2
    met_main = _digits_metrics(size)
    met_ms = _digits_metrics(max(1, int(round(size * CLOCK_MS_SCALE))))
    tag_font = fonts.load("digits", max(1, int(round(size * CLOCK_TAG_SCALE))))
    tag_width = max(tag_font.getlength("AM"), tag_font.getlength("PM"))

    out_path = export_path(
        "clock", "{0:02d}{1:02d}-{2:02d}_{3}s_{4}".format(
            sh, sm, ss, duration, style))

    # Same per-second base cache as the countdown's base_for, but keyed on
    # the displayed text/tag rather than remaining seconds — skipped
    # entirely when millis are on, since then every frame is unique anyway.
    bases = OrderedDict()
    bases_lock = threading.Lock()

    def block_for(big, small, tag):
        return _render_clock_block(
            big, small, tag, DIGITS_COLOR, accent, met_main, met_ms,
            tag_font, tag_width)

    def base_for(big, small, tag):
        key = (big, small, tag)
        with bases_lock:
            cached = bases.get(key)
            if cached is not None:
                bases.move_to_end(key)
                return cached
        block = block_for(big, small, tag)
        base = bg.copy()
        base.paste(block,
                   (WIDTH // 2 - block.width // 2,
                    digits_cy - block.height // 2),
                   block)
        with bases_lock:
            bases[key] = base
            while len(bases) > 16:
                bases.popitem(last=False)
        return base

    def make_frame(i):
        ms_elapsed = int(round(i * 1000.0 / fps))
        total_ms = start_ms + ms_elapsed
        main_text, tag = format_clock_time(
            total_ms, fmt, show_seconds, show_millis)
        if show_millis:
            big, small = main_text[:-4], main_text[-4:]
            block = block_for(big, small, tag)
            base = bg.copy()
            base.paste(block,
                       (WIDTH // 2 - block.width // 2,
                        digits_cy - block.height // 2),
                       block)
        else:
            base = base_for(main_text, "", tag)
        if style != "ring":
            return base
        frame = base.copy()
        sec_in_minute = (total_ms % _DAY_MS % 60000) / 1000.0
        frac = sec_in_minute / 60.0
        if frac > 0.0:
            frame.paste(accent_tile, _RING_ORIGIN, _ring_seconds_mask(frac))
        return frame

    encode_parallel(out_path, fps, total_frames, make_frame, progress_cb,
                    output_fps=out_fps)
    return os.path.basename(out_path)


# ---- main entry point -----------------------------------------------------------

def render_timer(options, progress_cb):
    """Render a timer MP4; returns the output filename basename.

    Dispatches on options["mode"]: "clock" renders a live wall clock
    (_render_clock, below); anything else — including the key being absent
    — is the original countdown, byte-for-byte unchanged.

    Countdown options: minutes, seconds (total 5..7200 s), style
    classic|ring|bar, accent '#rrggbb', warn_last10 bool, hold_seconds
    0..30, show_millis bool (default False — addendum v1.23.0). Counts down
    from the total to 0:00, then holds at 0:00 for hold_seconds (at least
    one full second of 0:00 is always shown, even with hold 0). With
    show_millis the last frame and the whole hold read "0:00.000".
    """
    options = options or {}
    if options.get("mode") == "clock":
        return _render_clock(options, progress_cb)

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
    # Addendum (v1.23.0): the same millis toggle clock mode uses, now also
    # accepted on a countdown. Every frame's ms differs, so the per-second
    # base cache below can't help it and 15fps->duplicated output would be
    # visibly choppy — millis countdowns get their own 30fps input/output,
    # exactly like clock mode's millis path. Without millis this whole
    # branch is skipped and `fps`/`out_fps` come out exactly as before.
    show_millis = bool(options.get("show_millis", False))
    if show_millis:
        fps, out_fps = 30, 30
    else:
        fps = _input_fps(style, total)
        out_fps = TIMER_OUTPUT_FPS
    # max(1, hold): with hold=0 the loop would stop at rem=1 and 0:00 would
    # never appear; always render at least one second of the finished state.
    total_frames = (total + max(1, hold)) * fps

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
    if show_millis:
        # ".mmm" widens the string a lot ("5:00" -> "5:00.000"); refit per
        # style exactly like clock mode's sizing, so the ring/bar digits
        # still clear their track. Reuses _clock_font_size (has_tag=False —
        # a countdown never has an AM/PM tag) with each style's own fit
        # width/cap; classic's defaults (1600/400) match _classic_font_size's
        # numbers exactly, just fitted to the wider millis string instead.
        # Without millis this block never runs — `size` above is untouched.
        if style == "ring":
            size = _clock_font_size(initial_text, True, False,
                                    RING_INNER_FIT, RING_DIGITS_MAX)
        elif style == "bar":
            size = _clock_font_size(initial_text, True, False,
                                    BAR_WIDTH, 330)
        else:
            size = _clock_font_size(initial_text, True, False)
    met = _digits_metrics(size)
    met_ms = _digits_metrics(max(1, int(round(size * CLOCK_MS_SCALE)))) \
        if show_millis else None

    out_path = export_path(
        "timer", "{0}m{1:02d}s_{2}{3}".format(
            total // 60, total % 60, style, "_ms" if show_millis else ""))

    # Digit bases (background + digits for one displayed second) are shared
    # by every frame within that second. The cache is small and lock-guarded
    # so frame generation can run on the encode_parallel thread pool —
    # frames are pure functions of their index. LRU-capped: a 2h timer would
    # otherwise hold thousands of full frames (~6 MB each) in memory.
    bases = OrderedDict()
    bases_lock = threading.Lock()

    def base_for(rem):
        color = accent if (warn_last10 and rem <= 10) else DIGITS_COLOR
        text = _format_remaining(rem, total)
        key = (text, color)
        with bases_lock:
            cached = bases.get(key)
            if cached is not None:
                bases.move_to_end(key)
                return cached
        block = _render_digits(text, color, met)
        base = bg.copy()
        base.paste(block,
                   (WIDTH // 2 - block.width // 2,
                    digits_cy - block.height // 2),
                   block)
        with bases_lock:
            bases[key] = base
            while len(bases) > 16:
                bases.popitem(last=False)
        return base

    def make_frame(i):
        t = i / float(fps)
        if show_millis:
            # Every frame's ms is unique (30fps, no per-second repeats), so
            # there is no base cache here — matches clock mode's millis path
            # (module docstring / _render_clock's make_frame above). Frame
            # index arithmetic mirrors format_clock_time's contract exactly:
            # ms = round(i*1000/fps), just counting DOWN instead of forward.
            rem_ms = max(0, total * 1000 - int(round(i * 1000.0 / fps)))
            color = (accent if (warn_last10 and rem_ms <= 10_000)
                    else DIGITS_COLOR)
            main_text = _format_remaining(rem_ms // 1000, total)
            # Leading "." makes this the same "small run" shape clock mode
            # passes (main_text[-4:] there always keeps the dot too) — the
            # "." gets its own narrow slot via _digits_metrics/_slot_width,
            # same as everywhere else a dot is drawn.
            ms_text = ".{0:03d}".format(rem_ms % 1000)
            block = _render_clock_block(main_text, ms_text, "", color, color,
                                        met, met_ms, None, 0)
            base = bg.copy()
            base.paste(block,
                       (WIDTH // 2 - block.width // 2,
                        digits_cy - block.height // 2),
                       block)
        else:
            elapsed = int(t)
            rem = total - elapsed if elapsed < total else 0
            base = base_for(rem)
        if style == "classic":
            return base                  # nothing animates within a second
        frame = base.copy()
        frac = max(0.0, 1.0 - t / float(total))
        if frac > 0.0:
            if style == "ring":
                frame.paste(accent_tile, _RING_ORIGIN, _ring_mask(frac))
            else:
                frame.paste(accent_tile, (BAR_MARGIN, BAR_TOP),
                            _bar_mask(frac))
        return frame

    # 15 fps out looks identical for countdown content and halves the encode
    # (a 5-minute timer at 30 fps meant 9000 encoded frames). Millis mode
    # uses 30/30 (set above) so every unique ms value actually gets its own
    # encoded frame instead of being smeared across duplicated output frames.
    encode_parallel(out_path, fps, total_frames, make_frame, progress_cb,
                    output_fps=out_fps)
    return os.path.basename(out_path)
