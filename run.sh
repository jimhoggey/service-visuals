#!/usr/bin/env bash
# Service Visuals — one-command start.
# Creates the virtualenv on first run, installs deps, starts the server.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "First run: creating virtual environment in .venv ..."
    python3 -m venv .venv
fi

.venv/bin/pip install -r requirements.txt -q

exec .venv/bin/python app.py
