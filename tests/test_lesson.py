"""Step-by-step lessons: reading the data files, the frames, and the clock."""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from day_in_lines import cli
from day_in_lines.lesson import (Frames, Hourly, began_at, check_lessons, frame_index, grid_shape, lesson_files, load_lesson,
                                 parse_lesson, parts_for, play, show_on_time, shuffled_pick, split, step_times)
from day_in_lines.pen import length

SWAN = Path(__file__).resolve().parent.parent / "lessons" / "animals" / "swan.json"
TINY = {"title": "Tent", "steps": [[{"line": [[10, 80], [50, 10], [90, 80]]}],
                                   [{"curve": [[0, 80], [50, 84], [100, 80]]}, {"circle": [50, 50, 8]}],
                                   [{"fill": [[40, 80], [50, 55], [60, 80]], "gap": 3}]]}


def ink(img):
    return np.asarray(img.convert("L")) < 128


def test_swan_loads():
    swan = load_lesson(SWAN)
    assert swan.title == "Swan" and len(swan.steps) == 20


@pytest.mark.parametrize("change, message", [
    ({"title": ""}, "title"),
    ({"steps": []}, "1 to 60 steps"),
    ({"steps": [[]]}, "step 1 needs"),
    ({"steps": [[{"square": [[0, 0], [1, 1]]}]]}, "step 1, shape 1"),
    ({"steps": [[{"line": [[0, 0]]}]]}, "too few points"),
    ({"steps": [[{"line": [[0, 0], [1, "x"]]}]]}, "must be numbers"),
    ({"steps": [[{"line": [[0, 0], [1e9, 1]]}]]}, "out of range"),
    ({"steps": [[{"circle": [0, 0, -1]}]]}, "radius"),
    ({"steps": [[{"fill": [[0, 0], [9, 0], [9, 9]], "gap": 0}]]}, "gap"),
    ({"steps": [[{"line": [[0, 0], [1, 1]], "gap": 2}]]}, "unknown keys"),
    ({"steps": [[{"line": [[5, 5], [5, 5]]}]]}, "no size"),
])
def test_bad_lessons_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        parse_lesson({**TINY, **change})


def test_unreadable_file_names_it(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="bad.json"):
        load_lesson(bad)


def test_split_keeps_every_bit_of_line_in_order():
    strokes = [dict(pts=np.array([[0., 0], [30, 0]]), p=1), dict(pts=np.array([[0., 5], [10, 5]]), p=1)]
    runs = split(strokes, 4)
    sizes = [sum(length(s["pts"]) for s in r) for r in runs]
    assert sum(sizes) == pytest.approx(40, abs=.01)
    assert all(abs(n - 10) <= 1.5 for n in sizes)
    assert runs[0][0]["pts"][0].tolist() == [0, 0] and runs[-1][-1]["pts"][-1].tolist() == [10, 5]


def test_five_minute_steps():
    at = lambda m: frame_index(m * 60, step_times(20))
    assert parts_for(5) == 4
    assert [at(m) for m in range(6)] == [0, 1, 2, 3, 3, 4]     # drawn over four minutes, held for the fifth
    assert at(99) == 79 and at(100) == 80 and at(1000) == 80    # then the progression, which stays
    assert parts_for(1) == 1 and frame_index(0, [1] * 3) == 0 and frame_index(180, [1] * 3) == 3


def test_drawing_time_is_shared_out_exactly():
    times = step_times(20, draw_minutes=55)
    assert times == [2] * 5 + [3] * 15 and sum(times) == 55
    last = sum(parts_for(m) for m in times)                     # the progression's index
    seen = [frame_index(m * 60, times) for m in range(61)]
    assert seen[54] == last - 1 and seen[55:] == [last] * 6     # drawing to :55, then all the steps
    assert sorted(set(seen)) == list(range(last + 1))           # every picture gets shown


@pytest.mark.parametrize("kwargs", [dict(draw_minutes=19), dict(step_minutes=0)])
def test_too_little_time_is_refused(kwargs):
    with pytest.raises(ValueError):
        step_times(20, **kwargs)


def test_daily_start():
    seven = dt.time(7)
    assert began_at(dt.datetime(2026, 3, 2, 7, 0), seven) == dt.datetime(2026, 3, 2, 7)
    assert began_at(dt.datetime(2026, 3, 2, 6, 59), seven) == dt.datetime(2026, 3, 1, 7)


def test_frames_only_add_marks(monkeypatch):
    import day_in_lines.lesson as module
    graphite, render = [], module.frame_for
    monkeypatch.setattr(module, "frame_for", lambda sheet, *a: graphite.append(sheet.surface.copy()) or render(sheet, *a))
    frames = Frames(parse_lesson(TINY), 4)
    for before, after in zip(graphite[:11], graphite[1:12]):
        assert (after <= before).all()                          # on the sheet nothing is ever taken away
    assert len(frames) == 13
    drawn = [ink(frames[i]) for i in range(12)]
    for before, after in zip(drawn, drawn[1:]):
        assert (before & ~after).sum() <= 2                     # shrinking to screen size can flip an edge pixel
    assert drawn[-1].sum() > drawn[0].sum() > 0
    assert (ink(frames.progression) != ink(frames[11])).any()


def test_progression_grid():
    assert grid_shape(20, 1.05) == (5, 4)
    assert grid_shape(1, 1.0) == (1, 1)


class Clock:
    def __init__(self, at):
        self.t = at

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += dt.timedelta(seconds=seconds)


class Panel:
    def __init__(self, clock, fail_first=False):
        self.clock, self.fail, self.shown = clock, fail_first, []

    def show(self, img, full=False):
        if self.fail:
            self.fail = False
            raise OSError("busy")
        self.shown.append((self.clock.t, img, full))


class Stub(list):
    """Frames that are just their own numbers, for watching the clock."""
    lesson = parse_lesson(TINY)

    def __init__(self, numbers=range(13)):
        super().__init__(numbers)


def test_play_once_ends_on_the_progression():
    clock = Clock(dt.datetime(2026, 3, 2, 9, 30, 17))
    panel = Panel(clock)
    play(Stub(), panel, None, [5] * 3, full_every=0, now=clock.now, sleep=clock.sleep)
    assert [i for _, i, _ in panel.shown] == list(range(13))
    assert [i for _, i, full in panel.shown if full] == [0, 12]
    assert (panel.shown[-1][0] - panel.shown[0][0]).total_seconds() == pytest.approx(15 * 60, abs=2)


def test_play_daily_holds_then_starts_again():
    clock = Clock(dt.datetime(2026, 3, 2, 12, 0))
    panel = Panel(clock)
    play(Stub(), panel, dt.time(7), [5] * 3, now=clock.now, sleep=clock.sleep,
         running=lambda: clock.t < dt.datetime(2026, 3, 3, 7, 2))
    seen = [(t.day, t.hour, t.minute, i) for t, i, _ in panel.shown]
    assert seen == [(2, 12, 0, 12), (3, 7, 0, 0), (3, 7, 1, 1)]
    assert panel.shown[1][2]                                    # the fresh sheet is a full refresh


def test_play_survives_a_display_error():
    clock = Clock(dt.datetime(2026, 3, 2, 9, 0))
    panel = Panel(clock, fail_first=True)
    play(Stub(), panel, None, [5] * 3, now=clock.now, sleep=clock.sleep)
    assert panel.shown[0][2] and panel.shown[-1][1] == 12


def test_cli_preview_needs_no_image(tmp_path, monkeypatch):
    f = tmp_path / "tent.json"
    f.write_text(json.dumps(TINY))
    monkeypatch.setattr(sys, "argv", ["x", "lesson", str(f), "--preview", str(tmp_path / "out")])
    cli.main()
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "lesson.gif", "progression.png", "step_01.png", "step_02.png", "step_03.png"]


def run_hourly(clock, hourly, until):
    panel = Panel(clock)
    show_on_time(hourly, panel, now=clock.now, sleep=clock.sleep, running=lambda: clock.t < until)
    return panel


def test_hourly_shows_the_steps_for_the_last_five_minutes():
    clock = Clock(dt.datetime(2026, 3, 2, 7, 0))
    times = step_times(3, draw_minutes=55)
    frames = Stub(range(sum(parts_for(m) for m in times) + 1))
    hourly = Hourly(1, lambda i: (frames, times), dt.time(7), clock.t)
    panel = run_hourly(clock, hourly, dt.datetime(2026, 3, 2, 8, 2))
    at = {(t.hour, t.minute): (i, full) for t, i, full in panel.shown}
    assert at[7, 55] == (len(frames) - 1, True)
    assert not [k for k in at if (7, 55) < k < (8, 0)]          # the steps hold to the hour
    assert at[8, 0] == (0, True)                                # then a fresh sheet


@pytest.mark.parametrize("n", [1, 2, 5, 12, 13])
def test_the_pick_is_random_but_fair(n):
    picks = [shuffled_pick(p, n) for p in range(n * 200)]
    for r in range(200):
        assert sorted(picks[r * n:(r + 1) * n]) == list(range(n))      # everything before anything again
    if n > 2:
        assert all(a != b for a, b in zip(picks, picks[1:]))
    if n > 4:
        assert len({tuple(picks[r * n:(r + 1) * n]) for r in range(20)}) > 5       # the order changes
    assert picks == [shuffled_pick(p, n) for p in range(n * 200)]      # a restart agrees


def test_hourly_picks_and_rests_overnight():
    hourly = Hourly(30, None, dt.time(7), None)
    at = lambda day, h, m=0: hourly.slot(dt.datetime(2026, 3, day, h, m))
    first = dt.date(2026, 3, 2).toordinal() * 12
    assert [at(2, h)[1] for h in range(7, 19)] == [shuffled_pick(first + i, 30) for i in range(12)]
    assert at(3, 7)[1] == shuffled_pick(first + 12, 30)
    assert at(2, 23) == at(3, 6, 59) == at(2, 18)
    assert at(2, 18)[0] == dt.datetime(2026, 3, 2, 18)


def test_hourly_from_now_goes_round_the_clock():
    first = dt.datetime(2026, 3, 2, 22, 17)
    hourly = Hourly(12, None, None, first)
    assert hourly.slot(first)[0] == first
    assert hourly.slot(first + dt.timedelta(hours=9, minutes=5))[0] == first + dt.timedelta(hours=9)
    start = first.date().toordinal() * 24 + first.hour
    assert [hourly.slot(first + dt.timedelta(hours=h))[1] for h in range(36)] == [shuffled_pick(start + h, 12)
                                                                                 for h in range(36)]


def test_hourly_only_prepares_a_lesson_when_it_changes():
    made = []
    clock = Clock(dt.datetime(2026, 3, 2, 7, 0))
    hourly = Hourly(1, lambda i: made.append(i) or (Stub(), [5] * 3), dt.time(7), clock.t)
    run_hourly(clock, hourly, dt.datetime(2026, 3, 2, 10, 0))
    assert made == [0]


def test_a_folder_stands_for_its_lessons_and_its_subfolders(tmp_path):
    for name in ("b/two.json", "a/one.json", "top.json", "notes.txt", ".hidden/no.json", "a/.no.json"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("{}")
    assert [p.relative_to(tmp_path).as_posix() for p in lesson_files([tmp_path])] == ["a/one.json", "b/two.json", "top.json"]
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no lesson files"):
        lesson_files([tmp_path / "empty"])


@pytest.mark.parametrize("extra, message", [([], "needs --hourly"), (["--hourly", "--draw-minutes", "60"], "under 60")])
def test_cli_refuses(tmp_path, monkeypatch, extra, message):
    files = [str(SWAN)] * (1 if extra else 2)
    monkeypatch.setattr(sys, "argv", ["x", "lesson", *files, *extra, "--out", str(tmp_path / "c.png")])
    with pytest.raises(SystemExit, match=message):
        cli.main()


def test_the_drawing_fills_the_sheet_with_nothing_else_on_it():
    lesson = parse_lesson({"title": "A long title", "credit": "someone",
                           "steps": [[{"line": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]}]]})
    ys, xs = np.nonzero(ink(Frames(lesson, 1)[0]))
    assert ys.min() <= 6 and ys.max() >= 473                    # top to bottom
    assert xs.min() >= 160 and xs.max() <= 640                  # a square, centred: no lettering at the sides


LESSONS = lesson_files([SWAN.parent.parent])


def test_every_lesson_in_the_repo_can_be_shown():
    good, problems = check_lessons(LESSONS)
    assert not problems and len(good) == len(LESSONS)


def test_check_finds_what_would_not_show(tmp_path):
    def put(name, data):
        (tmp_path / name).write_text(data if isinstance(data, str) else json.dumps(data))
        return tmp_path / name
    line = [{"line": [[0, 0], [100, 100]]}]
    files = [put("a.json", TINY), put("broken.json", "{"), put("copy.json", TINY),
             put("long.json", {"title": "Long", "steps": [line] * 56}),
             put("same.json", {"title": "Same twice", "steps": [line, line]})]
    good, problems = check_lessons(files)
    assert [f.name for f, _ in good] == ["a.json"]
    why = {f.name: w for f, w in problems}
    assert "already used" in why["copy.json"] and "at least 56 minutes" in why["long.json"]
    assert "step 2 adds nothing" in why["same.json"] and "broken.json" in why


def test_lessons_are_filed_by_subject_and_all_listed():
    root = SWAN.parent.parent
    assert all(f.parent.parent == root for f in LESSONS)              # lessons/<subject>/<name>.json
    assert len({f.stem for f in LESSONS}) == len(LESSONS)             # previews are named after the file
    index = (root / "README.md").read_text()
    assert all(f"({f.relative_to(root).as_posix()})" in index for f in LESSONS)
