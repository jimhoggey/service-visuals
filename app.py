"""Service Visuals — Flask app: routes, validation, wiring.

Serves the single-page UI, validates render requests, queues them on the
JobManager, and hands finished MP4s back for download / Finder reveal.
Runs on 127.0.0.1 only; port 8765 by default (5000 collides with macOS
AirPlay Receiver).
"""

import os
import re
import subprocess
import sys

from flask import Flask, jsonify, request, send_from_directory

from jobs import JobManager
from render.encoder import EXPORTS_DIR
from render.timer import render_timer
from render.spinner import render_spinner

app = Flask(__name__, static_folder="static", static_url_path="/static")

# The largest legitimate payload (20 entries x 40 chars plus options) is well
# under 2 KB; capping the body also caps json int parsing, which is quadratic
# in digit count on Python 3.9 (a multi-MB number would pin a core for ages).
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# Localhost-only trust model: reject foreign Host headers so a DNS-rebound
# page (attacker.com -> 127.0.0.1) cannot drive the unauthenticated API.
ALLOWED_HOSTNAMES = ("localhost", "127.0.0.1")


@app.before_request
def _reject_foreign_hosts():
    hostname = (request.host or "").rsplit(":", 1)[0]
    if hostname not in ALLOWED_HOSTNAMES:
        return jsonify({"error": "Host not allowed."}), 403


jobs = JobManager({"timer": render_timer, "spinner": render_spinner})

# NB: matched with .fullmatch() — "$" alone would accept a trailing newline.
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
EXPORT_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]+\.mp4")

TIMER_STYLES = ("classic", "ring", "bar")
SPINNER_MODES = ("random", "rigged")
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
    if len(entries) > 20:
        raise ValidationError(
            "The wheel supports at most 20 entries — you have {0}.".format(
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


VALIDATORS = {
    "timer": validate_timer_options,
    "spinner": validate_spinner_options,
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
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": (
            "The request body must be JSON, e.g. "
            '{"type": "timer", "options": {...}}.')}), 400

    visual_type = data.get("type")
    if not isinstance(visual_type, str) or visual_type not in VALIDATORS:
        return jsonify({"error": (
            'Unknown visual type — expected "timer" or "spinner".')}), 400

    options = data.get("options", {})
    if not isinstance(options, dict):
        return jsonify({"error": '"options" must be a JSON object.'}), 400

    try:
        clean_options = VALIDATORS[visual_type](options)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = jobs.submit(visual_type, clean_options)
    return jsonify({"job_id": job_id}), 202


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    info = jobs.get(job_id)
    if info is None:
        return jsonify({"error": "No such render job."}), 404
    return jsonify(info)


@app.route("/exports/<filename>")
def download_export(filename):
    return send_from_directory(EXPORTS_DIR, filename, as_attachment=True)


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


if __name__ == "__main__":
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    # Sweep leftovers from renders that a killed server never finished.
    for leftover in os.listdir(EXPORTS_DIR):
        if leftover.endswith(".part"):
            os.unlink(os.path.join(EXPORTS_DIR, leftover))
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
