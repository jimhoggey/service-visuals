"""Seamlessly-looping motion background renderer.

Renders a tasteful, DARK, broadcast-safe animated background that loops
perfectly for use as a ProPresenter background clip. Three styles:

- aurora: 4-6 soft colour blobs drifting on periodic sin/cos paths,
  composited additively at low res (~480x270), upscaled smooth.
- bokeh: ~30 out-of-focus dots drifting upward+sideways on paths that
  wrap exactly once per loop, rendered ~960x540, upscaled.
- waves: 3-5 slow horizontal wave bands (sine offsets in phase),
  rendered ~960x540, upscaled.

SEAMLESS LOOP is the core requirement. total_frames = duration_seconds * 30.
EVERY moving quantity is a function of phase = 2*pi*frame/total_frames (via
sin/cos, or motion that wraps exactly once per loop), so the last frame flows
into the first with zero jump. The self-test verifies this numerically.

Performance: low-res render + upscale, sprites/base cached outside the frame
loop, keeps a 12s loop well under a minute.

Colour scheme is derived from the accent: accent + an analogous hue + a deep
near-black base, kept dark and never full-brightness.
"""

import colorsys
import math
import os
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .encoder import (FrameEncoder, HEIGHT, OUTPUT_FPS, WIDTH, export_path)

# Pillow >= 9.1 moved resampling filters into an enum; keep 3.9-safe access.
_RESAMPLING = getattr(Image, "Resampling", Image)
_BICUBIC = _RESAMPLING.BICUBIC
_LANCZOS = _RESAMPLING.LANCZOS

DEFAULT_ACCENT = "#e8b44f"
DEFAULT_STYLE = "aurora"
DEFAULT_DURATION = 12
MIN_DURATION = 5
MAX_DURATION = 30
STYLES = ("aurora", "bokeh", "waves")

# Deepest near-black base tint for the whole scene (kept very dark).
BASE_RGB = (7, 8, 10)          # ~ #07080a


# ---------------------------------------------------------------- helpers

def _hex_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_hls(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return colorsys.rgb_to_hls(r, g, b)


def _hls_rgb(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)),
                                  max(0.0, min(1.0, s)))
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _derive_scheme(accent_rgb):
    """A dark 2-3 colour scheme from the accent: the accent itself (toned
    down) plus two nearby hues kept CLOSE to the accent so the result reads
    as a tasteful analogous palette in the accent's colour family rather than
    a rainbow. Returns a list of RGB tuples used to tint the moving elements.

    Hue offsets are deliberately small (+-~0.045 turn, ~16 deg) so an amber
    accent stays amber/ember/gold, a blue accent stays blue/teal, etc. Never
    a garish complementary or a vivid off-accent hue."""
    h, l, s = _rgb_hls(accent_rgb)
    # Keep saturation present but not garish; dial luminance down for dark.
    s = max(0.35, min(0.80, s))
    accent = _hls_rgb(h, min(0.52, max(0.42, l)), s)
    warm = _hls_rgb(h - 0.035, 0.40, s * 0.92)          # toward warmer/ember
    deep = _hls_rgb(h - 0.075, 0.33, s * 0.85)          # deeper, dimmer ember
    return [accent, warm, deep]


# ------------------------------------------------------------- validation

def _clean_options(options):
    """Defensive re-validation (upstream validates, but never trust it)."""
    if not isinstance(options, dict):
        raise ValueError("motion bg options must be an object")

    style = options.get("style", DEFAULT_STYLE)
    if style not in STYLES:
        style = DEFAULT_STYLE

    accent = options.get("accent") or DEFAULT_ACCENT
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(accent)):
        accent = DEFAULT_ACCENT

    try:
        duration = int(options.get("duration_seconds", DEFAULT_DURATION))
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION
    duration = max(MIN_DURATION, min(MAX_DURATION, duration))

    return style, accent, duration


# ------------------------------------------------------------ blob sprites

def _radial_sprite(size, color, peak_alpha, falloff=2.0):
    """RGBA soft radial sprite: `color` at center, alpha fading to 0 at the
    edge with a smooth falloff. Built once, reused every frame."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    c = (size - 1) / 2.0
    maxd = c
    r, g, b = color
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / maxd
            if d >= 1.0:
                a = 0.0
            else:
                # Smooth cosine-ish falloff raised to a power for a soft core.
                a = (0.5 + 0.5 * math.cos(math.pi * d)) ** falloff
            px[x, y] = (r, g, b, int(peak_alpha * a))
    return img


def _base_layer(w, h, tint):
    """Low-res RGB base: near-black with a faint radial tint toward center,
    so blobs sit on a subtly graded field rather than flat black."""
    img = Image.new("RGB", (w, h), BASE_RGB)
    px = img.load()
    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0
    maxd = math.hypot(cx, cy)
    tr, tg, tb = tint
    br, bg, bb = BASE_RGB
    for y in range(h):
        for x in range(w):
            f = 1.0 - (math.hypot(x - cx, y - cy) / maxd)
            f = max(0.0, f) * 0.10          # very subtle
            px[x, y] = (
                int(br + (tr - br) * f),
                int(bg + (tg - bg) * f),
                int(bb + (tb - bb) * f),
            )
    return img


def _screen_paste(base, sprite, pos):
    """Additive-ish 'screen' composite of an RGBA sprite onto an RGB base at
    integer top-left `pos`. Brightens without clipping to garish white as
    hard as pure addition, giving the soft aurora glow."""
    x0, y0 = pos
    sw, sh = sprite.size
    bw, bh = base.size
    # Clip the region to the base bounds.
    dx0 = max(0, x0)
    dy0 = max(0, y0)
    dx1 = min(bw, x0 + sw)
    dy1 = min(bh, y0 + sh)
    if dx1 <= dx0 or dy1 <= dy0:
        return
    sub = sprite.crop((dx0 - x0, dy0 - y0, dx1 - x0, dy1 - y0))
    region = base.crop((dx0, dy0, dx1, dy1))
    sr, sg, sb, sa = sub.split()
    # Pre-multiply sprite colour by its alpha, then screen-blend:
    # out = base + src*(1 - base/255)  approximated per-channel.
    src_rgb = Image.merge("RGB", (sr, sg, sb))
    # Weight source by alpha.
    src_rgb = Image.composite(
        src_rgb, Image.new("RGB", src_rgb.size, (0, 0, 0)), sa)
    # screen blend
    inv = region.point(lambda v: 255 - v)
    contrib = ImageChops.multiply(src_rgb, inv)
    out = ImageChops.add(region, contrib)
    base.paste(out, (dx0, dy0))


# ---------------------------------------------------------------- aurora

def _render_aurora(total_frames, scheme, progress_cb, out_path):
    lw, lh = 480, 270
    tint = tuple(min(255, int(c * 0.6)) for c in scheme[2])
    base = _base_layer(lw, lh, tint)

    # 5 blobs, each on a periodic Lissajous-style path (wraps once per loop),
    # with a gently breathing brightness. All periodic in phase.
    sprite_size = 260
    blobs = []
    specs = [
        # (color, ax, ay, fx, fy, px, py, radius_scale, alpha)
        # Alphas kept modest so overlapping blobs don't screen-blend up into
        # a garish yellow-green; the deep/cool blob anchors the palette dark.
        (scheme[0], 0.30, 0.22, 1, 1, 0.0, 0.5, 1.05, 120),
        (scheme[2], 0.26, 0.30, 1, 1, 1.3, 0.0, 0.95, 115),
        (scheme[1], 0.34, 0.18, 1, 1, 2.4, 1.1, 1.20, 95),
        (scheme[2], 0.22, 0.28, 1, 1, 3.5, 2.0, 0.85, 105),
        (scheme[0], 0.30, 0.24, 1, 1, 4.6, 3.3, 1.10, 110),
    ]
    for color, ax, ay, fx, fy, phx, phy, rscale, alpha in specs:
        s = int(sprite_size * rscale)
        blobs.append({
            "sprite": _radial_sprite(s, color, alpha, falloff=1.6),
            "ax": ax, "ay": ay, "fx": fx, "fy": fy,
            "phx": phx, "phy": phy, "size": s,
        })

    cx = lw / 2.0
    cy = lh / 2.0

    with FrameEncoder(out_path, OUTPUT_FPS) as enc:
        for k in range(total_frames):
            phase = 2.0 * math.pi * k / total_frames
            frame = base.copy()
            for i, b in enumerate(blobs):
                # Centre drifts on a closed periodic path.
                x = cx + b["ax"] * lw * math.cos(b["fx"] * phase + b["phx"])
                y = cy + b["ay"] * lh * math.sin(b["fy"] * phase + b["phy"])
                # Periodic breathing scale via a second harmonic.
                breathe = 1.0 + 0.06 * math.sin(2.0 * phase + b["phx"])
                sp = b["sprite"]
                if abs(breathe - 1.0) > 1e-6:
                    ns = max(8, int(b["size"] * breathe))
                    sp = sp.resize((ns, ns), _BICUBIC)
                pos = (int(round(x - sp.width / 2.0)),
                       int(round(y - sp.height / 2.0)))
                _screen_paste(frame, sp, pos)
            big = frame.resize((WIDTH, HEIGHT), _LANCZOS)
            enc.add_frame(big)
            progress_cb(int((k + 1) * 100.0 / total_frames))


# ---------------------------------------------------------------- bokeh

def _render_bokeh(total_frames, scheme, progress_cb, out_path):
    lw, lh = 960, 540
    tint = tuple(min(255, int(c * 0.5)) for c in scheme[2])
    base = _base_layer(lw, lh, tint)

    # Deterministic pseudo-random dots (no RNG so the loop is reproducible
    # and every dot's path wraps exactly once per loop).
    n_dots = 32
    dots = []
    # Pre-build a few sprite sizes/colours to reuse.
    for i in range(n_dots):
        # Cheap deterministic hashing into [0,1).
        u1 = ((i * 73 + 17) % 100) / 100.0
        u2 = ((i * 129 + 41) % 100) / 100.0
        u3 = ((i * 191 + 7) % 100) / 100.0
        u4 = ((i * 233 + 59) % 100) / 100.0
        color = scheme[i % len(scheme)]
        size = int(28 + u3 * 70)                 # varied blur size
        alpha = int(45 + u4 * 90)                # varied opacity
        sprite = _radial_sprite(size, color, alpha, falloff=2.4)
        dots.append({
            "sprite": sprite,
            "size": size,
            "x0": u1 * lw,
            # Horizontal sway amplitude and phase (periodic).
            "sway": (0.02 + u2 * 0.05) * lw,
            "swayph": u1 * 2.0 * math.pi,
            # Vertical drift: wraps exactly once per loop (upward).
            "yoff": u2,
            "vdir": -1.0,          # drift upward
        })

    with FrameEncoder(out_path, OUTPUT_FPS) as enc:
        for k in range(total_frames):
            frac = k / float(total_frames)       # 0..1, wraps once
            phase = 2.0 * math.pi * frac
            frame = base.copy()
            for d in dots:
                sway = d["sway"] * math.sin(phase + d["swayph"])
                x = d["x0"] + sway
                # Vertical position wraps once per loop (seamless): as frac
                # goes 0->1 the dot travels one full screen height upward and
                # re-enters from the bottom. Because it's exactly one wrap,
                # frame N == frame 0.
                travel = (d["yoff"] + d["vdir"] * frac) % 1.0
                # Map [0,1) across an extended range so dots exist above and
                # below and there's no hard pop at the edge (the sprite is
                # soft and larger than the step).
                y = travel * (lh + d["size"] * 2) - d["size"]
                sp = d["sprite"]
                pos = (int(round(x - sp.width / 2.0)),
                       int(round(y - sp.height / 2.0)))
                _screen_paste(frame, sp, pos)
            big = frame.resize((WIDTH, HEIGHT), _LANCZOS)
            enc.add_frame(big)
            progress_cb(int((k + 1) * 100.0 / total_frames))


# ---------------------------------------------------------------- waves

def _render_waves(total_frames, scheme, progress_cb, out_path):
    lw, lh = 960, 540

    # 4 slow horizontal wave bands, each a soft translucent sine curve, kept
    # low-contrast so they read as a gentle gradient rather than solid fills.
    # Deeper/dimmer bands sit lower; brighter accent band is highest and most
    # transparent. All motion periodic in phase.
    bands = [
        # (color, base_y_frac, amp_frac, wavelength_frac, speed, vdrift, alpha)
        (scheme[2], 0.86, 0.045, 0.9, 1.0, 0.018, 150),
        (scheme[1], 0.70, 0.055, 0.7, -1.0, 0.022, 110),
        (scheme[0], 0.55, 0.050, 1.1, 1.0, 0.016, 70),
        (scheme[1], 0.42, 0.060, 0.8, -1.0, 0.020, 45),
    ]

    tint = tuple(min(255, int(c * 0.30)) for c in scheme[2])
    base = _base_layer(lw, lh, tint)

    with FrameEncoder(out_path, OUTPUT_FPS) as enc:
        for k in range(total_frames):
            phase = 2.0 * math.pi * k / total_frames
            frame = base.copy()
            layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            for (color, byf, ampf, wlf, speed, vdrift, alpha) in bands:
                base_y = byf * lh + vdrift * lh * math.sin(phase)
                amp = ampf * lh
                # Horizontal phase drift wraps once per loop -> seamless.
                # wavelength in pixels
                wl = wlf * lw
                pts = []
                step = 12
                for x in range(0, lw + step, step):
                    # sine argument: spatial term + speed*phase (periodic).
                    arg = 2.0 * math.pi * x / wl + speed * phase
                    y = base_y + amp * math.sin(arg)
                    pts.append((x, y))
                # Close the polygon down to the bottom of the frame.
                poly = pts + [(lw, lh), (0, lh)]
                draw.polygon(poly, fill=color + (alpha,))
            # Soften the band edges so they read as smooth gradients.
            layer = layer.filter(ImageFilter.GaussianBlur(6))
            frame = Image.alpha_composite(frame.convert("RGBA"), layer)
            big = frame.convert("RGB").resize((WIDTH, HEIGHT), _LANCZOS)
            enc.add_frame(big)
            progress_cb(int((k + 1) * 100.0 / total_frames))


# ---------------------------------------------------------------- renderer

def render_motion_bg(options, progress_cb):
    """Render a seamlessly-looping motion background MP4. Returns basename."""
    style, accent, duration = _clean_options(options)
    total_frames = duration * OUTPUT_FPS
    scheme = _derive_scheme(_hex_rgb(accent))

    out_path = export_path("motionbg", "{0}_{1}s".format(style, duration))

    if style == "aurora":
        _render_aurora(total_frames, scheme, progress_cb, out_path)
    elif style == "bokeh":
        _render_bokeh(total_frames, scheme, progress_cb, out_path)
    else:
        _render_waves(total_frames, scheme, progress_cb, out_path)

    return os.path.basename(out_path)
