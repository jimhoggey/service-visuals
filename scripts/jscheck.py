"""Lightweight cross-file check for static/js/*.js (spec:
docs/specs/refactor-organise.md). Each file is a plain IIFE that shares
state through window.SV — no bundler, no import statements — so nothing
catches a typo'd or missing "var x = SV.x" alias until it breaks at
runtime in the browser. This is a regex pass, not a JS parser: it is
deliberately just thorough enough to catch that one mistake.

Two checks:
  1. `node --check` on every file, when node is on PATH (CI has it; a
     source machine without node just skips this half quietly).
  2. Per file, every bare `name(` call (not `obj.name(`) that isn't a
     local `function name()`/`var name`/parameter in that file, a JS
     keyword, or a whitelisted builtin, must be something the file
     actually reads as `SV.name` somewhere (our convention is always
     `var name = SV.name;`, so a plain substring/regex presence check
     is enough — see the docstring on _local_defs for why this doesn't
     need real scoping). And every `SV.name` a file reads must be a
     name core.js actually exports (`SV.name = ...`).

Run directly (`python scripts/jscheck.py`) or import check_js_modules()
from scripts/smoke.py.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"

# Global functions/constructors the codebase calls directly, that no
# static/js file defines or imports from SV. Exactly the list in the spec.
BUILTIN_CALLS = set("""
    fetch setTimeout clearTimeout setInterval clearInterval
    requestAnimationFrame cancelAnimationFrame parseInt parseFloat
    isNaN isFinite String Number Boolean Array Object Image Date
    Error FormData Blob URL Promise encodeURIComponent
    decodeURIComponent alert confirm Uint8Array Math JSON
""".split())

# Reserved words that can be immediately followed by "(" in ordinary
# control flow (if (), for (), catch (), an anonymous "function (") —
# none of these are ever a call to a name needing an SV import.
JS_KEYWORDS = set("""
    if for while switch catch function return typeof new delete void
    in of do else try throw instanceof with
""".split())

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def _strip_noncode(text):
    """Blank out comments and string contents before any of the regexes
    below run — comment prose is full of "word (parenthetical)" asides
    and apostrophes ("don't", "operator's") that would otherwise read as
    calls or string delimiters. Order matters: block comments first (a
    line-comment strip would otherwise stop at a "//" that happens to
    sit inside a /* */ comment spanning several lines), then line
    comments, then strings — only once comments are gone is it safe to
    hunt for quote characters without an apostrophe-in-a-comment matching
    as a string open and swallowing real code up to the next apostrophe.
    """
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub('""', text)
    return text


# A bare call: identifier immediately followed by "(", not preceded by
# "." (that would be a method call on some object/other file's export,
# which this check has no way to attribute and isn't what it's for).
_CALL_RE = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_FUNC_DEF_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_PARAM_LIST_RE = re.compile(r"function\s*(?:[A-Za-z_$][\w$]*)?\s*\(([^)]*)\)")
_VAR_DEF_RE = re.compile(r"(?:\bvar|,)\s*([A-Za-z_$][\w$]*)\s*=(?!=)")
_SV_READ_RE = re.compile(r"\bSV\.([A-Za-z_$][\w$]*)\b")
_SV_EXPORT_RE = re.compile(r"\bSV\.([A-Za-z_$][\w$]*)\s*=(?!=)")


def _local_defs(text):
    """Names this file defines for itself: function declarations, every
    var-statement's declared identifier(s) (including "var a = SV.a,
    b = SV.b;" — which is exactly why a name being in _local_defs does
    not, by itself, mean the check below considers it "imported": that
    happens via _sv_reads instead, and a var-alias line satisfies both
    at once — remove the line and a name drops out of *both* sets
    together, which is what makes the deliberate-break test below fail
    the way it's supposed to), and every function parameter (wireAccent's
    "onChange", for one — a real bare-called name that is neither a
    function nor a var).
    """
    names = set(_FUNC_DEF_RE.findall(text))
    names |= set(_VAR_DEF_RE.findall(text))
    for params in _PARAM_LIST_RE.findall(text):
        for p in params.split(","):
            p = p.strip()
            if p:
                names.add(p)
    return names


def check_js_modules():
    """Return a list of problem strings (empty means everything's fine)."""
    problems = []

    if not JS_DIR.is_dir():
        return ["static/js/ does not exist."]
    files = sorted(JS_DIR.glob("*.js"))
    if not files:
        return ["static/js/ has no .js files."]

    node = shutil.which("node")
    if node:
        for f in files:
            r = subprocess.run(
                [node, "--check", str(f)],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                problems.append(
                    "%s: node --check failed: %s"
                    % (f.name, (r.stderr or "").strip() or "unknown error")
                )
    # else: no node on PATH — skip quietly (CI runners have it).

    core_file = JS_DIR / "core.js"
    core_text = _strip_noncode(
        core_file.read_text(encoding="utf-8") if core_file.exists() else "")
    exported = set(_SV_EXPORT_RE.findall(core_text))
    if not exported:
        problems.append("core.js exports nothing (SV.x = ... not found).")

    for f in files:
        text = _strip_noncode(f.read_text(encoding="utf-8"))
        local_defs = _local_defs(text)
        sv_reads = set(_SV_READ_RE.findall(text))

        for m in _CALL_RE.finditer(text):
            name = m.group(1)
            if (name in JS_KEYWORDS or name in BUILTIN_CALLS
                    or name in local_defs or name in sv_reads):
                continue
            problems.append(
                '%s: calls "%s(" but never imports it (no "SV.%s" in '
                "this file)" % (f.name, name, name)
            )

        if f != core_file:
            for name in sv_reads:
                if name not in exported:
                    problems.append(
                        "%s: reads SV.%s, which core.js never exports"
                        % (f.name, name)
                    )

    return problems


if __name__ == "__main__":
    found = check_js_modules()
    if found:
        for p in found:
            print("FAIL:", p)
        print("%d problem(s)." % len(found))
        sys.exit(1)
    print("check_js_modules: OK.")
