"""Service Visuals — Flask app: routes, validation, wiring.

Serves the single-page UI, validates render requests, queues them on the
JobManager, and hands finished MP4s back for download / Finder reveal.
Runs on 127.0.0.1 only; port 8765 by default (5000 collides with macOS
AirPlay Receiver).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import webbrowser

from version import APP_VERSION  # noqa: E402
GITHUB_REPO = "jimhoggey/service-visuals"

import io
import uuid

from flask import (Flask, jsonify, request, send_file, send_from_directory)
from flask.signals import got_request_exception
from PIL import Image

import aiassist
import netutil
import stats
import updater
from jobs import JobManager
from render.encoder import EXPORTS_DIR, UPLOADS_DIR
from render.timer import CLOCK_STYLES, render_timer
from render.spinner import render_spinner
from render.qr import (POSITIONS, render_qr, render_qr_image,
                       render_qr_still)
from render.motionbg import render_motion_bg
from render.scoreboard import (BOARDS_DIR, BoardError, OCRUnavailable,
                               add_box, create_board, delete_board,
                               export_board, list_boards, load_board,
                               render_board, save_values)

# When frozen by PyInstaller the static files live under the unpack dir.
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, static_folder=os.path.join(_BASE_DIR, "static"),
            static_url_path="/static")

# Background-image uploads need headroom, so the global cap is generous; the
# /api/render route below enforces its own small JSON limit so a huge JSON
# number can't pin a core (quadratic int parsing on Python 3.9).
MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Localhost-only trust model: reject foreign Host headers so a DNS-rebound
# page (attacker.com -> 127.0.0.1) cannot drive the unauthenticated API.
ALLOWED_HOSTNAMES = ("localhost", "127.0.0.1")


@app.before_request
def _reject_foreign_hosts():
    hostname = (request.host or "").rsplit(":", 1)[0]
    if hostname not in ALLOWED_HOSTNAMES:
        return jsonify({"error": "Host not allowed."}), 403


def _counted(tool, fn):
    """Count an export once it has actually produced a file."""
    def run(options, progress_cb):
        filename = fn(options, progress_cb)
        stats.track("export", tool=tool)
        return filename
    return run


jobs = JobManager({"timer": _counted("timer", render_timer),
                   "spinner": _counted("spinner", render_spinner),
                   "qr": _counted("qr", render_qr),
                   "motionbg": _counted("motionbg", render_motion_bg)},
                  on_error=lambda tool, exc:
                      stats.report_error("render_failed", exc, tool=tool))


@got_request_exception.connect_via(app)
def _report_unhandled(sender, exception, **_extra):
    """A route that blew up (a 500) is a crash from the operator's seat."""
    # Endpoint name only — never the raw URL, which could carry a board id.
    stats.report_error("request_failed", exception,
                       route=str(request.endpoint or "unmatched")[:60])

# NB: matched with .fullmatch() — "$" alone would accept a trailing newline.
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
EXPORT_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]+\.(mp4|png)")

TIMER_STYLES = ("classic", "ring", "bar")
CLOCK_FORMATS = ("12h", "24h")
# HH:MM:SS, 24-hour, zero-padded — the exact shape the "Shows as ..." hint
# and the renderer both expect. fullmatch()'d, so trailing junk is rejected.
CLOCK_START_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d):([0-5]\d)")
SPINNER_MODES = ("random", "rigged")
MOTIONBG_STYLES = ("aurora", "bokeh", "waves")
DEFAULT_ACCENT = "#e8b44f"


class ValidationError(Exception):
    """Raised with a plain-English message suitable for the UI."""


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------

def _int_field(options, key, lo, hi, default, label):
    """Fetch an integer option, rejecting bools, strings and fractions.

    JSON has no separate int type, so a whole-valued float (e.g. 5.0) is
    accepted; True/False and "5" are not.
    """
    value = options.get(key, default)
    range_msg = "{0} must be a whole number between {1} and {2}.".format(
        label, lo, hi)
    if isinstance(value, bool):
        # bool is a subclass of int — reject it explicitly.
        raise ValidationError(range_msg + " (Got true/false.)")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError(range_msg + " (Fractions are not allowed.)")
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(
            range_msg + " (Got {0!r} — send a number, not text.)".format(value))
    if value < lo or value > hi:
        raise ValidationError(range_msg)
    return value


def _accent_field(options):
    accent = options.get("accent", DEFAULT_ACCENT)
    if not isinstance(accent, str) or not HEX_COLOR_RE.fullmatch(accent):
        raise ValidationError(
            'Accent must be a 6-digit hex color like "#e8b44f".')
    return accent


def validate_timer_options(options):
    """Dispatch on options["mode"]: "clock" is the new live-clock branch
    (below); anything else — including the key being absent — is the
    original countdown, validated exactly as before this feature existed.
    """
    if options.get("mode") == "clock":
        return _validate_clock_options(options)
    return _validate_countdown_options(options)


def _validate_countdown_options(options):
    minutes = _int_field(options, "minutes", 0, 120, 0, "Minutes")
    seconds = _int_field(options, "seconds", 0, 59, 0, "Seconds")
    total = minutes * 60 + seconds
    if total < 5:
        raise ValidationError("The timer must run for at least 5 seconds.")
    if total > 7200:
        raise ValidationError(
            "The timer can run for at most 120 minutes (2 hours) in total.")

    style = options.get("style", "classic")
    if not isinstance(style, str) or style not in TIMER_STYLES:
        raise ValidationError(
            "Style must be one of: classic, ring, or bar.")

    warn_last10 = options.get("warn_last10", True)
    if not isinstance(warn_last10, bool):
        raise ValidationError(
            'The "warn in the last 10 seconds" option must be true or false.')

    hold_seconds = _int_field(
        options, "hold_seconds", 0, 30, 5, "Hold at 00:00")

    return {
        "minutes": minutes,
        "seconds": seconds,
        "style": style,
        "accent": _accent_field(options),
        "warn_last10": warn_last10,
        "hold_seconds": hold_seconds,
    }


def _clip_length_field(options):
    """duration_seconds for clock mode. The spec's exact wording ends in a
    bare "seconds" that the generic _int_field template has no room for
    (every other _int_field caller spells its own unit into the label
    instead), so this mirrors _int_field's type-safety checks with that
    literal message rather than bending the shared helper for one caller.
    """
    value = options.get("duration_seconds", 30)
    msg = "Clip length must be a whole number between 5 and 1800 seconds."
    if isinstance(value, bool):
        raise ValidationError(msg + " (Got true/false.)")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError(msg + " (Fractions are not allowed.)")
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(
            msg + " (Got {0!r} — send a number, not text.)".format(value))
    if value < 5 or value > 1800:
        raise ValidationError(msg)
    return value


def _validate_clock_options(options):
    """mode: "clock" — a live wall clock, not a countdown (see
    docs/specs/clock-mode.md). Countdown-only keys (minutes, seconds,
    warn_last10, hold_seconds) are simply never read here, so they're
    silently ignored if a caller sends them alongside a clock payload.
    """
    start = options.get("start", "19:59:50")
    stripped = start.strip() if isinstance(start, str) else start
    if not isinstance(start, str) or not CLOCK_START_RE.fullmatch(stripped):
        raise ValidationError(
            "Start time must look like 19:59:50 (24-hour, hours 0-23).")
    start = stripped

    duration = _clip_length_field(options)

    fmt = options.get("format", "12h")
    if not isinstance(fmt, str) or fmt not in CLOCK_FORMATS:
        raise ValidationError('Format must be either "12h" or "24h".')

    show_seconds = options.get("show_seconds", True)
    if not isinstance(show_seconds, bool):
        raise ValidationError('"Show seconds" must be true or false.')
    show_millis = options.get("show_millis", False)
    if not isinstance(show_millis, bool):
        raise ValidationError('"Show milliseconds" must be true or false.')
    if show_millis:
        show_seconds = True   # can't have millis on screen without seconds

    style = options.get("style", "classic")
    if style == "bar":
        raise ValidationError(
            "Bar style isn't available for the clock — choose classic or "
            "ring.")
    if not isinstance(style, str) or style not in CLOCK_STYLES:
        raise ValidationError("Style must be classic or ring.")

    return {
        "mode": "clock",
        "start": start,
        "duration_seconds": duration,
        "format": fmt,
        "show_seconds": show_seconds,
        "show_millis": show_millis,
        "style": style,
        "accent": _accent_field(options),
    }


def validate_spinner_options(options):
    raw_entries = options.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError(
            "Entries must be a list of names (one wheel segment each).")

    entries = []
    for item in raw_entries:
        if not isinstance(item, str):
            raise ValidationError(
                "Every wheel entry must be text — got {0!r}.".format(item))
        text = item.strip()
        if not text:
            continue  # silently drop blank lines
        if len(text) > 40:
            raise ValidationError(
                'Each entry must be 40 characters or fewer — "{0}…" is too '
                "long.".format(text[:20]))
        entries.append(text)

    if len(entries) < 2:
        raise ValidationError(
            "The wheel needs at least 2 non-empty entries.")
    if len(entries) > 100:
        raise ValidationError(
            "The wheel supports at most 100 entries — you have {0}.".format(
                len(entries)))

    mode = options.get("mode", "random")
    if not isinstance(mode, str) or mode not in SPINNER_MODES:
        raise ValidationError('Mode must be either "random" or "rigged".')

    clean = {
        "entries": entries,
        "mode": mode,
        "accent": _accent_field(options),
        # Editable timeline: still -> spin -> winner card (whole seconds).
        "wait_seconds": _int_field(
            options, "wait_seconds", 0, 60, 0, "Wait before the spin"),
        "spin_seconds": _int_field(
            options, "spin_seconds", 2, 30, 7, "Spin length"),
        "winner_seconds": _int_field(
            options, "winner_seconds", 1, 30, 4, "Show-winner length"),
    }

    if mode == "rigged":
        winner = options.get("winner")
        if not isinstance(winner, str) or not winner.strip():
            raise ValidationError(
                "Rigged mode needs a winner — pick one of the entries.")
        winner = winner.strip()
        if winner not in entries:
            raise ValidationError(
                'The winner "{0}" must exactly match one of the '
                "entries.".format(winner))
        clean["winner"] = winner

    return clean


def _str_field(options, key, lo, hi, required, label):
    """Fetch a string option, rejecting non-strings, and enforce a length
    range on the stripped value. `lo`/`hi` are character bounds; when
    `required` is False an empty (or absent) value returns "" without error.
    """
    value = options.get(key, "")
    if not isinstance(value, str):
        raise ValidationError(
            "{0} must be text — got {1!r}.".format(label, value))
    value = value.strip()
    if not value:
        if required:
            raise ValidationError("{0} is required.".format(label))
        return ""
    if len(value) < lo or len(value) > hi:
        raise ValidationError(
            "{0} must be between {1} and {2} characters.".format(
                label, lo, hi))
    return value


UPLOAD_NAME_RE = re.compile(r"[A-Za-z0-9._-]+\.(png|jpg|jpeg|webp)")


def _background_field(options):
    """Validate an optional uploaded-background filename. Empty/absent -> "".
    Must be a safe name that resolves to a real file inside UPLOADS_DIR."""
    value = options.get("background", "")
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not UPLOAD_NAME_RE.fullmatch(value):
        raise ValidationError("That background image name is not valid.")
    root = os.path.realpath(UPLOADS_DIR)
    path = os.path.realpath(os.path.join(root, value))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise ValidationError(
            "That background image is no longer available — re-upload it.")
    return value


def validate_qr_options(options):
    url = _str_field(options, "url", 1, 1000, True, "The URL or text")
    heading = _str_field(options, "heading", 0, 30, False, "Heading")
    caption = _str_field(options, "caption", 0, 60, False, "Caption")
    duration = _int_field(
        options, "duration_seconds", 5, 60, 15, "Duration (seconds)")

    position = options.get("position", "center")
    if not isinstance(position, str) or position not in POSITIONS:
        raise ValidationError("That QR position is not valid.")

    return {
        "url": url,
        "heading": heading,
        "caption": caption,
        "accent": _accent_field(options),
        "duration_seconds": duration,
        "position": position,
        "background": _background_field(options),
    }


def validate_motion_bg_options(options):
    style = options.get("style", "aurora")
    if not isinstance(style, str) or style not in MOTIONBG_STYLES:
        raise ValidationError(
            "Style must be one of: aurora, bokeh, or waves.")
    duration = _int_field(
        options, "duration_seconds", 5, 30, 12, "Duration (seconds)")

    return {
        "style": style,
        "accent": _accent_field(options),
        "duration_seconds": duration,
    }


VALIDATORS = {
    "timer": validate_timer_options,
    "spinner": validate_spinner_options,
    "qr": validate_qr_options,
    "motionbg": validate_motion_bg_options,
}


# ---------------------------------------------------------------------------
# scoreboard validation
# ---------------------------------------------------------------------------

# Board ids are minted by render.scoreboard as 12 lowercase hex-ish chars; the
# id is used as a FOLDER NAME, so it is checked against this before anything
# touches the filesystem (no dots, no separators, no surprises).
BOARD_ID_RE = re.compile(r"[a-z0-9]{12}")

# ASCII digits ONLY, deliberately not str.isdigit(): that returns True for
# Arabic-Indic digits ("٣٥٠"), superscripts ("³") and other Unicode numerals,
# none of which we can harvest a glyph for.
BOARD_VALUE_RE = re.compile(r"[0-9]{1,6}")

DEFAULT_BOARD_NAME = "Points board"
BOARD_NAME_MAX = 60

# MAX_CONTENT_LENGTH bounds the upload BYTES, not the decoded image: a 12 MB
# JPEG can be 150 megapixels. The board pipeline is pure-Python per-pixel and
# every preview is synchronous, so an unbounded board would pin a worker
# thread for the best part of a minute per keystroke. Anything past the edge
# limit is scaled down; anything past the pixel limit is refused before it is
# decoded at all. A points board is a slide, not a poster.
BOARD_MAX_EDGE = 4000
BOARD_MAX_PIXELS = 60 * 1000 * 1000


def _board_id_field(board_id):
    """Validate a board id straight off the URL. Raises ValidationError."""
    if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
        raise ValidationError(
            "That is not a board we know about — pick one from the list.")
    return board_id


def _board_name_field(raw):
    """Optional board name -> a clean name (never empty)."""
    if raw is None:
        return DEFAULT_BOARD_NAME
    if not isinstance(raw, str):
        raise ValidationError("The board name must be text.")
    name = raw.strip()
    if not name:
        return DEFAULT_BOARD_NAME
    if len(name) > BOARD_NAME_MAX:
        raise ValidationError(
            "The board name must be {0} characters or fewer.".format(
                BOARD_NAME_MAX))
    return name


def _values_field(board, raw):
    """Validate {box_id: "400"} against the boxes this board actually has.

    Every key must name a box on THIS board and every value must be 1-6
    ASCII digits. An empty object is allowed and simply changes nothing.
    """
    if not isinstance(raw, dict):
        raise ValidationError(
            'Values must be an object like {"b0": "400"}.')

    known = set()
    for box in (board.get("boxes") or []):
        if isinstance(box, dict) and isinstance(box.get("id"), str):
            known.add(box["id"])

    # The client posts the WHOLE map back on every save, seeded from what the
    # server itself sent. So an out-of-contract value the user never typed —
    # written by an older build, or left by a change to this regex — must not
    # be able to fail the request: it is simply not carried forward, and the
    # keys the user did change still save.
    stored = board.get("values") or {}

    clean = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValidationError(
                "Each number is identified by text — got {0!r}.".format(key))
        if key not in known:
            raise ValidationError(
                'This board has no number called "{0}" — reopen the board '
                "and try again.".format(key[:40]))
        if not isinstance(value, str):
            raise ValidationError(
                'The value for "{0}" must be text, e.g. "400".'.format(
                    key[:40]))
        if not BOARD_VALUE_RE.fullmatch(value):
            if value == stored.get(key):
                continue
            raise ValidationError(
                'Each number must be 1 to 6 digits (0-9) — "{0}" is not.'
                .format(value[:20]))
        clean[key] = value
    return clean


def _open_board(board_id):
    """Validate the id and load the board.

    Raises ValidationError for a malformed id; BoardError when the board
    cannot be opened (callers turn that into a 404).
    """
    return load_board(_board_id_field(board_id))


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def api_health():
    # The UI's first request means the app has fully launched — clear the
    # boot marker so the next start doesn't report this one as failed.
    stats.boot_ready()
    # platform lets the UI label its file button correctly
    # ("Reveal in Finder" vs "Show in Explorer").
    return jsonify({"ok": True, "platform": sys.platform})


@app.route("/api/render", methods=["POST"])
def api_render():
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": (
            "The request body must be JSON, e.g. "
            '{"type": "timer", "options": {...}}.')}), 400

    visual_type = data.get("type")
    if not isinstance(visual_type, str) or visual_type not in VALIDATORS:
        return jsonify({"error": (
            'Unknown visual type — expected "timer", "spinner", "qr", '
            'or "motionbg".')}), 400

    options = data.get("options", {})
    if not isinstance(options, dict):
        return jsonify({"error": '"options" must be a JSON object.'}), 400

    try:
        clean_options = VALIDATORS[visual_type](options)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = jobs.submit(visual_type, clean_options)
    return jsonify({"job_id": job_id}), 202


@app.route("/api/qr-preview", methods=["POST"])
def api_qr_preview():
    """Render one still frame of the QR card as a PNG so the UI can show the
    REAL, scannable code (not an approximation) and update it live."""
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    options = request.get_json(silent=True)
    if not isinstance(options, dict):
        return jsonify({"error": "Body must be a JSON options object."}), 400
    try:
        clean = validate_qr_options(options)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    img = render_qr_still(clean, max_width=900)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/qr-image", methods=["POST"])
def api_qr_image():
    """Export the QR card as a still PNG instead of a clip. Fast enough
    (~0.2s) to do inline, so it skips the render queue and returns the
    finished filename straight away."""
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    options = request.get_json(silent=True)
    if not isinstance(options, dict):
        return jsonify({"error": "Body must be a JSON options object."}), 400
    try:
        clean = validate_qr_options(options)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    filename = render_qr_image(clean)
    stats.track("export", tool="qr_png")
    return jsonify({"filename": filename})


@app.route("/api/upload-bg", methods=["POST"])
def api_upload_bg():
    """Accept a background image, re-encode it through Pillow (which strips
    anything that isn't a real image), and store it in UPLOADS_DIR. Returns
    the stored filename to pass back as the qr `background` option."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "No image was uploaded."}), 400
    try:
        img = Image.open(file.stream)
        img.load()
        img = img.convert("RGB")
    except Exception:
        return jsonify({"error": (
            "That file is not an image we can read (use PNG or JPG).")}), 400

    os.makedirs(UPLOADS_DIR, exist_ok=True)
    name = "bg_{0}.png".format(uuid.uuid4().hex[:16])
    img.save(os.path.join(UPLOADS_DIR, name), format="PNG")
    return jsonify({"filename": name})


# ---------------------------------------------------------------------------
# scoreboard (points board) routes
#
# All synchronous — like /api/qr-image, an OCR pass or a board re-render takes
# well under a second, so none of this goes near the render queue.
# ---------------------------------------------------------------------------

@app.route("/api/board/analyse", methods=["POST"])
def api_board_analyse():
    """Take the user's existing points image ONCE, OCR it, and save it as a
    reusable board. The upload is re-encoded through Pillow first (same as
    /api/upload-bg), which rejects anything that isn't a real image."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "No image was uploaded."}), 400
    try:
        name = _board_name_field(request.form.get("name"))
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        img = Image.open(file.stream)
        # Header only so far — check the size BEFORE decoding it.
        if img.width * img.height > BOARD_MAX_PIXELS:
            return jsonify({"error": (
                "That image is {0}x{1}, which is too large to work with. "
                "Export the board at around {2}px across and try again."
            ).format(img.width, img.height, BOARD_MAX_EDGE)}), 400
        if max(img.size) > BOARD_MAX_EDGE:
            # Lets the JPEG decoder shrink as it decodes, so an oversized
            # export never becomes a full-resolution buffer.
            img.draft("RGB", (BOARD_MAX_EDGE, BOARD_MAX_EDGE))
        img.load()
        img = img.convert("RGB")
        if max(img.size) > BOARD_MAX_EDGE:
            img.thumbnail((BOARD_MAX_EDGE, BOARD_MAX_EDGE), Image.LANCZOS)
    except Exception:
        return jsonify({"error": (
            "That file is not an image we can read (use PNG or JPG).")}), 400

    try:
        board = create_board(img, name)
    except OCRUnavailable as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        stats.track("ocr_failed")
        return jsonify({"error": str(exc)}), 400
    except Exception:
        # Never let an unexpected error answer this route with HTML — the
        # client only knows how to read {"error": ...}.
        app.logger.exception("board analyse failed")
        return jsonify({"error": (
            "That board couldn't be saved. Check there is free disk space "
            "and try again.")}), 500

    board_id = board.get("id") or board.get("board_id")
    found = len(board.get("boxes") or [])
    # Zero found is not a failure any more — the board is kept and the
    # operator draws the numbers in by hand — but it is worth counting.
    stats.track("ocr_failed" if not found else "board_created",
                **({} if not found else {"numbers": found}))
    return jsonify({
        "board_id": board_id,
        "name": board.get("name", name),
        "width": board.get("width"),
        "height": board.get("height"),
        "boxes": board.get("boxes"),
        "values": board.get("values") or {},
    })


@app.route("/api/board/list")
def api_board_list():
    return jsonify({"boards": list_boards()})


@app.route("/api/board/<board_id>", methods=["GET"])
def api_board_get(board_id):
    try:
        board = _open_board(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(board)


@app.route("/api/board/<board_id>/source.png")
def api_board_source(board_id):
    """The untouched original upload, for the UI to draw its box overlay on."""
    try:
        _board_id_field(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    root = os.path.realpath(BOARDS_DIR)
    path = os.path.realpath(os.path.join(root, board_id, "source.png"))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "That board no longer exists."}), 404
    return send_file(path, mimetype="image/png")


@app.route("/api/board/<board_id>/values", methods=["POST"])
def api_board_values(board_id):
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": (
            'The request body must be JSON like {"values": {"b0": "400"}}.'
        )}), 400

    try:
        board = _open_board(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404

    try:
        values = _values_field(board, data.get("values"))
        # The name rides along with the numbers rather than needing its own
        # round trip — the UI saves both on the same debounce.
        new_name = (_board_name_field(data["name"])
                    if data.get("name") is not None else None)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        # ONE locked read-modify-write for both, not two: each extra write
        # widens the window in which a second tab's save can be lost.
        updated = save_values(board_id, values, name=new_name)
        # The UI redraws from whatever comes back, so always answer with a
        # board even if save_values only persists and returns nothing.
        if not isinstance(updated, dict):
            updated = load_board(board_id)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(updated)


@app.route("/api/board/<board_id>/boxes", methods=["POST"])
def api_board_add_box(board_id):
    """Make a number OCR missed editable: {"rect": {x,y,w,h}, "text": "350"}.

    Rect is in source-image pixels (the UI converts from its scaled overlay).
    """
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": (
            'The request body must be JSON like {"rect": {...}, "text": "350"}.'
        )}), 400
    rect = data.get("rect")
    if not isinstance(rect, dict):
        return jsonify({"error": "Send the box as {x, y, w, h}."}), 400
    try:
        _board_id_field(board_id)
        clean = {k: _int_field(rect, k, 0, 20000, None, "The box " + k)
                 for k in ("x", "y", "w", "h")}
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "Type the number as it appears on the board."}), 400
    try:
        board = add_box(board_id, clean, text.strip())
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("add box failed")
        return jsonify({"error": (
            "That number couldn't be added. Try drawing the box again.")}), 500
    stats.track("board_number_added")
    return jsonify(board)


@app.route("/api/board/<board_id>/preview", methods=["POST"])
def api_board_preview(board_id):
    """Re-render the board with its saved values and hand back the PNG, so the
    UI shows the REAL composite (harvested glyphs and all), not a mock-up."""
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    # Opened first so that "this board is gone" is a 404 like GET and DELETE,
    # and 400 stays for a request we could not honour (a bad stored value).
    try:
        board = _open_board(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404
    try:
        img = render_board(board)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/board/<board_id>/export", methods=["POST"])
def api_board_export(board_id):
    """Write the finished board into exports/ and return the filename, so the
    UI can offer the usual download / reveal-in-Finder pair."""
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    try:
        board = _open_board(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404
    try:
        filename = export_board(board)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    stats.track("export", tool="scoreboard")
    return jsonify({"filename": filename})


@app.route("/api/board/<board_id>", methods=["DELETE"])
def api_board_delete(board_id):
    try:
        _board_id_field(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        delete_board(board_id)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True})


@app.route("/api/ai/status")
def api_ai_status():
    return jsonify({
        "configured": aiassist.has_key(),
        "model": aiassist.get_model(),
        "models": aiassist.PRESET_MODELS,
    })


@app.route("/api/ai/settings", methods=["POST"])
def api_ai_settings():
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be JSON."}), 400
    if "key" not in data and "model" not in data:
        return jsonify({"error": "Nothing to save."}), 400
    try:
        aiassist.save_settings(key=data.get("key"), model=data.get("model"))
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "configured": aiassist.has_key(),
                    "model": aiassist.get_model()})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """Validate the saved key + connectivity without spending a generation."""
    try:
        message = aiassist.test_key()
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "message": message})


@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return jsonify({"error": "Request too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be JSON."}), 400

    description = data.get("description", "")
    if not isinstance(description, str) or not description.strip():
        return jsonify({"error": "Describe what entries you need."}), 400
    if len(description) > 200:
        return jsonify({"error": "Keep the description under 200 characters."}), 400

    full = bool(data.get("full"))
    try:
        count = int(data.get("count", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "How many? must be a whole number."}), 400
    if not full and (count < 1 or count > 100):
        return jsonify({"error": "Choose between 1 and 100 entries."}), 400

    existing = data.get("existing", [])
    if not isinstance(existing, list):
        existing = []

    model = data.get("model")
    if model is not None and not isinstance(model, str):
        model = None

    try:
        entries = aiassist.generate_entries(
            description, count, existing, model, full)
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    stats.track("ai_fill")
    return jsonify({"entries": entries})


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    info = jobs.get(job_id)
    if info is None:
        return jsonify({"error": "No such render job."}), 404
    return jsonify(info)


@app.route("/exports/<filename>")
def download_export(filename):
    return send_from_directory(EXPORTS_DIR, filename, as_attachment=True)


# One GitHub query per app run; failures (offline, rate limit) stay silent —
# an update nag must never get in the way of a Sunday morning.
_update = {"checked": False, "available": False, "latest": None, "url": None,
           "error": None}


def _version_tuple(tag):
    return tuple(int(p) for p in tag.strip().lstrip("v").split(".")[:3])


@app.route("/api/update-check")
def api_update_check():
    # ?force=1 re-queries GitHub (the manual "Check for updates" button); the
    # boot-time check only queries once.
    if request.args.get("force"):
        _update["checked"] = False
    if not _update["checked"]:
        _update["checked"] = True
        _update["error"] = None
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "service-visuals"})
            with netutil.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            tag = data.get("tag_name") or ""
            if _version_tuple(tag) > _version_tuple(APP_VERSION):
                _update.update(available=True, latest=tag,
                               url=data.get("html_url"),
                               assets=data.get("assets") or [])
        except Exception as exc:
            # A failed check must NOT look like "you're up to date" — record it
            # so the UI can say the check itself didn't work.
            _update["error"] = exc.__class__.__name__
    return jsonify({"current": "v" + APP_VERSION,
                    "latest": _update["latest"],
                    "update_available": _update["available"],
                    "check_failed": bool(_update.get("error")),
                    "can_self_install": bool(getattr(sys, "frozen", False)),
                    "last_install": _last_install_result()})


# The outcome of the previous self-update, read once at boot. A Windows user
# sat on v1.14 while the app said "RESTARTING…" and came back unchanged; this
# is how the app now admits that, and how it can offer the log.
_last_install = {"read": False, "result": None}


def _last_install_result():
    if not _last_install["read"]:
        _last_install["read"] = True
        result = updater.take_result(APP_VERSION)
        if result is not None:
            if result.get("ok"):
                stats.track("update_installed")
            else:
                stats.track("update_failed",
                            reason=result.get("reason") or "unknown")
        _last_install["result"] = result
    result, _last_install["result"] = _last_install["result"], None
    return result


@app.route("/api/update-log")
def api_update_log():
    return jsonify({"log": updater.read_log()})


@app.route("/api/stats", methods=["GET", "POST"])
def api_stats():
    """Anonymous usage counts on/off. See stats.py for what is (not) sent."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify({"error": 'Send {"enabled": true|false}.'}), 400
        stats.set_enabled(data["enabled"])
    return jsonify({"enabled": stats.enabled(), "events": list(stats.EVENTS)})


@app.route("/api/open-release", methods=["POST"])
def api_open_release():
    """Open the latest release page in the default browser (works from the
    packaged pywebview window too, where target=_blank links go nowhere)."""
    if not _update["url"]:
        return jsonify({"error": "No newer release known."}), 404
    webbrowser.open(_update["url"])
    return jsonify({"ok": True})


# In-place self-update (packaged app only). States: idle -> downloading ->
# staging -> restarting | error. The process exits itself at "restarting";
# updater's detached helper swaps the install and relaunches it.
_install_state = {"state": "idle", "pct": 0, "error": None}


def _do_install(url):
    try:
        workdir = tempfile.mkdtemp(prefix="service-visuals-update-")
        zip_path = os.path.join(workdir, "update.zip")
        updater.download(url, zip_path,
                         lambda p: _install_state.update(pct=p))
        _install_state.update(state="staging")
        staged = updater.stage(zip_path, workdir)
        # Written BEFORE the helper runs: if the swap fails after we exit,
        # the next launch finds this, sees it is still the old version, and
        # tells the user instead of silently pretending nothing happened.
        updater.mark_pending(APP_VERSION, _update.get("latest") or "")
        updater.spawn_replacer(staged, updater.install_root(), workdir)
        _install_state.update(state="restarting")
        threading.Timer(1.5, os._exit, args=(0,)).start()
    except Exception as exc:
        _install_state.update(state="error",
                              error=str(exc) or exc.__class__.__name__)


@app.route("/api/update-install", methods=["POST"])
def api_update_install():
    problem = updater.install_problem()
    if problem:
        # e.g. macOS App Translocation — replacing the running copy would be a
        # no-op, so say so rather than "restarting" into the same old version.
        return jsonify({"error": problem}), 400
    if not _update["available"]:
        return jsonify({"error": "No update available."}), 404
    asset = updater.platform_asset(_update.get("assets"))
    if asset is None:
        return jsonify({"error": (
            "The latest release has no download for this platform yet.")}), 404
    if jobs.busy():
        return jsonify({"error": (
            "An export is still rendering — try again when it finishes.")}), 409
    if _install_state["state"] == "idle" or _install_state["state"] == "error":
        _install_state.update(state="downloading", pct=0, error=None)
        threading.Thread(target=_do_install,
                         args=(asset["browser_download_url"],),
                         daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update-status")
def api_update_status():
    return jsonify(_install_state)


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": (
            'The request body must be JSON like {"filename": "..."}.')}), 400

    filename = data.get("filename")
    if not isinstance(filename, str) or not EXPORT_FILENAME_RE.fullmatch(filename):
        return jsonify({"error": (
            "That does not look like the name of an exported file.")}), 400

    exports_root = os.path.realpath(EXPORTS_DIR)
    path = os.path.realpath(os.path.join(exports_root, filename))
    if not path.startswith(exports_root + os.sep):
        return jsonify({"error": (
            "That file is not inside the exports folder.")}), 400
    if not os.path.isfile(path):
        return jsonify({"error": (
            "That file no longer exists in the exports folder.")}), 404

    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform == "win32":
        # Explorer needs the literal form  explorer /select,"C:\path"  — as a
        # COMMAND STRING. With an argument list, list2cmdline wraps the whole
        # '/select,C:\...' token in quotes (the path contains spaces), which
        # Explorer can't parse, so it fell back to opening the default
        # Documents folder instead of selecting the exported file.
        subprocess.run('explorer /select,"{0}"'.format(path), check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
    return jsonify({"ok": True})


def prepare_exports_dir():
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    updater.sweep_backups()
    stats.start(APP_VERSION)
    stats.report_previous_boot()     # also arms the marker for this boot
    # Sweep leftovers from renders that a killed server never finished.
    for leftover in os.listdir(EXPORTS_DIR):
        if leftover.endswith(".part"):
            os.unlink(os.path.join(EXPORTS_DIR, leftover))


if __name__ == "__main__":
    prepare_exports_dir()
    port = int(os.environ.get("PORT", "8765"))
    banner = "\n".join([
        "",
        "  =========================================",
        "   Service Visuals — render server running",
        "   Open:  http://localhost:{0}".format(port),
        "   MP4s:  {0}".format(EXPORTS_DIR),
        "   Stop:  Ctrl+C",
        "  =========================================",
        "",
    ])
    print(banner)
    app.run(host="127.0.0.1", port=port, debug=False)
