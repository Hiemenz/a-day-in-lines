# A Day in Lines

An e-ink picture for the wall that draws itself. Give it any image, and over 12 hours a Raspberry Pi redraws it on the panel as a graphite pencil sketch: one stroke group per minute, 720 in all. Nothing is erased or redrawn, so anyone walking past sees a drawing in the middle of being made.

![The finished sketch](docs/final.png)

![The day as a timelapse](docs/timelapse.gif)

The sheet at each hour of the day ([full size](docs/contact_sheet.png)), and the finished frame as the 1-bit panel shows it ([e-ink](docs/final_eink.png)).

## What's in the repo

| Path | What it is |
|---|---|
| `a_day_in_lines.py` | The whole program: turns images into a day of strokes, renders the frames, drives the display, and serves the viewer |
| `viewer.html` | The viewer page the Pi serves. Keep it next to the program; the program fills in the live drawing |
| `web/standalone.html` | The same viewer as one offline page with a sample drawing built in. Open it in any browser to break down your own images; no Pi needed |
| `examples/tanker.png` | Sample image |
| `docs/` | The images in this README: final sheet, e-ink frame, hourly contact sheet, timelapse |
| `systemd/day-in-lines.service` | Runs the display at boot |
| `requirements.txt` | numpy, scipy, Pillow, scikit-image |
| `LICENSE` | MIT |

## How a picture becomes a day of drawing

The image is fitted to an 800×480 sheet (cropped to 5:3 by default, or padded with `--fit pad`). Transparent areas count as blank paper. The program then plans 720 drawing events, the way an animator works at a desk:

| Minutes | Phase | What appears |
|---|---|---|
| 1–40 | Composition | Faint, loose construction lines of the big shapes, found by coarse edge detection |
| 41–270 | Linework | Contours from coarse to fine, strongest first, then thin dark lines (masts, rigging) and fine detail |
| 271–670 | Shading | Hatching in six layers, light to dark, each at its own angle, nudged to follow the image's local flow |
| 671–720 | Finishing | Darkest accents, re-stated strong edges, a few stray construction marks |

Every line goes through a simulated pencil: a slight wobble, overshoot at the ends, a taper, varying pressure, and graphite that catches on the paper grain. The plan is deterministic for a given image and seed.

## Process once, display from stored results

Each picture is processed exactly once, up front. Processing stores everything about it as static files in `~/.cache/a-day-in-lines/baked/<hash>-<fit>-<size>-<mono|gray>-g<gamma>-s<seed>/`:

- `frames/000.png` to `frames/720.png`: the finished display frame for every minute;
- `viewer.json`: the strokes, for the web viewer;
- `meta.json`: the name and exposure sheet, written last, so a half-finished folder is never used.

While running, the display does no drawing or rendering: each minute it opens that minute's frame and pushes it.

- **New pictures:** add a file to the picture folder (or upload one from the viewer) and the watcher notices it within a minute. It processes the picture in a separate low-priority process, so the display never waits on it. About 20–30 seconds per picture on a Pi 5.
- **Missing results:** if a picture's results don't exist yet when it's due, they are created then. This normally happens only for the very first picture on first boot.
- **No repeat processing:** results are keyed by the image's contents. Renaming, moving or re-uploading the same picture reuses them. A lock stops two processes (say, the service and a manual `bake`) from processing the same picture at once.
- **Pictures that can't be read** are reported once in the log and skipped; the display carries on with the others.
- **To process a whole folder ahead of time:** `python3 a_day_in_lines.py --image ~/pictures bake`
- **Storage:** about 10–20 MB per picture.
- **Changing settings:** if you change `--size`, `--gamma`, `--fit`, `--mono` or `--seed`, the frames are made again for the new settings. Each combination is processed once.
- **Daily pick:** each day's picture is chosen once and remembered in `library.json`, so reboots agree. New pictures go first, then whichever was drawn longest ago.

## Raspberry Pi setup

```bash
sudo apt install python3-numpy python3-scipy python3-pil python3-skimage
git clone https://github.com/Hiemenz/a-day-in-lines.git ~/a-day-in-lines
git clone https://github.com/waveshareteam/e-Paper.git ~/e-Paper       # panel driver

# Try it without the panel
python3 ~/a-day-in-lines/a_day_in_lines.py --image ~/a-day-in-lines/examples/tanker.png preview -o preview/

# Live on a 7.5" 800x480 panel, with the viewer on port 8080
export PYTHONPATH=~/e-Paper/RaspberryPi_JetsonNano/python/lib
python3 ~/a-day-in-lines/a_day_in_lines.py --image ~/pictures/ run --display waveshare:epd7in5_V2 --serve 8080
```

`--image` can be a single file or a folder; with a folder, a different picture is drawn each day. scikit-image is optional: without it the thin-line pass is skipped.

## The viewer on your phone (`--serve`)

Open `http://<pi-address>:8080/` on any device on your network.

- **Live:** the page shows exactly the strokes on the display, at the current minute. New lines appear in blue as the display draws them. You can scrub back through the day, or tap any row of the exposure sheet to jump to that minute.
- **Any image:** pick or drop an image to preview its breakdown, then tap **Send to display**.
  - *Start drawing now:* the panel clears and starts the new image at minute 1, replacing any other uploaded drawing on the sheet.
  - *Start at 07:00 tomorrow* (your `--start` time): waits for whatever is on the sheet to finish. Several uploads queue up and are drawn one after another; the page tells you when each one will start.
- **When an upload hands back:** a finished upload stays up until the next start time, like any day's drawing. If it finishes early in a day's drawing time (an upload started in the evening finishing the next morning), the day's drawing takes over straight away, part-drawn, instead of leaving the panel idle for a day.
- **Who can upload:** by default, anyone on your network. Start with `--upload-key SOMEWORD` and only a viewer opened as `http://<pi-address>:8080/?key=SOMEWORD` can send pictures; watching needs no key.

Uploaded images are saved into your picture folder (or `~/.cache/a-day-in-lines/uploads/` when `--image` is a single file), so they join the rotation and are processed once like any other. Formats the folder scan doesn't use, such as GIF, are stored as PNG.

## Breaking an image down without the display

```bash
python3 a_day_in_lines.py --image photo.jpg breakdown -o photo/ --frames 1
```

This writes:

- `exposure_sheet.csv`: every minute's time, phase, the lines it adds, and its stroke count.
- `day_in_lines.html`: the viewer for that image, which works offline.
- `frames/001.png … 720.png`: the sheet after each minute, with that minute's new lines in blue. Use `--frames 60` for one frame per hour.

## Command reference

Global options go before the command: `python3 a_day_in_lines.py --image PATH [--seed N] [--fit crop|pad] COMMAND ...`

| Command | What it does |
|---|---|
| `run` | Drive the display (see below) |
| `bake` | Process an image, or every image in a folder, ahead of time (`--size`, `--gamma`, `--gray`, `--cache`) |
| `plan` | Print the 720-minute schedule (`--start HH:MM`) |
| `breakdown` | Exposure sheet, offline viewer and optional per-minute frames (`-o`, `--start`, `--frames N`) |
| `frame K` | Render the sheet after K minutes (`-o file.png`, `--mono` for the 1-bit panel look, `--gamma`) |
| `preview` | Final sheet, e-ink frame, hourly contact sheet and timelapse GIF (`-o dir/`, `--every N`) |

Options for `run`:

| Option | Default | Meaning |
|---|---|---|
| `--display` | `file` | `file` writes `--out` (default `current.png`); `waveshare:<module>` drives a panel, e.g. `waveshare:epd7in5_V2` |
| `--start` | `07:00` | When each day's drawing begins |
| `--serve PORT` | off | Serve the viewer and accept uploads |
| `--upload-key KEY` | none | Require `?key=KEY` to upload |
| `--scan-every SEC` | `60` | How often to look for new pictures |
| `--mono / --no-mono` | mono | Store 1-bit frames, or greyscale frames that are dithered when shown |
| `--gamma` | `2.4` | Darkens mid-tones before dithering; higher is darker |
| `--full-refresh-every N` | `0` | A full (blinking) refresh every N updates to clear ghosting; 0 = never |
| `--cache` | `~/.cache/a-day-in-lines` | Where processed pictures, `library.json` and the upload queue live |

**Seeds:** on the display (`run`, `bake`) each picture's seed comes from its contents, so it gets its own fixed hand and never needs processing again. The one-off commands (`plan`, `frame`, `preview`, `breakdown`) default to the date as the seed, so the same picture comes out slightly differently on another day. `--seed` fixes it everywhere.

## Run at boot

```bash
sudo cp ~/a-day-in-lines/systemd/day-in-lines.service /etc/systemd/system/
sudo systemctl enable --now day-in-lines
journalctl -u day-in-lines -f        # watch it work
```

The unit assumes user `pi`, the repo in `~/a-day-in-lines`, pictures in `~/pictures` and the driver in `~/e-Paper`; edit it if yours differ.

## How it behaves

- **Every minute:** the stored frame for that minute is pushed with a partial refresh. Each frame differs from the one before it only by that minute's new strokes. The frames use ordered (Bayer) dithering, which is local, so only pixels under a new stroke change and the panel never shimmers.
- **Reboots:** the current minute's stored frame is opened straight away, including for an uploaded picture that is mid-drawing. Nothing is rebuilt.
- **Before the start time:** the last finished drawing stays up. At the start time the sheet clears to blank paper, which is the day's one full refresh.
- **Errors:** if a frame can't be shown (a display hiccup, a missing file), the error is logged and the display tries again 30 seconds later with a full refresh, instead of exiting.
- **Ghosting:** some panels build up ghosting after many partial refreshes. `--full-refresh-every 120` adds a blink every two hours; the image itself is unchanged.
- **Other panels:** `--display waveshare:<module>` takes any Waveshare module; partial refresh is used where the module supports `display_Partial`. Use `--fit pad` to keep the whole image instead of cropping it to 5:3.
