"""Fetches, verifies and updates yt-dlp and Deno for the YouTube-download
tile (docs/specs/youtube-download.md).

YouTube changes its player often enough that bundling yt-dlp with the app
would go stale between our own releases — and since 2025 it needs an
external JS runtime (Deno) to solve YouTube's challenges, which we don't
bundle either. So this module fetches both, once, into
``<config dir>/bin`` on first use, verifies them by sha256 against the
project's own published checksums, and `update_ytdlp()` keeps yt-dlp
current afterwards. The feature then outlives any single app release.

All network code lives here, and only here, so `downloader.py` and
`scripts/smoke.py` can exercise the pure helpers below (`asset_names`,
`verify_sha256`, ...) completely offline.
"""

import hashlib
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

# Same convention as backgrounds.py / stats.py: honour SERVICE_VISUALS_CONFIG
# so a smoke/dev run never touches the real ~/.service-visuals.
_CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")

BIN_DIR = os.path.join(_CONFIG_DIR, "bin")
STAMP_PATH = os.path.join(BIN_DIR, "last-update")
DOWNLOAD_LOG = os.path.join(_CONFIG_DIR, "download.log")

# Deno is pinned (not "latest") — a JS runtime is a bigger trust surface
# than yt-dlp itself, so it only moves when we deliberately bump this.
DENO_VERSION = "v2.9.6"

YTDLP_RELEASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
DENO_RELEASE = "https://github.com/denoland/deno/releases/download/" \
    + DENO_VERSION

_NETWORK_TIMEOUT = 30
_UPDATE_TIMEOUT = 60
_USER_AGENT = "service-visuals (+https://github.com/jimhoggey/service-visuals)"
_UPDATE_INTERVAL = 24 * 60 * 60  # seconds
_MAX_LOG_BYTES = 256 * 1024


class ToolsError(Exception):
    """Raised with a plain-English message safe to show the operator."""


# --------------------------------------------------------------- platform

def asset_names():
    """(ytdlp_asset, deno_asset) release filenames for this machine.

    Reads sys.platform / platform.machine() at CALL time (not import
    time) so smoke.py can monkeypatch both and check every platform/arch
    pair without actually running on that platform.
    """
    machine = (platform.machine() or "").lower()
    is_arm = machine in ("arm64", "aarch64")
    if sys.platform == "darwin":
        ytdlp = "yt-dlp_macos"
        deno = ("deno-aarch64-apple-darwin.zip" if is_arm
                else "deno-x86_64-apple-darwin.zip")
    elif sys.platform == "win32":
        ytdlp = "yt-dlp_arm64.exe" if is_arm else "yt-dlp.exe"
        deno = ("deno-aarch64-pc-windows-msvc.zip" if is_arm
                else "deno-x86_64-pc-windows-msvc.zip")
    else:
        # Not a supported platform for this feature (see spec's Windows/
        # Mac-only scope) — kept importable rather than raising, so the
        # module never blows up an unrelated import on Linux CI.
        ytdlp = "yt-dlp"
        deno = ("deno-aarch64-unknown-linux-gnu.zip" if is_arm
                else "deno-x86_64-unknown-linux-gnu.zip")
    return ytdlp, deno


def _binary_names():
    """The filenames yt-dlp/Deno are stored under once fetched — a plain
    name on POSIX, `.exe` on Windows (Deno's own release ships `deno.exe`
    inside the Windows zip, and yt-dlp's Windows asset already ends in
    `.exe`)."""
    if sys.platform == "win32":
        return "yt-dlp.exe", "deno.exe"
    return "yt-dlp", "deno"


def binary_paths():
    """(ytdlp_path, deno_path) inside BIN_DIR. Pure — just path math, so
    downloader.py can check what's already there before deciding how much
    of the progress bar the tools-fetch phase gets."""
    ytdlp_name, deno_name = _binary_names()
    return (os.path.join(BIN_DIR, ytdlp_name),
            os.path.join(BIN_DIR, deno_name))


# ------------------------------------------------------------ sha256/http

def verify_sha256(path, expected_hex):
    """True if the file at `path` sha256-hashes to `expected_hex`.

    Pure and offline — no network, no BIN_DIR — so smoke.py can point it
    at a temp file it wrote itself.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == (expected_hex or "").strip().lower()


def _find_sha256(sums_text, asset_name):
    """Pick the digest for `asset_name` out of a checksums file shaped
    like ``<hex>  <name>`` per line — both yt-dlp's combined
    SHA2-256SUMS and Deno's per-asset ``<asset>.sha256sum`` use this same
    line format."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            return parts[0]
    return None


def _http_get(url, dest=None, progress_cb=None):
    """GET `url`. With `dest`, streams the body to that path and returns
    it; otherwise returns the body as bytes. Any network failure becomes
    a ToolsError with the one message this app ever shows for "the
    internet isn't reachable" — the details (DNS, TLS, a corporate proxy)
    aren't actionable for a volunteer anyway.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_NETWORK_TIMEOUT) as resp:
            if dest is None:
                return resp.read()
            total = resp.length
            done = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(1.0, done / total))
            return dest
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ToolsError(
            "Couldn't reach GitHub to set up the downloader — check the "
            "internet connection and try again.") from exc


def _download_verified(asset_url, sums_url, asset_name, part_path,
                       progress_cb=None):
    """Download `asset_url` to `part_path`, verify it against the sha256
    sums file at `sums_url`, and raise ToolsError (deleting the partial
    file) on any mismatch — checked BEFORE the caller renames it into
    place, so a half-verified file is never mistaken for a real tool."""
    _http_get(asset_url, dest=part_path, progress_cb=progress_cb)
    sums_text = _http_get(sums_url).decode("utf-8", "replace")
    expected = _find_sha256(sums_text, asset_name)
    if expected is None or not verify_sha256(part_path, expected):
        try:
            os.unlink(part_path)
        except OSError:
            pass
        raise ToolsError(
            "The downloader's files did not verify — try again later.")
    return part_path


# ------------------------------------------------------------- fetching

def _fetch_ytdlp(asset, dest, progress_cb=None):
    part = dest + ".part"
    url = "{0}/{1}".format(YTDLP_RELEASE, asset)
    sums_url = "{0}/SHA2-256SUMS".format(YTDLP_RELEASE)
    _download_verified(url, sums_url, asset, part, progress_cb)
    os.replace(part, dest)
    if sys.platform != "win32":
        os.chmod(dest, 0o755)


def _fetch_deno(asset, dest, progress_cb=None):
    zip_part = os.path.join(BIN_DIR, asset + ".part")
    url = "{0}/{1}".format(DENO_RELEASE, asset)
    sums_url = "{0}/{1}.sha256sum".format(DENO_RELEASE, asset)
    _download_verified(url, sums_url, asset, zip_part, progress_cb)
    binary_name = os.path.basename(dest)  # "deno" or "deno.exe"
    try:
        with zipfile.ZipFile(zip_part) as zf:
            with zf.open(binary_name) as src, \
                    open(dest + ".part", "wb") as out:
                out.write(src.read())
        os.replace(dest + ".part", dest)
    finally:
        try:
            os.unlink(zip_part)
        except OSError:
            pass
    if sys.platform != "win32":
        os.chmod(dest, 0o755)


def ensure_tools(progress_cb=None, status_cb=None):
    """Make sure yt-dlp and Deno both exist (verified) in BIN_DIR,
    fetching whatever is missing. Returns {"ytdlp": path, "deno": path}.
    Idempotent — a second call with both already present does no network
    work at all.

    `progress_cb(pct)` reports 0..100 across whatever actually needs
    fetching this call (never called if nothing does). `status_cb(text)`
    is called once, only when a fetch is about to start — there is no
    per-job status-text channel today (JobManager's Job carries only a
    fixed queued/rendering/done/error state), so nothing currently reads
    this, but it's cheap to offer and matches the spec's contract.
    """
    ytdlp_path, deno_path = binary_paths()
    todo = []
    if not os.path.isfile(ytdlp_path):
        todo.append("ytdlp")
    if not os.path.isfile(deno_path):
        todo.append("deno")

    if not todo:
        return {"ytdlp": ytdlp_path, "deno": deno_path}

    if status_cb:
        status_cb("Setting up the downloader (one time)…")

    os.makedirs(BIN_DIR, exist_ok=True)
    ytdlp_asset, deno_asset = asset_names()
    total = len(todo)
    for i, item in enumerate(todo):
        def _item_progress(frac, _i=i):
            if progress_cb:
                progress_cb(int(((_i + frac) / total) * 100))
        if item == "ytdlp":
            _fetch_ytdlp(ytdlp_asset, ytdlp_path, _item_progress)
        else:
            _fetch_deno(deno_asset, deno_path, _item_progress)

    if progress_cb:
        progress_cb(100)
    return {"ytdlp": ytdlp_path, "deno": deno_path}


def update_ytdlp():
    """Run `<ytdlp> -U` at most once a day, so the feature stays current
    with YouTube without a network round-trip on every single download.
    Never raises — a Sunday-morning self-update hiccup should be
    invisible; yt-dlp keeps working on whatever version is already there.
    """
    ytdlp_path, _ = binary_paths()
    if not os.path.isfile(ytdlp_path):
        return
    try:
        due = time.time() - os.path.getmtime(STAMP_PATH) >= _UPDATE_INTERVAL
    except OSError:
        due = True  # no stamp yet
    if due:
        try:
            subprocess.run([ytdlp_path, "-U"], timeout=_UPDATE_TIMEOUT,
                           capture_output=True, check=False)
        except Exception as exc:
            log_line("update_ytdlp failed: {0}".format(exc))
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            with open(STAMP_PATH, "a"):
                pass
            os.utime(STAMP_PATH, None)
        except OSError:
            pass
    # The version may just have changed (or never been recorded); refresh
    # the cache the status line reads while we are already off the UI path.
    if due or not os.path.isfile(VERSION_PATH):
        _ytdlp_version(ytdlp_path, refresh=True, timeout=_UPDATE_TIMEOUT)


# yt-dlp's standalone binary is a PyInstaller onefile: every run unpacks
# itself first, which took 11 s on a busy Mac. Asking it for its version
# on every visit to the tile made the status line hang or read "null", so
# the answer is cached in a file and refreshed only after a self-update.
VERSION_PATH = os.path.join(BIN_DIR, "ytdlp-version")


def _ytdlp_version(ytdlp_path, refresh=False, timeout=10):
    if not refresh:
        try:
            with open(VERSION_PATH, encoding="utf-8") as fh:
                cached = fh.read().strip()
            if cached:
                return cached
        except OSError:
            pass
    version = None
    try:
        result = subprocess.run(
            [ytdlp_path, "--version"], timeout=timeout,
            capture_output=True, text=True, check=False)
        version = result.stdout.strip() or None
    except Exception:
        version = None
    if version:
        try:
            with open(VERSION_PATH, "w", encoding="utf-8") as fh:
                fh.write(version)
        except OSError:
            pass
    return version


def tools_status():
    """{"ready": bool, "ytdlp_version": str|None} for the status line the
    tile shows on entry. `ready` is just "the file is there" — a broken
    binary would still show ready with no version rather than crashing
    the status endpoint."""
    ytdlp_path, _ = binary_paths()
    ready = os.path.isfile(ytdlp_path)
    # Cache only — never spawn the binary here (see VERSION_PATH). Until a
    # first download has recorded it, the version is simply unknown.
    version = None
    if ready:
        try:
            with open(VERSION_PATH, encoding="utf-8") as fh:
                version = fh.read().strip() or None
        except OSError:
            version = None
    return {"ready": ready, "ytdlp_version": version}


def log_line(text):
    """Append one timestamped line to download.log — local only, never
    sent to analytics (see downloader.py's error handling and
    module docstring above). Best-effort; a logging failure must never
    break a download.
    """
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        line = "{0}  {1}\n".format(
            time.strftime("%Y-%m-%dT%H:%M:%S"), text)
        with open(DOWNLOAD_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
        if os.path.getsize(DOWNLOAD_LOG) > _MAX_LOG_BYTES:
            with open(DOWNLOAD_LOG, "rb") as fh:
                data = fh.read()
            with open(DOWNLOAD_LOG, "wb") as fh:
                fh.write(data[-_MAX_LOG_BYTES:])
    except OSError:
        pass
