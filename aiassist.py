"""AI entry generation for the spinner wheel, via OpenRouter.

The user pastes their OWN OpenRouter API key (openrouter.ai/keys). It is
stored locally in ~/.service-visuals/config.json (chmod 600) — never in the
repo, never sent to the browser (the status endpoint returns only a boolean).
The Flask server holds the key and calls OpenRouter server-side, so the key
never leaves the machine except to OpenRouter itself.

Model: defaults to `openrouter/free`, OpenRouter's router that picks whatever
free model is currently up and supports the request — far more reliable than
pinning one free model (any single one can be queued or offline). The user can
choose a specific model (e.g. openai/gpt-oss-120b:free) instead. If a chosen
model stalls or is unavailable, we fall back to openrouter/free automatically.

No third-party dependency: the HTTP call uses urllib, like the update checker.
"""

import json
import os
import re
import socket
import urllib.error
import urllib.request

import netutil

DEFAULT_MODEL = "openrouter/free"
# Suggestions offered in the UI dropdown (the user can also type a custom slug).
PRESET_MODELS = [
    "openrouter/free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "openrouter/auto",
]
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
TIMEOUT = 90                    # free models can queue for a while
MAX_ENTRIES = 100
MAX_ENTRY_LEN = 40

MODEL_RE = re.compile(r"[A-Za-z0-9._/:-]{1,80}")

# Config location (holds the key + chosen model). Overridable via
# SERVICE_VISUALS_CONFIG so it can be relocated or isolated for testing.
CONFIG_DIR = os.environ.get("SERVICE_VISUALS_CONFIG") or \
    os.path.join(os.path.expanduser("~"), ".service-visuals")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


class AiError(Exception):
    """Carries a plain-English message safe to show the user."""


# ---------------------------------------------------------------- config

def _read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def get_key():
    """The configured key: env var wins (handy for shared setups), else the
    local config file. Returns "" when none is set."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    return str(_read_config().get("openrouter_key", "")).strip()


def has_key():
    return bool(get_key())


def _clean_model(model):
    model = str(model or "").strip()
    return model if MODEL_RE.fullmatch(model) else ""


def get_model():
    env = os.environ.get("OPENROUTER_MODEL", "").strip()
    if env:
        return _clean_model(env) or DEFAULT_MODEL
    return _clean_model(_read_config().get("openrouter_model", "")) or DEFAULT_MODEL


def save_settings(key=None, model=None):
    """Update the stored key and/or model. Only non-None fields are touched."""
    config = _read_config()
    if key is not None:
        key = str(key).strip()
        if key:
            if len(key) > 300:
                raise AiError("That key looks too long to be valid.")
            config["openrouter_key"] = key
    if model is not None:
        clean = _clean_model(model)
        if not clean:
            raise AiError("Choose a valid model name.")
        config["openrouter_model"] = clean
    _write_config(config)


# ---------------------------------------------------------------- generation

def test_key():
    """Check the stored key against OpenRouter WITHOUT running a generation.

    Cheap and instant (no inference, no quota), so the UI can answer the two
    questions that actually matter: is my key saved and accepted, and can this
    machine reach OpenRouter at all? Returns a short status string."""
    key = get_key()
    if not key:
        raise AiError("No key is saved yet — paste your OpenRouter key above.")

    req = urllib.request.Request(KEY_ENDPOINT, headers={
        "Authorization": "Bearer " + key,
        "X-Title": "Service Visuals",
    })
    try:
        with netutil.urlopen(req, timeout=20) as resp:
            json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiError("OpenRouter rejected that key — paste it again.")
        if exc.code == 402:
            raise AiError("Key is valid, but the account is out of credit.")
        raise AiError("OpenRouter returned an error ({0}).".format(exc.code))
    except (socket.timeout, TimeoutError):
        raise AiError("OpenRouter timed out — try again in a moment.")
    except urllib.error.URLError:
        raise AiError("Couldn't reach OpenRouter — check your connection.")
    except ValueError:
        raise AiError("OpenRouter sent back something unreadable.")
    return "Key works — connected to OpenRouter."


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


class _Retry(Exception):
    """Internal: the current model failed in a way worth retrying on the
    fallback router. Carries the user-facing message to use if the retry
    also fails."""
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _chat(key, model, messages):
    """One OpenRouter call. Returns the assistant content string. Raises
    AiError (fatal) or _Retry (try the fallback model)."""
    body = json.dumps({
        "model": model,
        "messages": messages,
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
        with netutil.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiError("OpenRouter rejected the key — check it in the AI panel.")
        if exc.code == 402:
            raise AiError("That OpenRouter account is out of credit for this model.")
        if exc.code in (400, 404):
            raise _Retry("The model \"{0}\" isn't available right now.".format(model))
        if exc.code == 429:
            raise _Retry("OpenRouter is rate-limited right now.")
        raise _Retry("OpenRouter returned an error ({0}).".format(exc.code))
    except (socket.timeout, TimeoutError):
        raise _Retry(
            "The model took too long — free models can be busy. "
            "Try again, or use openrouter/free.")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", "")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise _Retry("The model took too long — free models can be busy.")
        raise AiError("Couldn't reach OpenRouter — check your connection.")
    except ValueError:
        raise _Retry("OpenRouter sent back something unreadable.")

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise _Retry("The AI didn't return any entries.")


def generate_entries(description, count, existing, model=None, full=False):
    """Ask the chosen model for spinner entries matching `description`,
    avoiding anything in `existing`. When `full` is True, return the complete
    natural set (e.g. every book of the Bible) up to MAX_ENTRIES; otherwise
    return exactly `count`. Falls back to openrouter/free if the chosen model
    stalls or is unavailable. Raises AiError with a friendly message on failure."""
    key = get_key()
    if not key:
        raise AiError("Add your free OpenRouter API key first (in the AI panel).")

    count = max(1, min(MAX_ENTRIES, int(count)))
    existing = [str(e).strip() for e in (existing or []) if str(e).strip()]
    avoid = ", ".join(existing[:60]) if existing else "(none)"

    chosen = _clean_model(model) or get_model()
    # Try the chosen model, then the router (unless it IS the router).
    chain = [chosen] if chosen == DEFAULT_MODEL else [chosen, DEFAULT_MODEL]

    if full:
        cap = MAX_ENTRIES
        ask = (
            "List the COMPLETE set for: {desc}.\n"
            "If it's a well-known finite set (e.g. books of the Bible, months "
            "of the year, countries in a region), return every item in the "
            "natural order — do not stop short and do not pad. Otherwise return "
            "a good variety. Return at most {cap} items."
        ).format(desc=str(description).strip(), cap=MAX_ENTRIES)
    else:
        cap = count
        ask = (
            "Give me exactly {n} entries for: {desc}."
        ).format(n=count, desc=str(description).strip())

    messages = [
        {"role": "system", "content": (
            "You generate entries for a spinner/wheel picker. "
            "Return ONLY a JSON array of short strings — no prose, no numbering, "
            "no markdown. Each entry is at most 40 characters. No duplicates.")},
        {"role": "user", "content": (
            ask + "\nDo not repeat any of these existing entries: {avoid}."
        ).format(avoid=avoid)},
    ]

    have = {e.lower() for e in existing}
    last_message = "The AI didn't return a usable list — try rewording."
    for attempt_model in chain:
        try:
            content = _chat(key, attempt_model, messages)
        except _Retry as retry:
            last_message = retry.message
            continue                     # try the fallback model
        entries = _parse_entries(content)
        entries = [e for e in entries if e.lower() not in have]
        if entries:
            return entries[:cap]
        last_message = "The AI didn't return a usable list — try rewording."

    raise AiError(last_message)
