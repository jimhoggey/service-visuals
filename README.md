# Service Visuals

A local web app for church tech teams. Quickly generate simple motion visuals —
a **countdown timer** and a **decision spinner wheel** — and export them as
1080p MP4 files ready to drag into **ProPresenter**.

No accounts, no cloud, no ffmpeg install needed (a static ffmpeg binary is
bundled via pip). Everything runs and renders on your machine.

## Install & run

You need **Python 3.9+** ([python.org/downloads](https://www.python.org/downloads/) —
on Windows, tick "Add Python to PATH" during install).

**Mac**

```bash
git clone https://github.com/jimhoggey/service-visuals.git
cd service-visuals
./run.sh
```

**Windows**

```bat
git clone https://github.com/jimhoggey/service-visuals.git
cd service-visuals
run.bat
```

(No git? Download the ZIP from GitHub — green **Code** button → Download ZIP —
unzip it, and run `run.sh` / `run.bat` from that folder.)

The first run creates a virtual environment and installs the three
dependencies (Flask, Pillow, imageio-ffmpeg), then starts the server.
Every run after that starts in a couple of seconds.

Then open **http://localhost:8765** in your browser.

## Using it

1. Pick a tile — **Timer** or **Spinner** — and configure it. The live
   preview shows what you'll get before you spend any render time.
   - **Timer**: duration, three styles (Classic / Ring / Bar), accent colour,
     warn colour in the final 10 seconds, hold at 0:00.
   - **Spinner**: one entry per line (2–20), Random or Choose-winner mode,
     accent colour. Try **Test Spin** in the preview.
2. Hit **Export**. The video renders locally into the `exports/` folder.
3. **Download** or **Reveal in Finder**, then drag the MP4 into a
   ProPresenter media bin or playlist.

Output spec: 1920×1080, 30 fps, H.264 MP4 (yuv420p, faststart) — plays in
ProPresenter out of the box.

## Roadmap

- Automatic import into ProPresenter (currently manual drag-in)
- More visual types — the tile grid is built to grow
