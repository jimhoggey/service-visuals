"""The YouTube-download render job (docs/specs/youtube-download.md).

`download_video` is a JobManager job like every renderer in render/ —
`fn(options, progress_cb) -> output filename` — just backed by yt-dlp
(via tools.py) instead of Pillow/ffmpeg frames. Kept at the top level
(not under render/) since nothing here draws a single pixel.
"""

import collections
import os
import re
import subprocess
import threading
import time

import imageio_ffmpeg

import tools
from render.encoder import EXPORTS_DIR

JOB_TIMEOUT = 30 * 60  # kill a stuck download rather than block the queue

_PROGRESS_RE = re.compile(r"^SV\s+([0-9]+(?:\.[0-9]+)?)%")


class DownloadError(Exception):
    """Carries a plain-English message safe to show the operator."""


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
    before it becomes the operator's problem."""
    text = (stderr or "").lower()
    return "not a bot" in text or "confirm you" in text


# Pauses before each retry of a bot-checked download. Two retries, ~1 min
# total: enough to outlast the short-lived flag without leaving a
# volunteer staring at a stalled bar during a service.
BOT_CHECK_DELAYS = (15, 45)


def friendly_error(stderr):
    """Map yt-dlp's stderr onto one of a few plain-English sentences.

    Matched case-insensitively on a handful of substrings rather than an
    exact string: yt-dlp's own wording shifts release to release faster
    than a hand-maintained exact match could keep up, and it self-updates
    daily (tools.update_ytdlp), so an exact match would go stale fast.
    """
    text = (stderr or "").lower()
    if is_bot_check(text):
        # Checked before "sign in": YouTube's wording is "Sign in to
        # confirm you're not a bot", and v1.29.1 read that as "private".
        return ("YouTube is asking this computer to prove it isn't a "
                "robot, so it refused the download. That is temporary — "
                "wait a few minutes and try again.")
    if "private video" in text or "sign in" in text:
        return ("That video is private or needs a sign-in, so it can't "
                "be downloaded.")
    if "age" in text:
        return "That video is age-restricted, so it can't be downloaded."
    if "unavailable" in text or "removed" in text:
        return "That video isn't available."
    if ("urlopen error" in text or "timed out" in text
            or "network" in text):
        return ("Couldn't reach YouTube — check the internet connection "
                "and try again.")
    return ("The download failed. YouTube may have changed something — "
            "the downloader updates itself daily, so try again tomorrow.")


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
            "--restrict-filenames", "--newline", "--no-warnings",
            "--socket-timeout", "30", "--retries", "3"]
    args += format_args(fmt)
    args += ["--progress-template", "download:SV %(progress._percent_str)s",
             "--print", "after_move:filepath",
             "-o", os.path.join(EXPORTS_DIR, "%(title).80s-%(id)s.%(ext)s")]
    return args


def download_video(options, progress_cb):
    fmt = options.get("format", "mp4")
    url = options["url"]

    ytdlp_path, deno_path = tools.binary_paths()
    # Whether THIS call has to fetch anything decides how the progress
    # bar is split: 0-30% tools setup + 30-100% download, or straight
    # 0-100% download when both tools were already there.
    needs_fetch = not (os.path.isfile(ytdlp_path)
                       and os.path.isfile(deno_path))

    def _tools_progress(pct):
        if needs_fetch:
            progress_cb(int(pct * 0.3))

    try:
        tools.ensure_tools(progress_cb=_tools_progress)
    except tools.ToolsError as exc:
        raise DownloadError(str(exc)) from exc
    tools.update_ytdlp()

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    args = _build_args(ytdlp_path, url, fmt, deno_path, ffmpeg_path)

    os.makedirs(EXPORTS_DIR, exist_ok=True)

    # A bot-checked attempt is retried after a pause (BOT_CHECK_DELAYS);
    # any other failure is final on the first try.
    for delay in (0,) + BOT_CHECK_DELAYS:
        if delay:
            tools.log_line("bot check — retrying in {0}s".format(delay))
            time.sleep(delay)
        code, result_path, stderr_text = _run_once(
            args, needs_fetch, progress_cb)
        if code == 0 and result_path is not None:
            progress_cb(100)
            return os.path.basename(result_path)
        if not is_bot_check(stderr_text):
            break

    tools.log_line(
        "download failed (exit {0}):\n{1}".format(code, stderr_text))
    raise DownloadError(friendly_error(stderr_text))


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
