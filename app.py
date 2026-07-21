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

APP_VERSION = "1.7.0"
GITHUB_REPO = "jimhoggey/service-visuals"

import io
import uuid

from flask import (Flask, jsonify, request, send_file, send_from_directory)
from PIL import Image

import aiassist
import updater
from jobs import JobManager
from render.encoder import EXPORTS_DIR, UPLOADS_DIR
from render.timer import render_timer
from render.spinner import render_spinner
from render.qr import POSITIONS, render_qr, render_qr_still
from render.motionbg import render_motion_bg

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


jobs = JobManager({"timer": render_timer, "spinner": render_spinner,
                   "qr": render_qr, "motionbg": render_motion_bg})

# NB: matched with .fullmatch() — "$" alone would accept a trailing newline.
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
EXPORT_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]+\.mp4")

TIMER_STYLES = ("classic", "ring", "bar")
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
# routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


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
_update = {"checked": False, "available": False, "latest": None, "url": None}


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
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "service-visuals"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.load(resp)
            tag = data.get("tag_name") or ""
            if _version_tuple(tag) > _version_tuple(APP_VERSION):
                _update.update(available=True, latest=tag,
                               url=data.get("html_url"),
                               assets=data.get("assets") or [])
        except Exception:
            pass
    return jsonify({"current": "v" + APP_VERSION,
                    "latest": _update["latest"],
                    "update_available": _update["available"],
                    "can_self_install": bool(getattr(sys, "frozen", False))})


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
        updater.spawn_replacer(staged, updater.install_root(), workdir)
        _install_state.update(state="restarting")
        threading.Timer(1.5, os._exit, args=(0,)).start()
    except Exception as exc:
        _install_state.update(state="error",
                              error=str(exc) or exc.__class__.__name__)


@app.route("/api/update-install", methods=["POST"])
def api_update_install():
    if not getattr(sys, "frozen", False):
        return jsonify({"error": (
            "Self-update only works in the packaged app. "
            "Running from source? Use git pull.")}), 400
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
            "That does not look like the name of an exported MP4.")}), 400

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
        subprocess.run(["explorer", "/select,", path], check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
    return jsonify({"ok": True})


def prepare_exports_dir():
    os.makedirs(EXPORTS_DIR, exist_ok=True)
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
