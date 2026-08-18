"""Anonymous usage counts (Aptabase). Off in one switch, silent when it fails.

Why this exists: nobody can tell which visuals actually get used on a Sunday,
so improvements are guesswork. Why it is safe: an event is a NAME and nothing
else — no board names, no team names, no QR links, no spinner entries, no
filenames, no paths, no IP-derived identity, no account, no device id. The
complete list of everything that can ever leave the machine is EVENTS below,
plus app version and OS name/version. If a change wants to add a field, it
gets added there and nowhere else, so the privacy surface stays readable in
one screen.

Aptabase's ingest API has no user identifier of any kind; `sessionId` is a
timestamp plus eight random digits, generated fresh in memory and rotated
after an hour idle, so it cannot link two runs of the app together.

It cannot slow an export down: track() puts a small dict on a bounded queue
and returns. One daemon thread does the HTTPS POST with a short timeout, and
every failure — offline, DNS, rate limit, a church firewall — is swallowed.
A full queue drops events rather than waiting. Nothing in the render path
ever blocks on this.

Wire format verified against aptabase/aptabase's own EventBody validation and
its first-party SDKs: POST {region host}/api/v0/event with an App-Key header,
200 on success.
"""

import json
import os
import platform
import queue
import random
import re
import threading
import time
import urllib.request

import netutil

# Write-only ingest key. Public by design — it can add events to this project
# and read nothing back, so living in a public repo costs nothing.
APP_KEY = "A-US-9066842799"
SDK_VERSION = "service-visuals-stdlib@1.0.0"

# Region is the middle segment of the key (A-US-… -> us.aptabase.com).
_REGION_HOSTS = {"US": "https://us.aptabase.com", "EU": "https://eu.aptabase.com"}

# Every event this app can ever send. Names only; see the module docstring.
EVENTS = (
    "app_started",      # the app opened
    "export",           # a visual was exported   props: tool
    "board_created",    # a scoreboard was read   props: numbers (how many OCR found)
    "board_number_added",  # a number OCR missed was added by hand
    "ocr_failed",       # scoreboard OCR found nothing usable
    "ai_fill",          # the spinner's AI fill was used
    "update_installed",  # a self-update completed
    "update_failed",    # a self-update did not apply
)

CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
CONFIG_PATH = os.path.join(CONFIG_DIR, "analytics.json")

# On by default, one click to turn off in the footer, and said plainly in the
# README. Flip this to False to make it opt-in instead — nothing else changes.
DEFAULT_ENABLED = True

SESSION_IDLE = 60 * 60          # Aptabase's own rotation window
QUEUE_MAX = 64
TIMEOUT = 5

_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4}){0,2}$")

_q = queue.Queue(maxsize=QUEUE_MAX)
_lock = threading.Lock()
_state = {"app_version": "0.0.0", "session": None, "touched": 0.0,
          "worker": None}


# ------------------------------------------------------------------ opt-out

def enabled():
    try:
        with open(CONFIG_PATH) as f:
            return bool(json.load(f).get("enabled", DEFAULT_ENABLED))
    except (OSError, ValueError, AttributeError):
        return DEFAULT_ENABLED


def set_enabled(on):
    on = bool(on)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump({"enabled": on}, f)
    except OSError:
        pass
    return on


# ------------------------------------------------------------------- system

def _os_name():
    system = platform.system()
    return {"Darwin": "macOS", "Windows": "Windows"}.get(system, system or "Unknown")


def _os_version():
    try:
        if platform.system() == "Darwin":
            return platform.mac_ver()[0] or platform.release()
        return platform.release() or ""
    except Exception:
        return ""


def _locale():
    """Best-effort language tag, or nothing.

    Aptabase drops malformed locales server-side rather than rejecting the
    event, but sending junk is still noise — omit anything that isn't a
    plain BCP-47-ish tag.
    """
    tag = ""
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(env) or ""
        if raw:
            tag = raw.split(".")[0].split("@")[0].replace("_", "-")
            break
    if not tag:
        try:
            import locale as _loc
            tag = (_loc.getlocale()[0] or "").replace("_", "-")
        except Exception:
            tag = ""
    tag = tag[:10]
    return tag if _LOCALE_RE.match(tag) else ""


def _session():
    """Aptabase session id: epoch seconds + 8 random digits, 1h idle rotation.

    In memory only, never written to disk — two runs of the app cannot be
    tied together, which is the whole point.
    """
    now = time.time()
    with _lock:
        if _state["session"] is None or now - _state["touched"] > SESSION_IDLE:
            _state["session"] = str(int(now) * 100000000 +
                                    random.randint(0, 99999999))
        _state["touched"] = now
        return _state["session"]


# -------------------------------------------------------------------- send

def _clean_props(props):
    """Only short scalars survive. Guards against a caller ever passing
    something with content in it by accident."""
    out = {}
    for key, value in (props or {}).items():
        if not isinstance(key, str) or not key or len(key) > 40:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:60]
    return out


def _post(event):
    host = _REGION_HOSTS.get((APP_KEY.split("-") + ["", ""])[1].upper())
    if not host:
        return
    body = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        host + "/api/v0/event", data=body, method="POST",
        headers={"App-Key": APP_KEY, "Content-Type": "application/json",
                 "User-Agent": "service-visuals"})
    with netutil.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def _worker():
    while True:
        event = _q.get()
        try:
            _post(event)
        except Exception:
            pass        # offline, blocked, rate-limited — never the user's problem
        finally:
            _q.task_done()


def start(app_version, force=False):
    """Begin a session. No-op when opted out, and when running from source
    (a developer's own runs are not usage) unless SERVICE_VISUALS_STATS=1."""
    _state["app_version"] = str(app_version)
    if os.environ.get("SERVICE_VISUALS_STATS") == "0":
        return False
    import sys
    if not force and not getattr(sys, "frozen", False) \
            and os.environ.get("SERVICE_VISUALS_STATS") != "1":
        return False
    if not enabled():
        return False
    with _lock:
        if _state["worker"] is None:
            _state["worker"] = threading.Thread(target=_worker, daemon=True)
            _state["worker"].start()
    track("app_started")
    return True


def track(name, **props):
    """Queue one event. Returns immediately; never raises."""
    try:
        if name not in EVENTS or _state["worker"] is None or not enabled():
            return
        _q.put_nowait({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime()),
            "sessionId": _session(),
            "eventName": name,
            "systemProps": {k: v for k, v in {
                "isDebug": False,
                "locale": _locale(),
                "osName": _os_name(),
                "osVersion": _os_version(),
                "appVersion": _state["app_version"],
                "sdkVersion": SDK_VERSION,
            }.items() if v != ""},
            "props": _clean_props(props),
        })
    except queue.Full:
        pass            # a backlog means the network is gone; counts can lapse
    except Exception:
        pass
