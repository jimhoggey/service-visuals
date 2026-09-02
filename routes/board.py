"""/api/board/* — the scoreboard (points board) feature.

Split out of app.py (docs/specs/refactor-organise.md). All synchronous —
like /api/qr-image, an OCR pass or a board re-render takes well under a
second, so none of this goes near the render queue.
"""

import io
import os
import time

from flask import Blueprint, current_app, jsonify, request, send_file
from PIL import Image

import stats
from backgrounds import _send_png
from render.scoreboard import (BOARDS_DIR, BoardError, OCRUnavailable,
                               add_box, create_board, delete_board,
                               export_board, list_boards, load_board,
                               render_board, save_values)
from validation import (ValidationError, _board_id_field, _board_name_field,
                        _int_field, _values_field)
from webutil import json_body

bp = Blueprint("board", __name__)

# MAX_CONTENT_LENGTH bounds the upload BYTES, not the decoded image: a 12 MB
# JPEG can be 150 megapixels. The board pipeline is pure-Python per-pixel and
# every preview is synchronous, so an unbounded board would pin a worker
# thread for the best part of a minute per keystroke. Anything past the edge
# limit is scaled down; anything past the pixel limit is refused before it is
# decoded at all. A points board is a slide, not a poster.
BOARD_MAX_EDGE = 4000
BOARD_MAX_PIXELS = 60 * 1000 * 1000


def _open_board(board_id):
    """Validate the id and load the board.

    Raises ValidationError for a malformed id; BoardError when the board
    cannot be opened (callers turn that into a 404).
    """
    return load_board(_board_id_field(board_id))


def board_or_error(board_id):
    """Open a board, or hand back the (response, status) pair every
    /api/board/<id>/... route below used to build inline via its own
    try/except _open_board() block (ValidationError -> 400, BoardError ->
    404, same JSON shape every time) — see docs/specs/refactor-organise.md.

    Returns (board, None) on success, or (None, error_response) to return
    straight from the caller.
    """
    try:
        return _open_board(board_id), None
    except ValidationError as exc:
        return None, (jsonify({"error": str(exc)}), 400)
    except BoardError as exc:
        return None, (jsonify({"error": str(exc)}), 404)


@bp.route("/api/board/analyse", methods=["POST"])
def api_board_analyse():
    """Take the user's existing points image ONCE, OCR it, and save it as a
    reusable board. The upload is re-encoded through Pillow first (same as
    /api/upload-bg), which rejects anything that isn't a real image."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "No image was uploaded."}), 400
    try:
        name = _board_name_field(request.form.get("name"))
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        img = Image.open(file.stream)
        # Header only so far — check the size BEFORE decoding it.
        if img.width * img.height > BOARD_MAX_PIXELS:
            return jsonify({"error": (
                "That image is {0}x{1}, which is too large to work with. "
                "Export the board at around {2}px across and try again."
            ).format(img.width, img.height, BOARD_MAX_EDGE)}), 400
        if max(img.size) > BOARD_MAX_EDGE:
            # Lets the JPEG decoder shrink as it decodes, so an oversized
            # export never becomes a full-resolution buffer.
            img.draft("RGB", (BOARD_MAX_EDGE, BOARD_MAX_EDGE))
        img.load()
        img = img.convert("RGB")
        if max(img.size) > BOARD_MAX_EDGE:
            img.thumbnail((BOARD_MAX_EDGE, BOARD_MAX_EDGE), Image.LANCZOS)
    except Exception:
        return jsonify({"error": (
            "That file is not an image we can read (use PNG or JPG).")}), 400

    try:
        board = create_board(img, name)
    except OCRUnavailable as exc:
        return jsonify({"error": str(exc)}), 400
    except BoardError as exc:
        stats.track("ocr_failed")
        return jsonify({"error": str(exc)}), 400
    except Exception:
        # Never let an unexpected error answer this route with HTML — the
        # client only knows how to read {"error": ...}.
        current_app.logger.exception("board analyse failed")
        return jsonify({"error": (
            "That board couldn't be saved. Check there is free disk space "
            "and try again.")}), 500

    board_id = board.get("id") or board.get("board_id")
    found = len(board.get("boxes") or [])
    # Zero found is not a failure any more — the board is kept and the
    # operator draws the numbers in by hand — but it is worth counting.
    stats.track("ocr_failed" if not found else "board_created",
                **({} if not found else {"numbers": found}))
    return jsonify({
        "board_id": board_id,
        "name": board.get("name", name),
        "width": board.get("width"),
        "height": board.get("height"),
        "boxes": board.get("boxes"),
        "values": board.get("values") or {},
    })


@bp.route("/api/board/list")
def api_board_list():
    return jsonify({"boards": list_boards()})


@bp.route("/api/board/<board_id>", methods=["GET"])
def api_board_get(board_id):
    board, error = board_or_error(board_id)
    if error:
        return error
    return jsonify(board)


@bp.route("/api/board/<board_id>/source.png")
def api_board_source(board_id):
    """The untouched original upload, for the UI to draw its box overlay on."""
    try:
        _board_id_field(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    root = os.path.realpath(BOARDS_DIR)
    path = os.path.realpath(os.path.join(root, board_id, "source.png"))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "That board no longer exists."}), 404
    return _send_png(path)


@bp.route("/api/board/<board_id>/values", methods=["POST"])
def api_board_values(board_id):
    data, error = json_body(
        'The request body must be JSON like {"values": {"b0": "400"}}.')
    if error:
        return error

    board, error = board_or_error(board_id)
    if error:
        return error

    try:
        values = _values_field(board, data.get("values"))
        # The name rides along with the numbers rather than needing its own
        # round trip — the UI saves both on the same debounce.
        new_name = (_board_name_field(data["name"])
                    if data.get("name") is not None else None)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        # ONE locked read-modify-write for both, not two: each extra write
        # widens the window in which a second tab's save can be lost.
        updated = save_values(board_id, values, name=new_name)
        # The UI redraws from whatever comes back, so always answer with a
        # board even if save_values only persists and returns nothing.
        if not isinstance(updated, dict):
            updated = load_board(board_id)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(updated)


@bp.route("/api/board/<board_id>/boxes", methods=["POST"])
def api_board_add_box(board_id):
    """Make a number OCR missed editable: {"rect": {x,y,w,h}, "text": "350"}.

    Rect is in source-image pixels (the UI converts from its scaled overlay).
    """
    data, error = json_body(
        'The request body must be JSON like {"rect": {...}, "text": "350"}.')
    if error:
        return error
    rect = data.get("rect")
    if not isinstance(rect, dict):
        return jsonify({"error": "Send the box as {x, y, w, h}."}), 400
    try:
        _board_id_field(board_id)
        clean = {k: _int_field(rect, k, 0, 20000, None, "The box " + k)
                 for k in ("x", "y", "w", "h")}
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "Type the number as it appears on the board."}), 400
    try:
        board = add_box(board_id, clean, text.strip())
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("add box failed")
        return jsonify({"error": (
            "That number couldn't be added. Try drawing the box again.")}), 500
    stats.track("board_number_added")
    return jsonify(board)


@bp.route("/api/board/<board_id>/preview", methods=["POST"])
def api_board_preview(board_id):
    """Re-render the board with its saved values and hand back the PNG, so the
    UI shows the REAL composite (harvested glyphs and all), not a mock-up."""
    _, error = json_body(None)
    if error:
        return error
    # Opened first so that "this board is gone" is a 404 like GET and DELETE,
    # and 400 stays for a request we could not honour (a bad stored value).
    board, error = board_or_error(board_id)
    if error:
        return error
    try:
        img = render_board(board)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.route("/api/board/<board_id>/export", methods=["POST"])
def api_board_export(board_id):
    """Write the finished board into exports/ and return the filename, so the
    UI can offer the usual download / reveal-in-Finder pair."""
    _, error = json_body(None)
    if error:
        return error
    board, error = board_or_error(board_id)
    if error:
        return error
    started = time.time()
    try:
        filename = export_board(board)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 400
    # track_export lives in app.py (analytics stays with the rest of the
    # export-tracking code there); imported here, not at module level, to
    # avoid a circular import at blueprint-registration time.
    from app import track_export
    track_export("scoreboard", started)
    return jsonify({"filename": filename})


@bp.route("/api/board/<board_id>", methods=["DELETE"])
def api_board_delete(board_id):
    try:
        _board_id_field(board_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        delete_board(board_id)
    except BoardError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True})
