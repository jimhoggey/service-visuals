"""Headless smoke test for the Service Visuals renderers.

Renders a short timer in each style plus a rigged spinner, then decodes
each MP4 with the bundled ffmpeg and asserts:

  * codec  : h264
  * size   : 1920x1080
  * pixfmt : yuv420p
  * length : expected duration +/- 0.5 s

Prints PASS/FAIL per check, exits nonzero on any failure, and removes its
own test files from exports/ so the user's export folder stays clean.

Run:  .venv/bin/python scripts/smoke.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

# Allow running from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The scoreboard check writes boards to ~/.service-visuals unless redirected.
# Point it at a throwaway dir BEFORE render.scoreboard is imported (it reads
# the variable once, at import) so a smoke run never touches real boards.
_BOARD_TMP = tempfile.mkdtemp(prefix="sv-smoke-boards-")
os.environ["SERVICE_VISUALS_CONFIG"] = _BOARD_TMP

import imageio_ffmpeg  # noqa: E402

from render.encoder import EXPORTS_DIR  # noqa: E402
from render.timer import render_timer  # noqa: E402
from render.spinner import render_spinner  # noqa: E402
from render.qr import render_qr  # noqa: E402
from render.motionbg import render_motion_bg  # noqa: E402
from render import scoreboard  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_board  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

failures = []


def check(label, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    line = "  [{0}] {1}".format(status, label)
    if detail and not ok:
        line += "  ({0})".format(detail)
    print(line)
    if not ok:
        failures.append(label)


def probe(path):
    """Decode stream metadata with the bundled ffmpeg (no ffprobe shipped).

    `ffmpeg -i <file>` exits nonzero (no output specified) but prints the
    container/stream info we need to stderr.
    """
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    text = proc.stderr.decode("utf-8", "replace")

    info = {"codec": None, "size": None, "pixfmt": None, "duration": None}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                            + float(m.group(3)))

    m = re.search(r"Stream #\d+:\d+.*?: Video: (\w+)[^\n]*", text)
    if m:
        line = m.group(0)
        info["codec"] = m.group(1)
        sm = re.search(r"(\d{3,5})x(\d{3,5})", line)
        if sm:
            info["size"] = (int(sm.group(1)), int(sm.group(2)))
        pm = re.search(r"yuv\w+|rgb\w+", line)
        if pm:
            info["pixfmt"] = pm.group(0)
    return info


def verify(name, filename, expected_duration):
    path = os.path.join(EXPORTS_DIR, filename)
    print("{0}: {1}".format(name, filename))
    if not os.path.isfile(path):
        check("{0} file exists".format(name), False, "missing: " + path)
        return
    info = probe(path)
    check("{0} codec h264".format(name), info["codec"] == "h264",
          "got {0!r}".format(info["codec"]))
    check("{0} size 1920x1080".format(name), info["size"] == (1920, 1080),
          "got {0!r}".format(info["size"]))
    check("{0} pixfmt yuv420p".format(name), info["pixfmt"] == "yuv420p",
          "got {0!r}".format(info["pixfmt"]))
    dur = info["duration"]
    ok = dur is not None and abs(dur - expected_duration) <= 0.5
    check("{0} duration ~{1}s".format(name, expected_duration), ok,
          "got {0!r}".format(dur))


def check_vision_flip():
    """Apple Vision's bottom-left origin must be flipped, not copied.

    Getting this backwards mirrors every box vertically and still looks
    plausible, so it is asserted explicitly. Pure arithmetic — no OCR engine
    involved, so it runs on CI too.
    """
    print("Scoreboard: Vision rect conversion")
    # A box across the TOP of a 1000x800 image: Vision measures y up from the
    # bottom, so a top-of-image box has a HIGH y (0.75) and must come back as
    # a LOW pixel y (0.0). A naive y*height would put it at 600.
    x, y, w, h = scoreboard._from_vision_rect(0.25, 0.75, 0.5, 0.25, 1000, 800)
    check("vision y-flip puts a top box at the top",
          (x, y, w, h) == (250, 0, 500, 200),
          "got {0!r}, expected (250, 0, 500, 200)".format((x, y, w, h)))

    # ...and a box across the BOTTOM (y=0) must land at the bottom.
    x, y, w, h = scoreboard._from_vision_rect(0.0, 0.0, 1.0, 0.25, 1000, 800)
    check("vision y-flip puts a bottom box at the bottom",
          (x, y, w, h) == (0, 600, 1000, 200),
          "got {0!r}, expected (0, 600, 1000, 200)".format((x, y, w, h)))


def _winocr_stub(result, limit=10000):
    """A stand-in for the winocr module, shaped like the real one.

    `recognize_pil_sync` returns the dict winocr builds out of an OcrResult
    ({"text_angle", "lines":[{"words":[{"text","bounding_rect"}]}]}), and
    OcrEngine carries the static max_image_dimension. Lets the Windows branch
    be exercised on this Mac, which is otherwise the only code path in the
    feature nobody can run before release.
    """
    import types

    module = types.ModuleType("winocr")

    class OcrEngine(object):
        max_image_dimension = limit

    module.OcrEngine = OcrEngine
    module.recognize_pil_sync = lambda image, lang: result
    return module


def _rect(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def check_windows_ocr():
    """The Windows detector, driven with hand-written winocr output."""
    import sys as _sys

    from PIL import Image

    print("Scoreboard: Windows OCR contract (stubbed)")
    img = Image.new("RGB", (900, 500), (255, 255, 255))

    # Letter-spaced "3 5 0" arrives as three words; a small label digit and a
    # phone number must not become editable boxes.
    words = [
        {"text": "3", "bounding_rect": _rect(100, 100, 30, 60)},
        {"text": "5", "bounding_rect": _rect(140, 100, 30, 60)},
        {"text": "0", "bounding_rect": _rect(180, 100, 30, 60)},
        {"text": "1", "bounding_rect": _rect(400, 110, 10, 20)},
        {"text": "0412345678", "bounding_rect": _rect(100, 300, 200, 60)},
    ]
    result = {"text_angle": 0.0, "lines": [{"words": words}]}

    saved = _sys.modules.get("winocr")
    _sys.modules["winocr"] = _winocr_stub(result)
    try:
        boxes = scoreboard._detect_windows(img)
        check("split digits are rejoined into one number",
              [b["text"] for b in boxes] == ["350"],
              "got {0!r}".format([b.get("text") for b in boxes]))
        check("the rejoined box spans all three digits",
              boxes and (boxes[0]["x"], boxes[0]["w"]) == (100, 110),
              "got {0!r}".format(boxes[:1]))

        # A tilted capture must be refused, not silently mapped onto the
        # wrong pixels: the rects Windows returns are de-skewed.
        _sys.modules["winocr"] = _winocr_stub(
            {"text_angle": -3.0, "lines": [{"words": words}]})
        try:
            scoreboard._detect_windows(img)
            check("a tilted board is refused", False, "no error raised")
        except scoreboard.BoardError as exc:
            check("a tilted board is refused", "rotated" in str(exc),
                  "got {0!r}".format(str(exc)))

        # Too big for Windows.Media.Ocr is the input's fault, not the
        # platform's, so it must NOT claim OCR is unavailable.
        _sys.modules["winocr"] = _winocr_stub(result, limit=100)
        try:
            scoreboard._detect_windows(img)
            check("an oversized image is refused", False, "no error raised")
        except scoreboard.OCRUnavailable as exc:
            check("an oversized image is refused", False,
                  "blamed the system: {0!r}".format(str(exc)))
        except scoreboard.BoardError as exc:
            check("an oversized image is refused", "900px" in str(exc),
                  "got {0!r}".format(str(exc)))
    finally:
        if saved is None:
            _sys.modules.pop("winocr", None)
        else:
            _sys.modules["winocr"] = saved


def check_scoreboard():
    """Board harvest -> erase -> recompose, with the boxes supplied directly.

    Deliberately does NOT call detect_numbers: CI is Linux, which has no OS
    text recogniser. Everything that can actually break — digit harvesting,
    the content-aware erase, and compositing the new value — happens after
    detection, so handing in the rects tests the interesting part everywhere.
    """
    import io

    from PIL import Image, ImageChops

    print("Scoreboard: synthetic board (no OCR)")
    source = mock_board.build()
    boxes = mock_board.number_boxes()

    board = scoreboard.create_board(source, "Smoke board", boxes=boxes)
    check("board has one box per number",
          len(board["boxes"]) == len(boxes),
          "got {0} boxes, expected {1}".format(
              len(board["boxes"]), len(boxes)))
    check("board values seeded from the image",
          [board["values"][b["id"]] for b in board["boxes"]]
          == list(mock_board.NUMBERS),
          "got {0!r}".format(board.get("values")))

    # Change exactly one number; every other box must be left completely alone.
    target = board["boxes"][0]
    scoreboard.save_values(board["id"], {target["id"]: "409"})
    board = scoreboard.load_board(board["id"])

    out = scoreboard.render_board(board["id"])   # by id, the way app.py calls it
    check("render matches the source size (not forced to 1920x1080)",
          out.size == source.size,
          "got {0!r}, source is {1!r}".format(out.size, source.size))

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    reopened = Image.open(buf)
    reopened.load()
    check("render is a valid PNG",
          reopened.format == "PNG" and reopened.size == source.size,
          "got format={0!r} size={1!r}".format(
              reopened.format, reopened.size))

    # The only pixels allowed to move are inside the edited box's padded crop.
    diff = ImageChops.difference(source.convert("RGB"), out).getbbox()
    check("changing a value actually changed pixels", diff is not None,
          "the render is byte-identical to the source")

    if diff is not None:
        pad_x = scoreboard.BOX_PAD
        pad_y = max(scoreboard.BOX_PAD, int(round(target["h"] * 0.18)))
        allowed = (target["x"] - pad_x, target["y"] - pad_y,
                   target["x"] + target["w"] + pad_x,
                   target["y"] + target["h"] + pad_y)
        inside = (diff[0] >= allowed[0] and diff[1] >= allowed[1]
                  and diff[2] <= allowed[2] and diff[3] <= allowed[3])
        check("nothing outside the edited box moved", inside,
              "changed region {0!r} escapes the box's crop {1!r}".format(
                  diff, allowed))

        # And the untouched boxes are byte-identical, not merely close.
        other = board["boxes"][-1]
        crop = (other["x"] - pad_x, other["y"] - pad_y,
                other["x"] + other["w"] + pad_x,
                other["y"] + other["h"] + pad_y)
        check("an untouched number is byte-identical",
              source.crop(crop).tobytes() == out.crop(crop).tobytes(),
              "box {0!r} changed".format(other["id"]))

    # A value longer than the original must be scaled into its own card, not
    # painted over the artwork beside it. Compared on a horizontal strip so
    # only this box's row is in the diff.
    wide = board["boxes"][-1]
    scoreboard.save_values(board["id"], {wide["id"]: "123456"})
    long_out = scoreboard.render_board(board["id"])
    pad_y = max(scoreboard.BOX_PAD, int(round(wide["h"] * 0.18)))
    strip = (0, max(0, wide["y"] - pad_y), source.width,
             min(source.height, wide["y"] + wide["h"] + pad_y))
    moved = ImageChops.difference(source.convert("RGB").crop(strip),
                                  long_out.crop(strip)).getbbox()
    lo = min(wide.get("safe_x0", wide["x"]), wide["x"]) - scoreboard.BOX_PAD
    hi = max(wide.get("safe_x1", wide["x"] + wide["w"]),
             wide["x"] + wide["w"]) + scoreboard.BOX_PAD
    check("a six-digit value stays inside its card",
          moved is not None and moved[0] >= lo and moved[2] <= hi,
          "changed columns {0!r}, card allows {1}..{2}".format(moved, lo, hi))

    # The export lands in exports/ as a real PNG, like the QR still does.
    filename = scoreboard.export_board(board["id"])
    path = os.path.join(EXPORTS_DIR, filename)
    try:
        check("export writes a .png into exports/",
              filename.endswith(".png") and os.path.isfile(path),
              "got {0!r}".format(filename))
        if os.path.isfile(path):
            saved = Image.open(path)
            saved.load()
            check("exported PNG matches the source size",
                  saved.format == "PNG" and saved.size == source.size,
                  "got format={0!r} size={1!r}".format(
                      saved.format, saved.size))
    finally:
        if os.path.isfile(path):
            os.unlink(path)

    scoreboard.delete_board(board["id"])
    check("deleting a board removes its folder",
          not os.path.isdir(os.path.join(scoreboard.BOARDS_DIR, board["id"])))


def main():
    rendered = []  # basenames to clean up

    def quiet_progress(pct):
        pass

    print("Rendering test videos (this takes a minute)...")
    try:
        for style in ("classic", "ring", "bar"):
            fn = render_timer(
                {"minutes": 0, "seconds": 6, "style": style,
                 "accent": "#e8b44f", "warn_last10": True,
                 "hold_seconds": 2},
                quiet_progress)
            rendered.append(("timer/" + style, fn, 8.0))

        fn = render_spinner(
            {"entries": ["Alice", "Bob", "Carol", "Dave"],
             "mode": "rigged", "winner": "Carol", "accent": "#e8b44f"},
            quiet_progress)
        rendered.append(("spinner", fn, 11.8))  # 0 wait + 0.8 windup + 7 spin + 4 winner

        # QR "scan to..." card — short 5 s clip.
        fn = render_qr(
            {"url": "https://church.example/give",
             "heading": "SCAN TO GIVE", "caption": "Thank you",
             "accent": "#e8b44f", "duration_seconds": 5},
            quiet_progress)
        rendered.append(("qr", fn, 5.0))

        # Motion background — a short loop in each style. The renderer floors
        # duration at MIN_DURATION (5 s) per the design, so a 3 s request is
        # clamped up to 5 s; probe against the real contracted 5 s output.
        for style in ("aurora", "bokeh", "waves"):
            fn = render_motion_bg(
                {"style": style, "accent": "#e8b44f",
                 "duration_seconds": 5},
                quiet_progress)
            rendered.append(("motionbg/" + style, fn, 5.0))

        print()
        for name, filename, expected in rendered:
            verify(name, filename, expected)
    finally:
        # Keep exports/ clean for the user.
        for _name, filename, _expected in rendered:
            path = os.path.join(EXPORTS_DIR, filename)
            if os.path.isfile(path):
                os.unlink(path)

    print()
    try:
        check_vision_flip()
        print()
        check_windows_ocr()
        print()
        check_scoreboard()
    finally:
        shutil.rmtree(_BOARD_TMP, ignore_errors=True)

    print()
    if failures:
        print("SMOKE FAILED — {0} check(s) failed:".format(len(failures)))
        for f in failures:
            print("  - " + f)
        return 1
    print("SMOKE PASSED — all checks green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
