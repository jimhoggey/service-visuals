# Spec: YouTube download tile

**Status:** shipped in v1.29.0. **Owner:** orchestrator. **Implementers:** backend
Sonnet agent, frontend Sonnet agent (disjoint files, below).

**Learned in verification.** (1) `--ffmpeg-location` must be the exact
ffmpeg *file*, not its folder: imageio's binary is named
`ffmpeg-macos-aarch64-v7.1`, so folder discovery fails, and on a Mac with
Homebrew ffmpeg on PATH the bug hides — with PATH stripped the MP4 merge
silently no-ops and leaves two fragments. (2) The standalone yt-dlp is a
PyInstaller onefile that unpacks on every run (11 s on a busy Mac), so
`--version` is never asked on the status route; the version is cached in
`bin/ytdlp-version` after a download's update check. (3) The export
lifecycle's `RENDERING` label was wrong for a download; tiles may now
supply `busyText(job)` and a `done(filename)` hook. (4) `jscheck.py`'s
comment stripper truncated a line at a `//` inside a string (the URL in
the validation message); it now scans strings and comments in one pass.

## Why

Volunteers grab clips from ad-ridden download sites. A tile in the app
they already trust — paste a link, pick MP4 or MP3, click DOWNLOAD — is
cleaner and safer. The owner has made the terms-of-service call; the
README notes that a download is not a licence to show the content.

**The engineering risk is upkeep, not the download.** YouTube changes its
player often; `yt-dlp` updates to match, and since 2025 it needs an
external JavaScript runtime (Deno) to solve YouTube's challenges. So the
app does **not** bundle either tool. On first use it fetches the official
`yt-dlp` binary and Deno into `~/.service-visuals/bin/`, verifies their
checksums, and thereafter `yt-dlp -U` keeps itself current — the feature
outlives any single app release.

## Behaviour

Home tile **YOUTUBE DOWNLOAD** (`tile-download`, description *"Paste a
YouTube link and get an MP4 for ProPresenter, or an MP3."*), view
`view-download`, kind `download`. The view has no preview canvas.

| Control | Details |
|---|---|
| YOUTUBE LINK | `input id="download-url"` (type `url`), placeholder `https://www.youtube.com/watch?v=…`. Hint: `Paste a YouTube link. A playlist link downloads only that one video.` |
| FORMAT | Segmented `download-format`: `MP4 VIDEO` (default, value `mp4`) \| `MP3 AUDIO` (value `mp3`). Hint under it: `MP4 is H.264 up to 1080p, ready for ProPresenter.` |
| Status line | `id="download-tools"`, filled from `GET /api/download/status` on entering the view: `Downloader ready · yt-dlp <version>` or `The first download sets up the downloader (about 75 MB, one time).` |
| DOWNLOAD | `button id="download-export"` — the standard export lifecycle: `download-status`, `download-bar`, `download-pct`, `download-track`, `download-error`, done panel with `download-reveal`. |

Files land in the exports folder like every other tile. Job status text
while tools are being fetched: `Setting up the downloader (one time)…`;
while downloading: `Downloading…`; MP3 conversion: `Converting to MP3…`.

## Tools — `tools.py` (backend)

- `BIN_DIR = <config dir>/bin` — honour `SERVICE_VISUALS_CONFIG` exactly
  as `backgrounds.py` does.
- `asset_names()` → `(ytdlp_asset, deno_asset)` by platform:
  - darwin: `yt-dlp_macos`; deno `deno-aarch64-apple-darwin.zip` on
    `arm64`, `deno-x86_64-apple-darwin.zip` otherwise.
  - win32: `yt-dlp.exe` (`yt-dlp_arm64.exe` on ARM64); deno
    `deno-x86_64-pc-windows-msvc.zip` / `deno-aarch64-pc-windows-msvc.zip`.
- URLs: yt-dlp from
  `https://github.com/yt-dlp/yt-dlp/releases/latest/download/<asset>`,
  verified against `.../latest/download/SHA2-256SUMS` (lines `<hex>  <name>`).
  Deno pinned: `DENO_VERSION = "v2.9.6"`, from
  `https://github.com/denoland/deno/releases/download/v2.9.6/<asset>`,
  verified against `<asset>.sha256sum` (same line format). Unzip; the
  binary is `deno` / `deno.exe`; `chmod 0o755` on POSIX.
- `ensure_tools(progress_cb, status_cb)` → `{"ytdlp": path, "deno": path}`.
  Idempotent. Stream each download to `<name>.part` with `urllib`
  (30 s timeout, a `User-Agent`), report bytes progress, verify sha256
  **before** renaming into place; on mismatch delete and raise
  `ToolsError("The downloader's files did not verify — try again later.")`;
  on network failure raise `ToolsError("Couldn't reach GitHub to set up the
  downloader — check the internet connection and try again.")`.
- `update_ytdlp()` — runs `<ytdlp> -U` (60 s timeout) when
  `bin/last-update` is older than 24 h or missing; touches the stamp;
  failures are logged to `<config dir>/download.log` and ignored.
- `tools_status()` → `{"ready": bool, "ytdlp_version": str|None}`
  (`<ytdlp> --version`, 10 s timeout, only when the file exists).
- Never write anywhere but `BIN_DIR`, the stamp and `download.log`.

## Download job — `downloader.py` (backend)

`download_video(options, progress_cb)` — a JobManager job like the
renderers, registered as type `"download"`:

1. `ensure_tools` (progress 0–30 % only if something had to be fetched,
   else skip straight on), then `update_ytdlp()`.
2. Run yt-dlp with exactly these arguments (plus the format set):
   ```
   <ytdlp> <url> --no-playlist
     --js-runtimes deno:<deno path>
     --ffmpeg-location <dirname of imageio_ffmpeg.get_ffmpeg_exe()>
     --restrict-filenames --newline --no-warnings
     --socket-timeout 30 --retries 3
     --progress-template "download:SV %(progress._percent_str)s"
     --print after_move:filepath
     -o "<EXPORTS_DIR>/%(title).80s-%(id)s.%(ext)s"
   ```
   - mp4: `-f "bv*+ba/b" -S "codec:h264:m4a,res:1080" --merge-output-format mp4`
     (H.264 first, then up to 1080p — ProPresenter's AVFoundation plays
     H.264; it does not play VP9/AV1 in an MP4).
   - mp3: `-f "ba/b" -x --audio-format mp3 --audio-quality 0`.
3. Progress: lines beginning `SV ` carry the percent; map onto the
   remaining 30–100 % (or 0–100 %). Kill the process after 30 minutes.
4. Result: the `after_move:filepath` line — the last stdout line that is an
   existing file inside `EXPORTS_DIR` (realpath containment). Return its
   basename. Anything else → the generic error below.
5. Errors (match on stderr, case-insensitive; write the last 40 stderr
   lines to `download.log`, **never** to analytics):
   - `private video` / `sign in` → *"That video is private or needs a sign-in, so it can't be downloaded."*
   - `age` → *"That video is age-restricted, so it can't be downloaded."*
   - `unavailable` / `removed` → *"That video isn't available."*
   - `urlopen error` / `timed out` / `network` → *"Couldn't reach YouTube — check the internet connection and try again."*
   - otherwise → *"The download failed. YouTube may have changed something — the downloader updates itself daily, so try again tomorrow."*
   Pure helpers for smoke: `format_args(fmt)`, `parse_progress(line)`,
   `friendly_error(stderr)`.

## API contract

`POST /api/render` `{"type": "download", "options": {"url": "...", "format": "mp4"}}`.

`validate_download_options` in `validation.py`, added to `VALIDATORS`:
- `url`: string ≤ 500 chars; `urllib.parse` scheme `http`/`https`; host
  (lower-cased, port stripped) in `youtube.com`, `www.youtube.com`,
  `m.youtube.com`, `music.youtube.com`, `youtu.be`, `www.youtu.be` →
  otherwise *"Paste a YouTube link, like https://www.youtube.com/watch?v=…"*
- `format`: `"mp4"` \| `"mp3"`, default `mp4` → *"Format must be mp4 or mp3."*
- Returns `{"url", "format"}` only.

`GET /api/download/status` → `tools_status()`, in a new blueprint
`routes/download.py`.

Analytics: `_counted("download", download_video, _download_props)` with
`_download_props` → `{"format": "mp4"|"mp3"}` and the matching event name
added to `stats.EVENTS`. **Never** the URL, title, id or filename.

`EXPORT_FILENAME_RE` (and any sibling used by `/api/reveal`) gains `mp3`
so the done panel and Reveal work for audio.

## Files & ownership (edit ONLY your own files)

**Backend agent** — `tools.py` (new), `downloader.py` (new),
`routes/download.py` (new), `validation.py`, `app.py`, `stats.py` (one
`EVENTS` entry), `scripts/smoke.py`, `README.md` (one bullet: what it does,
that content licensing is the church's responsibility).

Smoke stays **offline** — no network, no binaries:
(a) validation accepts a `watch?v=`, a `youtu.be/`, a `shorts/` and a
`music.youtube.com` link and rejects `vimeo.com`, a non-URL, an empty
string and a 600-char string, with the exact messages; `format` default
and rejection; (b) `format_args("mp4")` contains `-S` with
`codec:h264:m4a,res:1080` and `--merge-output-format mp4`;
`format_args("mp3")` contains `-x` and `--audio-format mp3`;
(c) `parse_progress("SV  42.3%")` → 42, non-progress lines → `None`;
(d) `friendly_error` maps three stderr samples and the fallback;
(e) sha256 verification helper accepts a temp file with the right digest
and rejects a wrong one; (f) `asset_names()` under monkeypatched
`sys.platform`/`platform.machine` returns the four expected pairs;
(g) `EXPORT_FILENAME_RE` matches `Talk-abc123.mp3`;
(h) `validate_download_options` is reachable through `VALIDATORS["download"]`.

**Frontend agent** — `static/index.html`, `static/js/download.js` (new),
`static/js/core.js` (**only** add `"view-download"` to `VIEWS` and
`download` to `VIEW_KIND`), `static/style.css`.
- Follow `static/js/qr.js` as the template for a tile file: locals from
  `SV`, `readDownload` / `validateDownload` (same messages as the backend,
  verbatim) / `downloadPayload` / `updateDownload`, `SV.registerTile`,
  `SV.wireTileForm("download", tile, {autoUpdate: true})`; `enter` fetches
  `/api/download/status` into `download-tools`.
- Script tag for `download.js` goes **before** `shell.js`.
- Keep the styling to existing classes (`.group`, `.seg-group`, `.hint`,
  `.btn`); at most a few lines of new CSS.

## Do not

- No cookies, logins, playlists, other sites, quality pickers, or
  subtitles. No bundling of yt-dlp or Deno into the app.
- No URL, title or filename in any analytics prop, crash report or log
  that leaves the machine. `download.log` is local only.
- Do not touch other tiles, `render/`, `updater.py`, `version.py`,
  `whatsnew.py`. Do not run anything on port 8765; dev runs use
  `PORT=8799 SERVICE_VISUALS_STATS=0 SERVICE_VISUALS_CONFIG=<scratch>`.
- Do not `git stash`, `reset` or `checkout`. Do not commit.

## Done means

- `SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py` passes
  (offline) and pyflakes is clean over `*.py routes/*.py scripts/*.py`.
- Orchestrator, on this Mac: one real MP4 and one real MP3 of a
  Creative-Commons video through the UI; `ffprobe` shows `h264`/`aac` in
  an mp4 container and `mp3`; a `vimeo.com` link and a private video show
  their plain-English messages; the first run fetched and verified both
  tools with the progress bar moving.
- Windows is unverified here (binary names, `deno.exe` unzip, VC++
  runtime for `yt-dlp.exe`) — say so in the release notes to the owner.
