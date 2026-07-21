"""QR "scan to..." card renderer.

Builds a static base ONCE (background + white rounded card + crisp
dark-on-white QR + accent heading + off-white caption), then per frame
composites only a soft accent ring that gently breathes OUTSIDE the card.
Everything that matters for a scan is static: the card, the QR, and the quiet
zone never move or scale, so the code stays crisp and readable.

Scannability rules (non-negotiable — a code that won't scan is useless):

- The QR is ALWAYS dark modules (#0a0c0e) on a WHITE card. Never inverted;
  many phone scanners refuse light-on-dark codes.
- Modules are drawn BIG. We size the card to the code so modules land around
  MODULE_TARGET px (never below MODULE_MIN), which survives H.264/yuv420p
  softening and reads from across a room and off a projector.
- Error correction is level Q (25%), so partial blur/glare still decodes.
- A quiet zone of QUIET (>=4) white modules surrounds the code.
- Modules are crisp filled squares on exact integer boundaries (no
  anti-aliasing, no dark-module "dot gain" from overlapping rectangles).
- The accent colour touches ONLY the heading text and the breathing ring.

Position: the whole heading+card+caption block can be anchored to a 3x3 grid
(top-left ... bottom-right, default center), so the code can sit out of the
way of other lower-thirds.

Background: an uploaded image can replace the vignette; it is cover-fit to
1080p and darkened with a scrim so the card and text stay legible.

Seamless loop: total_frames = duration * INPUT_FPS. The ring's opacity/width
follow sin(phase), phase = 2*pi*frame/total_frames, so frame 0 == frame N
(no fade-in, no jump). ProPresenter loops the clip, so a clean wrap is required.
"""

import math
import os
import re

import segno
from PIL import Image, ImageDraw

from . import fonts
from .encoder import (FrameEncoder, HEIGHT, OUTPUT_FPS, UPLOADS_DIR, WIDTH,
                      export_path)

# Pillow >= 9.1 moved resampling filters into an enum; keep 3.9-safe access.
_RESAMPLING = getattr(Image, "Resampling", Image)
_BILINEAR = _RESAMPLING.BILINEAR
_LANCZOS = _RESAMPLING.LANCZOS

# ---------------------------------------------------------------- palette

BG_BASE = "#0e1013"
BG_EDGE = "#07080a"
TEXT_LIGHT = "#f2f0eb"
CARD_WHITE = "#ffffff"
QR_DARK = "#0a0c0e"
DEFAULT_ACCENT = "#e8b44f"

# ---------------------------------------------------------------- QR sizing

QR_ERROR = "q"                  # 25% error correction — robust to blur/glare
QUIET = 4                       # quiet-zone modules of white each side
MODULE_TARGET = 16              # preferred module size (px) — nice and chunky
MODULE_MIN = 9                  # never shrink below this
CARD_PAD = 46                   # white padding between quiet zone and card edge
CARD_MAX = 820                  # cap card side so it fits with heading/caption
CARD_RADIUS = 40

# ---------------------------------------------------------------- layout

CENTER_X = WIDTH // 2
MARGIN = 96                     # gap from frame edge for cornered positions

HEADING_SIZE = 76
CAPTION_SIZE = 42
HEADING_GAP = 42
CAPTION_GAP = 38
HEADING_TRACKING = 6

# Ring geometry (outside the card).
RING_GAP = 24
RING_BASE_W = 3.0
RING_SWING_W = 5.0
RING_MIN_A = 40
RING_MAX_A = 150
RING_SS = 2

# ---------------------------------------------------------------- timeline

INPUT_FPS = 15                  # only the ring moves; ffmpeg dupes up to 30
DEFAULT_DURATION = 15
MIN_DURATION = 5
MAX_DURATION = 60

MAX_URL_LEN = 1000
MAX_HEADING_LEN = 30
MAX_CAPTION_LEN = 60

POSITIONS = (
    "top-left", "top-center", "top-right",
    "mid-left", "center", "mid-right",
    "bottom-left", "bottom-center", "bottom-right",
)
DEFAULT_POSITION = "center"


# ---------------------------------------------------------------- helpers

def _hex_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _resolve_background(name):
    """Return an absolute path to an uploaded background, or None.

    Validated to live inside UPLOADS_DIR (defends against traversal even
    though app.py already checks)."""
    if not name:
        return None
    root = os.path.realpath(UPLOADS_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if path.startswith(root + os.sep) and os.path.isfile(path):
        return path
    return None


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

    position = options.get("position", DEFAULT_POSITION)
    if position not in POSITIONS:
        position = DEFAULT_POSITION

    background = _resolve_background(options.get("background"))

    try:
        duration = int(options.get("duration_seconds", DEFAULT_DURATION))
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION
    duration = max(MIN_DURATION, min(MAX_DURATION, duration))

    return {
        "url": url, "heading": heading, "caption": caption,
        "accent": str(accent), "position": position,
        "background": background, "duration": duration,
    }


def _vignette_background():
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


def _image_background(path):
    """Cover-fit an uploaded image to 1080p and darken it with a scrim so the
    white card and accent text stay legible on any photo."""
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return _vignette_background()
    iw, ih = img.size
    scale = max(WIDTH / iw, HEIGHT / ih)
    nw, nh = int(math.ceil(iw * scale)), int(math.ceil(ih * scale))
    img = img.resize((nw, nh), _LANCZOS)
    x = (nw - WIDTH) // 2
    y = (nh - HEIGHT) // 2
    img = img.crop((x, y, x + WIDTH, y + HEIGHT))
    # 48% black scrim for legibility.
    scrim = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    return Image.blend(img, scrim, 0.48)


def _build_background(background):
    return _image_background(background) if background \
        else _vignette_background()


def _qr_matrix(url):
    """segno matrix as a list of rows of ints (0/1); n = side length."""
    qr = segno.make(url, error=QR_ERROR)
    matrix = [list(row) for row in qr.matrix]
    return matrix, len(matrix)


def _module_px(n):
    """Choose a module size: MODULE_TARGET, shrunk only if the card would
    exceed CARD_MAX, and never below MODULE_MIN."""
    span = n + 2 * QUIET                       # modules across incl. quiet zone
    fit = (CARD_MAX - 2 * CARD_PAD) // span
    return max(MODULE_MIN, min(MODULE_TARGET, int(fit)))


def _build_card(matrix, n):
    """White rounded card (RGBA) with the QR drawn crisp and centered.
    Returns (card_image, card_size)."""
    module_px = _module_px(n)
    code_px = module_px * n
    inner = code_px + 2 * QUIET * module_px    # code + quiet zone
    card_size = inner + 2 * CARD_PAD

    card = Image.new("RGBA", (card_size, card_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        [0, 0, card_size - 1, card_size - 1], radius=CARD_RADIUS,
        fill=_hex_rgb(CARD_WHITE) + (255,))

    origin = (card_size - code_px) // 2        # exact-centered code block
    dark = _hex_rgb(QR_DARK)
    for r in range(n):
        y0 = origin + r * module_px
        for c in range(n):
            if matrix[r][c]:
                x0 = origin + c * module_px
                # Exact cell: [x0 .. x0+module_px-1] fills module_px pixels with
                # no overlap into the next cell, so dark and light modules are
                # the same size (no dot gain that would fail a marginal scan).
                draw.rectangle(
                    [x0, y0, x0 + module_px - 1, y0 + module_px - 1], fill=dark)
    return card, card_size


def _text_width(font, text, tracking):
    if not text:
        return 0
    return int(sum(font.getlength(ch) for ch in text)
               + tracking * (len(text) - 1))


def _draw_tracked_text(draw, cx, top, text, font, fill, tracking):
    """Horizontally-centered text at glyph-bbox-top `top`, with letter-spacing."""
    total = _text_width(font, text, tracking)
    bbox = font.getbbox(text)
    x = cx - total / 2.0
    y = top - bbox[1]
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking


def _layout(opts, card_size):
    """Compute geometry for the heading+card+caption block at the chosen
    position. Returns dict with card_cx, card_top, heading_top, caption_top."""
    heading = opts["heading"].upper()
    caption = opts["caption"]
    heading_font = fonts.load("label", HEADING_SIZE) if heading else None
    caption_font = fonts.load("caption", CAPTION_SIZE) if caption else None

    heading_h = 0
    if heading_font is not None:
        hb = heading_font.getbbox(heading)
        heading_h = hb[3] - hb[1]
    caption_h = 0
    if caption_font is not None:
        cb = caption_font.getbbox(caption)
        caption_h = cb[3] - cb[1]

    block_h = card_size
    if heading_h:
        block_h += heading_h + HEADING_GAP
    if caption_h:
        block_h += caption_h + CAPTION_GAP

    pos = opts["position"]
    vert, horiz = pos.split("-") if "-" in pos else ("mid", "center")

    # Horizontal: card center x.
    half = card_size // 2
    if horiz == "left":
        card_cx = MARGIN + half
    elif horiz == "right":
        card_cx = WIDTH - MARGIN - half
    else:
        card_cx = CENTER_X

    # Vertical: top of the whole block.
    if vert == "top":
        block_top = MARGIN
    elif vert == "bottom":
        block_top = HEIGHT - MARGIN - block_h
    else:
        block_top = (HEIGHT - block_h) // 2

    y = block_top
    heading_top = None
    if heading_h:
        heading_top = y
        y += heading_h + HEADING_GAP
    card_top = y
    y += card_size
    caption_top = (y + CAPTION_GAP) if caption_h else None

    return {
        "card_cx": card_cx, "card_top": card_top, "card_size": card_size,
        "heading_top": heading_top, "caption_top": caption_top,
        "heading_font": heading_font, "caption_font": caption_font,
        "heading": heading, "caption": caption,
    }


def _compose_base(opts):
    """Build the static base frame (RGB) and return (base_rgb, geometry)."""
    matrix, n = _qr_matrix(opts["url"])
    card, card_size = _build_card(matrix, n)
    geo = _layout(opts, card_size)

    base = _build_background(opts["background"]).convert("RGBA")
    draw = ImageDraw.Draw(base)
    accent = opts["accent"]

    if geo["heading_font"] is not None:
        _draw_tracked_text(
            draw, geo["card_cx"], geo["heading_top"], geo["heading"],
            geo["heading_font"], _hex_rgb(accent) + (255,), HEADING_TRACKING)
    if geo["caption_font"] is not None:
        _draw_tracked_text(
            draw, geo["card_cx"], geo["caption_top"], geo["caption"],
            geo["caption_font"], _hex_rgb(TEXT_LIGHT) + (255,), 0)

    base.alpha_composite(card, (geo["card_cx"] - card_size // 2,
                                geo["card_top"]))
    return base.convert("RGB"), geo


# The ring only ever occupies a thin band around the card, and its appearance
# depends solely on s = 0.5*(1+sin(phase)). Building a full 3840x2160 overlay
# every frame cost ~86 ms/frame — nearly the entire render. Instead we draw a
# small tile around the card and cache it per quantised s: at RING_STEPS levels
# the alpha moves ~1.7/255 and the width ~0.08 px per step, so the breathe is
# still smooth but a clip needs at most RING_STEPS tiles instead of one per frame.
RING_STEPS = 64
RING_PAD = RING_GAP + int(math.ceil(RING_BASE_W + RING_SWING_W)) + 2


def _ring_tile(card_size, step, accent):
    """RGBA tile of the ring for a quantised breathe `step`. Paste it at
    (card_left - RING_PAD, card_top - RING_PAD)."""
    s = step / float(RING_STEPS - 1)
    alpha = int(round(RING_MIN_A + (RING_MAX_A - RING_MIN_A) * s))
    width = RING_BASE_W + RING_SWING_W * s

    box = card_size + 2 * RING_PAD
    tile = Image.new("RGBA", (box * RING_SS, box * RING_SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    inset = (RING_PAD - RING_GAP) * RING_SS
    far = (box - (RING_PAD - RING_GAP)) * RING_SS - 1
    draw.rounded_rectangle(
        [inset, inset, far, far], radius=(CARD_RADIUS + RING_GAP) * RING_SS,
        outline=_hex_rgb(accent) + (alpha,),
        width=max(1, int(round(width * RING_SS))))
    return tile.resize((box, box), _LANCZOS)


def _ring_step(phase):
    """Quantise the breathe so tiles can be cached."""
    s = 0.5 * (1.0 + math.sin(phase))
    return min(RING_STEPS - 1, max(0, int(round(s * (RING_STEPS - 1)))))


# ---------------------------------------------------------------- renderer

def _ring_pos(geo):
    return (geo["card_cx"] - geo["card_size"] // 2 - RING_PAD,
            geo["card_top"] - RING_PAD)


def render_qr_still(options, max_width=900):
    """Render ONE representative frame (base + ring near peak) as an RGB PIL
    image, downscaled to max_width. Used by the live preview endpoint so the
    preview shows the exact, scannable QR that will be exported."""
    opts = _clean_options(options)
    base_rgb, geo = _compose_base(opts)
    tile = _ring_tile(geo["card_size"], RING_STEPS - 1, opts["accent"])
    frame = base_rgb.copy()
    frame.paste(tile, _ring_pos(geo), tile)
    if max_width and max_width < WIDTH:
        h = int(HEIGHT * max_width / WIDTH)
        frame = frame.resize((max_width, h), _LANCZOS)
    return frame


def render_qr_image(options):
    """Export the QR card as a single full-resolution PNG.

    Nothing in this visual actually moves except the accent ring's slow
    breathe, so a still image is usually the more sensible thing to drop into
    ProPresenter (instant, tiny, no clip length to think about). Same 1920x1080
    composition as the video, ring drawn at its peak.
    Returns the filename basename."""
    opts = _clean_options(options)
    frame = render_qr_still(options, max_width=0)     # 0 = keep full 1920x1080
    out_path = export_path("qr", opts["heading"] or "code", ext=".png")
    frame.save(out_path, format="PNG")
    return os.path.basename(out_path)


def render_qr(options, progress_cb):
    """Render the QR "scan to..." card MP4. Returns the filename basename."""
    opts = _clean_options(options)
    base_rgb, geo = _compose_base(opts)

    total_frames = opts["duration"] * INPUT_FPS
    pos = _ring_pos(geo)
    tiles = {}                       # quantised step -> ring tile
    out_path = export_path("qr", opts["heading"] or "code")
    # Only the ring breathes, so 1:1 input->output (15 fps) avoids
    # encoding 2x the frames for no visible gain.
    with FrameEncoder(out_path, INPUT_FPS, output_fps=INPUT_FPS) as enc:
        for k in range(total_frames):
            step = _ring_step(2.0 * math.pi * k / total_frames)
            tile = tiles.get(step)
            if tile is None:
                tile = tiles[step] = _ring_tile(
                    geo["card_size"], step, opts["accent"])
            frame = base_rgb.copy()
            frame.paste(tile, pos, tile)
            enc.add_frame(frame)
            progress_cb(int((k + 1) * 100.0 / total_frames))
    return os.path.basename(out_path)
