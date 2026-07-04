@echo off
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
)
.venv\Scripts\pip install -r requirements.txt -q
.venv\Scripts\python app.py
