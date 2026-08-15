#!/usr/bin/env python3
"""Generate a stand-in "GROUP POINTS" board for scoreboard development.

The real board is a weekly youth-group graphic the user uploads once. Nobody
should need that private image to work on (or test) render/scoreboard.py, so
this draws a board with the same shape: a blue field, six cream rounded cards,
each with a team name, a big pink 3-digit number and "POINTS" underneath.

    python scripts/mock_board.py [out_path]        # default /tmp/mock_board.png

Deliberately close to the real thing in the ways that matter to the OCR +
digit-harvest pipeline: the numbers are a strongly saturated colour on a light
card (so ink/background separation is a real test), they are anti-aliased, they
sit on a shared baseline, and there is other non-numeric text nearby that must
be ignored and left untouched.
"""

import os
import sys

from PIL import Image, ImageDraw

# scripts/ is not on sys.path when run directly; add the project root so
# `render` imports resolve the same way they do inside the app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from render import fonts  # noqa: E402

WIDTH = 1656
HEIGHT = 931

BLUE = (18, 52, 120)
BLUE_DEEP = (12, 36, 88)
CREAM = (246, 239, 223)
DARK = (35, 37, 43)
PINK = (236, 20, 140)

TEAMS = ("RED", "BLUE", "GREEN", "YELLOW", "PURPLE", "ORANGE")
NUMBERS = ("350", "240", "265", "365", "250", "270")

MARGIN = 56
GAP = 36
TITLE_TOP = 34
TITLE_SIZE = 62
CARDS_TOP = 150

NAME_SIZE = 46
NUMBER_SIZE = 96
POINTS_SIZE = 32
CARD_RADIUS = 34


def _centered(draw, cx, top, text, font, fill):
    """Draw `text` centred on cx with its glyph-bbox top at `top`."""
    bbox = font.getbbox(text)
    draw.text((cx - (bbox[2] - bbox[0]) / 2.0 - bbox[0], top - bbox[1]),
              text, font=font, fill=fill)


NUMBER_TOP = 128        # number's glyph top, relative to its card
NAME_TOP = 34
POINTS_TOP = 264


def _layout():
    """Yield (team, number, x0, y0, cx) for each card.

    Single source of truth for the grid, so `number_boxes()` can never drift
    away from where `build()` actually draws the numbers.
    """
    card_w = (WIDTH - 2 * MARGIN - 2 * GAP) // 3
    card_h = (HEIGHT - CARDS_TOP - MARGIN - GAP) // 2
    for i, (team, number) in enumerate(zip(TEAMS, NUMBERS)):
        col, row = i % 3, i // 3
        x0 = MARGIN + col * (card_w + GAP)
        y0 = CARDS_TOP + row * (card_h + GAP)
        yield team, number, x0, y0, x0 + card_w // 2, card_w, card_h


def number_boxes():
    """The numbers' rects, in the shape `detect_numbers` returns.

    Lets the smoke test drive the board pipeline with the boxes handed in
    directly, so it runs on Linux CI where no OS text recogniser exists.
    Deliberately a little loose — like a real OCR rect — but tight enough to
    exclude the team name above and "POINTS" below, which are a different ink
    colour and would confuse the ink/background sampling.
    """
    boxes = []
    for _team, number, _x0, y0, cx, _cw, _ch in _layout():
        boxes.append({"text": number,
                      "x": cx - 100, "y": y0 + NUMBER_TOP, "w": 200, "h": 80})
    return boxes


def build():
    img = Image.new("RGB", (WIDTH, HEIGHT), BLUE)
    draw = ImageDraw.Draw(img)

    # A soft vertical gradient so the background is not one flat colour —
    # a flat fill would let a lazy "erase" implementation pass by accident.
    for y in range(HEIGHT):
        f = y / float(HEIGHT - 1)
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=(round(BLUE[0] + (BLUE_DEEP[0] - BLUE[0]) * f),
                  round(BLUE[1] + (BLUE_DEEP[1] - BLUE[1]) * f),
                  round(BLUE[2] + (BLUE_DEEP[2] - BLUE[2]) * f)))

    title_font = fonts.load("label", TITLE_SIZE)
    name_font = fonts.load("label", NAME_SIZE)
    number_font = fonts.load("digits", NUMBER_SIZE)
    points_font = fonts.load("caption", POINTS_SIZE)

    _centered(draw, WIDTH // 2, TITLE_TOP, "GROUP POINTS", title_font, CREAM)

    for team, number, x0, y0, cx, card_w, card_h in _layout():
        draw.rounded_rectangle([x0, y0, x0 + card_w - 1, y0 + card_h - 1],
                               radius=CARD_RADIUS, fill=CREAM)
        _centered(draw, cx, y0 + NAME_TOP, team, name_font, DARK)
        _centered(draw, cx, y0 + NUMBER_TOP, number, number_font, PINK)
        _centered(draw, cx, y0 + POINTS_TOP, "POINTS", points_font, DARK)

    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mock_board.png"
    img = build()
    img.save(out, format="PNG")
    print(out)


if __name__ == "__main__":
    main()
