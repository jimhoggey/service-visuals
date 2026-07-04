"""Desktop entry point for the packaged app.

Starts the render server on a free localhost port and opens the UI in a
native window (pywebview). Closing the window quits the app. If no native
webview is available, falls back to the default browser.

Set SERVICE_VISUALS_HEADLESS=1 to start the server without any window
(used by packaging smoke tests).
"""

import os
import socket
import threading
import webbrowser


def _pick_port(preferred=8765):
    for candidate in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError("no free localhost port")


def main():
    from app import app, prepare_exports_dir
    from werkzeug.serving import make_server

    prepare_exports_dir()
    port = _pick_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    if os.environ.get("SERVICE_VISUALS_HEADLESS"):
        print(url, flush=True)
        threading.Event().wait()
        return

    try:
        import webview
        webview.create_window(
            "Service Visuals", url,
            width=1320, height=900, min_size=(900, 640),
            background_color="#0b0c0e")
        webview.start()
    except Exception:
        # No native webview available — the browser works just as well,
        # but there is no window to close, so the process stays up until
        # the user quits it (dock/taskbar).
        webbrowser.open(url)
        threading.Event().wait()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
