# Service Visuals

A local desktop app for church tech volunteers: generates countdown timers, a
wall clock, spinner wheels, QR cards, motion backgrounds and a scoreboard
editor as ProPresenter-ready 1080p MP4s / PNGs. Flask on 127.0.0.1 + Pillow +
bundled ffmpeg, wrapped in pywebview, packaged by PyInstaller.

Owner: Fynn (GitHub `jimhoggey`). Repo: **jimhoggey/service-visuals** (public).

## Working agreements

- **Consult the graphify knowledge graph before changing code.** From the repo
  root: `graphify query "<question>" --budget 700`. It shows what a change
  touches across files. Refresh it after a big change with `graphify . --update`
  on a subagent. `graphify-out/` is gitignored. Note: that build does not index
  `.css`, so style.css being reported "deleted" is a known quirk, not real.
- **Subagents run on Sonnet or Haiku — never Opus or Fable.** This is the
  owner's standing instruction, including for heavy background jobs.
- **Features are built from a written spec.** Put it in `docs/specs/<name>.md`
  with: behaviour table, display rules the renderer AND the JS preview must
  share, the exact API contract (key names, defaults, ranges, error strings),
  frame-rate rules, per-agent file ownership (disjoint), a "do not" list, and
  done-means. Then implementers in parallel + a reviewer. See
  `docs/specs/clock-mode.md` for the format that works.
- **Verify by rendering and looking.** Every feature so far has shipped
  something the implementing and reviewing agents both missed that one
  extracted frame revealed in seconds. Render the real thing, pull frames with
  the bundled ffmpeg (`imageio_ffmpeg.get_ffmpeg_exe()`), and view them. Same
  for UI: drive it in the browser, don't ask the owner to check.

## Running it

```bash
SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py   # the gate — must pass
PORT=8799 SERVICE_VISUALS_STATS=0 .venv/bin/python app.py   # dev server
```

- **Port 8765 is the owner's INSTALLED packaged app**, which does not reflect
  source changes. Always use `PORT=8799` for a source run, and leave 8765 alone.
- `scripts/smoke.py` is the entire test suite and CI runs it before every build.
  Add a check there for anything you add. It is deliberately OCR-free where it
  can be, so it runs on Linux CI.
- Always set `SERVICE_VISUALS_STATS=0` outside a real release so test runs don't
  land in the owner's analytics.
- `.venv/bin/python` — the project venv, Python 3.9.

## Conventions

- **Python 3.9.** No walrus, no `match`, no `X | Y` type unions.
- 79-column lines, 4-space indent. (~84 pre-existing long lines; don't add more.)
- **Comment the WHY, not the what.** The codebase explains the reasoning behind
  non-obvious choices — often naming the bug that forced them. Match that.
- User-facing errors are plain English sentences a volunteer can act on.
- `version.py` is the single source of the version (`app.py`, `desktop.py` and
  CI all read it). Only a release bumps it.

## Things that will bite you

- **Countdown timer output is byte-identical-guarded.** Changing
  `render/timer.py` must not move a countdown pixel. Prove it: `git show
  HEAD:render/timer.py` (plus `encoder.py`, `fonts.py`) into a temp package,
  render 6 s classic/ring/bar old vs new, extract frames, `cmp` them.
- **Never commit the owner's church content.** `Grouppoints.png`,
  `ChatGPT Image*.png`, `Summit-*.png` are gitignored — keep it that way.
- `exports/` is the owner's real output folder in the packaged app. Delete any
  test renders you create there.
- User data lives in `~/.service-visuals` (API key, boards, update log,
  crash.log) so it survives updates — never inside the app bundle.
- **Analytics are surface-level by design.** `stats.py` sends event NAME +
  version + OS only, never content. Do not add a prop carrying user text, file
  names or paths. Crash reports send the error's *shape*, scrubbed.
- **Windows can't be tested here.** The self-update helper, `winocr` detection
  and the GPU encoder are macOS-unverifiable. Reason carefully, say plainly
  what is unverified, and prefer designs that fail visibly over silently.
- macOS self-update only works from `/Applications` (App Translocation runs a
  quarantined app from a read-only copy).

## Releasing

Tag-triggered: pushing `vX.Y.Z` builds the Mac `.app` and Windows `.exe` in
GitHub Actions and attaches both to the Release. Users auto-update, so a bad
tag reaches real churches. Use the `/release` skill rather than doing it by hand.
