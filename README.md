# Service Visuals

A desktop app for church tech teams. Quickly generate simple motion visuals —
a **countdown timer**, a **decision spinner wheel**, a **QR "scan to…" card**
and a **seamless motion background** — and export them as 1080p MP4 files
ready to drag into **ProPresenter**.

No accounts, no cloud, nothing else to install. Everything renders on your
machine.

## Install (no terminal needed)

Grab the latest build from the
[**Releases page**](https://github.com/jimhoggey/service-visuals/releases):

**Mac** — download `ServiceVisuals-mac.zip`, unzip, drag **Service Visuals**
into your **Applications** folder, and open it from Launchpad or Spotlight.
First open only: the app is unsigned, so right-click it → **Open** → **Open**.

**Windows** — download `ServiceVisuals-windows.zip`, unzip, and put
**Service Visuals.exe** wherever you like (e.g. your Desktop). To get it in
the Start menu, right-click the exe → **Pin to Start**. First open only:
if SmartScreen appears, click **More info** → **Run anyway**.

Launching the app opens the Service Visuals window — configure, export, done.
Exported MP4s are saved to **Documents → Service Visuals**.

**Updates install themselves.** When a new version is released, an amber
UPDATE pill appears in the header — click **INSTALL** and the app downloads
the new version, swaps itself out, and restarts. No manual reinstalling.

## Using it

1. Pick a tile — **Timer**, **Spinner**, **QR card** or **Motion background** —
   and configure it. The live
   preview shows what you'll get before you spend any render time.
   - **Timer**: duration, three styles (Classic / Ring / Bar), accent colour,
     warn colour in the final 10 seconds, hold at 0:00.
   - **Spinner**: one entry per line (2–20), Random or Choose-winner mode,
     accent colour. Try **Test Spin** in the preview.
   - **QR card**: a "scan to…" code from any URL or text, with an optional
     heading and caption, accent colour, and clip length — perfect for giving,
     connect cards and signups.
   - **Motion background**: a seamlessly-looping ambient background in your
     accent colour — Aurora, Bokeh or Waves — for worship and ambient moments.
2. Hit **Export**. The video renders locally.
3. **Download** or **Reveal in Finder/Explorer**, then drag the MP4 into a
   ProPresenter media bin or playlist.

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
