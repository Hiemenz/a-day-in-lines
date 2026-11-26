"""Step-by-step drawing lessons: a drawing made a few minutes at a step, then every step side by side.

A lesson is a JSON file (see `lessons/animals/swan.json` and the README):

    {"title": "Swan", "credit": "after E. G. Lutz, 1913",
     "steps": [[shape, ...], [shape, ...], ...]}

and a shape is one of

    {"line":   [[x, y], ...]}                 straight segments
    {"curve":  [[x, y], ...]}                 a smooth curve through the points
    {"circle": [x, y, r]}
    {"fill":   [[x, y], ...], "gap": 4}       a patch of level strokes; a bigger gap is paler

Coordinates are in any units, x to the right and y down: the drawing is scaled to fit the sheet.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from .constants import H, SS, W
from .pen import Pen, arc, length, resample
from .render import Sheet, frame_for

STEP_MINUTES = 5
HOURLY_DRAW_MINUTES = 55                     # on the hour: this long drawing, the rest of the hour on all the steps
HOURS_A_DAY = 12
MIN_NEW_PIXELS = 12                          # fewer new black pixels than this and a step can't be seen
MAX_STEPS = 60
MAX_COORD = 100_000
SHAPES = ("line", "curve", "circle", "fill")
BOX = (4, 4, W - 4, H - 4)                   # where the large drawing goes: the whole sheet, nothing else on it
GRID = (6, 50, W - 6, H - 6)                 # where the progression goes
CELL_MARGIN = (22, 5, 3, 5)                  # left, top, right, bottom: room for the step's number


@dataclass(frozen=True)
class Lesson:
    title: str
    credit: str
    steps: tuple               # per step, a list of paths (each an (n, 2) array)

    @property
    def bounds(self):
        pts = np.vstack([p for st in self.steps for p in st])
        return (*pts.min(0), *pts.max(0))


# ---- reading a lesson -------------------------------------------------------
def spline(pts, per=14):
    """A smooth curve through every point (Catmull-Rom)."""
    if len(pts) < 3:
        return pts
    p = np.vstack([pts[0], pts, pts[-1]])
    t = np.linspace(0, 1, per)[:, None]
    out = []
    for a, b, c, d in zip(p, p[1:], p[2:], p[3:]):
        out.append(.5 * (2 * b + (c - a) * t + (2 * a - 5 * b + 4 * c - d) * t ** 2 + (3 * b - a - 3 * c + d) * t ** 3))
    return np.vstack(out)


def level_strokes(poly, gap):
    """Level strokes `gap` apart that fill a polygon."""
    out, y = [], poly[:, 1].min() + min(1.0, gap / 2)
    while y < poly[:, 1].max():
        xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                    for a, b in zip(poly, np.roll(poly, -1, 0)) if (a[1] <= y) != (b[1] <= y))
        out += [np.array([(a, y), (b, y)]) for a, b in zip(xs[::2], xs[1::2])]
        y += gap
    return out


def _numbers(value, shape, what):
    try:
        a = np.asarray(value, float)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be numbers")
    if a.ndim != len(shape) or any(want is not None and got != want for got, want in zip(a.shape, shape)):
        raise ValueError(f"{what} has the wrong shape")
    if not (np.isfinite(a).all() and (np.abs(a) <= MAX_COORD).all()):
        raise ValueError(f"{what} is out of range")
    return a


def shape_paths(shape, what):
    kinds = [k for k in SHAPES if k in shape] if isinstance(shape, dict) else []
    if len(kinds) != 1:
        raise ValueError(f"{what} must have exactly one of {', '.join(SHAPES)}")
    kind = kinds[0]
    extra = set(shape) - {kind} - ({"gap"} if kind == "fill" else set())
    if extra:
        raise ValueError(f"{what} has unknown keys: {', '.join(sorted(extra))}")
    if kind == "circle":
        x, y, r = _numbers(shape[kind], (3,), what)
        if r <= 0:
            raise ValueError(f"{what}: the radius must be above 0")
        return [arc(x, y, r, r, 0, 360, 36)]
    pts = _numbers(shape[kind], (None, 2), what)
    if len(pts) < (3 if kind == "fill" else 2):
        raise ValueError(f"{what} has too few points")
    if kind == "fill":
        gap = float(_numbers(shape.get("gap", 4), (), f"{what} gap"))
        if gap < .5:
            raise ValueError(f"{what}: the gap must be at least 0.5")
        paths = level_strokes(pts, gap)
        if not paths:
            raise ValueError(f"{what} is too small to fill")
        return paths
    return [spline(pts) if kind == "curve" else pts]


def parse_lesson(data) -> Lesson:
    """A Lesson from a decoded JSON file. Raises ValueError saying what is wrong."""
    if not isinstance(data, dict):
        raise ValueError("a lesson is a JSON object")
    title, credit, steps = data.get("title"), data.get("credit", ""), data.get("steps")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("a lesson needs a title")
    if not isinstance(credit, str):
        raise ValueError("the credit must be text")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"a lesson needs 1 to {MAX_STEPS} steps")
    out = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, list) or not step:
            raise ValueError(f"step {i} needs at least one shape")
        out.append([p for j, s in enumerate(step, 1) for p in shape_paths(s, f"step {i}, shape {j}")])
    lesson = Lesson(title.strip(), credit.strip(), tuple(out))
    x0, y0, x1, y1 = lesson.bounds
    if max(x1 - x0, y1 - y0) <= 0:
        raise ValueError("the drawing has no size")
    return lesson


def load_lesson(path: Path) -> Lesson:
    try:
        return parse_lesson(json.loads(Path(path).read_text()))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"{path}: {e}")


def lesson_files(paths) -> list:
    """The lesson files named, with each folder standing for the .json files in it and its subfolders."""
    out = []
    for p in paths:
        found = (sorted(f for f in Path(p).rglob("*.json") if not any(x.startswith(".") for x in f.relative_to(p).parts))
                 if Path(p).is_dir() else [Path(p)])
        if not found:
            raise ValueError(f"{p} has no lesson files")
        out += found
    return out


def check_lessons(files, draw_minutes: int = HOURLY_DRAW_MINUTES):
    """Make sure every file can really be shown: it reads, its steps fit the drawing time, every
    step puts something new on the screen, and no two share a title. Returns (good, problems):
    [(file, lesson)] and [(file, what is wrong)]."""
    good, problems, titles = [], [], {}
    for f in files:
        try:
            lesson = load_lesson(f)
            step_times(len(lesson.steps), draw_minutes=draw_minutes)
            frames = Frames(lesson, 1)
            drawn = np.zeros((H, W), bool)
            for i, frame in enumerate(frames.drawing, 1):
                ink = np.asarray(frame.convert("L")) < 128
                # only marks clear of what was there: going over an old line wobbles a pixel or two off it
                if (ink & ~ndimage.binary_dilation(drawn, iterations=2)).sum() < MIN_NEW_PIXELS:
                    raise ValueError(f"step {i} adds nothing you could see")
                drawn = ink
            if lesson.title.lower() in titles:
                raise ValueError(f"the title is already used by {titles[lesson.title.lower()]}")
        except (OSError, ValueError) as e:
            problems.append((f, str(e).removeprefix(f"{f}: ")))
            continue
        titles[lesson.title.lower()] = Path(f).name
        good.append((f, lesson))
    return good, problems


# ---- drawing it -------------------------------------------------------------
def fitter(bounds, box):
    """paths in lesson units -> sheet pixels, scaled evenly and centred in `box`."""
    x0, y0, x1, y1 = bounds
    scale = min((box[2] - box[0]) / max(x1 - x0, 1e-9), (box[3] - box[1]) / max(y1 - y0, 1e-9))
    off = np.array([(box[0] + box[2] - (x0 + x1) * scale) / 2, (box[1] + box[3] - (y0 + y1) * scale) / 2])
    return lambda p: np.asarray(p, float) * scale + off


def inked(pen, path, width):
    """One path as pencil strokes, pressed hard so it reads on a 1-bit panel."""
    return pen.line(path, p=1.0, w=width, wob=.2, taper=False)


def draw(sheet, strokes):
    for s in strokes:
        sheet.draw_stroke(s)
        sheet.draw_stroke(s)                  # gone over twice: one pass is too faint at this size


def split(strokes, n):
    """Cut a step's strokes into n runs of about equal length, in drawing order, so the step
    is drawn bit by bit. A long stroke is cut where a run ends."""
    pieces = [(s, resample(s["pts"], 1.0)) for s in strokes]
    total = sum(length(q) for _, q in pieces)
    runs = [[] for _ in range(n)]
    done = 0.0
    for s, q in pieces:
        at = done + np.concatenate([[0], np.cumsum(np.hypot(*np.diff(q, axis=0).T))])
        done = at[-1]
        which = np.minimum((at[1:] / max(total, 1e-9) * n - 1e-9).astype(int), n - 1).clip(0)
        edges = [0, *(np.flatnonzero(np.diff(which)) + 1), len(which)]
        for a, b in zip(edges[:-1], edges[1:]):
            runs[which[a]].append(dict(s, pts=q[a:b + 1].astype(np.float32)))
    return runs


def write(sheet, text, x, y, size, right=False):
    """Lettering on the sheet; with `right`, x is where it ends."""
    if not text:
        return
    mask = Image.new("L", (W * SS, H * SS), 0)
    d = ImageDraw.Draw(mask)
    try:
        font = ImageFont.load_default(size=size * SS)
    except TypeError:
        font = ImageFont.load_default()
    px = x * SS - (d.textlength(text, font=font) if right else 0)
    d.text((px, y * SS), text, font=font, fill=255)
    sheet.canvas *= 1 - np.asarray(mask, np.float32) / 255 * .9


class Frames:
    """Every picture a lesson shows, made up front: each step's pictures as it is drawn
    (`parts` of them, one number for all or a list with one per step), then the progression
    with all the steps side by side."""

    def __init__(self, lesson: Lesson, parts, size=(W, H), mode="mono", gamma=2.4, seed=3):
        self.lesson = lesson
        render = lambda sheet: frame_for(sheet, size, mode, gamma)
        to_sheet = fitter(lesson.bounds, BOX)
        pen = Pen(np.random.default_rng([seed, 1]))
        base = Sheet(seed, pegs=False)
        n = len(lesson.steps)
        parts = [parts] * n if isinstance(parts, int) else list(parts)
        self.drawing = []
        for i, step in enumerate(lesson.steps):
            strokes = [s for p in step for s in inked(pen, to_sheet(p), 1.5)]
            for run in split(strokes, max(1, parts[i])):
                draw(base, run)
                self.drawing.append(render(base))
        self.progression = render(progression_sheet(lesson, seed))

    def __len__(self):
        return len(self.drawing) + 1

    def __getitem__(self, i):
        return self.progression if i == len(self.drawing) else self.drawing[i]


def grid_shape(n, aspect):
    """(columns, rows) for n pictures of the given width / height in the progression area."""
    gw, gh = GRID[2] - GRID[0], GRID[3] - GRID[1]
    left, top, right, bottom = CELL_MARGIN

    def height(c):                            # how tall each drawing comes out with c columns
        return min((gw / c - left - right) / aspect, gh / math.ceil(n / c) - top - bottom)

    cols = max(range(1, n + 1), key=lambda c: (height(c), -c))
    return cols, math.ceil(n / cols)


def progression_sheet(lesson: Lesson, seed=3) -> Sheet:
    """All the steps on one sheet, in reading order, each showing the drawing as far as that step."""
    sheet = Sheet(seed, pegs=False)
    n = len(lesson.steps)
    write(sheet, f"{lesson.title}, in {n} step{'s' if n > 1 else ''}", 22, 10, 24)
    write(sheet, lesson.credit, W - 18, 16, 15, right=True)
    x0, y0, x1, y1 = lesson.bounds
    cols, rows = grid_shape(n, max(x1 - x0, 1) / max(y1 - y0, 1))
    cw, ch = (GRID[2] - GRID[0]) / cols, (GRID[3] - GRID[1]) / rows
    pen = Pen(np.random.default_rng([seed, 2]))
    for i in range(n):
        x, y = GRID[0] + (i % cols) * cw, GRID[1] + (i // cols) * ch
        left, top, right, bottom = CELL_MARGIN
        to_cell = fitter(lesson.bounds, (x + left, y + top, x + cw - right, y + ch - bottom))
        for step in lesson.steps[:i + 1]:
            for p in step:
                draw(sheet, inked(pen, to_cell(p), .9))
        write(sheet, str(i + 1), x + 6, y + 4, 13)
    return sheet


# ---- the clock --------------------------------------------------------------
def step_times(steps: int, step_minutes: int = STEP_MINUTES, draw_minutes: int | None = None) -> list:
    """Whole minutes for each step: `step_minutes` each, or `draw_minutes` shared out over all
    of them. When that doesn't divide evenly the later steps, which tend to add more, get the
    extra minute."""
    if draw_minutes is None:
        if step_minutes < 1:
            raise ValueError("each step needs at least a minute")
        return [step_minutes] * steps
    if draw_minutes < steps:
        raise ValueError(f"{steps} steps need at least {steps} minutes of drawing")
    each, extra = divmod(draw_minutes, steps)
    return [each + (i >= steps - extra) for i in range(steps)]


def parts_for(minutes: int) -> int:
    """A new part each minute, leaving a step's last minute to look at it finished."""
    return max(1, minutes - 1)


def frame_index(elapsed: float, times: list) -> int:
    """Which of a lesson's frames is up `elapsed` seconds after it began, with `times` minutes
    for each step. The last index is the progression, which then stays up."""
    into, first = max(0, int(elapsed // 60)), 0
    for minutes in times:
        if into < minutes:
            return first + min(into, parts_for(minutes) - 1)
        into -= minutes
        first += parts_for(minutes)
    return first


def began_at(now: dt.datetime, start: dt.time) -> dt.datetime:
    """The latest daily start at or before `now`."""
    began = dt.datetime.combine(now.date(), start)
    return began if began <= now else began - dt.timedelta(days=1)


def shuffled_pick(position: int, n: int) -> int:
    """Which of n lessons goes in this position: a random order in which every lesson comes up
    once before any comes up again, then a new order. It depends only on the position, so a
    restart carries on with the same drawings."""
    def order(round_):
        o = np.random.default_rng([round_, n]).permutation(n)
        if n > 2 and round_ and o[0] == np.random.default_rng([round_ - 1, n]).permutation(n)[-1]:
            o[[0, 1]] = o[[1, 0]]              # never the same drawing twice running (the last place is never swapped)
        return o

    round_, place = divmod(position, n)
    return int(order(round_)[place])


class Hourly:
    """A drawing every hour: `count` of them a day from `start`, picked at random but with none
    repeated until all have had a turn. Between the last one and the next day's first,
    the last one stays up. With no `start`, one every hour from `first`, round the clock.
    `make(i)` gives lesson i's (frames, times); only the one on show is kept."""

    def __init__(self, lessons: int, make, start: dt.time | None, first: dt.datetime, count=HOURS_A_DAY):
        self.lessons, self.make, self.start, self.first, self.count = lessons, make, start, first, count
        self._made = (None, None)

    def slot(self, now: dt.datetime):
        """(when the current drawing began, which lesson it is)."""
        hour = dt.timedelta(hours=1)
        if self.start is None:
            n = max(0, int((now.timestamp() - self.first.timestamp()) // 3600))
            return self.first + n * hour, shuffled_pick(self.first.date().toordinal() * 24 + self.first.hour + n, self.lessons)
        day = began_at(now, self.start)
        i = min(self.count - 1, int((now - day) // hour))
        return day + i * hour, shuffled_pick(day.date().toordinal() * self.count + i, self.lessons)

    def __call__(self, now: dt.datetime):
        began, i = self.slot(now)
        if self._made[0] != i:
            self._made = (i, self.make(i))
        return (began, *self._made[1])


def describe(index: int, times: list) -> str:
    for step, minutes in enumerate(times, 1):
        if index < parts_for(minutes):
            return f"step {step} of {len(times)}"
        index -= parts_for(minutes)
    return "all the steps"


def play(frames: Frames, display, start: dt.time | None, times: list, full_every=20,
         now=dt.datetime.now, sleep=time.sleep, running=lambda: True):
    """Show the lesson on `display`, with `times` minutes for each step. With a `start` time it
    is drawn again every day from then, and the progression stays up in between. With none it
    begins at once, is drawn once, and returns when the progression is up."""
    first = now()
    show_on_time(lambda t: (began_at(t, start) if start else first, frames, times), display,
                 start is None, full_every, now, sleep, running)


def show_on_time(current, display, once=False, full_every=20, now=dt.datetime.now, sleep=time.sleep,
                 running=lambda: True):
    """Keep the panel on time. `current(t)` says what is being drawn at t: (when it began, its
    frames, its minutes per step). With `once`, return when the progression is up."""
    shown, since_full = None, 0
    while running():
        t = now()
        began, frames, times = current(t)
        index = frame_index(t.timestamp() - began.timestamp(), times)
        key = (began, index)
        if key != shown:
            since_full += 1
            # full refresh: the first picture, a fresh sheet, the change to the progression
            full = (shown is None or began != shown[0] or index == len(frames) - 1
                    or bool(full_every and since_full >= full_every))
            try:
                display.show(frames[index], full=full)
            except Exception as e:           # a display hiccup must not stop the lesson
                print(f"error: {e}; trying again shortly", flush=True)
                shown = None
                sleep(30)
                continue
            since_full = 0 if full else since_full
            shown = key
            print(f"{t:%H:%M}  {frames.lesson.title}: {describe(index, times)}", flush=True)
        if once and index == len(frames) - 1:
            return
        sleep(60 - (now().timestamp() - began.timestamp()) % 60 + .2)


def preview(lesson: Lesson, outdir: Path, gamma=2.4):
    """Each finished step, the progression, and a quick GIF of the lot."""
    outdir.mkdir(parents=True, exist_ok=True)
    frames = Frames(lesson, 1, gamma=gamma)
    for i, f in enumerate(frames.drawing, 1):
        f.save(outdir / f"step_{i:02d}.png")
    frames.progression.save(outdir / "progression.png")
    gif = [frames[i].convert("L") for i in range(len(frames))]
    gif[0].save(outdir / "lesson.gif", save_all=True, append_images=gif[1:], loop=0,
                duration=[700] * (len(gif) - 2) + [2000, 5000][-min(len(gif), 2):])
    return outdir
