"""/api/backgrounds/* — the timer background image library
(docs/specs/timer-backgrounds.md).

Split out of app.py (docs/specs/refactor-organise.md). All synchronous —
like /api/qr-image, adding or re-encoding one image takes well under a
second, so none of this goes near the render queue.
"""

import os
import uuid

from flask import Blueprint, jsonify, request
from PIL import Image

from backgrounds import (
    BACKGROUNDS_DIR, BACKGROUND_ID_RE, BACKGROUND_LIBRARY_MAX,
    _background_path, _send_png)
from render.timer import cover_fit_image, prepare_background
from validation import ValidationError, _background_id_field

bp = Blueprint("backgrounds", __name__)


@bp.route("/api/backgrounds", methods=["POST"])
def api_backgrounds_add():
    """Add one image to the background library: multipart `image` ->
    {"id": "<16 hex>"}. Re-encoded through Pillow (rejects anything that
    isn't a real image, same wording as /api/upload-bg) and cover-fit to
    1920x1080 once here so the renderer never has to re-fit it."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "No image was uploaded."}), 400
    try:
        img = Image.open(file.stream)
        img.load()
        img = cover_fit_image(img)
    except Exception:
        return jsonify({"error": (
            "That file is not an image we can read (use PNG or JPG).")}), 400

    os.makedirs(BACKGROUNDS_DIR, exist_ok=True)
    existing = [n for n in os.listdir(BACKGROUNDS_DIR) if n.endswith(".png")]
    if len(existing) >= BACKGROUND_LIBRARY_MAX:
        return jsonify({"error": (
            "You have {0} background images saved — delete some before "
            "adding more.").format(BACKGROUND_LIBRARY_MAX)}), 400

    image_id = uuid.uuid4().hex[:16]
    img.save(os.path.join(BACKGROUNDS_DIR, image_id + ".png"), format="PNG")
    return jsonify({"id": image_id})


@bp.route("/api/backgrounds", methods=["GET"])
def api_backgrounds_list():
    """{"images": [{"id", "added"}]}, newest first."""
    images = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for name in os.listdir(BACKGROUNDS_DIR):
            if not name.endswith(".png"):
                continue
            image_id = name[:-4]
            if not BACKGROUND_ID_RE.fullmatch(image_id):
                continue  # not one of ours — never let a stray file in
            path = os.path.join(BACKGROUNDS_DIR, name)
            try:
                added = os.path.getmtime(path)
            except OSError:
                continue
            images.append({"id": image_id, "added": added})
    images.sort(key=lambda item: item["added"], reverse=True)
    return jsonify({"images": images})


@bp.route("/api/backgrounds/<image_id>", methods=["GET"])
def api_backgrounds_get(image_id):
    """?blur=1 (anything else is ignored, same as omitting it) returns the
    image with the renderer's own blur applied (prepare_background, same
    BG_BLUR_RADIUS render/timer.py uses for a real render) instead of the
    plain original — the preview canvas draws this directly rather than
    trying to reproduce the blur with a CSS/canvas filter, which the
    embedded webview does not reliably apply (see docs/specs/timer-
    backgrounds.md). The blurred variant is cached on disk next to the
    original so repeat requests (every redraw while BLUR is on) are an
    instant file read, not a re-blur."""
    try:
        _background_id_field(image_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    path = _background_path(image_id)
    if path is None or not os.path.isfile(path):
        return jsonify({"error": "That background image no longer exists."}
                       ), 404
    if request.args.get("blur") == "1":
        blur_path = _background_path(image_id, ".blur.png")
        if blur_path is None:
            return jsonify({"error": "That is not a background image we "
                                      "know about."}), 400
        if not os.path.isfile(blur_path):
            # dim=0 here: DIM is drawn separately (a plain black fillRect
            # in the JS preview, Image.blend in the renderer) — this route
            # only ever needs to hand back blur, never a dimmed image.
            blurred = prepare_background(path, 0, True)
            blurred.save(blur_path, format="PNG")
        return _send_png(blur_path)
    return _send_png(path)


@bp.route("/api/backgrounds/<image_id>", methods=["DELETE"])
def api_backgrounds_delete(image_id):
    """Remove one image from the library. A saved timer that still refers
    to this id is left alone — the render just errors with the missing-
    image message the next time it is used, same as a board that outlives
    a deleted number: nothing here needs to know about saved forms."""
    try:
        _background_id_field(image_id)
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    stuck = False
    for target in (_background_path(image_id),
                   _background_path(image_id, ".blur.png")):
        if target is None or not os.path.isfile(target):
            continue
        try:
            os.unlink(target)
        except OSError:
            # Belt and braces behind _send_png: if something still holds
            # the file, say so in a sentence rather than 500ing at the
            # operator with a Windows error number.
            stuck = True
    if stuck:
        return jsonify({"error": (
            "That image is still in use — close the timer preview and "
            "try deleting it again.")}), 409
    return jsonify({"ok": True})
