"""Golden-frame regression harness for the app.py / static/app.js split.

Renders a fixed job list and hashes the **decoded pixels** of each output
(mp4 -> rawvideo via the bundled ffmpeg, png -> Pillow's RGB bytes), so the
proof covers the encoder too, not just "did it crash". A refactor that
moves code around without changing behaviour must reproduce every hash
exactly. See docs/specs/refactor-organise.md's "Golden harness" section.

Env vars are set before any `render.*` import — encoder.py reads
SERVICE_VISUALS_EXPORTS and scoreboard.py reads SERVICE_VISUALS_CONFIG
once, at import time, same discipline as scripts/smoke.py:

  SERVICE_VISUALS_STATS=0          no analytics from a harness run
  SERVICE_VISUALS_CONFIG=<tmp>     throwaway boards/backgrounds dir
  SERVICE_VISUALS_EXPORTS=<tmp>    throwaway exports dir (never exports/)
  SERVICE_VISUALS_ENCODER=libx264  same encoder on every machine — the
                                    Windows hardware-encoder probe
                                    (NVENC/QSV/AMF) would hash differently
                                    from libx264 on identical source frames

Usage:
  SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/golden.py --record
  SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/golden.py --check
  SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/golden.py --only qr/image
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

_CONFIG_DIR = tempfile.mkdtemp(prefix="sv-golden-config-")
_EXPORTS_DIR = tempfile.mkdtemp(prefix="sv-golden-exports-")
os.environ["SERVICE_VISUALS_STATS"] = "0"
os.environ["SERVICE_VISUALS_CONFIG"] = _CONFIG_DIR
os.environ["SERVICE_VISUALS_EXPORTS"] = _EXPORTS_DIR
os.environ["SERVICE_VISUALS_ENCODER"] = "libx264"

# Allow running as `scripts/golden.py` from anywhere (repo root for
# `render`, this dir for mock_board) — same trick as scripts/smoke.py.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import imageio_ffmpeg  # noqa: E402
from PIL import Image  # noqa: E402

from render.encoder import EXPORTS_DIR  # noqa: E402
from render.timer import render_timer  # noqa: E402
from render.spinner import render_spinner  # noqa: E402
from render.qr import render_qr, render_qr_image  # noqa: E402
from render.motionbg import render_motion_bg  # noqa: E402
from render import scoreboard  # noqa: E402

import mock_board  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
GOLDEN_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "golden.json")

_CHUNK = 1024 * 1024   # 1 MiB — bounds memory use decoding a multi-MB clip


def _quiet(pct):
    pass


# --------------------------------------------------------------- hashing
def _hash_mp4(path):
    """Decode to raw RGB24 with the bundled ffmpeg and hash the pixel
    stream in fixed-size chunks — a clip is never held whole in memory."""
    proc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", path,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    digest = hashlib.sha256()
    nbytes = 0
    while True:
        chunk = proc.stdout.read(_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        nbytes += len(chunk)
    _, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed decoding {0}: {1}".format(
            path, err.decode("utf-8", "replace")))
    return digest.hexdigest(), nbytes


def _hash_png(path):
    """sha256 of the decoded RGB pixel bytes (not the file bytes, so a
    re-encode with a different PNG compression level still matches)."""
    img = Image.open(path)
    img.load()
    raw = img.convert("RGB").tobytes()
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _hash(path):
    if path.lower().endswith(".png"):
        return _hash_png(path)
    return _hash_mp4(path)


# ----------------------------------------------------------------- jobs
def _timer(options):
    return os.path.join(EXPORTS_DIR, render_timer(options, _quiet))


def _spinner(options):
    return os.path.join(EXPORTS_DIR, render_spinner(options, _quiet))


def _qr(options):
    return os.path.join(EXPORTS_DIR, render_qr(options, _quiet))


def _qr_image(options):
    return os.path.join(EXPORTS_DIR, render_qr_image(options))


def _motionbg(options):
    return os.path.join(EXPORTS_DIR, render_motion_bg(options, _quiet))


def _make_timer_backgrounds():
    """Two solid-colour PNGs (portrait + landscape, like the real
    feature's own test images in check_prepare_background), written into
    the scratch config's backgrounds/ dir — same layout as app.py's
    BACKGROUNDS_DIR. render_timer takes literal paths, same as
    scripts/smoke.py's two-backgrounds job."""
    bg_dir = os.path.join(_CONFIG_DIR, "backgrounds")
    os.makedirs(bg_dir, exist_ok=True)
    wide = os.path.join(bg_dir, "wide.png")
    tall = os.path.join(bg_dir, "tall.png")
    Image.new("RGB", (400, 100), (210, 90, 40)).save(wide)
    Image.new("RGB", (100, 400), (40, 90, 210)).save(tall)
    return [wide, tall]


def _scoreboard():
    """mock board -> create -> change one value -> export PNG — the
    shortest path that exercises harvest, erase and recomposite (see
    scripts/smoke.py's check_scoreboard). export_board() writes its own
    copy into EXPORTS_DIR, so deleting the board afterward (keeping the
    scratch config dir tidy) does not touch the exported file."""
    source = mock_board.build()
    boxes = mock_board.number_boxes()
    board = scoreboard.create_board(source, "Golden board", boxes=boxes)
    target = board["boxes"][0]
    scoreboard.save_values(board["id"], {target["id"]: "409"})
    filename = scoreboard.export_board(board["id"])
    scoreboard.delete_board(board["id"])
    return os.path.join(EXPORTS_DIR, filename)


def _build_jobs():
    """The fixed job list (docs/specs/refactor-organise.md). Order is
    stable; each entry is (name, fn) where fn() renders and returns the
    absolute output path."""
    timer_backgrounds = _make_timer_backgrounds()
    six = ["Alice", "Bob", "Carol", "Dave", "Erin", "Frank"]

    def spinner_random():
        # Both spinner modes draw from `random` for the landing jitter,
        # not just "random" mode's winner pick — seed right before the
        # call so a wheel job is reproducible regardless of run order.
        random.seed(7)
        return _spinner({"entries": six, "mode": "random",
                          "accent": "#e8b44f"})

    def spinner_rigged():
        random.seed(7)
        return _spinner({"entries": six, "mode": "rigged",
                          "winner": "Carol", "accent": "#e8b44f"})

    return [
        ("timer/classic", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "classic",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2})),
        ("timer/ring", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "ring",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2})),
        ("timer/bar", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "bar",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2})),
        ("timer/classic-millis", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "classic",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2, "show_millis": True})),
        ("timer/bar-two-backgrounds", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "bar",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2, "backgrounds": timer_backgrounds,
             "bg_seconds": 2, "bg_dim": 45, "bg_blur": True})),
        # green_screen (docs/specs/green-screen.md) is landing in
        # parallel with this harness and may not exist yet — a renderer
        # that ignores the key still produces something to hash, which
        # is the point: this job pins whatever comes out today and will
        # start pinning the real feature the moment it lands.
        ("timer/classic-green-screen", lambda: _timer(
            {"minutes": 0, "seconds": 6, "style": "classic",
             "accent": "#e8b44f", "warn_last10": True,
             "hold_seconds": 2, "green_screen": True})),
        ("clock/classic-12h-millis", lambda: _timer(
            {"mode": "clock", "start": "19:59:57",
             "duration_seconds": 6, "format": "12h",
             "show_seconds": True, "show_millis": True,
             "style": "classic", "accent": "#e8b44f"})),
        ("clock/ring-24h", lambda: _timer(
            {"mode": "clock", "start": "23:59:55",
             "duration_seconds": 6, "format": "24h",
             "show_seconds": True, "show_millis": False,
             "style": "ring", "accent": "#e8b44f"})),
        ("spinner/random", spinner_random),
        ("spinner/rigged", spinner_rigged),
        ("qr/video", lambda: _qr(
            {"url": "https://church.example/give",
             "heading": "SCAN TO GIVE", "caption": "Thank you",
             "accent": "#e8b44f", "duration_seconds": 5})),
        ("qr/image", lambda: _qr_image(
            {"url": "https://church.example/give",
             "heading": "SCAN TO GIVE", "caption": "Thank you",
             "accent": "#e8b44f"})),
        ("motionbg/aurora", lambda: _motionbg(
            {"style": "aurora", "accent": "#e8b44f",
             "duration_seconds": 5})),
        ("motionbg/bokeh", lambda: _motionbg(
            {"style": "bokeh", "accent": "#e8b44f",
             "duration_seconds": 5})),
        ("motionbg/waves", lambda: _motionbg(
            {"style": "waves", "accent": "#e8b44f",
             "duration_seconds": 5})),
        ("scoreboard/export", _scoreboard),
    ]


def run_jobs(only=None):
    """Render every job (or just the names in `only`), hash each output,
    and return {name: {"sha": .., "bytes": n}}, printed as it goes."""
    jobs = _build_jobs()
    if only:
        wanted = set(only)
        jobs = [(n, fn) for n, fn in jobs if n in wanted]
        missing = wanted - {n for n, _fn in jobs}
        if missing:
            raise SystemExit("--only: no such job(s): {0}".format(
                ", ".join(sorted(missing))))
    results = {}
    for name, fn in jobs:
        path = fn()
        sha, nbytes = _hash(path)
        results[name] = {"sha": sha, "bytes": nbytes}
        print("  {0:<28} {1}  ({2} bytes)".format(name, sha[:12], nbytes))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Golden-frame harness (see docs/specs/"
                     "refactor-organise.md).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true",
                       help="render the job list and write golden.json")
    mode.add_argument("--check", action="store_true",
                       help="render and diff against golden.json "
                            "(default)")
    parser.add_argument("--only", action="append", default=None,
                         metavar="NAME",
                         help="run just this job (repeatable); "
                              "default is every job")
    args = parser.parse_args()

    start = time.time()
    try:
        results = run_jobs(args.only)
    finally:
        # Scratch dirs only — the real exports/ and ~/.service-visuals
        # are never touched (SERVICE_VISUALS_EXPORTS/_CONFIG override).
        shutil.rmtree(_CONFIG_DIR, ignore_errors=True)
        shutil.rmtree(_EXPORTS_DIR, ignore_errors=True)
    elapsed = time.time() - start
    print("\n{0} job(s) in {1:.1f}s".format(len(results), elapsed))

    if args.record:
        with open(GOLDEN_JSON, "w") as f:
            json.dump(results, f, indent=2, sort_keys=True)
            f.write("\n")
        print("Wrote {0}".format(GOLDEN_JSON))
        return 0

    # --check (the default with no flag at all)
    if not os.path.isfile(GOLDEN_JSON):
        print("No {0} — run --record first.".format(GOLDEN_JSON))
        return 1
    with open(GOLDEN_JSON) as f:
        recorded = json.load(f)

    mismatches = []
    for name, got in results.items():
        want = recorded.get(name)
        if want is None:
            mismatches.append("{0}: not in golden.json".format(name))
        elif want != got:
            mismatches.append(
                "{0}: recorded sha={1} bytes={2}, got sha={3} "
                "bytes={4}".format(
                    name, want.get("sha", "?")[:12], want.get("bytes"),
                    got["sha"][:12], got["bytes"]))
    if mismatches:
        print("GOLDEN CHECK FAILED — {0} mismatch(es):".format(
            len(mismatches)))
        for m in mismatches:
            print("  - " + m)
        return 1
    print("GOLDEN CHECK PASSED — {0} job(s) match.".format(len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
