"""QR "scan to..." card renderer.

Builds a static base ONCE (vignette background + white rounded card + crisp
dark-on-white QR + accent heading above + off-white caption below), then per
frame composites only a soft accent ring that gently breathes OUTSIDE the
card. Everything that matters for a scan is static: the card, the QR, and the
quiet zone never move or scale, so the code stays crisp and readable.

Scannability rules (non-negotiable):

- The QR is ALWAYS dark modules (#111417) on a WHITE card. Never inverted;
  many phone scanners refuse light-on-dark codes.
- A quiet zone of >= 4 modules of white surrounds the code. We reserve 8
  modules of margin (4 each side) inside the card.
- Modules are drawn as crisp filled squares with NO anti-aliasing, and the
  grid is pixel-snapped and centered so rounding never pushes a module off
  the card or blurs an edge.
- The accent colour touches ONLY the heading text and the breathing ring.

Seamless loop:

- total_frames = duration_seconds * OUTPUT_FPS. The ring's opacity/width are
  functions of phase = 2*pi*frame/total_frames via sin(), so frame 0 is
  identical to frame total_frames (no fade-in, no jump). ProPresenter loops
  the exported clip, so a perfect wrap-around is required.
- Only the ring changes, so we feed frames to the encoder at a lower input
  fps (INPUT_FPS) and let ffmpeg duplicate up to 30. total_frames is computed
  at INPUT_FPS and the sin() period is the whole clip, so the loop stays
  seamless at the chosen input fps too.
"""

import math
import os
import re

import segno
from PIL import Image, ImageDraw

from . import fonts
from .encoder import (FrameEncoder, HEIGHT, OUTPUT_FPS, WIDTH, export_path)

# Pillow >= 9.1 moved resampling filters into an enum; keep 3.9-safe access.
_RESAMPLING = getattr(Image, "Resampling", Image)
_BILINEAR = _RESAMPLING.BILINEAR
_LANCZOS = _RESAMPLING.LANCZOS

# ---------------------------------------------------------------- palette

BG_BASE = "#0e1013"
BG_EDGE = "#07080a"
TEXT_LIGHT = "#f2f0eb"
CARD_WHITE = "#ffffff"
QR_DARK = "#111417"
DEFAULT_ACCENT = "#e8b44f"

# ---------------------------------------------------------------- layout

CENTER_X = WIDTH // 2
CARD_SIZE = 560                 # outer white card, square (px)
CARD_RADIUS = 40
QUIET_MODULES = 8               # total quiet-zone margin (4 each side) in modules
CARD_PAD = 36                   # white padding between card edge and QR block

HEADING_SIZE = 72
CAPTION_SIZE = 40
HEADING_GAP = 40                # px between heading baseline block and card
CAPTION_GAP = 36                # px between card and caption
HEADING_TRACKING = 6            # letter-spacing for the uppercase heading

# Ring geometry (outside the card).
RING_GAP = 22                   # px from card edge to ring at rest
RING_BASE_W = 3.0               # min stroke width (px)
RING_SWING_W = 5.0              # added stroke width at peak
RING_MIN_A = 40                 # min ring alpha (0..255)
RING_MAX_A = 150                # max ring alpha
RING_SS = 2                     # ring supersample for smooth curve

# ---------------------------------------------------------------- timeline

INPUT_FPS = 15                  # only the ring moves; ffmpeg dupes up to 30
DEFAULT_DURATION = 15
MIN_DURATION = 5
MAX_DURATION = 60

MAX_URL_LEN = 1000
MAX_HEADING_LEN = 30
MAX_CAPTION_LEN = 60


# ---------------------------------------------------------------- helpers

def _hex_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _clean_options(options):
    """Defensive re-validation (upstream validates, but never trust it)."""
    if not isinstance(options, dict):
        raise ValueError("qr options must be an object")

    url = str(options.get("url") or "").strip()
    if not (1 <= len(url) <= MAX_URL_LEN):
        raise ValueError(
            "qr url must be 1 to {0} characters".format(MAX_URL_LEN))

    heading = str(options.get("heading") or "").strip()[:MAX_HEADING_LEN]
    caption = str(options.get("caption") or "").strip()[:MAX_CAPTION_LEN]

    accent = options.get("accent") or DEFAULT_ACCENT
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(accent)):
        accent = DEFAULT_ACCENT

    try:
        duration = int(options.get("duration_seconds", DEFAULT_DURATION))
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION
    duration = max(MIN_DURATION, min(MAX_DURATION, duration))

    return url, heading, caption, str(accent), duration


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


def _qr_matrix(url):
    """segno matrix as a list of rows of ints (0/1); n = side length."""
    qr = segno.make(url, error="m")
    matrix = [list(row) for row in qr.matrix]
    return matrix, len(matrix)


def _draw_qr_on_card(card_draw, matrix, n, card_size):
    """Draw crisp dark modules centered on the white card, with the quiet
    zone. Returns (module_px, origin_x, origin_y) so callers can sample.

    module_px = int(card_inner / (n + QUIET_MODULES)); the code is centered
    inside CARD_PAD so leftover pixels split evenly and no module falls off.
    """
    card_inner = card_size - 2 * CARD_PAD
    module_px = int(card_inner // (n + QUIET_MODULES))
    if module_px < 1:
        module_px = 1

    code_px = module_px * n                    # just the modules (no quiet zone)
    # Center the whole code within the full card so the quiet zone is even.
    origin = (card_size - code_px) / 2.0
    origin_x = int(round(origin))
    origin_y = int(round(origin))

    dark = _hex_rgb(QR_DARK)
    for r in range(n):
        y0 = origin_y + r * module_px
        for c in range(n):
            if matrix[r][c]:
                x0 = origin_x + c * module_px
                # +1 on the far edge keeps adjacent squares seamless (no
                # background hairlines) without anti-aliasing.
                card_draw.rectangle(
                    [x0, y0, x0 + module_px, y0 + module_px], fill=dark)
    return module_px, origin_x, origin_y


def _build_card(matrix, n, card_size):
    """White rounded card (RGBA) with the QR drawn on it. Returns the image
    plus the drawing metadata for corner-sampling in the self-test."""
    card = Image.new("RGBA", (card_size, card_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        [0, 0, card_size - 1, card_size - 1], radius=CARD_RADIUS,
        fill=_hex_rgb(CARD_WHITE) + (255,))
    module_px, ox, oy = _draw_qr_on_card(draw, matrix, n, card_size)
    return card, module_px, ox, oy


def _text_width(font, text, tracking):
    if not text:
        return 0
    return int(sum(font.getlength(ch) for ch in text)
               + tracking * (len(text) - 1))


def _draw_tracked_text(draw, cx, top, text, font, fill, tracking):
    """Draw horizontally-centered text at vertical position `top` with
    letter-spacing. `top` is the y of the glyph bbox top."""
    total = _text_width(font, text, tracking)
    bbox = font.getbbox(text)
    x = cx - total / 2.0
    y = top - bbox[1]
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking


def _build_ring(card_top, card_size, phase, accent):
    """RGBA overlay (full frame) with a single soft accent ring outside the
    card. Opacity and width follow sin(phase) so frame 0 == frame N.

    Drawn at RING_SS supersample and downscaled with LANCZOS for a smooth
    curve; the QR area is never touched (the ring sits in RING_GAP outside
    the card).
    """
    s = 0.5 * (1.0 + math.sin(phase))          # 0..1, periodic in phase
    alpha = int(round(RING_MIN_A + (RING_MAX_A - RING_MIN_A) * s))
    width = RING_BASE_W + RING_SWING_W * s

    overlay = Image.new("RGBA", (WIDTH * RING_SS, HEIGHT * RING_SS),
                        (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    left = (CENTER_X - card_size // 2 - RING_GAP) * RING_SS
    top = (card_top - RING_GAP) * RING_SS
    right = (CENTER_X + card_size // 2 + RING_GAP) * RING_SS
    bottom = (card_top + card_size + RING_GAP) * RING_SS
    radius = (CARD_RADIUS + RING_GAP) * RING_SS

    draw.rounded_rectangle(
        [left, top, right, bottom], radius=radius,
        outline=_hex_rgb(accent) + (alpha,),
        width=max(1, int(round(width * RING_SS))))

    return overlay.resize((WIDTH, HEIGHT), _LANCZOS)


# ---------------------------------------------------------------- renderer

def render_qr(options, progress_cb):
    """Render the QR "scan to..." card MP4. Returns the filename basename."""
    url, heading, caption, accent, duration = _clean_options(options)

    matrix, n = _qr_matrix(url)

    # --- vertical layout: measure the stack, then center it as a block ---
    heading_font = fonts.load("label", HEADING_SIZE) if heading else None
    caption_font = fonts.load("caption", CAPTION_SIZE) if caption else None

    heading_up = heading.upper()
    heading_h = 0
    if heading_font is not None:
        hb = heading_font.getbbox(heading_up)
        heading_h = hb[3] - hb[1]
    caption_h = 0
    if caption_font is not None:
        cb = caption_font.getbbox(caption)
        caption_h = cb[3] - cb[1]

    stack_h = CARD_SIZE
    if heading_h:
        stack_h += heading_h + HEADING_GAP
    if caption_h:
        stack_h += caption_h + CAPTION_GAP

    top = (HEIGHT - stack_h) // 2
    y = top
    heading_top = None
    if heading_h:
        heading_top = y
        y += heading_h + HEADING_GAP
    card_top = y
    y += CARD_SIZE
    caption_top = None
    if caption_h:
        caption_top = y + CAPTION_GAP

    # --- build the static base once (bg + text + card + QR) ---
    base = _build_background().convert("RGBA")
    draw = ImageDraw.Draw(base)

    if heading_font is not None:
        _draw_tracked_text(
            draw, CENTER_X, heading_top, heading_up, heading_font,
            _hex_rgb(accent) + (255,), HEADING_TRACKING)
    if caption_font is not None:
        _draw_tracked_text(
            draw, CENTER_X, caption_top, caption, caption_font,
            _hex_rgb(TEXT_LIGHT) + (255,), 0)

    card, module_px, ox, oy = _build_card(matrix, n, CARD_SIZE)
    card_left = CENTER_X - CARD_SIZE // 2
    base.alpha_composite(card, (card_left, card_top))

    base_rgb = base.convert("RGB")

    total_frames = duration * INPUT_FPS

    out_path = export_path("qr", heading or "code")
    with FrameEncoder(out_path, INPUT_FPS) as enc:
        for k in range(total_frames):
            phase = 2.0 * math.pi * k / total_frames
            ring = _build_ring(card_top, CARD_SIZE, phase, accent)
            frame = base_rgb.copy()
            frame.paste(ring, (0, 0), ring)
            enc.add_frame(frame)
            progress_cb(int((k + 1) * 100.0 / total_frames))

    return os.path.basename(out_path)
