"""Request-option validation: turn a JSON options dict into either a clean
dict a renderer can trust, or a ValidationError with a plain-English
message the UI can show as-is.

Split out of app.py (docs/specs/refactor-organise.md) — no Flask import,
so this can be unit-tested (and imported by other validation-only code)
without pulling in a whole Flask app.
"""

import os
import re
from urllib.parse import urlsplit

from render.encoder import UPLOADS_DIR
from render.qr import POSITIONS, QR_STYLES
from render.timer import CLOCK_STYLES
from backgrounds import (
    BACKGROUNDS_DIR, BACKGROUND_ID_RE, BACKGROUNDS_PER_TIMER_MAX)


class ValidationError(Exception):
    """Raised with a plain-English message suitable for the UI."""


# Every prop below is drawn from a fixed set of our own words — a mode, a
# style name, a coarse bucket. None of them can carry what the operator
# typed, uploaded or named (stats.py's privacy rule).

def _one_of(value, allowed, default):
    """Only ever emit a word from our own fixed set.

    These props are read straight off the options dict, which reaches here
    already validated — so today `style` cannot be anything but a style
    name. Clamping anyway makes the privacy promise structural instead of a
    consequence of call order: a future path that reaches a renderer without
    validating still cannot turn an operator's text into an analytics prop.
    """
    return value if value in allowed else default


# NB: matched with .fullmatch() — "$" alone would accept a trailing newline.
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")

TIMER_STYLES = ("classic", "ring", "bar")
MILLIS_MAX_SECONDS = 1800        # countdown ceiling when milliseconds are on
CLOCK_FORMATS = ("12h", "24h")
# HH:MM:SS, 24-hour, zero-padded — the exact shape the "Shows as ..." hint
# and the renderer both expect. fullmatch()'d, so trailing junk is rejected.
CLOCK_START_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d):([0-5]\d)")
SPINNER_MODES = ("random", "rigged")
MOTIONBG_STYLES = ("aurora", "bokeh", "waves")
DEFAULT_ACCENT = "#e8b44f"


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
        options, "hold_seconds", 0, 30, 5, "Keep 0:00 on screen (seconds)")

    # Addendum (v1.23.0): the same millis toggle clock mode uses, now also
    # accepted on a countdown. Same message/shape as the clock branch below.
    show_millis = options.get("show_millis", False)
    if not isinstance(show_millis, bool):
        raise ValidationError('"Show milliseconds" must be true or false.')
    # Millis mean 30 unique frames a second with nothing to cache: a 2-hour
    # countdown would be 216,000 frames (~20 minutes to render). Same ceiling
    # the clock mode has; nobody reads milliseconds on a 45-minute timer.
    if show_millis and total > MILLIS_MAX_SECONDS:
        raise ValidationError(
            "With milliseconds on, the timer can run for at most 30 minutes. "
            "Turn milliseconds off for a longer timer.")

    clean = {
        "minutes": minutes,
        "seconds": seconds,
        "style": style,
        "accent": _accent_field(options),
        "warn_last10": warn_last10,
        "hold_seconds": hold_seconds,
        "show_millis": show_millis,
    }
    clean.update(_timer_background_options(options))
    return clean


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

    clean = {
        "mode": "clock",
        "start": start,
        "duration_seconds": duration,
        "format": fmt,
        "show_seconds": show_seconds,
        "show_millis": show_millis,
        "style": style,
        "accent": _accent_field(options),
    }
    clean.update(_timer_background_options(options))
    return clean


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


def _background_id_field(image_id):
    """Validate an id straight off the URL. Raises ValidationError."""
    if not isinstance(image_id, str) or not BACKGROUND_ID_RE.fullmatch(image_id):
        raise ValidationError("That is not a background image we know about.")
    return image_id



def _backgrounds_field(options):
    """Validate the optional list of background image ids -> a list of
    absolute file paths inside BACKGROUNDS_DIR, so render_timer never has
    to think about ids, storage layout or path safety at all.
    """
    raw = options.get("backgrounds", [])
    if not isinstance(raw, list):
        raise ValidationError(
            "A timer can use up to {0} background images.".format(
                BACKGROUNDS_PER_TIMER_MAX))
    if len(raw) > BACKGROUNDS_PER_TIMER_MAX:
        raise ValidationError(
            "A timer can use up to {0} background images.".format(
                BACKGROUNDS_PER_TIMER_MAX))
    paths = []
    for item in raw:
        if not isinstance(item, str) or not BACKGROUND_ID_RE.fullmatch(item):
            raise ValidationError(
                "One of the background images is missing — remove it "
                "and add it again.")
        path = os.path.join(BACKGROUNDS_DIR, item + ".png")
        if not os.path.isfile(path):
            raise ValidationError(
                "One of the background images is missing — remove it "
                "and add it again.")
        paths.append(path)
    return paths


def _timer_background_options(options):
    """The four Background-group keys, shared verbatim by countdown and
    clock validation (docs/specs/timer-backgrounds.md) — both modes offer
    the same group, so there is exactly one place that can drift.

    green_screen (docs/specs/green-screen.md) lives here too, for the same
    reason: when it's true the normalised "backgrounds" is forced to []
    WITHOUT ever calling _backgrounds_field(), so a stale/deleted image id
    left in a hidden set can't block a green export.
    """
    green_screen = options.get("green_screen", False)
    if not isinstance(green_screen, bool):
        raise ValidationError("Green screen must be true or false.")
    bg_dim = _int_field(options, "bg_dim", 0, 80, 45, "Dim")
    bg_blur = options.get("bg_blur", False)
    if not isinstance(bg_blur, bool):
        raise ValidationError("Blur must be true or false.")
    return {
        "backgrounds": [] if green_screen else _backgrounds_field(options),
        "bg_seconds": _int_field(
            options, "bg_seconds", 2, 120, 10, "Seconds per image"),
        "bg_dim": bg_dim,
        "bg_blur": bg_blur,
        "green_screen": green_screen,
    }


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

    style = options.get("style", "card")
    if not isinstance(style, str) or style not in QR_STYLES:
        raise ValidationError("That QR style is not valid.")

    return {
        "url": url,
        "heading": heading,
        "caption": caption,
        "accent": _accent_field(options),
        "duration_seconds": duration,
        "position": position,
        "style": style,
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


# youtube-download.md: the exact link message covers every way a url can
# be wrong (missing, too long, wrong scheme, not a YouTube host) — a
# volunteer pasting the wrong thing needs "paste a YouTube link", not a
# diagnosis of which check tripped.
DOWNLOAD_URL_ERROR = (
    "Paste a YouTube link, like https://www.youtube.com/watch?v=…")
DOWNLOAD_URL_MAX_LEN = 500
DOWNLOAD_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com",
                  "music.youtube.com", "youtu.be", "www.youtu.be")
DOWNLOAD_FORMATS = ("mp4", "mp3")


def validate_download_options(options):
    url = options.get("url", "")
    if not isinstance(url, str):
        raise ValidationError(DOWNLOAD_URL_ERROR)
    url = url.strip()
    if not url or len(url) > DOWNLOAD_URL_MAX_LEN:
        raise ValidationError(DOWNLOAD_URL_ERROR)

    parsed = urlsplit(url)
    # .hostname is already lower-cased and has the port stripped.
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or host not in DOWNLOAD_HOSTS:
        raise ValidationError(DOWNLOAD_URL_ERROR)

    fmt = options.get("format", "mp4")
    if fmt not in DOWNLOAD_FORMATS:
        raise ValidationError("Format must be mp4 or mp3.")

    return {"url": url, "format": fmt}


VALIDATORS = {
    "timer": validate_timer_options,
    "spinner": validate_spinner_options,
    "qr": validate_qr_options,
    "motionbg": validate_motion_bg_options,
    "download": validate_download_options,
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
