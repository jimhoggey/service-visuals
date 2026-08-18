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

Errors are the one place a little more than a name is sent, because a crash
nobody hears about never gets fixed: the exception TYPE, WHERE in the app's
own code it happened (file basename, line, function — never a directory), and
a message with anything path-shaped replaced by <path> and capped short. The
full, unredacted traceback goes only to ~/.service-visuals/crash.log so the
operator can hand it over by choice. Error events are capped per run so a
crash loop cannot flood.

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
import sys
import threading
import time
import traceback
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
    # Errors — props: error (type), where (frames), msg (sanitised), plus:
    "crash",            # uncaught exception at process level      where_kind
    "render_failed",    # an export's renderer raised               tool
    "request_failed",   # a route answered 500                      route
    "startup_failed",   # the previous launch never reached the UI
)


CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
CONFIG_PATH = os.path.join(CONFIG_DIR, "analytics.json")
CRASH_LOG = os.path.join(CONFIG_DIR, "crash.log")
BOOT_PATH = os.path.join(CONFIG_DIR, "boot-pending.json")
MAX_ERROR_EVENTS = 20           # per run — a crash loop is one bug, not 500
MAX_TRACE_FRAMES = 4
MAX_MSG_LEN = 160
MAX_CRASH_LOG_BYTES = 256 * 1024

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
          "worker": None, "errors": 0, "sending": False}


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
        _state["sending"] = True
        try:
            _post(event)
        except Exception:
            pass        # offline, blocked, rate-limited — never the user's problem
        finally:
            _state["sending"] = False
            _q.task_done()


def flush(timeout=3.0):
    """Wait briefly for queued events to go out. Only used on the way down
    (a crash, a restart) — never on a request or render path."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _q.empty() and not _state["sending"]:
            return True
        time.sleep(0.05)
    return False


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
        if _state["worker"] is not None:
            return True                 # already running; don't count twice
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


# ------------------------------------------------------------------ errors

_PATH_RE = re.compile(r"\S*[\\/]\S*")            # any token with a slash in it
_FILE_RE = re.compile(
    r"\S+\.(?:png|jpe?g|gif|mp4|mov|json|zip|exe|app|dmg|log|txt|py|bat|sh|"
    r"ttf|otf|ini|cfg)\b", re.IGNORECASE)          # bare filenames (exports carry names)
_HOME_RE = re.compile(r"(?i)\b(?:users|home)[\\/][^\\/\s]+")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|“[^”]*”")


def _scrub(text, limit=MAX_MSG_LEN):
    """A message with nothing path-, file- or person-shaped left in it.

    Export filenames are scrubbed too: they embed the QR heading or board
    name, which is exactly the content this module promises never to send.
    """
    text = " ".join(str(text or "").split())
    # Quoted spans are where reprs of the operator's own values end up
    # (KeyError('Group points'), "invalid literal ... '350'") — drop them.
    text = _QUOTED_RE.sub("'…'", text)
    text = _PATH_RE.sub("<path>", text)
    text = _FILE_RE.sub("<file>", text)
    text = _HOME_RE.sub("<user>", text)
    try:
        home = os.path.basename(os.path.expanduser("~"))
        if home and len(home) > 2:
            text = text.replace(home, "<user>")
    except Exception:
        pass
    return text[:limit]


_LIBRARY_HINTS = ("site-packages", "dist-packages", "lib/python", "lib\\python",
                  "importlib", "threading.py", "socketserver.py")


def _where(exc):
    """Innermost few frames as 'file.py:LINE func', basenames only.

    Frames from inside Flask/werkzeug/the stdlib are dropped when any of our
    own remain — Flask's app.py:902 dispatch_request says nothing, and its
    basename even collides with ours.
    """
    try:
        raw = traceback.extract_tb(getattr(exc, "__traceback__", None))
    except Exception:
        return ""
    ours = [fr for fr in raw
            if not any(h in str(fr.filename or "") for h in _LIBRARY_HINTS)]
    picked = (ours or raw)[-MAX_TRACE_FRAMES:]
    frames = ["{0}:{1} {2}".format(os.path.basename(str(fr.filename or "?")),
                                   fr.lineno, fr.name) for fr in picked]
    return " < ".join(reversed(frames))[:240]


def _crash_log(kind, exc, extra=None):
    """Full traceback, locally, for the operator to share by choice."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(CRASH_LOG) and \
                os.path.getsize(CRASH_LOG) > MAX_CRASH_LOG_BYTES:
            os.unlink(CRASH_LOG)
        with open(CRASH_LOG, "a") as f:
            f.write("=== {0} {1} v{2} {3}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), kind,
                _state["app_version"], extra or ""))
            f.write("".join(traceback.format_exception(
                type(exc), exc, getattr(exc, "__traceback__", None))))
            f.write("\n")
    except Exception:
        pass


_REPORTED = []          # id()s of exceptions already reported this run


def report_error(kind, exc, **props):
    """Log an exception locally in full and send its shape anonymously.

    kind is one of the error EVENTS. Never raises. Rate-limited per run, and
    the same exception object is reported once even if it passes through
    both a try/except and sys.excepthook on its way out.
    """
    try:
        with _lock:
            if id(exc) in _REPORTED:
                return
            _REPORTED.append(id(exc))
            del _REPORTED[:-64]
    except Exception:
        pass
    try:
        _crash_log(kind, exc, " ".join(
            "{0}={1}".format(k, v) for k, v in sorted(props.items())))
    except Exception:
        pass
    try:
        with _lock:
            if _state["errors"] >= MAX_ERROR_EVENTS:
                return
            _state["errors"] += 1
        track(kind,
              error=type(exc).__name__[:60],
              where=_where(exc),
              msg=_scrub(exc),
              **props)
    except Exception:
        pass


def install_excepthooks():
    """Report uncaught exceptions on the main thread and on threads."""
    def hook(exc_type, exc, tb, _prev=sys.excepthook):
        try:
            if exc is not None:
                report_error("crash", exc, where_kind="main")
                flush(2.0)
        finally:
            _prev(exc_type, exc, tb)
    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        def thook(args, _prev=threading.excepthook):
            try:
                if args.exc_value is not None:
                    report_error("crash", args.exc_value,
                                 where_kind="thread")
            finally:
                _prev(args)
        threading.excepthook = thook


# ---------------------------------------------------------- boot marker
# "The app doesn't open" is the crash the developer never sees, because
# nothing is running to report it. So the launcher drops a marker as its
# first act and the UI's first request clears it; a marker still there on
# the NEXT launch means the last one never got that far.

def mark_boot(app_version):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(BOOT_PATH, "w") as f:
            json.dump({"version": str(app_version), "at": time.time(),
                       "pid": os.getpid()}, f)
    except OSError:
        pass


def boot_ready():
    """Clear THIS process's marker. A second instance started meanwhile
    owns the file now (its pid is in it), so leave that one alone: two
    instances can lose a report, never invent one."""
    if _state.get("booted"):
        return
    _state["booted"] = True
    try:
        with open(BOOT_PATH) as f:
            owner = json.load(f).get("pid")
    except (OSError, ValueError, AttributeError):
        return
    if owner in (None, os.getpid()):
        try:
            os.unlink(BOOT_PATH)
        except OSError:
            pass


def take_boot_result():
    """The previous launch's marker, if it was never cleared; consumed."""
    try:
        with open(BOOT_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {"version": str(data.get("version") or ""),
            "at": float(data.get("at") or 0)}


def _last_crash_after(stamp):
    """Error type from crash.log entries newer than `stamp`, or ''."""
    try:
        with open(CRASH_LOG) as f:
            tail = f.read()[-8000:]
    except OSError:
        return ""
    found = ""
    for block in tail.split("=== ")[1:]:
        head = block.split("\n", 1)[0]
        try:
            when = time.mktime(time.strptime(head[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if when >= stamp - 1:
            lines = [ln for ln in block.strip().split("\n") if ln.strip()]
            if lines:
                # "SomeError: message" -> "SomeError"; anything else -> ''
                m = re.match(r"\s*([A-Za-z_][\w.]*)\s*(?::|$)", lines[-1])
                found = _scrub(m.group(1))[:60] if m else ""
    return found


def report_previous_boot():
    """Call at startup, after start(): sends startup_failed if the last
    launch never reached the UI, then arms the marker for THIS boot. Runs
    once per process — the launcher calls it before importing the app (so a
    crash inside those imports is covered) and the app calls it again.
    """
    if _state.get("armed"):
        return
    _state["armed"] = True
    prev = take_boot_result()
    if prev:
        try:
            track("startup_failed",
                  prev_version=prev["version"][:20],
                  error=_last_crash_after(prev["at"]) or "unknown")
        except Exception:
            pass
    mark_boot(_state["app_version"])
