"""AI entry generation for the spinner wheel, via OpenRouter's free GPT-OSS.

The user pastes their OWN free OpenRouter API key (openrouter.ai/keys). It is
stored locally in ~/.service-visuals/config.json (chmod 600) — never in the
repo, never sent to the browser (the status endpoint returns only a boolean).
The Flask server holds the key and calls OpenRouter server-side, so the key
never leaves the machine except to OpenRouter itself.

No third-party dependency: the HTTP call uses urllib, like the update checker.
"""

import json
import os
import re
import urllib.error
import urllib.request

MODEL = "openai/gpt-oss-20b:free"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_ENTRIES = 20
MAX_ENTRY_LEN = 40

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".service-visuals")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


class AiError(Exception):
    """Carries a plain-English message safe to show the user."""


# ---------------------------------------------------------------- key storage

def _read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_key():
    """The configured key: env var wins (handy for shared setups), else the
    local config file. Returns "" when none is set."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    return str(_read_config().get("openrouter_key", "")).strip()


def has_key():
    return bool(get_key())


def save_key(key):
    key = str(key or "").strip()
    if not key:
        raise AiError("Paste a valid OpenRouter API key.")
    if len(key) > 300:
        raise AiError("That key looks too long to be valid.")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config = _read_config()
    config["openrouter_key"] = key
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- generation

def _parse_entries(text):
    """Pull a clean list of short strings out of the model's reply, whether it
    returned a JSON array or a plain/bulleted list."""
    text = (text or "").strip()
    items = []

    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            arr = json.loads(match.group(0))
            if isinstance(arr, list):
                items = [str(x) for x in arr]
        except ValueError:
            items = []

    if not items:                       # fallback: one entry per line
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
            line = line.strip('"').strip("'").strip()
            if line:
                items.append(line)

    clean = []
    for item in items:
        item = " ".join(str(item).split())[:MAX_ENTRY_LEN].strip()
        if item and item.lower() not in {c.lower() for c in clean}:
            clean.append(item)
    return clean


def generate_entries(description, count, existing):
    """Ask GPT-OSS for `count` spinner entries matching `description`, avoiding
    anything already in `existing`. Returns a list of clean strings.

    Raises AiError with a friendly message on any failure."""
    key = get_key()
    if not key:
        raise AiError("Add your free OpenRouter API key first (in the AI panel).")

    count = max(1, min(MAX_ENTRIES, int(count)))
    existing = [str(e).strip() for e in (existing or []) if str(e).strip()]
    avoid = ", ".join(existing[:40]) if existing else "(none)"

    system = (
        "You generate entries for a spinner/wheel picker. "
        "Return ONLY a JSON array of short strings — no prose, no numbering, "
        "no markdown. Each entry is at most 40 characters. No duplicates."
    )
    user = (
        "Give me exactly {n} entries for: {desc}.\n"
        "Do not repeat any of these existing entries: {avoid}."
    ).format(n=count, desc=str(description).strip(), avoid=avoid)

    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        "max_tokens": 800,
    }).encode("utf-8")

    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "X-Title": "Service Visuals",
            "HTTP-Referer": "https://github.com/jimhoggey/service-visuals",
        })

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiError(
                "OpenRouter rejected the key — check it in the AI panel.")
        if exc.code == 402:
            raise AiError(
                "That OpenRouter account is out of free credit for this model.")
        if exc.code == 429:
            raise AiError("OpenRouter is busy right now — try again in a moment.")
        raise AiError("OpenRouter returned an error ({0}).".format(exc.code))
    except (urllib.error.URLError, TimeoutError):
        raise AiError("Couldn't reach OpenRouter — check your connection.")
    except ValueError:
        raise AiError("OpenRouter sent back something unreadable.")

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AiError("The AI didn't return any entries — try again.")

    entries = _parse_entries(content)
    # Drop anything the user already has, so the AI genuinely adds to the wheel.
    have = {e.lower() for e in existing}
    entries = [e for e in entries if e.lower() not in have]
    if not entries:
        raise AiError(
            "The AI didn't return a usable list — try rewording the request.")
    return entries[:count]
