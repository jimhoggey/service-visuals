"""Self-update: download the latest release asset and swap it in place.

A running binary cannot overwrite itself, so install goes: download zip ->
stage the new app in a temp dir -> spawn a tiny detached helper script ->
exit. The helper waits for this process to die, replaces the install
(the .app bundle on macOS, the .exe on Windows), relaunches it, and
deletes itself. macOS zips are extracted with `ditto -x -k` because
zipfile would drop the symlinks and exec bits inside the bundle.
"""

import os
import shlex
import subprocess
import sys
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


def spawn_replacer(staged, install, workdir, pid=None):
    """Write and detach the helper that performs the swap after we exit."""
    pid = os.getpid() if pid is None else pid

    if sys.platform == "win32":
        # Wait by polling the PID with tasklist — NOT by trying to delete the
        # exe. The old script used "del the exe until it succeeds" as its
        # exit detector, which meant the app was already gone by the time the
        # move ran: any failure there (antivirus lock, disk full) left the
        # user with no app at all. Now the old exe is renamed aside, the new
        # one moved in, and the backup removed only once that succeeded —
        # restoring it otherwise. Same guarantee as the macOS branch below.
        # No parenthesized blocks: %n% inside ( ) would expand at parse time.
        # `ping` is the sleep that still works without a console window.
        script = os.path.join(workdir, "sv-update.bat")
        backup = install + ".old"
        with open(script, "w") as f:
            f.write("\r\n".join([
                "@echo off",
                "set /a n=0",
                ":wait",
                'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
                "if errorlevel 1 goto swap",
                "set /a n+=1",
                "if %n% geq 240 goto done",
                "ping -n 2 127.0.0.1 >nul",
                "goto wait",
                ":swap",
                'del /f /q "{backup}" >nul 2>&1',
                'move /y "{install}" "{backup}" >nul 2>&1',
                'move /y "{staged}" "{install}" >nul 2>&1',
                'if exist "{install}" goto ok',
                'move /y "{backup}" "{install}" >nul 2>&1',
                "goto done",
                ":ok",
                'del /f /q "{backup}" >nul 2>&1',
                'start "" "{install}"',
                ":done",
                'del "%~f0"',
            ]).format(pid=pid, install=install, staged=staged,
                      backup=backup) + "\r\n")
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(["cmd", "/c", script], close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
        return script

    q_install = shlex.quote(install)
    q_staged = shlex.quote(staged)
    relaunch = ("" if os.environ.get("SERVICE_VISUALS_NO_RELAUNCH")
                else "open {0}".format(q_install))
    # Swap safely: move the old app aside, put the new one in place, and only
    # then delete the backup. If the move fails we restore, so a failed update
    # can never leave the user with no app at all.
    script = os.path.join(workdir, "sv-update.sh")
    with open(script, "w") as f:
        f.write("\n".join([
            "#!/bin/sh",
            "n=0",
            "while kill -0 {pid} 2>/dev/null; do",
            "  sleep 0.5",
            '  n=$((n+1)); [ "$n" -gt 240 ] && exit 1',
            "done",
            'backup={q_install}.old-$$',
            "rm -rf \"$backup\"",
            'if mv {q_install} "$backup"; then',
            "  if mv {q_staged} {q_install}; then",
            '    rm -rf "$backup"',
            "  else",
            '    mv "$backup" {q_install}',
            "  fi",
            "fi",
            relaunch,
            'rm -f "$0"',
        ]).format(pid=pid, q_install=q_install, q_staged=q_staged) + "\n")
    subprocess.Popen(["/bin/sh", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return script
