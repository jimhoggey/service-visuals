"""System font discovery for renderers.

All fonts ship with macOS, so renders work offline with no font downloads.
Face indices inside the .ttc collections were verified on this machine:
  HelveticaNeue.ttc: 0=Regular, 1=Bold, 10=Medium
  Avenir Next.ttc:   0=Bold, 2=Demi Bold, 5=Medium, 7=Regular, 8=Heavy
  Menlo.ttc:         0=Regular, 1=Bold
"""

from functools import lru_cache

from PIL import ImageFont

# role -> ordered candidates of (path, ttc_index)
_CANDIDATES = {
    # Big countdown digits: geometric, heavy, highly legible at distance.
    "digits": [
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),   # Bold
        ("/System/Library/Fonts/Avenir Next.ttc", 8),     # Heavy
        ("/System/Library/Fonts/Helvetica.ttc", 1),
        ("C:/Windows/Fonts/arialbd.ttf", 0),
        ("C:/Windows/Fonts/segoeuib.ttf", 0),
    ],
    # Labels, wheel entries, winner card.
    "label": [
        ("/System/Library/Fonts/Avenir Next.ttc", 2),     # Demi Bold
        ("/System/Library/Fonts/HelveticaNeue.ttc", 10),  # Medium
        ("/System/Library/Fonts/Helvetica.ttc", 0),
        ("C:/Windows/Fonts/seguisb.ttf", 0),
        ("C:/Windows/Fonts/arialbd.ttf", 0),
    ],
    # Small utility text (e.g. "TIME REMAINING" caption).
    "caption": [
        ("/System/Library/Fonts/Avenir Next.ttc", 5),     # Medium
        ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 0),
        ("C:/Windows/Fonts/segoeui.ttf", 0),
        ("C:/Windows/Fonts/arial.ttf", 0),
    ],
    # Monospace fallback / technical.
    "mono": [
        ("/System/Library/Fonts/Menlo.ttc", 1),           # Bold
        ("/System/Library/Fonts/Menlo.ttc", 0),
        ("C:/Windows/Fonts/consolab.ttf", 0),
        ("C:/Windows/Fonts/consola.ttf", 0),
    ],
}


@lru_cache(maxsize=64)
def load(role, size):
    """Return an ImageFont for the given role at the given pixel size.

    Falls back down the candidate list, then to PIL's built-in default,
    so rendering never hard-fails on a missing font.
    """
    for path, index in _CANDIDATES.get(role, []):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size)
