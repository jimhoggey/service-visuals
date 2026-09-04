"""/api/download/status — the YouTube-download tile's one extra endpoint
(docs/specs/youtube-download.md).

Split out like routes/backgrounds.py. The actual render (type="download")
goes through the same /api/render + JobManager path every other tile
uses (wired in app.py) — this blueprint only answers the status check the
tile makes on entering its view, before the operator has clicked anything.
"""

from flask import Blueprint, jsonify

import tools

bp = Blueprint("download", __name__)


@bp.route("/api/download/status", methods=["GET"])
def api_download_status():
    return jsonify(tools.tools_status())


@bp.route("/api/download/tools", methods=["DELETE"])
def api_download_tools_remove():
    """The REMOVE button beside the status line: the two fetched binaries
    are ~120 MB on disk, and a church that tried the tile once should be
    able to give that back without finding ~/.service-visuals by hand."""
    return jsonify(tools.remove_tools())
