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
import time

from version import APP_VERSION  # noqa: E402

import io
import uuid

from flask import (Flask, jsonify, request, send_file, send_from_directory)
from flask.signals import got_request_exception
from PIL import Image

import stats
import updater
import whatsnew
import tools
from downloader import ERROR_REASONS, download_video
from jobs import JobManager
from render.encoder import EXPORTS_DIR, UPLOADS_DIR
from render.timer import render_timer
from render.spinner import render_spinner
from render.qr import render_qr, render_qr_image, render_qr_still
from render.motionbg import render_motion_bg
from validation import (DOWNLOAD_FORMATS, MOTIONBG_STYLES, QR_STYLES,
                        SPINNER_MODES, TIMER_STYLES, VALIDATORS,
                        ValidationError, _one_of, validate_qr_options)
from webutil import MAX_UPLOAD_BYTES, json_body

# When frozen by PyInstaller the static files live under the unpack dir.
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, static_folder=os.path.join(_BASE_DIR, "static"),
            static_url_path="/static")

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Localhost-only trust model: reject foreign Host headers so a DNS-rebound
# page (attacker.com -> 127.0.0.1) cannot drive the unauthenticated API.
ALLOWED_HOSTNAMES = ("localhost", "127.0.0.1")


@app.before_request
def _reject_foreign_hosts():
    hostname = (request.host or "").rsplit(":", 1)[0]
    if hostname not in ALLOWED_HOSTNAMES:
        return jsonify({"error": "Host not allowed."}), 403


# Blueprints split out of this file (docs/specs/refactor-organise.md) — the
# host guard above is registered on `app`, not any one blueprint, so Flask
# runs it before every request regardless of which blueprint answers it
# (confirmed with a test_client request to a blueprint route, see smoke.py).
from routes.ai import bp as _ai_bp  # noqa: E402
from routes.backgrounds import bp as _backgrounds_bp  # noqa: E402
from routes.board import bp as _board_bp  # noqa: E402
from routes.download import bp as _download_bp  # noqa: E402
from routes.update import bp as _update_bp  # noqa: E402

app.register_blueprint(_ai_bp)
app.register_blueprint(_backgrounds_bp)
app.register_blueprint(_board_bp)
app.register_blueprint(_download_bp)
app.register_blueprint(_update_bp)


# How long a render took, as a bucket as well as a number. The raw figure is
# what you chart; the bucket is what you can read at a glance in the Aptabase
# dashboard, which lists a prop's distinct values — thousands of distinct
# millisecond readings would be unreadable on their own.
TOOK_BUCKETS = ((5000, "<5s"), (15000, "5-15s"), (60000, "15-60s"),
                (300000, "1-5m"))


def _took_bucket(ms):
    for limit, label in TOOK_BUCKETS:
        if ms < limit:
            return label
    return ">5m"


def track_export(tool, started, **props):
    """Record a finished export: what it was, and how long it took.

    Duration is wall time around the renderer only — the queue wait is not
    in it, so this is the machine's render speed rather than how busy the
    app was. Rounded to a tenth of a second: nobody needs millisecond
    precision and it keeps the number of distinct values down.
    """
    ms = int(round((time.time() - started) * 1000))
    stats.track("export", tool=tool, render_ms=int(round(ms, -2)),
                took=_took_bucket(ms), **props)


def _counted(tool, fn, extra_props=None):
    """Count an export once it has actually produced a file."""
    def run(options, progress_cb):
        started = time.time()
        filename = fn(options, progress_cb)
        props = extra_props(options) if extra_props else {}
        track_export(tool, started, **props)
        return filename
    return run


def _timer_props(options):
    if options.get("green_screen"):
        bg = "green"
    else:
        n = len(options.get("backgrounds") or [])
        bg = "none" if n == 0 else ("one" if n == 1 else "many")
    return {"mode": _one_of(options.get("mode"),
                            ("countdown", "clock"), "countdown"),
            "style": _one_of(options.get("style"), TIMER_STYLES, "classic"),
            "bg": bg}


def _spinner_props(options):
    return {"mode": _one_of(options.get("mode"), SPINNER_MODES, "random")}


def _motionbg_props(options):
    return {"style": _one_of(options.get("style"),
                             MOTIONBG_STYLES, "aurora")}


def _qr_props(options):
    return {"style": _one_of(options.get("style"), QR_STYLES, "card")}


def _ytdlp_version_prop():
    """Which yt-dlp release did this, e.g. "2026.08.19".

    A public release date, not user content — and the one number that
    explains a wave of failures, because the binary self-updates
    independently of Service Visuals. Read from the cached file; never
    spawn the binary here (its onefile unpack takes seconds).
    """
    return str(tools.tools_status().get("ytdlp_version") or "none")[:20]


def _download_props(options):
    return {"format": _one_of(options.get("format"), DOWNLOAD_FORMATS,
                              "mp4"),
            "ytdlp": _ytdlp_version_prop()}


def _on_job_error(tool, exc):
    """A render or download raised. Every tool reports the exception's
    shape as usual; a download also reports WHY in one of our own words
    (downloader.ERROR_REASONS) — never yt-dlp's stderr, which names the
    video. The full text stays in ~/.service-visuals/download.log.
    """
    if tool == "download":
        stats.track("download_failed",
                    reason=_one_of(getattr(exc, "reason", None),
                                   ERROR_REASONS, "unknown"),
                    ytdlp=_ytdlp_version_prop())
    stats.report_error("render_failed", exc, tool=tool)


jobs = JobManager({"timer": _counted("timer", render_timer, _timer_props),
                   "spinner": _counted("spinner", render_spinner,
                                       _spinner_props),
                   "qr": _counted("qr", render_qr, _qr_props),
                   "motionbg": _counted("motionbg", render_motion_bg,
                                        _motionbg_props),
                   "download": _counted("download", download_video,
                                        _download_props)},
                  on_error=_on_job_error)


@got_request_exception.connect_via(app)
def _report_unhandled(sender, exception, **_extra):
    """A route that blew up (a 500) is a crash from the operator's seat."""
    # Endpoint name only — never the raw URL, which could carry a board id.
    stats.report_error("request_failed", exception,
                       route=str(request.endpoint or "unmatched")[:60])

# NB: matched with .fullmatch() — "$" alone would accept a trailing newline.
# mp3 added for the YouTube-download tile's audio export (youtube-
# download.md) — the done panel and /api/reveal both need to recognise it.
# A downloaded video is named after its real title now — spaces, accents,
# brackets, and dots inside the name ("Fred again.. - ...mp4") — so the old
# [A-Za-z0-9._-] whitelist would refuse to reveal the app's own files. What
# actually has to hold is that the name cannot escape the exports folder:
# no path separators, no control characters, and one of our extensions.
# Every call site still resolves the realpath and checks containment, which
# is the real boundary; this is the cheap first gate in front of it.
EXPORT_FILENAME_RE = re.compile(r"[^/\\\x00-\x1f]{1,200}\.(mp4|png|mp3)")


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


@app.route("/api/whats-new")
def api_whats_new():
    """Read-only (docs/specs/whats-new.md) — a GET must stay safe to
    repeat, so nothing here writes last-seen.json; that only happens on
    the dismiss POST below and at boot (prepare_exports_dir)."""
    show = whatsnew.should_show(APP_VERSION)
    items = whatsnew.notes_for(APP_VERSION) if show else []
    if not items:
        # NOTES having gone empty must never announce an empty card.
        show = False
    if show:
        # Fired here, not on the dismiss POST: the UI fetches this once per
        # boot, so this is one event per actual appearance of the card —
        # including the case where the volunteer quits without clicking
        # GOT IT, which the dismiss POST would never see at all.
        stats.track("whats_new_shown")
    return jsonify({"show": show, "version": APP_VERSION,
                    "intro": whatsnew.INTRO, "items": items})


@app.route("/api/whats-new/seen", methods=["POST"])
def api_whats_new_seen():
    """Dismiss: GOT IT / Esc / backdrop click all land here. Idempotent —
    calling it twice just writes the same version twice."""
    whatsnew.mark_seen(APP_VERSION)
    return jsonify({"ok": True})


@app.route("/api/render", methods=["POST"])
def api_render():
    data, error = json_body(
        'The request body must be JSON, e.g. '
        '{"type": "timer", "options": {...}}.')
    if error:
        return error

    visual_type = data.get("type")
    if not isinstance(visual_type, str) or visual_type not in VALIDATORS:
        return jsonify({"error": (
            'Unknown visual type — expected "timer", "spinner", "qr", '
            '"motionbg", or "download".')}), 400

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
    options, error = json_body("Body must be a JSON options object.")
    if error:
        return error
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
    options, error = json_body("Body must be a JSON options object.")
    if error:
        return error
    try:
        clean = validate_qr_options(options)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    started = time.time()
    filename = render_qr_image(clean)
    track_export("qr_png", started)
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


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    info = jobs.get(job_id)
    if info is None:
        return jsonify({"error": "No such render job."}), 404
    return jsonify(info)


@app.route("/exports/<filename>")
def download_export(filename):
    return send_from_directory(EXPORTS_DIR, filename, as_attachment=True)


@app.route("/api/stats", methods=["GET", "POST"])
def api_stats():
    """Anonymous usage counts on/off. See stats.py for what is (not) sent."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify({"error": 'Send {"enabled": true|false}.'}), 400
        stats.set_enabled(data["enabled"])
    return jsonify({"enabled": stats.enabled(), "events": list(stats.EVENTS)})


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
    # Seeded FIRST, before anything else touches CONFIG_DIR: in particular
    # stats.report_previous_boot() below drops its own boot-pending.json
    # marker into that same directory on every launch, analytics on or
    # off. Seeding after that would find the "config dir" already
    # non-empty on every fresh install and never write last-seen.json at
    # all — so a genuinely fresh install would silently stay unseeded and
    # only be saved from showing the card by luck of request timing
    # (docs/specs/whats-new.md's row 4 must not depend on that).
    whatsnew.seed_if_fresh_install(APP_VERSION)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    updater.sweep_backups()
    stats.start(APP_VERSION)
    stats.report_previous_boot()     # also arms the marker for this boot
    # Keep the YouTube downloader current without waiting for a Service
    # Visuals release: a background thread, a no-op unless it has been a
    # day, and skipped entirely if the tools were never fetched.
    tools.start_background_update()
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
