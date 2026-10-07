# A Day in Lines

An e-ink picture for the wall that draws itself. Give it any image, and over 12 hours a Raspberry Pi redraws it on the panel as a graphite pencil sketch: one stroke group per minute, 720 in all. Nothing is erased or redrawn, so anyone walking past sees a drawing in the middle of being made.

![The finished sketch](docs/final.png)

![The day as a timelapse](docs/timelapse.gif)

The sheet at each hour of the day ([full size](docs/contact_sheet.png)), and the finished frame as the 1-bit panel shows it ([e-ink](docs/final_eink.png)).

| Minutes | Phase | What appears |
|---|---|---|
| 1–40 | Composition | Faint, loose construction lines of the big shapes |
| 41–270 | Linework | Contours from coarse to fine, strongest first, then thin lines and detail |
| 271–670 | Shading | Hatching in six layers, light to dark, swept left to right |
| 671–720 | Finishing | Darkest accents, re-stated edges, a few stray marks |

## What's here

| Path | What it is |
|---|---|
| `a_day_in_lines.py` | The program: processes images, drives the display, serves the viewer |
| `viewer.html` | The viewer page the Pi serves (keep it next to the program) |
| `web/standalone.html` | The same viewer as a single offline page. Open it in any browser to break down your own images; no Pi needed |
| `examples/tanker.png` | Sample image |
| `systemd/day-in-lines.service` | Runs it at boot |
| `LICENSE` | MIT |

## Process once, display from stored results

Each picture is processed exactly once, up front. Processing stores everything about the picture as static files:

- the finished display frame for every minute (`frames/000.png` to `frames/720.png`);
- the strokes for the web viewer;
- the exposure sheet.

While running, the display does no drawing or rendering: each minute it opens that minute's frame and pushes it.

- **New pictures:** add a file to the picture folder (or upload one from the viewer) and the watcher notices it within a minute. It processes the picture in a separate low-priority process, so the display never waits on it.
- **Missing results:** if a picture's results don't exist yet when it's due, they are created then. This normally happens only for the very first picture on first boot.
- **No repeat processing:** results are keyed by the image's contents. Renaming, moving or re-uploading the same picture reuses them.
- **To process a whole folder ahead of time:** `python3 a_day_in_lines.py --image ~/pictures bake`
- **Storage:** about 5–15 MB per picture, in `~/.cache/a-day-in-lines/baked/`.
- **Changing display settings:** if you change `--size`, `--gamma`, `--fit` or `--mono`, the frames are made again for the new settings. Each setting combination is processed once.
- **Daily pick:** each day's picture is chosen once and remembered. New pictures go first, then whichever was drawn longest ago.

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

`--image` can be a single file or a folder; with a folder, a different picture is drawn each day.

## The viewer on your phone (`--serve`)

Open `http://<pi-address>:8080/` on any device on your network.

- **Live:** the page shows exactly the strokes on the display, at the current minute. New lines appear in blue as the display draws them. You can scrub back through the day, or tap any row of the exposure sheet to jump to that minute.
- **Any image:** pick or drop an image to preview its breakdown, then tap **Send to display**.
  - *Start drawing now:* the panel clears and starts the new image at minute 1. It finishes 12 hours later, stays up until the next 07:00 start, then the normal schedule resumes.
  - *Start at 07:00 tomorrow:* today's drawing finishes first.

Uploaded images are saved into your picture folder, so they join the rotation and are processed once like any other.

## Breaking an image down without the display

```bash
python3 a_day_in_lines.py --image photo.jpg breakdown -o photo/ --frames 1
```

This writes:

- `exposure_sheet.csv`: every minute's time, phase, the lines it adds, and its stroke count.
- `day_in_lines.html`: the viewer for that image, which works offline.
- `frames/001.png … 720.png`: the sheet after each minute, with that minute's new lines in blue. Use `--frames 60` for one frame per hour.

Other commands:

- `plan` prints the schedule.
- `frame 360 -o f.png --mono` renders the sheet at any minute, as the panel shows it.
- `preview -o dir/` makes the final sheet, an hourly contact sheet and a timelapse GIF.

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
- **Ghosting:** some panels build up ghosting after many partial refreshes. `--full-refresh-every 120` adds a blink every two hours; the image itself is unchanged.
- **Other panels:** `--display waveshare:<module>` takes any Waveshare module that supports `display_Partial`. Use `--fit pad` to keep the whole image instead of cropping it to 5:3.
- **Tuning:** use `--gamma` for e-ink darkness (default 2.4, higher means darker). Each picture has its own fixed hand, derived from its contents, so its results never need redoing.
