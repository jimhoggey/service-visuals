"""Shared HTTP-request-body plumbing used by every JSON-accepting route.

Split out of app.py (docs/specs/refactor-organise.md) — every POST route
that reads a JSON body repeated the same "is it too big / is it a JSON
object" prologue nine times over. json_body() is that prologue, kept
byte-identical to every call site it replaces.
"""

from flask import jsonify, request

# Background-image uploads need headroom, so the global cap is generous; the
# /api/render route (and friends) enforces its own small JSON limit so a huge
# JSON number can't pin a core (quadratic int parsing on Python 3.9).
MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def json_body(message):
    """Enforce the small JSON size cap and, when `message` is given,
    require the body to decode to a JSON object.

    Returns (data, error_response). Callers do:

        data, error = json_body("caller's message")
        if error:
            return error

    `message=None` is for the couple of routes that only need the size
    guard and never actually read a body (board preview/export) — passing
    None skips the get_json()/isinstance check entirely, so a bodyless
    POST to those routes keeps working exactly as it always did.
    """
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return None, (jsonify({"error": "Request too large."}), 413)
    if message is None:
        return None, None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": message}), 400)
    return data, None
