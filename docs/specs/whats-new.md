# Spec: "What's new" popup after an update (v1.26.0)

**Status:** to implement. **Owner:** orchestrator. **Implementers:** two Sonnet agents (backend, frontend), one reviewer.

## Why

Users update and have no idea anything changed. A small, dismissible card on
first launch after an update — three bullets, a bit of humour, gone in five
seconds. Not a changelog, not a modal that needs reading.

## When it shows

`~/.service-visuals/last-seen.json` holds `{"version": "1.25.0"}`.

| Situation | Behaviour |
|---|---|
| Running version **newer** than the stored one | Show, then store the new version |
| Same version, or older | Never show |
| **No file, but `~/.service-visuals/` already has other content** (analytics.json, boards/, backgrounds/, update.log…) | Show — this is an existing user on the release that introduces the feature, and they are exactly who should see it |
| **No file and the config dir is empty/absent** | Do not show. Fresh install: seed the file silently. Nobody wants "what's new" on first ever launch |

Shown once per version — dismissing writes the file immediately, so quitting
without dismissing shows it again next launch (that is fine and intended).

## Content

`whatsnew.py`, a module of plain data. `NOTES` maps a version to a list of
short lines. **Use this copy verbatim** — it is written to be read out loud
by a volunteer, not a release engineer:

```python
INTRO = "Nice. You're up to date."

NOTES = {
    "1.26.0": [
        "This little box, so you stop finding out about features by accident.",
    ],
    "1.25.0": [],          # analytics only — nothing a volunteer would notice
    "1.24.0": [
        "Timers and clocks can sit on your own artwork now — one image, or a "
        "few that cycle.",
        "“Hold at 0:00” is now “Keep 0:00 on screen for”, "
        "because nobody knew what the old one meant.",
    ],
    "1.23.0": [
        "Milliseconds on a countdown, for when the last ten seconds need to "
        "feel dramatic.",
    ],
    "1.22.0": [
        "The timer can be an actual clock — start it at 7:59:50 and let it "
        "roll over to 8:00 on screen.",
    ],
    "1.21.0": [
        "The scoreboard reads your numbers properly, and stopped dropping a "
        "white box over the one you just edited.",
    ],
}
```

`notes_for(version, limit=3)` returns up to `limit` lines: this version's
first, then top up from earlier versions newest-first until it has `limit`.
So v1.25.0 (empty) shows three lines drawn from 1.24 and 1.23. Unknown
version → top up from the newest known. Never return more than `limit`,
never return duplicates, and if `NOTES` is somehow empty return `[]` and the
API reports `show: false` rather than an empty card.

Keep every line under ~90 characters — it has to fit two lines on screen.

## API

| Route | Behaviour |
|---|---|
| `GET /api/whats-new` | `{"show": bool, "version": "1.26.0", "intro": "...", "items": [...]}`. Decides per the table above but does **not** write anything — a GET must stay safe to repeat. |
| `POST /api/whats-new/seen` | Writes `last-seen.json` with the running version. `{"ok": true}`. Idempotent. |

Analytics: one event `whats_new_shown` (no props). Add it to the `EVENTS`
whitelist in `stats.py`.

## UI

A card, not a full modal takeover:

- Fixed, centred, above a dimmed backdrop; max-width ~460 px.
- Header: `WHAT'S NEW` on the left, a version pill (`v1.26.0`) in the accent
  colour on the right.
- The intro line, then the bullets as a normal `<ul>` — no icons.
- One button: `GOT IT`, accent-filled, right-aligned.
- Dismiss on: the button, `Esc`, or a backdrop click. Any of them POSTs
  `/api/whats-new/seen` and hides the card.
- Reuse existing tokens (`--accent`, `--panel`, `--line-2`, `--mono`). No new
  fonts, no animation beyond a plain fade-in.
- Focus moves to `GOT IT` on open and returns to `document.body` on close;
  `role="dialog"` + `aria-modal="true"` + `aria-labelledby`.
- Fetched once on boot, after the health check. If the request fails, show
  nothing — this must never block or break the app.

## Files & ownership

**Backend** — `whatsnew.py` (new), `app.py`, `stats.py`, `scripts/smoke.py`,
`.claude/skills/release/SKILL.md`.
- `notes_for` exactly as described; the two routes; the `last-seen.json`
  read/write under `CONFIG_DIR` (honour `SERVICE_VISUALS_CONFIG`); the
  `whats_new_shown` event.
- Smoke: `notes_for("1.25.0")` returns 3 lines drawn from 1.24/1.23;
  `notes_for("1.26.0")` leads with the 1.26 line and tops up to 3; an
  unknown version still returns 3; no duplicates; every line ≤ 90 chars;
  and the show/hide decision for all four rows of the table above, driven
  through a temp `SERVICE_VISUALS_CONFIG`.
- Add one line to the release skill: **update `whatsnew.py` before tagging**
  (leave the list empty if the release has nothing user-facing — the
  fallback handles it).

**Frontend** — `static/index.html`, `static/app.js`, `static/style.css`.
- The card, the fetch on boot, the three dismiss paths, the POST.

## Do not

- Do not show it on a fresh install.
- Do not block the UI behind it — the app must be fully usable if the fetch
  fails or the card never renders.
- Do not add a "don't show again" checkbox, a carousel, or links out.
- Do not bump `version.py` or tag.

## Done means

- Smoke passes; pyflakes clean; `node --check static/app.js` clean.
- Driving the app with a stale `last-seen.json` shows the card once; after
  `GOT IT` a reload does not show it again.
- With an empty config dir it does not appear at all.
