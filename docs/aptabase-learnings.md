# Aptabase for a desktop app — what we learned (Service Visuals, Aug 2026)

Portable notes for adding anonymous, privacy-first usage counts + crash
reporting to another local/desktop app. Written after doing it in a Python
(Flask + pywebview + PyInstaller) app; the wire protocol and the design rules
apply to any language.

## Why Aptabase

- Built for desktop/mobile apps, not websites: no cookies, no fingerprinting,
  no device or user identifier anywhere in the API. Sessions are anonymous.
- Free tier is generous (~1M events/month), open source (AGPL), self-hostable.
- Ingest is one HTTPS POST. **No SDK needed** — the official SDKs are ~100
  lines; a stdlib client is trivial and avoids a dependency in a frozen build.

## The wire protocol (verified against Aptabase's backend source)

App key looks like `A-US-1234567890`. The middle segment is the region:

| key prefix | host |
|---|---|
| `A-US-…` | `https://us.aptabase.com` |
| `A-EU-…` | `https://eu.aptabase.com` |
| `A-SH-…` | your own self-hosted URL |

**Endpoint:** `POST {host}/api/v0/event` (single) or `/api/v0/events` (JSON
array, max 25 per request). **Headers:** `App-Key: <key>`,
`Content-Type: application/json`. **Success:** `200` with body `{}`.
`404` = wrong/missing key, `400` = validation, `429` = rate limit (20 req/s
per IP).

Body (single event):

```json
{
  "timestamp": "2026-08-18T12:34:56.000Z",
  "sessionId": "178703338357000533",
  "eventName": "export",
  "systemProps": {
    "isDebug": false,
    "locale": "en-US",
    "osName": "macOS",
    "osVersion": "15.5",
    "appVersion": "1.19.0",
    "sdkVersion": "myapp-stdlib@1.0.0"
  },
  "props": { "tool": "timer" }
}
```

Rules that matter:

- `eventName` ≤ 60 chars, required. `sdkVersion` required (any short string).
- `sessionId`: convention is `epoch_seconds * 100_000_000 + 8 random digits`,
  as a string. Rotate after **1 hour idle** (what every official SDK does).
  Keep it in memory only — never persist it, or two runs become linkable.
- `osName` must be set for a desktop app, otherwise the server treats the
  event as a web hit and parses User-Agent.
- `props` values: only **string / number / bool** survive. Arrays/objects are
  replaced with `"[Array]"`/`"{Object}"`. Keys ≤ 40 chars.
- Timestamps > 1 day old or > 10 min in the future are rejected.
- The app key is **write-only**. It is fine in a public repo.

## How to implement it (the shape that worked)

1. **One module** (`stats.py` here), no dependency, ~200 lines including
   the crash-report scrubbing.
2. **Fire-and-forget:** `track(name, **props)` puts a dict on a bounded
   `queue.Queue` and returns. One daemon thread POSTs with a short timeout
   (5 s). Every failure is swallowed. A full queue *drops* events. Nothing
   in a request/render path ever waits on it. Only `flush(timeout)` waits,
   and only on the way down (a crash, a self-update restart).
3. **Whitelist the events.** Keep the complete list of event names in one
   tuple at the top of the module; `track()` ignores anything not in it. That
   tuple *is* the privacy surface — anyone can read it in one screen.
4. **Send names, not content.** Version + OS + event name + tiny scalar
   props (`tool=timer`, `numbers=6`). Never user text, file names, paths,
   URLs, or anything typed. Filenames are sneaky: exports often embed a
   heading or a board name in the file name.
5. **Off in dev.** No-op unless the app is frozen/packaged, or an env var
   (`MYAPP_STATS=1`) forces it. Add `MYAPP_STATS=0` to force it off in CI.
6. **A visible toggle** in the UI + a README paragraph saying exactly what is
   sent. On by default is defensible when it is genuinely anonymous; make the
   default a single constant so it can be flipped to opt-in.
7. **Verify with one real POST** before wiring anything: build the body,
   send it with the real key, expect `200 {}`. Then check the dashboard.

## Crash / error reporting on top of it (the part people get wrong)

Local crash logs nobody ever sees are worthless — send the *shape* of the
error, keep the *detail* local:

- Send: `error` = exception type name; `where` = innermost 3–4 frames as
  `file.py:LINE func` (basenames only, prefer your own files over
  Flask/stdlib frames); `msg` = scrubbed message ≤ 160 chars; plus a
  category prop (`tool=timer`, `route=api_export`, `where_kind=startup`).
- Scrub the message before sending: replace any token containing `/` or `\`
  with `<path>`, anything with a file extension with `<file>`, anything in
  quotes with `'…'` (reprs of user values live in quotes:
  `KeyError('Team name')`), and the OS username with `<user>`.
- Write the **full unredacted traceback** to a local `crash.log` so a user
  can hand it over by choice.
- **Cap error events per run** (20) — a crash loop is one bug, not 500
  events.
- Hook points: process-level `sys.excepthook` + `threading.excepthook`;
  a `try/except` around the whole launcher `main()`; the framework's
  unhandled-exception signal (Flask: `got_request_exception`); your job
  runner's error path.
- **"The app won't open"** is the crash nothing is running to report. Fix:
  the launcher writes a `boot-pending` marker as its first act; the UI's
  first request to the backend deletes it; a marker still present on the
  *next* launch → send `startup_failed` (+ the error type from crash.log if
  one was written after the marker's timestamp). Same trick works for
  "self-update didn't apply": write `expect=v1.19` before exiting, compare
  on next boot.
- Idempotent `start()`: the launcher starts stats *before* importing the
  heavy app module, so an import-time crash still reports; the app calls
  `start()` again and it must not double-count `app_started`. Keep the
  version in a tiny `version.py` both can import.

## Things that bit us

- `str.format()` on a shell script template with `{ }` braces — build
  scripts by concatenation.
- Aptabase shows each distinct prop value as its own row; long unique
  strings (traces) are fine for debugging but keep them short and stable.
- Don't put the boot-marker delete on every health poll — do it once.
- Test the scrubber with real-looking messages from your platforms
  (`[Errno 13] … '/Users/x/…'`, `C:\Users\x\AppData\Local\Temp\_MEI…`,
  `\\server\share\file.png`) — the first regex order I wrote left fragments.

## Minimal Python client (stdlib)

```python
import json, os, platform, queue, random, threading, time, urllib.request

APP_KEY = "A-US-1234567890"
HOST = {"US": "https://us.aptabase.com", "EU": "https://eu.aptabase.com"}[APP_KEY.split("-")[1]]
EVENTS = ("app_started", "export", "crash")           # the whole privacy surface
_q = queue.Queue(maxsize=64); _s = {"sid": None, "t": 0.0, "v": "0"}

def _session():
    now = time.time()
    if _s["sid"] is None or now - _s["t"] > 3600:
        _s["sid"] = str(int(now) * 100000000 + random.randint(0, 99999999))
    _s["t"] = now; return _s["sid"]

def _worker():
    while True:
        ev = _q.get()
        try:
            req = urllib.request.Request(HOST + "/api/v0/event", data=json.dumps(ev).encode(),
                headers={"App-Key": APP_KEY, "Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
        _q.task_done()

def start(version):
    _s["v"] = version
    threading.Thread(target=_worker, daemon=True).start()
    track("app_started")

def track(name, **props):
    if name not in EVENTS: return
    try:
        _q.put_nowait({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "sessionId": _session(), "eventName": name,
            "systemProps": {"isDebug": False, "osName": platform.system(),
                "osVersion": platform.release(), "appVersion": _s["v"], "sdkVersion": "myapp-stdlib@1"},
            "props": {k: v for k, v in props.items() if isinstance(v, (str, int, float, bool))}})
    except queue.Full:
        pass
```

(In a PyInstaller build, use `certifi` for the SSL context — frozen Python
ships no CA certificates and HTTPS silently fails otherwise.)

Reference implementation with crash reporting, scrubbing and boot marker:
`stats.py` in https://github.com/jimhoggey/service-visuals.
