"""/api/update-*, /api/open-release — the GitHub-releases update check and
the packaged app's in-place self-update.

Split out of app.py (docs/specs/refactor-organise.md).
"""

import json
import os
import sys
import tempfile
import threading
import urllib.request
import webbrowser

from flask import Blueprint, jsonify, request

import netutil
import stats
import updater
from version import APP_VERSION

GITHUB_REPO = "jimhoggey/service-visuals"

bp = Blueprint("update", __name__)

# One GitHub query per app run; failures (offline, rate limit) stay silent —
# an update nag must never get in the way of a Sunday morning.
_update = {"checked": False, "available": False, "latest": None, "url": None,
           "error": None}


def _version_tuple(tag):
    return tuple(int(p) for p in tag.strip().lstrip("v").split(".")[:3])


def _release_version(release):
    """(major, minor, patch) for a release we could install, else None.

    Drafts and prereleases are not for churches, and a tag that isn't three
    numbers is skipped rather than allowed to raise — one malformed tag in
    the list must not take the whole update check down with it.
    """
    if not isinstance(release, dict):
        return None
    if release.get("draft") or release.get("prerelease"):
        return None
    try:
        return _version_tuple(release.get("tag_name") or "")
    except (AttributeError, ValueError):
        return None


def newest_release(releases):
    """The highest-VERSION release, not the most recently published one.

    GitHub's /releases/latest means "most recent by publish date", which is
    the same answer as "newest version" right up until a hotfix goes out on
    an older line: publish v1.24.2 after v1.26.1 and /releases/latest
    answers v1.24.2, so somebody still on v1.23 gets offered a version
    OLDER than the newest one available, and never sees v1.26.1 at all.
    Picking by version instead drops the dependency on publish order.
    """
    best = None
    best_version = None
    for release in releases or []:
        version = _release_version(release)
        if version is None:
            continue
        if best_version is None or version > best_version:
            best, best_version = release, version
    return best


def _github_json(path, timeout=10):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/%s" % (GITHUB_REPO, path),
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "service-visuals"})
    with netutil.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


@bp.route("/api/update-check")
def api_update_check():
    # ?force=1 re-queries GitHub (the manual "Check for updates" button); the
    # boot-time check only queries once.
    if request.args.get("force"):
        _update["checked"] = False
    if not _update["checked"]:
        _update["checked"] = True
        _update["error"] = None
        try:
            # Ask for the whole list and pick the highest version. The old
            # single /releases/latest call is kept as the fallback: if the
            # list request fails or comes back with nothing installable, a
            # working check on one release beats no check at all.
            data = None
            try:
                data = newest_release(_github_json("releases?per_page=100"))
            except Exception:
                data = None
            if data is None:
                data = _github_json("releases/latest")
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


@bp.route("/api/update-log")
def api_update_log():
    return jsonify({"log": updater.read_log()})


@bp.route("/api/open-release", methods=["POST"])
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


@bp.route("/api/update-install", methods=["POST"])
def api_update_install():
    # jobs (the render queue) lives in app.py; imported here, not at module
    # level, to avoid a circular import at blueprint-registration time.
    from app import jobs
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


@bp.route("/api/update-status")
def api_update_status():
    return jsonify(_install_state)
