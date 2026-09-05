"""The "What's new" card: copy, the notes_for() assembly rule, and the
show/hide decision against ~/.service-visuals/last-seen.json (docs/specs/
whats-new.md).

A module of plain data plus small, pure helpers — no Flask here, so the
whole thing is testable without a server (see scripts/smoke.py). The two
routes and the last-seen.json read/write live in app.py, which calls
should_show()/notes_for() to decide what to answer and mark_seen()/
seed_if_fresh_install() to persist it.
"""

import json
import os

# Same derivation SERVICE_VISUALS_CONFIG gets everywhere else in this app
# (stats.py, aiassist.py, updater.py, render/scoreboard.py) — one env var,
# read once at import, so a test run's throwaway config dir isolates this
# module too.
CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
LAST_SEEN_PATH = os.path.join(CONFIG_DIR, "last-seen.json")


INTRO = "Nice. You're up to date."

# Copy is final — written to be read out loud by a volunteer, not a release
# engineer. Keep every line under ~90 characters where a new entry allows
# it (it has to fit two lines on screen); three of the lines below already
# run longer (92/94/106 chars) because they shipped before that guideline
# was written down — left as-is rather than reworded after the fact.
NOTES = {
    "1.31.0": [
        "Timer: tick \"Fixed 00:00:00 format\" and every countdown reads "
        "00:05:00 or 00:00:30 — same size at any length.",
        "Timer: the settings you rarely touch now sit under an ADVANCED "
        "drop-down, so the form is shorter.",
    ],
    "1.30.0": [
        "QR card: three styles — CARD, LIGHT (bright, full screen) and "
        "DOTS (rounded modules).",
        "QR card: untick \"Accent ring\" for a plain card with nothing "
        "moving.",
        "YouTube download: if YouTube asks for a robot check, the app "
        "waits and retries by itself.",
        "YouTube download: a REMOVE button deletes the downloader's "
        "files (about 120 MB) if you don't need it.",
    ],
    "1.29.1": [
        "Fixed: the YouTube downloader's one-time setup failed with "
        "\"Couldn't reach GitHub\" in the installed app.",
        "Bigger, clearer text fields on the QR and download tiles.",
    ],
    "1.29.0": [
        "New tile: YOUTUBE DOWNLOAD. Paste a link, choose MP4 or MP3, and "
        "it lands in your exports folder.",
        "The first download sets up the downloader (about 75 MB, one time); "
        "it then keeps itself up to date.",
    ],
    "1.28.1": [],          # Windows build fix — nothing a volunteer would notice
    "1.28.0": [
        "Timer and clock: a GREEN SCREEN toggle under Background gives "
        "you solid green to key out.",
        "Put your own footage behind the numbers in ProPresenter or an "
        "editor.",
    ],
    "1.27.1": [],          # a false crash report in our own analytics
    "1.27.0": [
        "Updates always jump straight to the newest version, however far "
        "behind you are.",
        "The blur option now shows in the preview, so you can see it before "
        "you export.",
    ],
    "1.26.0": [
        "This little box, so you stop finding out about features by accident.",
    ],
    "1.25.0": [],          # analytics only — nothing a volunteer would notice
    "1.24.0": [
        "Timers and clocks can sit on your own artwork now — one image, or a "
        "few that cycle.",
        "“Hold at 0:00” is now “Keep 0:00 on screen for”, "
        "because nobody knew what the old one meant.",
    ],
    "1.23.0": [
        "Milliseconds on a countdown, for when the last ten seconds need to "
        "feel dramatic.",
    ],
    "1.22.0": [
        "The timer can be an actual clock — start it at 7:59:50 and let it "
        "roll over to 8:00 on screen.",
    ],
    "1.21.0": [
        "The scoreboard reads your numbers properly, and stopped dropping a "
        "white box over the one you just edited.",
    ],
}


def _version_tuple(version):
    """"1.26.0" -> (1, 26, 0). Raises ValueError on anything malformed —
    callers decide what that means for them rather than this guessing."""
    return tuple(int(part) for part in str(version).strip().split("."))


def notes_for(version, limit=3):
    """Up to `limit` lines: this version's own notes first, then earlier
    versions' notes newest-first until `limit` is reached.

    "Earlier" means a smaller version tuple than `version`, so a version
    this dict has never heard of (a future release nobody updated NOTES
    for yet) still tops up from whatever is newest here rather than
    returning nothing. Never returns more than `limit`, never repeats a
    line, and an empty NOTES returns [] (the route turns that into
    show: false rather than announcing a card with nothing in it).
    """
    if not NOTES:
        return []
    try:
        running = _version_tuple(version)
    except ValueError:
        running = None      # can't compare -> treat every known entry as fair game

    ordered = sorted(NOTES, key=_version_tuple, reverse=True)
    pool = ([version] if version in NOTES else []) + [
        v for v in ordered
        if v != version and (running is None or _version_tuple(v) < running)
    ]

    lines = []
    for v in pool:
        for line in NOTES[v]:
            if len(lines) >= limit:
                return lines
            if line not in lines:
                lines.append(line)
    return lines


def _read_last_seen():
    """The stored version string, or None if there is no file / it is
    unreadable / it doesn't have the shape we write. Never raises."""
    try:
        with open(LAST_SEEN_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def _config_dir_is_empty():
    """True when CONFIG_DIR does not exist, or exists with nothing in it
    at all — the "genuinely fresh install" half of the show/hide table.
    Any content (analytics.json, boards/, backgrounds/, update.log, ...)
    means this is an existing user, even with last-seen.json absent."""
    try:
        return not os.listdir(CONFIG_DIR)
    except OSError:
        return True     # doesn't exist -> nothing in it either


def should_show(running_version):
    """The show/hide decision (docs/specs/whats-new.md), read-only — safe
    to call on every GET, since it never writes last-seen.json itself.

    | stored file | config dir  | -> show |
    |-------------|-------------|---------|
    | newer stored| n/a         | no      |
    | same/older  | n/a         | no      |
    | absent      | has content | yes     |
    | absent      | empty       | no      |
    """
    stored = _read_last_seen()
    if stored is not None:
        try:
            return _version_tuple(running_version) > _version_tuple(stored)
        except ValueError:
            return False     # a corrupt version string in the file: don't show
    return not _config_dir_is_empty()


def mark_seen(running_version):
    """Write last-seen.json with the running version. Idempotent, and used
    by both the dismiss POST and the boot-time seed below."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LAST_SEEN_PATH, "w") as f:
            json.dump({"version": str(running_version)}, f)
    except OSError:
        pass


def seed_if_fresh_install(running_version):
    """Called once at boot (app.prepare_exports_dir), before any request —
    NOT from the GET route, which must stay a pure read.

    A genuinely fresh install (no last-seen.json, and nothing else in
    CONFIG_DIR either) gets the file written silently here so the very
    first launch never shows the card, AND so the decision is locked in
    before this same session's own activity (a render, the analytics
    opt-in) fills the config dir with other content that GET would
    otherwise read as "existing user, show it".
    """
    if _read_last_seen() is None and _config_dir_is_empty():
        mark_seen(running_version)
