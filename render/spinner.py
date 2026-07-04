"""Decision wheel renderer.

Draws the full wheel ONCE at 2x supersample (segments, gaps, labels),
then per frame rotates it (BICUBIC), downsamples, and composites onto a
cached vignette background. Hub and pointer are static overlays drawn on
top so they stay crisp instead of shimmering through per-frame resampling.

Angle conventions (the part that must be provably right):

- PIL ImageDraw angles (pieslice): degrees measured from 3 o'clock,
  increasing CLOCKWISE as displayed (y axis points down).
- Image.rotate(a) turns the image COUNTER-clockwise (as displayed) by a
  degrees. "rotation" throughout this module means that CCW angle.
- Segment i is drawn spanning display angles
  [i*seg - 90, (i+1)*seg - 90), so segment 0 begins at the pointer
  (12 o'clock) and segments proceed clockwise.
- After rotating the wheel CCW by r, the wheel angle visible under the
  fixed pointer at display angle -90 is (r - 90), therefore:
      segment_at_pointer(r, n) == floor((r % 360) / (360 / n))
  landing_rotation() is the inverse, aimed at the central 70% of the
  winner's segment so it never lands ambiguously on a boundary and
  rigged results don't look staged by always centering.

The pure helpers segment_at_pointer() / landing_rotation() are the only
place this convention is encoded; everything else just calls them.
"""

import math
import os
import random
import re

from PIL import Image, ImageDraw

from . import fonts
from .encoder import (FrameEncoder, HEIGHT, OUTPUT_FPS, WIDTH, export_path)

# Pillow >= 9.1 moved resampling filters into an enum; keep 3.9-safe access.
_RESAMPLING = getattr(Image, "Resampling", Image)
_BICUBIC = _RESAMPLING.BICUBIC
_BILINEAR = _RESAMPLING.BILINEAR
_LANCZOS = _RESAMPLING.LANCZOS

# ---------------------------------------------------------------- visuals

BG_BASE = "#0e1013"
BG_EDGE = "#07080a"
TEXT_LIGHT = "#f2f0eb"
TEXT_DARK = "#101014"
HUB_FILL = "#141619"
CARD_FILL = "#141619"
DEFAULT_ACCENT = "#e8b44f"

PALETTE = [
    "#e8b44f", "#5aa9e6", "#e2725b", "#7fb069", "#9b7ede",
    "#f2c14e", "#4ecdc4", "#e63946", "#f4a261", "#457b9d",
]

CENTER = (WIDTH // 2, HEIGHT // 2)      # (960, 540)
WHEEL_R = 430
HUB_R = 90
HUB_RING = 6
SEG_GAP = 4                              # px gap between segments (1x)
SS = 2                                   # wheel supersample factor
WHEEL_MARGIN = 12                        # 1x margin around the wheel disc
WHEEL_BOX = 2 * (WHEEL_R + WHEEL_MARGIN)          # 1x composited size
WHEEL_BOX2 = SS * WHEEL_BOX                        # 2x drawing size
LABEL_RADIUS_FRAC = 0.62
LABEL_MAX_PX = 46                        # font-size cap at 1x

CARD_CENTER_Y = 880

# ---------------------------------------------------------------- timeline

DURATION = 12.0
TOTAL_FRAMES = int(DURATION * OUTPUT_FPS)         # 360
WINDUP_END = 0.8
SPIN_END = 7.8
WINDUP_DEG = -25.0
FULL_SPINS = 5
CARD_IN = 8.0
CARD_FADE = 0.4

MIN_ENTRIES = 2
MAX_ENTRIES = 20
MAX_ENTRY_LEN = 40


# ------------------------------------------------------------ landing math

def segment_at_pointer(rotation_deg, n):
    """Index of the segment under the 12-o'clock pointer for a wheel
    rotated `rotation_deg` degrees CCW (Image.rotate convention)."""
    seg = 360.0 / n
    return int((rotation_deg % 360.0) // seg) % n


def landing_rotation(winner_index, n, jitter01):
    """CCW rotation (degrees, in [0, 360)) that parks the pointer inside
    segment `winner_index`. jitter01 in [0, 1) maps to the central 70%
    of the segment: fraction 0.15 .. 0.85, never a boundary."""
    seg = 360.0 / n
    frac = 0.15 + 0.7 * (jitter01 % 1.0)
    return (winner_index + frac) * seg


def segment_colors(n):
    """A palette color per segment; adjacent segments (including the
    wrap-around last/first pair) never share a color. Repeats of the
    palette are offset (by 3 per cycle) with a nudge fix-up so the
    guarantee holds for every n in 2..20."""
    m = len(PALETTE)
    idxs = []
    for i in range(n):
        base = (i + (i // m) * 3) % m
        prev = idxs[-1] if idxs else None
        first = idxs[0] if idxs else None
        pick = base
        for step in range(m):
            cand = (base + step) % m
            if cand == prev:
                continue
            if i == n - 1 and cand == first:
                continue
            pick = cand
            break
        idxs.append(pick)
    return [PALETTE[k] for k in idxs]


# ---------------------------------------------------------------- helpers

def _hex_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _luminance(rgb):
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _ease_in_out_quad(u):
    if u < 0.5:
        return 2.0 * u * u
    return 1.0 - ((-2.0 * u + 2.0) ** 2) / 2.0


def _ease_out_cubic(u):
    return 1.0 - (1.0 - u) ** 3


def _rotation_at(t, final_rotation):
    """Wheel rotation (deg CCW) at time t seconds."""
    if t <= WINDUP_END:
        u = t / WINDUP_END
        return WINDUP_DEG * _ease_in_out_quad(u)
    if t < SPIN_END:
        u = (t - WINDUP_END) / (SPIN_END - WINDUP_END)
        return WINDUP_DEG + (final_rotation - WINDUP_DEG) * _ease_out_cubic(u)
    return final_rotation


# ------------------------------------------------------------- validation

def _clean_options(options):
    """Defensive re-validation (upstream validates, but never trust it)."""
    if not isinstance(options, dict):
        raise ValueError("spinner options must be an object")

    raw = options.get("entries")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("entries must be a list of names")
    entries = []
    for item in raw:
        text = str(item).strip()[:MAX_ENTRY_LEN]
        if text:
            entries.append(text)
    if not MIN_ENTRIES <= len(entries) <= MAX_ENTRIES:
        raise ValueError(
            "spinner needs {0} to {1} non-empty entries".format(
                MIN_ENTRIES, MAX_ENTRIES))

    mode = options.get("mode", "random")
    if mode not in ("random", "rigged"):
        mode = "random"

    winner = None
    if mode == "rigged":
        winner = str(options.get("winner") or "").strip()[:MAX_ENTRY_LEN]
        if winner not in entries:
            raise ValueError("rigged winner must be one of the entries")

    accent = options.get("accent") or DEFAULT_ACCENT
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(accent)):
        accent = DEFAULT_ACCENT

    return entries, mode, winner, accent


# ------------------------------------------------------- cached art layers

def _build_background():
    """1920x1080 RGB: BG_BASE with a radial vignette to BG_EDGE at edges."""
    small_w, small_h = 240, 135
    base = _hex_rgb(BG_BASE)
    edge = _hex_rgb(BG_EDGE)
    img = Image.new("RGB", (small_w, small_h))
    px = img.load()
    cx = (small_w - 1) / 2.0
    cy = (small_h - 1) / 2.0
    max_d = math.hypot(cx, cy)
    for y in range(small_h):
        for x in range(small_w):
            f = (math.hypot(x - cx, y - cy) / max_d) ** 2.0
            px[x, y] = (
                round(base[0] + (edge[0] - base[0]) * f),
                round(base[1] + (edge[1] - base[1]) * f),
                round(base[2] + (edge[2] - base[2]) * f),
            )
    return img.resize((WIDTH, HEIGHT), _BILINEAR)


def _label_image(text, seg_color, seg_deg):
    """RGBA image of one label, horizontal, sized to fit its segment.
    Sizes are in 2x (supersampled) pixels."""
    r2 = WHEEL_R * SS
    hub2 = HUB_R * SS
    # Radial run available for the text (2x px).
    max_w = int(r2 - hub2 - 60 * SS)
    # Tangential space near the label band (2x px).
    band = 2.0 * LABEL_RADIUS_FRAC * r2 * math.sin(math.radians(seg_deg) / 2)
    max_h = max(30, min(int(band) - 2 * SEG_GAP * SS, 160))

    size = LABEL_MAX_PX * SS
    font = fonts.load("label", size)
    while size > 10 * SS:
        font = fonts.load("label", size)
        x0, y0, x1, y1 = font.getbbox(text)
        if x1 - x0 <= max_w and y1 - y0 <= max_h:
            break
        size -= 2 * SS
    # Last resort at min size: ellipsize.
    shown = text
    x0, y0, x1, y1 = font.getbbox(shown)
    while len(shown) > 1 and x1 - x0 > max_w:
        shown = shown[:-2].rstrip() + "…"
        x0, y0, x1, y1 = font.getbbox(shown)

    color = TEXT_DARK if _luminance(_hex_rgb(seg_color)) > 0.55 else TEXT_LIGHT
    pad = 4
    img = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad),
                    (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - x0, pad - y0), shown,
                             font=font, fill=color)
    return img


def _build_wheel(entries, colors):
    """The full wheel at 2x as RGBA: segments, transparent 4px gaps
    (background shows through), and radial labels. Rotated per frame."""
    n = len(entries)
    seg = 360.0 / n
    c = WHEEL_BOX2 // 2
    r2 = WHEEL_R * SS
    bbox = [c - r2, c - r2, c + r2, c + r2]

    img = Image.new("RGBA", (WHEEL_BOX2, WHEEL_BOX2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i in range(n):
        draw.pieslice(bbox, i * seg - 90.0, (i + 1) * seg - 90.0,
                      fill=_hex_rgb(colors[i]) + (255,))

    # Labels at ~0.62*R along each mid-angle, rotated to read along the
    # radius. Left-half labels are flipped 180 so no label starts life
    # upside down (they read inward instead).
    for i, entry in enumerate(entries):
        label = _label_image(entry, colors[i], seg)
        theta = math.radians((i + 0.5) * seg - 90.0)
        px = c + LABEL_RADIUS_FRAC * r2 * math.cos(theta)
        py = c + LABEL_RADIUS_FRAC * r2 * math.sin(theta)
        angle = -math.degrees(theta)          # CCW rotate: +x -> outward
        if math.cos(theta) < -1e-9:
            angle += 180.0                    # keep left half upright
        rotated = label.rotate(angle, resample=_BICUBIC, expand=True)
        img.alpha_composite(
            rotated,
            (int(round(px - rotated.width / 2.0)),
             int(round(py - rotated.height / 2.0))))

    # Alpha mask: solid disc minus 4px boundary gaps, so the vignette
    # background shows through between segments.
    mask = Image.new("L", (WHEEL_BOX2, WHEEL_BOX2), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse(bbox, fill=255)
    gap_w = SEG_GAP * SS
    for i in range(n):
        ang = math.radians(i * seg - 90.0)
        end = (c + (r2 + gap_w) * math.cos(ang),
               c + (r2 + gap_w) * math.sin(ang))
        mdraw.line([(c, c), end], fill=0, width=gap_w)
    img.putalpha(mask)
    return img


def _build_hub(accent):
    """Static center hub: HUB_FILL disc with a 6px accent ring (4x AA)."""
    ss = 4
    box = 2 * (HUB_R + 4)
    big = Image.new("RGBA", (box * ss, box * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    m = 4 * ss
    bbox = [m, m, box * ss - m, box * ss - m]
    draw.ellipse(bbox, fill=_hex_rgb(HUB_FILL) + (255,),
                 outline=_hex_rgb(accent) + (255,), width=HUB_RING * ss)
    return big.resize((box, box), _LANCZOS)


def _build_pointer():
    """Crisp downward triangle for 12 o'clock: light fill, subtle dark
    outline (drawn as a slightly inflated dark triangle behind)."""
    ss = 4
    w, h = 76, 62
    pts = [(6.0, 4.0), (70.0, 4.0), (38.0, 56.0)]
    cx = sum(p[0] for p in pts) / 3.0
    cy = sum(p[1] for p in pts) / 3.0

    def scaled(k, dx=0.0, dy=0.0):
        return [((cx + (x - cx) * k + dx) * ss, (cy + (y - cy) * k + dy) * ss)
                for x, y in pts]

    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    draw.polygon(scaled(1.10, dy=1.0), fill=(10, 11, 13, 220))   # outline
    draw.polygon(scaled(1.0), fill=_hex_rgb(TEXT_LIGHT) + (255,))
    return big.resize((w, h), _LANCZOS)


def _build_winner_card(winner, accent):
    """Winner card RGBA at full opacity (alpha is scaled during fade-in):
    rounded rect, 95% opaque fill, 2px accent border, letterspaced
    WINNER caption over the winner name. Drawn at 2x for crisp corners."""
    ss = 2
    caption = "WINNER"
    cap_font = fonts.load("caption", 26 * ss)
    tracking = 8 * ss

    name_size = 64
    max_name_w = 1200 * ss
    name_font = fonts.load("label", name_size * ss)
    while name_size > 24:
        name_font = fonts.load("label", name_size * ss)
        nb = name_font.getbbox(winner)
        if nb[2] - nb[0] <= max_name_w:
            break
        name_size -= 2
    nb = name_font.getbbox(winner)
    name_w, name_h = nb[2] - nb[0], nb[3] - nb[1]

    cb = cap_font.getbbox(caption)
    cap_h = cb[3] - cb[1]
    cap_w = int(sum(cap_font.getlength(ch) for ch in caption)
                + tracking * (len(caption) - 1))

    pad_x = 64 * ss
    pad_top = 30 * ss
    gap = 12 * ss
    pad_bot = 34 * ss
    w = max(cap_w, name_w) + 2 * pad_x
    w = max(w, 420 * ss)
    h = pad_top + cap_h + gap + name_h + pad_bot

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bw = 2 * ss
    draw.rounded_rectangle(
        [bw // 2, bw // 2, w - 1 - bw // 2, h - 1 - bw // 2],
        radius=18 * ss, fill=_hex_rgb(CARD_FILL) + (242,),   # ~95% opaque
        outline=_hex_rgb(accent) + (255,), width=bw)

    # Letterspaced caption, centered.
    x = (w - cap_w) / 2.0
    y = pad_top - cb[1]
    for ch in caption:
        draw.text((x, y), ch, font=cap_font, fill=_hex_rgb(accent) + (255,))
        x += cap_font.getlength(ch) + tracking

    # Winner name, centered.
    draw.text(((w - name_w) / 2.0 - nb[0], pad_top + cap_h + gap - nb[1]),
              winner, font=name_font, fill=_hex_rgb(TEXT_LIGHT) + (255,))

    return img.resize((w // ss, h // ss), _LANCZOS)


def _faded(rgba, factor):
    """Copy of an RGBA image with its alpha channel scaled by factor."""
    out = rgba.copy()
    alpha = out.getchannel("A").point(lambda a: int(a * factor))
    out.putalpha(alpha)
    return out


# ---------------------------------------------------------------- renderer

def render_spinner(options, progress_cb):
    """Render the decision wheel MP4. Returns the filename basename."""
    entries, mode, winner, accent = _clean_options(options)
    n = len(entries)

    if mode == "random":
        winner = random.choice(entries)
    winner_index = entries.index(winner)

    jitter = random.random()
    final_rotation = FULL_SPINS * 360.0 + landing_rotation(
        winner_index, n, jitter)
    # Belt and braces: the pure math must agree before we spend render time.
    assert segment_at_pointer(final_rotation % 360.0, n) == winner_index

    background = _build_background()
    wheel = _build_wheel(entries, segment_colors(n))
    hub = _build_hub(accent)
    pointer = _build_pointer()
    card = _build_winner_card(winner, accent)

    wheel_pos = (CENTER[0] - WHEEL_BOX // 2, CENTER[1] - WHEEL_BOX // 2)
    hub_pos = (CENTER[0] - hub.width // 2, CENTER[1] - hub.height // 2)
    pointer_pos = (CENTER[0] - pointer.width // 2,
                   CENTER[1] - WHEEL_R - pointer.height + 44)
    card_pos = (WIDTH // 2 - card.width // 2,
                CARD_CENTER_Y - card.height // 2)

    def compose_base(rotation):
        frame = background.copy()
        spun = wheel.rotate(rotation, resample=_BICUBIC)
        spun = spun.resize((WHEEL_BOX, WHEEL_BOX), _LANCZOS)
        frame.paste(spun, wheel_pos, spun)
        frame.paste(hub, hub_pos, hub)
        frame.paste(pointer, pointer_pos, pointer)
        return frame

    out_path = export_path("spinner", "{0}items".format(n))
    last_rotation = None
    base = None
    settled = None      # fully-faded final frame, reused for the hold
    with FrameEncoder(out_path, OUTPUT_FPS) as enc:
        for k in range(TOTAL_FRAMES):
            t = k / float(OUTPUT_FPS)
            rotation = _rotation_at(t, final_rotation)
            if base is None or rotation != last_rotation:
                base = compose_base(rotation)
                last_rotation = rotation

            fade = (t - CARD_IN) / CARD_FADE
            if fade <= 0.0:
                frame = base
            elif fade < 1.0:
                frame = base.copy()
                overlay = _faded(card, fade)
                frame.paste(overlay, card_pos, overlay)
            else:
                if settled is None:
                    settled = base.copy()
                    settled.paste(card, card_pos, card)
                frame = settled

            enc.add_frame(frame)
            progress_cb(int((k + 1) * 100.0 / TOTAL_FRAMES))

    return os.path.basename(out_path)
