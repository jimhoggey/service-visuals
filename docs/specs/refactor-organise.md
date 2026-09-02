# Spec: Organise the codebase (behaviour-preserving)

**Status:** shipped in v1.28.0. **Owner:** orchestrator. **Implementers:** Sonnet
agents with disjoint file ownership; one reviewer.

**Learned in verification.** (1) The AST/regex body proof, the route-set
proof and `jscheck` all passed while five buttons were dead: the JS split
dropped the update-banner/footer wiring block (it was loose code in the
tail, not a function, so no proof looked at it). Only clicking the
buttons in a browser and watching the network log found it — keep doing
that. (2) The clock preview ticks live, so its canvas hash is never
stable; compare canvases old-vs-new under identical click sequences in
the same tab, not against a hash from an earlier session. (3) An agent
ran `git stash` mid-task, reverting every other agent's in-flight edits;
"do not stash, reset or checkout" now belongs in every implementer brief.
(4) `validation` ↔ `backgrounds` briefly imported each other by relying
on definition order; `_background_id_field` moved to `validation.py` so
the graph is a DAG again.

## Why

`app.py` (1,543 lines) and `static/app.js` (3,169 lines) each hold five
tiles' worth of unrelated code. Every future change means scrolling past
four tiles to reach the fifth. This split makes each file about one thing.
**Nothing user-visible changes** — not a pixel, not a message, not a route.

Out of scope (decided, don't revisit): splitting `render/scoreboard.py`,
`style.css`, `render/timer.py`, `stats.py`, `updater.py`, `smoke.py`.

## Proof of "nothing changed"

1. `scripts/golden.py` (new, written FIRST, baseline recorded BEFORE any
   refactor) — see below. `--check` must pass after every agent's work.
2. `SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py` passes.
3. pyflakes clean on `*.py routes/*.py render/*.py scripts/*.py`.
4. Reviewer: an AST comparison — every Python function/method body that
   exists before and after must be **identical** (`ast.dump` of the body
   after stripping docstrings), except the sites this spec names. For JS,
   the same with regex-extracted function text, whitespace-normalised.
5. Orchestrator: drives every tile in the browser, hashes each preview
   canvas before/after, runs one export per tile, checks the console.

## Golden harness — `scripts/golden.py`

Renders a fixed job list and hashes the **decoded frames**, so the proof
covers the encoder too. Env set inside the script before imports:
`SERVICE_VISUALS_STATS=0`, `SERVICE_VISUALS_CONFIG=<mkdtemp>`,
`SERVICE_VISUALS_EXPORTS=<mkdtemp>`, `SERVICE_VISUALS_ENCODER=libx264`.

Jobs (call the renderers directly, `quiet_progress`, all ≤ 6 s):
timer classic/ring/bar (6 s, hold 2, warn on); timer classic millis; timer
bar with two generated 400×100 / 100×400 backgrounds (`bg_seconds` 2, dim
45, blur on — write the PNGs into the scratch config's `backgrounds/`);
timer classic `green_screen`; clock classic 12h millis; clock ring 24h;
spinner random and rigged (`random.seed(7)` immediately before each call,
six entries); qr mp4 + `render_qr_image` PNG; motion bg aurora/bokeh/waves at
the shortest duration the renderer accepts; scoreboard: `mock_board.build()`
→ `create_board` → `save_values` (change one value) → `export_board` PNG.

Hash: mp4 → `ffmpeg -i f -f rawvideo -pix_fmt rgb24 -` piped to sha256,
plus the byte count; png → sha256 of the RGB bytes. Write
`golden.json` at the repo root (`{name: {"sha": .., "bytes": n}}`), which
is **gitignored** — hashes are per-machine (fonts). CLI: `--record`,
`--check` (default; exit 1 listing mismatches), `--only NAME`. Under 3 min.

## Python — `app.py` split (one agent)

Owns: `app.py`, new `validation.py`, `webutil.py`, `backgrounds.py`,
`routes/__init__.py`, `routes/backgrounds.py`, `routes/board.py`,
`routes/ai.py`, `routes/update.py`, `scripts/smoke.py` (imports only),
`render/scoreboard.py` (one deletion only), `CLAUDE.md` (one line).

- `validation.py` (no Flask import): `ValidationError`, the `*_RE`
  constants and option tuples, `_int_field`, `_str_field`, `_accent_field`,
  `_one_of`, `_clip_length_field`, every `validate_*`/`_validate_*`,
  `_timer_background_options`, `_backgrounds_field`, `_background_field`,
  `VALIDATORS`, and the board field validators (`_board_id_field`,
  `_board_name_field`, `_values_field`) with their constants.
- `backgrounds.py`: `BACKGROUNDS_DIR`, `BACKGROUND_ID_RE`, the two `_MAX`
  constants, `_background_id_field`, `_background_path`, `_send_png`, and
  the library operations the routes call. `validation._backgrounds_field`
  imports `background_path` from here. No import of `app`.
- `webutil.py`: `MAX_JSON_BYTES`, `MAX_UPLOAD_BYTES`, and
  `json_body(message)` → `(data, error_response)` replacing the nine
  repeated prologues (`request.content_length` check → 413 *"Request too
  large."*; non-dict → 400 with the caller's message). Status codes and
  messages must be identical per site — copy each site's message string.
- `routes/board.py`: the `/api/board/*` routes plus `board_or_error(id)`
  replacing the four identical `_open_board` try/except blocks
  (`ValidationError` → 400, `BoardError` → 404, same JSON shape).
- `routes/ai.py`: `/api/ai/*`. `routes/update.py`: `/api/update-*`,
  `/api/update-log`, `/api/open-release`, `newest_release` and its helpers,
  `_last_install_result`, `_do_install`.
- Flask `Blueprint`s, registered in `app.py`. `@app.before_request` host
  guard stays in `app.py` (blueprints inherit it — confirm with a
  `test_client` request to a blueprint route with a foreign `Host`).
- `app.py` keeps: app creation, host guard, analytics props/`_counted`,
  `JobManager` wiring, `/`, health, whats-new, render, qr-preview/-image,
  upload-bg, jobs, exports download, stats, reveal, `prepare_exports_dir`,
  `main`. `desktop.py` imports `app, prepare_exports_dir` — keep those names.
- Delete `rename_board` in `render/scoreboard.py:1444-1450` (unreferenced).
- `scripts/smoke.py`: change only import lines / attribute paths
  (`app.validate_timer_options` → `validation.validate_timer_options`,
  `newest_release`, etc.).
- Function bodies move **verbatim**. Move the comments with them — they
  explain the WHY and are part of the code.
- PyInstaller collects pure-Python imports automatically; `routes/` needs
  an `__init__.py`. No spec/CI change.

## JavaScript — `static/app.js` split (one agent)

Owns: `static/app.js` (deleted at the end), new `static/js/*.js`,
`static/index.html` (script tags only), `scripts/smoke.py`
(one new `check_js_modules`).

No bundler: files are plain `<script src="/static/js/…" defer>` tags in this
order, each its own IIFE with `"use strict"`, sharing one global `window.SV`:

| File | Contents (from current `app.js` lines) |
|---|---|
| `core.js` | `$`, `PW/PH`, the shared palette (12–30), helpers (33–86), `showView` (303–320) with the hooks below, `wireAccent` (2860+), the export lifecycle (2653–2857) with `updaters` replaced by the registry, `applyPlatform`. Ends with `window.SV = {…}` exporting every name a tile file uses. |
| `timer.js` | 321–1151 + its wiring from the tail (form input/change, accent, presets, mode/style/bg controls, submit, reveal, `tile-timer`, `back-timer`). |
| `spinner.js` | 1152–1702 (incl. the AI panel) + its wiring. |
| `qr.js` | 1703–1800 + wiring. |
| `motionbg.js` | 1801–2007 + wiring. |
| `board.js` | 2008–2652 + wiring (incl. `back-board`'s `flushBoardSave()`). |
| `shell.js` | health (87–115), update check (116–237), what's-new (238–293), and boot: the last-lines initial `update…()` calls become `Object.keys(SV.tiles).forEach(k => SV.tiles[k].update())` plus `SV.tiles.motionbg.drawFrame(0)`. |

- Registry: `SV.tiles = {}`; each tile file ends with
  `SV.registerTile("timer", {update, validate, payload, enter, leave})`.
  `showView` replaces its five `if (id === "view-…") …()` lines and the
  `stopMotionPreview()` line with `leave()` on the tile being left and
  `enter()` on the one entered (board's `enter` = `enterBoardView`;
  motionbg's `leave` = `stopMotionPreview`; others' `enter` = their
  `update`). `pollJob`/`finishExport` use `SV.tiles[kind].update`.
- Compression, as part of the move: a `SV.wireTileForm(kind, tile,
  {autoUpdate})` in `core.js` does the repeated input/change → update,
  `wireAccent`, submit → validate/showError/startExport, reveal → click
  pattern (timer/qr/motionbg with `autoUpdate: true`, spinner without);
  `tile-<kind>`/`back-<kind>` navigation is wired there too (board passes a
  `beforeBack: flushBoardSave` option). `formatClock`'s local `pad` → `pad2`.
- Each tile file begins by pulling what it uses into locals
  (`var $ = SV.$, PW = SV.PW, …`) so function **bodies stay verbatim**.
- `check_js_modules` in `smoke.py`: (1) `node --check` on each file when
  `node` is on PATH (skip quietly otherwise; CI runners have it);
  (2) a regex pass: per file, every identifier called as `name(` that is not
  preceded by `.`, not defined in that file (`function name(` / `var name`),
  not in a builtin whitelist (`fetch setTimeout clearTimeout setInterval
  clearInterval requestAnimationFrame cancelAnimationFrame parseInt
  parseFloat isNaN isFinite String Number Boolean Array Object Image Date
  Error FormData Blob URL Promise encodeURIComponent decodeURIComponent
  alert confirm Uint8Array Math JSON`, plus JS keywords), must appear in the
  file's `SV.` import block; and every `SV.<name>` read must be exported by
  `core.js`. Prove it works by temporarily removing one import and seeing
  the check fail, then restore.

## Do not

- Do not rename functions, ids, routes, option keys, or messages.
- Do not "improve" logic you are moving. If you find a bug, report it in
  your final message; do not fix it.
- Do not touch `render/timer.py`, `stats.py`, `updater.py`, `style.css`.
- Do not bump `version.py`, edit `whatsnew.py`, or tag.
- Do not run anything against port 8765. Use `PORT=8799` with
  `SERVICE_VISUALS_STATS=0` and `SERVICE_VISUALS_CONFIG=<scratch dir>`.

## Done means

All five proofs above pass. Report the final line count per new file and
the list of function bodies you changed (expected: only the `json_body` and
`board_or_error` call sites, `showView`, the tile wiring, and boot).
