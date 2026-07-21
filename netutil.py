"""Shared HTTPS setup for outbound calls (GitHub, OpenRouter).

Why this exists: a PyInstaller bundle carries its own Python, but NOT the CA
certificates that Python's ssl module expects to find on disk. Running from
source on macOS this goes unnoticed — the system falls back to
/private/etc/ssl/cert.pem — but the released binaries (built in CI with a
different Python) look for a path that doesn't exist inside the bundle, so
every HTTPS request fails with URLError. That silently broke the update check,
the self-update download and the OpenRouter call in shipped builds.

Fix: always verify against certifi's bundled CA file, which IS packaged with
the app (see --collect-all certifi in .github/workflows/build.yml).
"""

import ssl
import urllib.request

_context = None


def ssl_context():
    """A verifying SSL context that works inside a frozen bundle."""
    global _context
    if _context is None:
        try:
            import certifi
            _context = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            # Never hard-fail: fall back to the platform default. Running from
            # source this is normally fine.
            _context = ssl.create_default_context()
    return _context


def urlopen(req, timeout=30):
    """urllib.request.urlopen with certificates that work when frozen."""
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
