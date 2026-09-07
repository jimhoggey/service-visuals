"""The YouTube-download render job (docs/specs/youtube-download.md,
docs/specs/batch-download.md).

`download_video` is a JobManager job like every renderer in render/ —
`fn(options, progress_cb) -> ...` — just backed by yt-dlp (via tools.py)
instead of Pillow/ffmpeg frames. Kept at the top level (not under
render/) since nothing here draws a single pixel. Unlike the others it
returns a dict, not a bare filename: `options["urls"]` is always a list
(validation.py's one downstream shape, even for a single pasted link),
so this always runs it as a batch of BATCH_LANES concurrent lanes and
reports one result per link — see batch-download.md for the shape.
"""

import collections
import concurrent.futures
import os
import random
import re
import subprocess
import threading
import time

import imageio_ffmpeg

import tools
from render.encoder import EXPORTS_DIR

JOB_TIMEOUT = 30 * 60  # kill a stuck download rather than block the queue

# batch-download.md, "owner's decisions": 3 lanes, not 1 or 6. Each lane
# works through its own slice of the submitted list in order, sleeping a
# jittered BATCH_GAP before every item except its first -- a fixed
# interval would itself be a machine signature (the spec's words), and a
# lane that only ever gets one item (any batch of BATCH_LANES or fewer)
# never sleeps at all, which is what keeps a single download's timing
# exactly what it was before batching existed.
BATCH_LANES = 3
BATCH_GAP = (8, 20)

_PROGRESS_RE = re.compile(r"^SV\s+([0-9]+(?:\.[0-9]+)?)%")


class DownloadError(Exception):
    """Carries a plain-English message safe to show the operator, plus a
    `reason` from ERROR_REASONS below — one of OUR words, so app.py can
    report why a download failed without ever forwarding yt-dlp's text."""

    def __init__(self, message, reason="unknown"):
        Exception.__init__(self, message)
        self.reason = reason


# The only vocabulary that ever leaves the machine about a failed
# download. yt-dlp's stderr names the video ("[youtube] S9IJ1GgAAxE: Sign
# in to confirm..."), so the raw text stays local in download.log and
# analytics get the category alone (stats.py's rule: never user content).
ERROR_REASONS = ("setup", "bot_check", "private", "age", "unavailable",
                 "network", "unknown")

_REASON_MESSAGES = {
    "bot_check": ("YouTube is asking this computer to prove it isn't a "
                  "robot, so it refused the download. That is temporary — "
                  "wait a few minutes and try again."),
    "private": ("That video is private or needs a sign-in, so it can't "
                "be downloaded."),
    "age": "That video is age-restricted, so it can't be downloaded.",
    "unavailable": "That video isn't available.",
    "network": ("Couldn't reach YouTube — check the internet connection "
                "and try again."),
    "unknown": ("The download failed. YouTube may have changed something "
                "— the downloader updates itself daily, so try again "
                "tomorrow."),
}


def classify_error(stderr):
    """Sort yt-dlp's stderr into one ERROR_REASONS word.

    Matched case-insensitively on a handful of substrings rather than an
    exact string: yt-dlp's own wording shifts release to release faster
    than a hand-maintained exact match could keep up, and it self-updates
    daily (tools.update_ytdlp), so an exact match would go stale fast.
    """
    text = (stderr or "").lower()
    if is_bot_check(text):
        # Checked before "sign in": YouTube's wording is "Sign in to
        # confirm you're not a bot", and v1.29.1 read that as "private".
        return "bot_check"
    # Age before private: YouTube's age wall says "Sign in to confirm
    # your age", which the "sign in" test below would otherwise swallow.
    # Matched on the specific phrases, not a bare "age" — that substring
    # also lives inside "message", "page" and "storage".
    if ("age-restricted" in text or "age restricted" in text
            or "confirm your age" in text):
        return "age"
    if "private video" in text or "sign in" in text:
        return "private"
    if "unavailable" in text or "removed" in text:
        return "unavailable"
    if "urlopen error" in text or "timed out" in text or "network" in text:
        return "network"
    return "unknown"


def format_args(fmt):
    """The yt-dlp flags that differ between mp4 and mp3. Pure, so smoke
    can assert on them without ever running yt-dlp."""
    if fmt == "mp3":
        return ["-f", "ba/b", "-x", "--audio-format", "mp3",
                "--audio-quality", "0"]
    # mp4 (default): H.264 first, then up to 1080p — ProPresenter's
    # AVFoundation plays H.264 but not VP9/AV1 packaged in an mp4.
    return ["-f", "bv*+ba/b", "-S", "codec:h264:m4a,res:1080",
            "--merge-output-format", "mp4"]


def parse_progress(line):
    """int 0..100 from a `--progress-template` line shaped
    "SV  42.3%", or None for anything else (yt-dlp's other --newline
    chatter, warnings, the final blank line, ...)."""
    match = _PROGRESS_RE.match((line or "").strip())
    if not match:
        return None
    try:
        return max(0, min(100, int(float(match.group(1)))))
    except ValueError:
        return None


def is_bot_check(stderr):
    """YouTube's intermittent "prove you're not a bot" refusal. Seen on
    the owner's home network twice in three minutes and gone ten minutes
    later with the identical request, so it is worth a pause and a retry
    before it becomes the operator's problem.

    Must require the word "bot": v1.30.0 matched a bare "confirm you",
    which YouTube's age wall ("Sign in to confirm your age") also
    contains — so an age-restricted video was retried twice for nothing
    and then shown the wrong sentence.
    """
    text = (stderr or "").lower()
    return "not a bot" in text or ("confirm" in text and "bot" in text)


# Pauses before each retry of a bot-checked download. Two retries, ~1 min
# total: enough to outlast the short-lived flag without leaving a
# volunteer staring at a stalled bar during a service.
BOT_CHECK_DELAYS = (15, 45)


def friendly_error(stderr):
    """The sentence the operator sees, for the same stderr classify_error
    sorts. One classifier feeds both, so the message and the reported
    reason can never drift apart."""
    return _REASON_MESSAGES[classify_error(stderr)]


def _build_args(ytdlp_path, url, fmt, deno_path, ffmpeg_path):
    # The exact FILE, not its directory: imageio_ffmpeg's binary isn't
    # literally named "ffmpeg" (e.g. "ffmpeg-macos-aarch64-v7.1"), so a
    # directory-form --ffmpeg-location can't auto-discover it by name and
    # yt-dlp silently has no usable ffmpeg at all — proven live: a merge
    # (mp4) left two unmerged fragments with no error, and an extraction
    # (mp3) hard-failed with "ffprobe and ffmpeg not found", on a machine
    # with no OTHER ffmpeg on PATH to fall back to. Pointing at the file
    # itself fixed both, confirmed with PATH stripped of every system
    # ffmpeg/ffprobe.
    args = [ytdlp_path, url, "--no-playlist",
            "--js-runtimes", "deno:{0}".format(deno_path),
            "--ffmpeg-location", ffmpeg_path,
            # No --restrict-filenames: it flattens a title to ASCII
            # underscores ("Fred_again.._-_Delilah_...-Cl6Rz1Uvi2M.mp4"),
            # which is unreadable in a ProPresenter media bin. yt-dlp still
            # strips characters the filesystem cannot take.
            "--newline", "--no-warnings",
            "--socket-timeout", "30", "--retries", "3"]
    args += format_args(fmt)
    args += ["--progress-template", "download:SV %(progress._percent_str)s",
             "--print", "after_move:filepath",
             # Title only — the id was there for uniqueness, but it is
             # noise to a human scanning a media folder. Two different
             # videos sharing a title is rare; --no-overwrites below is
             # what makes pasting the SAME video in twice a no-op skip
             # instead of a silent re-download.
             "-o", os.path.join(EXPORTS_DIR, "%(title).80s.%(ext)s"),
             # batch-download.md's "skip already downloaded". Confirmed
             # empirically (running the same link twice): with --print
             # after_move:filepath in play, yt-dlp's stdout is BYTE
             # IDENTICAL on a skip vs a fresh save — one filepath line,
             # exit 0, nothing on stderr either — so there is no text
             # here to branch on. What --no-overwrites does do is leave
             # an existing file's mtime untouched (also confirmed live:
             # identical mtime before and after a repeat run), which is
             # what _download_one below checks instead.
             "--no-overwrites"]
    return args


def _download_one(url, fmt, ytdlp_path, deno_path, ffmpeg_path):
    """One link of a batch. Never raises — a bad link must not sink the
    other 19 (batch-download.md), so every failure mode, anticipated or
    not, comes back as a {"state": "failed", ...} item rather than an
    exception. Byte progress isn't reported here at all: the batch bar
    tracks completed ITEMS, not bytes (see download_video), so `_run_once`
    gets a progress_cb that throws its percentage away.
    """
    item_started = time.time()
    try:
        args = _build_args(ytdlp_path, url, fmt, deno_path, ffmpeg_path)

        # A bot-checked attempt is retried after a pause (BOT_CHECK_DELAYS,
        # unchanged and still per-item); any other failure is final on the
        # first try. attempt_started is captured AFTER the retry ladder's
        # own sleeping, right before the attempt that actually succeeds —
        # what the mtime check below needs is "was this file touched by
        # the attempt that produced it", not by an earlier, failed one.
        code, result_path, stderr_text = None, None, ""
        for delay in (0,) + BOT_CHECK_DELAYS:
            if delay:
                tools.log_line(
                    "bot check — retrying in {0}s".format(delay))
                time.sleep(delay)
            attempt_started = time.time()
            code, result_path, stderr_text = _run_once(
                args, False, lambda _pct: None)
            if code == 0 and result_path is not None:
                break
            if not is_bot_check(stderr_text):
                break

        seconds = round(time.time() - item_started, 1)

        if code == 0 and result_path is not None:
            # Empirically (batch-download.md): with --no-overwrites and
            # --print after_move:filepath, yt-dlp prints the identical
            # single filepath line whether this run just saved the file
            # or skipped an existing one — nothing here to parse. But
            # --no-overwrites does not touch an existing file, so a
            # result whose mtime predates this attempt was never written
            # by it (confirmed live: identical mtime before/after a
            # repeat run). The 1s slack absorbs filesystem timestamp
            # rounding, not clock error — a real download takes far
            # longer than a second either way.
            try:
                touched = (os.path.getmtime(result_path)
                          >= attempt_started - 1.0)
            except OSError:
                touched = True  # file vanished under us; assume ours
            filename = os.path.basename(result_path)
            state = "saved" if touched else "skipped"
            return {"link": url, "state": state, "filename": filename,
                    "message": None, "reason": None, "seconds": seconds}

        tools.log_line(
            "download failed (exit {0}):\n{1}".format(code, stderr_text))
        return {"link": url, "state": "failed", "filename": None,
                "message": friendly_error(stderr_text),
                "reason": classify_error(stderr_text),
                "seconds": seconds}
    except Exception as exc:  # one item's bug must not sink the batch
        tools.log_line("download item crashed: {0}: {1}".format(
            exc.__class__.__name__, exc))
        return {"link": url, "state": "failed", "filename": None,
                "message": _REASON_MESSAGES["unknown"], "reason": "unknown",
                "seconds": round(time.time() - item_started, 1)}


def _run_lane(assignments, fmt, ytdlp_path, deno_path, ffmpeg_path,
             on_settled):
    """One lane's items, strictly in the order they were assigned. The
    jittered BATCH_GAP sits before every item except the lane's first —
    a fixed interval would itself be a machine signature; the jitter is
    the point (batch-download.md)."""
    for position, (index, url) in enumerate(assignments):
        if position > 0:
            time.sleep(random.uniform(*BATCH_GAP))
        result = _download_one(url, fmt, ytdlp_path, deno_path, ffmpeg_path)
        on_settled(index, result)


def download_video(options, progress_cb):
    """options["urls"] is always a list (validation.py's one downstream
    shape) of at least one link. Downloads up to BATCH_LANES at a time
    and returns {"filename": <last SAVED item's basename, or None>,
    "items": [...]} in the SUBMITTED order, even though the work itself
    is concurrent (docs/specs/batch-download.md).
    """
    urls = options["urls"]
    fmt = options.get("format", "mp4")
    total = len(urls)

    ytdlp_path, deno_path = tools.binary_paths()
    # Whether THIS call has to fetch anything decides how the progress
    # bar is split: 0-30% tools setup + 30-100% items, or straight
    # 0-100% items when both tools were already there. Fetched (and
    # update-checked) ONCE for the whole batch, never per item — a
    # 20-link batch must not re-verify two sha256s twenty times.
    needs_fetch = not (os.path.isfile(ytdlp_path)
                       and os.path.isfile(deno_path))

    def _tools_progress(pct):
        if needs_fetch:
            progress_cb(int(pct * 0.3))

    try:
        tools.ensure_tools(progress_cb=_tools_progress)
    except tools.ToolsError as exc:
        # Fetching yt-dlp/Deno failed, not any one download — a whole-
        # batch failure (nothing was even attempted), worth telling
        # apart in analytics (v1.29.1's certificate bug looked exactly
        # like this and nothing reported it).
        raise DownloadError(str(exc), "setup") from exc
    tools.update_ytdlp()

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(EXPORTS_DIR, exist_ok=True)

    results = [None] * total
    done_count = [0]
    progress_lock = threading.Lock()

    def _on_settled(index, result):
        # The bar tracks completed items, not bytes (batch-download.md)
        # — a per-item byte percentage would have to be reconciled across
        # up to BATCH_LANES concurrent downloads for no real benefit.
        results[index] = result
        with progress_lock:
            done_count[0] += 1
            fraction = int(done_count[0] * 100 / total)
        progress_cb(30 + int(fraction * 0.7) if needs_fetch else fraction)

    # Round-robin, not contiguous blocks: with BATCH_LANES=3 every lane
    # in a batch of 3-or-fewer gets exactly one item, so none of them
    # ever hits the "not my first" branch below — this is what makes a
    # one-item batch (today's single-link path) sleep exactly as much as
    # it always did: not at all.
    lanes = [[] for _ in range(BATCH_LANES)]
    for i, url in enumerate(urls):
        lanes[i % BATCH_LANES].append((i, url))
    lanes = [lane for lane in lanes if lane]

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(lanes)) as pool:
        futures = [pool.submit(_run_lane, lane, fmt, ytdlp_path, deno_path,
                               ffmpeg_path, _on_settled)
                  for lane in lanes]
        for future in futures:
            future.result()

    progress_cb(100)

    filename = None
    for item in results:
        if item["state"] == "saved":
            filename = item["filename"]
    return {"filename": filename, "items": results}


def _run_once(args, needs_fetch, progress_cb):
    """One yt-dlp run. Returns (exit code, result path or None, stderr
    tail) — the caller decides whether a failure is worth retrying."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)

    printed_lines = []
    stderr_tail = collections.deque(maxlen=40)

    def _pump_stdout():
        for line in proc.stdout:
            line = line.rstrip("\n")
            printed_lines.append(line)
            pct = parse_progress(line)
            if pct is None:
                continue
            if needs_fetch:
                progress_cb(30 + int(pct * 0.7))
            else:
                progress_cb(pct)

    def _pump_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip("\n"))

    out_thread = threading.Thread(target=_pump_stdout, daemon=True)
    err_thread = threading.Thread(target=_pump_stderr, daemon=True)
    out_thread.start()
    err_thread.start()

    try:
        proc.wait(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    out_thread.join(timeout=5)
    err_thread.join(timeout=5)

    stderr_text = "\n".join(stderr_tail)

    exports_root = os.path.realpath(EXPORTS_DIR)
    result_path = None
    for line in reversed(printed_lines):
        candidate = os.path.realpath(line.strip())
        if (candidate.startswith(exports_root + os.sep)
                and os.path.isfile(candidate)):
            result_path = candidate
            break
    return proc.returncode, result_path, stderr_text
