"""/api/ai/* — AI-assisted spinner entry generation.

Split out of app.py (docs/specs/refactor-organise.md).
"""

from flask import Blueprint, jsonify

import aiassist
import stats
from webutil import json_body

bp = Blueprint("ai", __name__)


@bp.route("/api/ai/status")
def api_ai_status():
    return jsonify({
        "configured": aiassist.has_key(),
        "model": aiassist.get_model(),
        "models": aiassist.PRESET_MODELS,
    })


@bp.route("/api/ai/settings", methods=["POST"])
def api_ai_settings():
    data, error = json_body("Body must be JSON.")
    if error:
        return error
    if "key" not in data and "model" not in data:
        return jsonify({"error": "Nothing to save."}), 400
    try:
        aiassist.save_settings(key=data.get("key"), model=data.get("model"))
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "configured": aiassist.has_key(),
                    "model": aiassist.get_model()})


@bp.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """Validate the saved key + connectivity without spending a generation."""
    try:
        message = aiassist.test_key()
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "message": message})


@bp.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    data, error = json_body("Body must be JSON.")
    if error:
        return error

    description = data.get("description", "")
    if not isinstance(description, str) or not description.strip():
        return jsonify({"error": "Describe what entries you need."}), 400
    if len(description) > 200:
        return jsonify({"error": "Keep the description under 200 characters."}), 400

    full = bool(data.get("full"))
    try:
        count = int(data.get("count", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "How many? must be a whole number."}), 400
    if not full and (count < 1 or count > 100):
        return jsonify({"error": "Choose between 1 and 100 entries."}), 400

    existing = data.get("existing", [])
    if not isinstance(existing, list):
        existing = []

    model = data.get("model")
    if model is not None and not isinstance(model, str):
        model = None

    try:
        entries = aiassist.generate_entries(
            description, count, existing, model, full)
    except aiassist.AiError as exc:
        return jsonify({"error": str(exc)}), 400
    stats.track("ai_fill")
    return jsonify({"entries": entries})
