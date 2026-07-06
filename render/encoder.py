"""FrameEncoder: pipes raw RGB frames from Pillow into the bundled ffmpeg.

Uses the static ffmpeg binary shipped by the imageio-ffmpeg pip package, so
nothing needs to be installed system-wide. Output is H.264 / yuv420p /
+faststart MP4 at a constant 30 fps — the most ProPresenter-compatible combo.

Renderers may feed frames at a lower input fps (e.g. 1 fps for a digits-only
timer); ffmpeg duplicates frames up to the 30 fps output.
"""

import os
import re
import subprocess
import sys
import tempfile
import time

import imageio_ffmpeg

WIDTH = 1920
HEIGHT = 1080
OUTPUT_FPS = 30


def _default_exports_dir():
    """Single source of truth for where finished MP4s land.

    Running from source: <repo>/exports. Packaged app (PyInstaller): a
    visible folder in the user's Documents, since the bundle dir is not a
    sane place for user files. Overridable via SERVICE_VISUALS_EXPORTS.
    """
    override = os.environ.get("SERVICE_VISUALS_EXPORTS")
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.join(
            os.path.expanduser("~"), "Documents", "Service Visuals")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")


EXPORTS_DIR = _default_exports_dir()

# Uploaded background images live in a temp dir (they are inputs, not outputs,
# so they don't belong next to the user's finished MP4s). Cleared by the OS.
UPLOADS_DIR = os.path.join(tempfile.gettempdir(), "service-visuals-uploads")


def export_path(prefix, descriptor):
    """Build a unique, filesystem-safe path in exports/.

    e.g. export_path("timer", "5m00s_ring") ->
         .../exports/timer_5m00s_ring_20260704-103000.mp4
    """
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    descriptor = re.sub(r"[^A-Za-z0-9_-]+", "-", descriptor).strip("-")[:60]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{prefix}_{descriptor}_{stamp}"
    path = os.path.join(EXPORTS_DIR, base + ".mp4")
    n = 2
    while os.path.exists(path):
        path = os.path.join(EXPORTS_DIR, f"{base}_{n}.mp4")
        n += 1
    return path


class EncoderError(RuntimeError):
    pass


class FrameEncoder:
    """Context manager that encodes PIL RGB frames to an MP4 file.

    Usage:
        with FrameEncoder("/path/out.mp4", input_fps=10) as enc:
            enc.add_frame(pil_image)   # 1920x1080, mode RGB
    """

    def __init__(self, out_path, input_fps, width=WIDTH, height=HEIGHT):
        self.out_path = out_path
        # Encode to a temporary *.part name and os.replace() it into place
        # only on success, so a killed process (Ctrl+C mid-render) can never
        # leave a playable-but-truncated MP4 under the final filename.
        self._tmp_path = out_path + ".part"
        self.width = width
        self.height = height
        self.frames_written = 0
        # ffmpeg writes progress chatter to stderr; buffer it in a temp file
        # so the pipe can never fill up and deadlock us.
        self._stderr = tempfile.TemporaryFile()
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}",
            "-r", str(input_fps),
            "-i", "-",
            "-an",
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(OUTPUT_FPS),
            "-preset", "veryfast",
            "-crf", "19",
            "-movflags", "+faststart",
            "-f", "mp4",          # .part suffix hides the extension
            self._tmp_path,
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=self._stderr,
        )

    def add_frame(self, image):
        if image.size != (self.width, self.height):
            raise EncoderError(
                f"frame is {image.size}, expected {(self.width, self.height)}")
        if image.mode != "RGB":
            image = image.convert("RGB")
        try:
            self._proc.stdin.write(image.tobytes())
        except BrokenPipeError:
            raise EncoderError(
                "ffmpeg exited early: " + self._stderr_tail()) from None
        self.frames_written += 1

    def close(self):
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()
        code = self._proc.wait()
        tail = self._stderr_tail()
        self._stderr.close()
        if code != 0:
            # Don't leave a truncated MP4 lying around for the user to import.
            if os.path.exists(self._tmp_path):
                os.unlink(self._tmp_path)
            raise EncoderError(f"ffmpeg failed (exit {code}): {tail}")
        os.replace(self._tmp_path, self.out_path)

    def abort(self):
        """Kill ffmpeg and delete partial output (used on renderer errors)."""
        try:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except OSError:
                pass  # flushing into a dying ffmpeg can raise BrokenPipeError
            self._proc.kill()
            self._proc.wait()
        finally:
            self._stderr.close()
            if os.path.exists(self._tmp_path):
                os.unlink(self._tmp_path)

    def _stderr_tail(self, limit=800):
        try:
            self._stderr.seek(0)
            data = self._stderr.read().decode("utf-8", "replace")
            return data[-limit:].strip()
        except ValueError:  # already closed
            return ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False
