# Service Visuals

A localhost web app for church tech volunteers. It generates simple motion
visuals — currently a **countdown timer** and a **decision spinner wheel** —
and exports them as MP4 files ready to drop into ProPresenter. Everything
runs on your Mac: no cloud, no accounts, no system installs (a static ffmpeg
binary ships with the Python dependencies).

## Quick start

```bash
./run.sh
```

Then open <http://localhost:8765> in your browser.

The first run creates a `.venv` virtual environment and installs the three
Python dependencies (Flask, Pillow, imageio-ffmpeg); after that it starts in
a couple of seconds. Stop the server with `Ctrl+C`.

If port 8765 is taken, pick another one: `PORT=9000 ./run.sh`.

## The visuals

### Countdown timer

Counts down from your chosen time to 00:00, then holds.

| Option | What it does |
|---|---|
| Minutes / seconds | Total length, from 5 seconds up to 120 minutes. |
| Style | `classic` (huge centered digits), `ring` (digits inside a depleting circular ring), or `bar` (thin progress bar along the bottom). |
| Accent color | Hex color (e.g. `#e8b44f`) used for the ring/bar and warning digits. |
| Warn in last 10 s | Digits shift to the accent color for the final 10 seconds. |
| Hold at 00:00 | Keeps 00:00 on screen for 0–30 extra seconds at the end (default 5). |

Timers of 60 minutes or more display as H:MM:SS; shorter ones as MM:SS.

### Spinner wheel

A wheel of 2–20 entries (names, options, prizes — up to 40 characters each)
that winds up, spins for several seconds, and lands on a winner with a
highlight card.

| Option | What it does |
|---|---|
| Entries | One wheel segment per line; blank lines are ignored. |
| Mode | `random` picks the winner honestly. `rigged` lands on the entry you choose — the landing spot is jittered so it never looks staged. |
| Winner | Required in rigged mode; must exactly match one of the entries. |
| Accent color | Hex color used for the pointer and winner highlight. |

## Where the files go

Finished videos land in the `exports/` folder next to this README, with
descriptive names like `timer_5m00s_ring_20260704-103000.mp4`. When a render
finishes, the UI offers **Download** and **Reveal in Finder** buttons.

## Importing into ProPresenter

1. Render and locate the MP4 (use **Reveal in Finder**).
2. In ProPresenter, drag the file into a **Media Bin** — or straight into a
   playlist — like any other video.
3. For countdowns, set the media's playback behavior so it does not loop.

## Output spec

- 1920×1080 (1080p), 30 fps
- H.264 in an MP4 container, `yuv420p` pixel format, `+faststart` — the
  most ProPresenter-compatible combination.
- No audio track.

## Roadmap

- Automatic import into ProPresenter (v1 is manual drag-in).
- More visual types — the home screen's tile grid and the renderer registry
  are designed so new visuals slot in without restructuring.
