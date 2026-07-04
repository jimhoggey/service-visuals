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
import subprocess
import sys

# Allow running from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imageio_ffmpeg  # noqa: E402

from render.encoder import EXPORTS_DIR  # noqa: E402
from render.timer import render_timer  # noqa: E402
from render.spinner import render_spinner  # noqa: E402

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
        rendered.append(("spinner", fn, 12.0))

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
    if failures:
        print("SMOKE FAILED — {0} check(s) failed:".format(len(failures)))
        for f in failures:
            print("  - " + f)
        return 1
    print("SMOKE PASSED — all checks green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
