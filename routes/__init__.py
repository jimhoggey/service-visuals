"""Flask Blueprints split out of app.py (docs/specs/refactor-organise.md).

Package marker only — each module below registers its own Blueprint;
app.py imports and registers them. PyInstaller collects pure-Python
packages automatically as long as this file exists.
"""
