# Service Visuals

A desktop app for church tech teams. Quickly generate simple visuals —
a **countdown timer**, a **decision spinner wheel**, a **QR "scan to…" card**,
a **seamless motion background**, and a **scoreboard editor** — and export them
as 1080p MP4s or PNGs ready to drag into **ProPresenter**.

No accounts, no cloud, nothing else to install. Everything renders on your
machine.

## Install (no terminal needed)

Grab the latest build from the
[**Releases page**](https://github.com/jimhoggey/service-visuals/releases):

**Mac** — download `ServiceVisuals-mac.zip`, unzip, then **drag Service Visuals
into your Applications folder** and open it from there (Launchpad or Spotlight).
First open only: the app is unsigned, so right-click it → **Open** → **Open**.

> Moving it to Applications isn't optional housekeeping. If you run it straight
> from Downloads, macOS launches a temporary read-only copy of it
> ("App Translocation") and the app cannot update itself — it will tell you so
> rather than pretending to update.

**Windows** — download `ServiceVisuals-windows.zip`, unzip, and put
**Service Visuals.exe** wherever you like (e.g. your Desktop). To get it in
the Start menu, right-click the exe → **Pin to Start**. First open only:
if SmartScreen appears, click **More info** → **Run anyway**.

Launching the app opens the Service Visuals window — configure, export, done.
Exported MP4s are saved to **Documents → Service Visuals**.

**Updates install themselves.** When a new version is released, an amber
UPDATE pill appears in the header — click **INSTALL** and the app downloads
the new version, swaps itself out, and restarts. No manual reinstalling. If a
swap ever fails, the app says so on its next launch (and keeps a log at
`~/.service-visuals/update.log`) instead of quietly staying on the old version.

**Anonymous usage counts.** So we can see which tools actually get used, the
app sends a tiny anonymous event when a visual is exported — just the event
name (e.g. `export`, `scoreboard`), the app version and the operating system.
If something goes wrong (an export fails, the app crashes or won't open) it
also sends the *shape* of the error: the error type and where in the app's own
code it happened, with any file paths, file names and quoted values removed
first — the full details stay on your computer in
`~/.service-visuals/crash.log`. Nothing you type, upload, name or link is ever
sent, there is no user or device ID, and it can't be traced back to a person
or a church. It goes to [Aptabase](https://aptabase.com/) (open source,
privacy-first). Turn it off any time with the **ANONYMOUS USAGE COUNTS**
switch in the footer. The full list of events lives in [`stats.py`](stats.py).

## Using it

1. Pick a tile — **Timer**, **Spinner**, **QR card** or **Motion background** —
   and configure it. The live
   preview shows what you'll get before you spend any render time.
   - **Timer**: duration, three styles (Classic / Ring / Bar), accent colour,
     warn colour in the final 10 seconds, hold at 0:00.
   - **Spinner**: one entry per line (2–100), Random or Choose-winner mode,
     accent colour. Try **Test Spin** in the preview. **Fill with AI** can
     top up the wheel — type what you need (e.g. "books of the Bible", "world
     capitals") and either give an exact number or tick **Full list** to let
     the AI return the complete set. Uses your own
     [OpenRouter](https://openrouter.ai/keys) key, pasted once and stored only
     on your computer; the model is selectable (defaults to `openrouter/free`,
     which auto-picks a working free model).
   - **QR card**: a "scan to…" code from any website or plain text, with an
     optional heading and caption, accent colour, a 9-way on-screen position,
     and an optional background image. The preview shows the real, scannable
     code (hit **Refresh** to regenerate) — perfect for giving, connect cards
     and signups. Export it as a clip (**EXPORT MP4**) or, since nothing in it
     moves, as a still **EXPORT PNG** — instant, and usually the simpler thing
     to drop into ProPresenter.
   - **Motion background**: a seamlessly-looping ambient background in your
     accent colour — Aurora, Bokeh or Waves — for worship and ambient moments.
   - **Scoreboard**: upload your existing points screen once. The app reads the
     numbers with your operating system's own text recognition and saves it as a
     reusable board. Each week: open it, click a number, type the new score,
     export. The PNG is identical to your original except for the digits you
     changed — the new digits are lifted from the image itself, so they match the
     original font exactly. Numbers it misses can be marked by hand with
     **ADD A NUMBER**. Works best on flat or lightly-textured backgrounds —
     on heavily textured artwork (busy AI-generated posters with gradients
     and multi-layer outlines) the digits are replaced correctly but faint
     traces of the old number can remain around them. If the recogniser misses a number (it happens more
     on Windows than on Mac), click **ADD A NUMBER** and drag a box around it —
     it becomes editable like the rest.
2. Hit **Export**. The video renders locally.
3. Hit **Reveal in Finder** (Mac) / **Show in Explorer** (Windows) — it
   selects the exported file so you can drag it straight into a ProPresenter
   media bin or playlist.

Output spec: 1920×1080, 30 fps, H.264 MP4 (yuv420p, faststart) — plays in
ProPresenter out of the box.

## Run from source (developers)

Needs Python 3.9+. Clone the repo, then `./run.sh` (Mac) or `run.bat`
(Windows) and open http://localhost:8765. Exports land in `exports/` inside
the repo. Desktop builds are produced by CI (`.github/workflows/build.yml`,
PyInstaller + pywebview) on every `v*` tag.

## Roadmap

- Automatic import into ProPresenter (currently manual drag-in)
- More visual types — the tile grid is built to grow
