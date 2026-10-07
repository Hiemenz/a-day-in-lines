"""The exposure sheet and viewer page: an image broken down into its 720 line additions."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .constants import H, TOTAL, W
from .planner import build_plan
from .render import Sheet


def viewer_plan(events) -> str:
    """Compact JSON the viewer page reads: [phase, label, [[p, w, taper, delta-encoded pts x2], ...]]."""
    out = []
    for e in events:
        ss = []
        for s in e["strokes"]:
            p = np.round(np.asarray(s["pts"]) * 2).astype(int)
            flat = [int(p[0, 0]), int(p[0, 1])] + np.diff(p, axis=0).ravel().tolist()
            ss.append([round(s["p"], 2), round(s["w"], 2), 1 if s["taper"] else 0, flat])
        out.append([e["phase"][0], e["label"], ss])
    return json.dumps(out, separators=(",", ":"))


def script_json(text: str) -> str:
    """JSON that is safe inside a <script> block (a name can't close the tag)."""
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def viewer_page(plan_json: str) -> str:
    """The viewer with a plan in it; the live slot is left for fill_live."""
    tpl = (Path(__file__).resolve().parent.parent / "viewer.html").read_text()
    tpl = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
           '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>' + tpl + '</body></html>')
    return tpl.replace("/*PLAN*/[]", script_json(plan_json))


def fill_live(page: str, live) -> str:
    return page.replace("/*LIVE*/null", script_json(json.dumps(live)))


def viewer_html(events, live=None, plan_json=None) -> str:
    return fill_live(viewer_page(plan_json if plan_json is not None else viewer_plan(events)), live)


def write_xsheet(events, path: Path, start: dt.time, dur=dt.timedelta(minutes=TOTAL)):
    t0 = dt.datetime.combine(dt.date.today(), start)
    with open(path, "w") as f:
        f.write("minute,time,phase,lines_added,strokes\n")
        for i, e in enumerate(events):
            t = t0 + dur * (i + 1) / TOTAL
            f.write(f'{i + 1},{t:%H:%M},{e["phase"]},"{e["label"]}",{len(e["strokes"])}\n')


def breakdown(image, seed, fit_mode, outdir: Path, start: dt.time, frames_every: int, dur=dt.timedelta(minutes=TOTAL)):
    """Everything the viewer shows, as files: the exposure sheet, an offline viewer
    page, and optionally the sheet at each iteration with that minute's new lines in blue."""
    outdir.mkdir(parents=True, exist_ok=True)
    events = build_plan(image, seed, fit_mode)
    write_xsheet(events, outdir / "exposure_sheet.csv", start, dur)
    (outdir / "day_in_lines.html").write_text(viewer_html(events))
    if frames_every:
        fd = outdir / "frames"
        fd.mkdir(exist_ok=True)
        sh = Sheet(seed)
        blue = np.array([0.18, 0.43, 0.84], np.float32)
        for i, e in enumerate(events):
            sh.draw_event(e)
            m = i + 1
            if m % frames_every and m != TOTAL:
                continue
            new = Sheet(seed, pegs=False)
            new.draw_event(e)
            ink = (1 - new.canvas)[..., None]
            img = np.asarray(sh.paper(), np.float32) / 255
            img = img * (1 - ink * .85) + blue * ink * .85
            Image.fromarray(np.clip(img * 255, 0, 255).astype(np.uint8)).resize((W, H), Image.LANCZOS) \
                .save(fd / f"{m:03d}.png")
    total = sum(len(e["strokes"]) for e in events)
    print(f"{image.name}: {total} strokes in {TOTAL} minutes -> {outdir}")
    return events
