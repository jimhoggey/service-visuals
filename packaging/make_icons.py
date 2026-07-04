"""Generate app icons (assets/icon.icns, assets/icon.ico, assets/icon.png).

Run once from the repo root: .venv/bin/python packaging/make_icons.py
The motif matches the app: charcoal rounded square, amber timer ring.
"""

import os

from PIL import Image, ImageDraw

SIZE = 1024
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def draw_icon():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded-square charcoal tile
    m = 64
    d.rounded_rectangle([m, m, SIZE - m, SIZE - m], radius=200, fill=(14, 16, 19, 255))
    # Amber ring, 12 o'clock anchored, 270-degree arc (timer motif)
    cx = cy = SIZE // 2
    r = 300
    d.arc([cx - r, cy - r, cx + r, cy + r], start=270, end=180,
          fill=(232, 180, 79, 255), width=64)
    # Track for the remaining quarter
    d.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=270,
          fill=(35, 38, 43, 255), width=64)
    # Center dot
    d.ellipse([cx - 56, cy - 56, cx + 56, cy + 56], fill=(232, 180, 79, 255))
    return img


def main():
    os.makedirs(ASSETS, exist_ok=True)
    img = draw_icon()
    img.save(os.path.join(ASSETS, "icon.png"))
    img.save(os.path.join(ASSETS, "icon.icns"))
    img.save(os.path.join(ASSETS, "icon.ico"),
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
