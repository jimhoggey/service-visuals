"""Self-update: download the latest release asset and swap it in place.

A running binary cannot overwrite itself, so install goes: download zip ->
stage the new app in a temp dir -> spawn a tiny detached helper script ->
exit. The helper waits for this process to let go of the install, replaces
it (the .app bundle on macOS, the .exe on Windows), relaunches it, and
deletes itself. macOS zips are extracted with `ditto -x -k` because
zipfile would drop the symlinks and exec bits inside the bundle.

Two invariants the helper must never break, both learned the hard way:

* It ALWAYS relaunches something. An early Windows helper had a timeout path
  that swapped nothing and launched nothing, so the app simply vanished; a
  user sat on v1.14 through four releases while the UI kept saying
  "RESTARTING...".
* It ALWAYS leaves a working app on disk. The old install is renamed aside
  and only deleted once the new one is verified in place, so a failed swap
  costs an update, never the app.

Every run appends to update.log, and `mark_pending`/`take_result` turn a
silent failure into something the next launch can tell the user about.
"""

import json
import ntpath
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

import netutil

ASSET_NAMES = {
    "darwin": "ServiceVisuals-mac.zip",
    "win32": "ServiceVisuals-windows.zip",
}

STAGED_NAMES = {
    "darwin": "Service Visuals.app",
    "win32": "Service Visuals.exe",
}

# Shared with aiassist/scoreboard: outside the app so it survives an update.
CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
LOG_PATH = os.path.join(CONFIG_DIR, "update.log")
PENDING_PATH = os.path.join(CONFIG_DIR, "update-pending.json")

# Leftover renamed-aside installs are swept on launch. Unique per attempt so a
# copy the OS still has open (renaming a running exe succeeds on Windows, so
# the backup is often still executing) can never block the NEXT update by
# sitting undeletable at a fixed name.
BACKUP_SUFFIX = ".sv-old-"


def platform_asset(assets):
    wanted = ASSET_NAMES.get(sys.platform)
    for asset in assets or []:
        if asset.get("name") == wanted:
            return asset
    return None


def install_root():
    """The path the helper replaces.

    macOS: sys.executable is .../Service Visuals.app/Contents/MacOS/<bin>,
    three levels below the bundle. Windows onefile: the exe itself.
    """
    if sys.platform == "darwin":
        return os.path.abspath(os.path.join(sys.executable, "..", "..", ".."))
    return sys.executable


def install_problem():
    """Return a plain-English reason self-update cannot work here, or None.

    macOS runs a freshly-downloaded (quarantined) app from a randomized
    READ-ONLY copy under .../AppTranslocation/ until the user moves it in
    Finder. In that state sys.executable points at a throwaway copy, so the
    swap below would replace the copy and leave the real app untouched — the
    update would look like it worked and change nothing. Detect it and tell
    the user what to do instead of silently doing nothing.
    """
    if not getattr(sys, "frozen", False):
        return ("Self-update only works in the packaged app. "
                "Running from source? Use git pull.")
    root = install_root()
    if "/AppTranslocation/" in root:
        return ("macOS is running Service Visuals from a temporary read-only "
                "copy, so it can't replace itself. Drag Service Visuals into "
                "your Applications folder, open it from there, then update.")
    parent = os.path.dirname(root) or "/"
    if not os.access(parent, os.W_OK):
        return ("Service Visuals can't update itself from this folder "
                "(no permission to write there). Move it to your Applications "
                "folder and try again.")
    return None


def download(url, dest, progress_cb):
    req = urllib.request.Request(url, headers={"User-Agent": "service-visuals"})
    with netutil.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                progress_cb(min(99, int(got * 100 / total)))
    progress_cb(100)


def stage(zip_path, workdir):
    """Extract the zip and return the path of the staged app/exe."""
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-x", "-k", zip_path, workdir], check=True)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(workdir)
    staged = os.path.join(workdir, STAGED_NAMES[sys.platform])
    if not os.path.exists(staged):
        raise RuntimeError("the downloaded update did not contain the app")
    return staged


# ------------------------------------------------------------------ logging
# The helper runs after this process is gone, so its own log is the only
# evidence of what happened. Kept in the config dir (not %TEMP%) so it is
# still there on the next launch, and truncated so it can't grow forever.

MAX_LOG_BYTES = 64 * 1024


def _log(message):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            os.unlink(LOG_PATH)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a") as f:
            f.write("[{0}] {1}\n".format(stamp, message))
    except OSError:
        pass


def read_log(limit=4000):
    try:
        with open(LOG_PATH) as f:
            return f.read()[-limit:]
    except OSError:
        return ""


# --------------------------------------------------------- pending marker
# An update that fails after this process exits is invisible: the app just
# reopens at the old version, which is exactly what happened on Windows. Drop
# a marker before exiting; the next launch compares it with the version that
# actually booted and can say so out loud.

def mark_pending(current, expected):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PENDING_PATH, "w") as f:
            json.dump({"from": current, "expect": expected, "at": time.time()}, f)
    except OSError:
        pass


def _clear_pending():
    try:
        os.unlink(PENDING_PATH)
    except OSError:
        pass


def take_result(current):
    """Consume the marker left by the last install; report what it says.

    Returns None normally, {"ok": True, "version": v} after a successful
    update, or {"ok": False, ...} when the app came back on the SAME version
    it tried to leave — the silent failure that stranded a user on v1.14.
    Reads once and clears, so the user is told exactly once.
    """
    try:
        with open(PENDING_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    _clear_pending()
    if not isinstance(data, dict):
        return None
    expected = str(data.get("expect") or "").lstrip("v")
    if not expected:
        return None

    def parts(tag):
        try:
            return tuple(int(p) for p in str(tag).lstrip("v").split(".")[:3])
        except ValueError:
            return ()

    if parts(current) >= parts(expected):
        return {"ok": True, "version": current}
    return {"ok": False, "expected": expected, "current": current,
            "reason": failure_reason(), "log": read_log(1200)}


def failure_reason():
    """One coarse word for why the last swap failed, from the helper's log.

    This is the only thing about a failed update that the anonymous usage
    counter is allowed to send: a category, never a path or a message.
    """
    tail = read_log(2000)
    if "stayed locked" in tail:
        return "locked"
    if "could not install the new" in tail:
        return "install"
    if "could not move the old" in tail:
        return "locked"
    if "[helper]" not in tail:
        return "helper_never_ran"
    return "unknown"


def sweep_backups():
    """Delete installs renamed aside by earlier updates, best effort."""
    try:
        root = install_root()
    except Exception:
        return
    folder = os.path.dirname(root) or "."
    base = os.path.basename(root) + BACKUP_SUFFIX
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.startswith(base):
            continue
        victim = os.path.join(folder, name)
        try:
            if os.path.isdir(victim):
                import shutil
                shutil.rmtree(victim, ignore_errors=True)
            else:
                os.unlink(victim)
        except OSError:
            pass          # still running or still locked; next launch retries


# ------------------------------------------------------------ swap helpers

def _win_script(staged, install, workdir, backup, log_path):
    """Batch that swaps the exe once Windows lets go of it.

    Readiness is tested by ATTEMPTING THE RENAME, not by watching a PID.
    The PID is the wrong question here: a PyInstaller onefile app is two
    processes — a bootloader parent plus the Python child that calls
    os._exit — so os.getpid() names the child while the parent may still
    hold the image open, and Defender routinely keeps a freshly-extracted
    exe open for a few seconds as well. Renaming is the operation we
    actually need, so just retry it until it succeeds. That also removes
    every dependence on how `tasklist` formats or localises its output,
    which the previous helper parsed with `find`.

    Every exit path reaches :launch, so the user always gets their app back.
    """
    quiet = " >nul 2>&1"
    lines = [
        "@echo off",
        "setlocal enableextensions",
        'set "LOG=' + log_path + '"',
        'set "INSTALL=' + install + '"',
        'set "STAGED=' + staged + '"',
        'set "BACKUP=' + backup + '"',
        'set "WORKDIR=' + workdir + '"',
        'set "HOME_DIR=' + (ntpath.dirname(install) or ".") + '"',
        '>>"%LOG%" echo [helper] swapping "%INSTALL%"',
        # Windows allows renaming a RUNNING exe, so the rename can succeed
        # before the old app has finished closing; give its 1.5 s exit timer
        # room so the new window never opens beside the old one.
        "ping -n 4 127.0.0.1" + quiet,
        "set /a n=0",
        # --- wait for the old exe to become renameable -------------------
        ":try",
        'move /y "%INSTALL%" "%BACKUP%"' + quiet,
        'if not exist "%INSTALL%" goto moved',
        "set /a n+=1",
        "if %n% geq 40 goto giveup",
        "ping -n 2 127.0.0.1" + quiet,
        "goto try",
        # --- install the new exe -----------------------------------------
        ":moved",
        '>>"%LOG%" echo [helper] old exe moved aside on attempt %n%',
        'move /y "%STAGED%" "%INSTALL%"' + quiet,
        'if not exist "%INSTALL%" goto restore',
        '>>"%LOG%" echo [helper] new exe in place',
        "goto launch",
        # --- new exe would not move in: put the old one back -------------
        ":restore",
        '>>"%LOG%" echo [helper] ERROR could not install the new exe, restoring the old one',
        'move /y "%BACKUP%" "%INSTALL%"' + quiet,
        "goto launch",
        # --- old exe never unlocked: keep it, abandon the update ---------
        ":giveup",
        '>>"%LOG%" echo [helper] ERROR "%INSTALL%" stayed locked for 40 tries, update abandoned',
        # --- always leave the user with a running app --------------------
        # `start` is the normal way; if it reports failure (it can from a
        # console-less cmd), hand the exe to Explorer, which launches it
        # exactly as a double-click would.
        ":launch",
        'if not exist "%INSTALL%" goto cleanup',
        'start "" /D "%HOME_DIR%" "%INSTALL%"',
        'if errorlevel 1 explorer.exe "%INSTALL%"',
        '>>"%LOG%" echo [helper] relaunched',
        ":cleanup",
        'rmdir /s /q "%WORKDIR%"' + quiet,
        'del "%~f0"' + quiet,
    ]
    return "\r\n".join(lines) + "\r\n"


def _posix_script(staged, install, workdir, backup, log_path, pid):
    q_install = shlex.quote(install)
    q_staged = shlex.quote(staged)
    q_backup = shlex.quote(backup)
    q_work = shlex.quote(workdir)
    q_log = shlex.quote(log_path)
    relaunch = ("true" if os.environ.get("SERVICE_VISUALS_NO_RELAUNCH")
                else "open " + q_install)
    # Assembled by concatenation, not str.format — the shell function braces
    # below would otherwise be read as format fields.
    lines = [
        "#!/bin/sh",
        "say() { echo \"[helper] $1\" >> " + q_log + "; }",
        "n=0",
        "while kill -0 " + str(pid) + " 2>/dev/null; do",
        "  sleep 0.5",
        "  n=$((n+1)); [ \"$n\" -gt 240 ] && break",
        "done",
        "if mv " + q_install + " " + q_backup + " 2>/dev/null; then",
        "  if mv " + q_staged + " " + q_install + " 2>/dev/null; then",
        "    say \"new app in place\"",
        "    rm -rf " + q_backup,
        "  else",
        "    say \"ERROR could not install the new app, restoring the old one\"",
        "    mv " + q_backup + " " + q_install,
        "  fi",
        "else",
        "  say \"ERROR could not move the old app aside, update abandoned\"",
        "fi",
        relaunch,
        "rm -rf " + q_work,
        "rm -f \"$0\"",
    ]
    return "\n".join(lines) + "\n"


def spawn_replacer(staged, install, workdir, pid=None):
    """Write and detach the helper that performs the swap after we exit."""
    pid = os.getpid() if pid is None else pid
    os.makedirs(CONFIG_DIR, exist_ok=True)
    backup = install + BACKUP_SUFFIX + str(pid)
    # The helper lives OUTSIDE workdir: it deletes workdir on the way out,
    # and a batch file cannot safely delete the directory it is running from.
    handle, script = tempfile.mkstemp(
        prefix="sv-update-",
        suffix=".bat" if sys.platform == "win32" else ".sh")
    os.close(handle)
    _log("staged {0!r} -> {1!r}".format(staged, install))

    if sys.platform == "win32":
        with open(script, "w") as f:
            f.write(_win_script(staged, install, workdir, backup, LOG_PATH))
        DETACHED_PROCESS = 0x00000008
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        # The helper must outlive us. If some launcher put this app in a job
        # object that kills children on exit, ask to break away; that flag is
        # refused when the job forbids it, so fall back to a plain detach.
        for flags in (DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB,
                      DETACHED_PROCESS):
            try:
                subprocess.Popen(["cmd", "/c", script], close_fds=True,
                                 creationflags=flags)
                break
            except OSError:
                if flags == DETACHED_PROCESS:
                    raise
        _log("helper spawned")
        return script

    with open(script, "w") as f:
        f.write(_posix_script(staged, install, workdir, backup, LOG_PATH, pid))
    subprocess.Popen(["/bin/sh", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return script
