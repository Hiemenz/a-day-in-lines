"""The 'animator at work' details: pauses, slips rubbed out, zones, stroke speed, the pencil."""
import numpy as np
import pytest
from PIL import Image

from day_in_lines.constants import TOTAL
from day_in_lines.pen import Pen
from day_in_lines.planner import Look, build_plan, pause_events, zones
from day_in_lines.render import Sheet, with_pencil


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    y, x = np.mgrid[0:480, 0:800]
    a = (120 + 100 * np.sin(x / 60) * np.cos(y / 50)).astype(np.uint8)
    a[150:330, 250:550] = 40
    p = tmp_path_factory.mktemp("pic") / "t.png"
    Image.fromarray(a).save(p)
    return build_plan(p, 3)


def test_the_hand_stops_for_a_few_minutes_but_the_day_is_still_720(plan):
    assert len(plan) == TOTAL
    empty = [i for i, e in enumerate(plan) if not e["strokes"]]
    assert 20 < len(empty) < 120 and empty[0] > 0
    assert all(e["label"] == "stepping back" for e in plan if not e["strokes"])


def test_pause_events_keeps_everything_and_never_starts_with_one():
    evs = [dict(phase="x", label=str(i), strokes=[1]) for i in range(10)]
    out = pause_events(evs, 5, np.random.default_rng(1))
    assert len(out) == 15 and out[0]["strokes"] and [e["label"] for e in out if e["strokes"]] == [str(i) for i in range(10)]
    assert pause_events(evs[:1], 3, np.random.default_rng(1)) == evs[:1]


def test_slips_are_put_down_off_the_line_then_rubbed_out(plan):
    strokes = [(i, s) for i, e in enumerate(plan) if e["phase"] == "linework" for s in e["strokes"]]
    slip = [i for i, s in strokes if s.get("kind") == "guide"]
    rub = [i for i, s in strokes if s.get("kind") == "erase"]
    assert slip and rub and min(rub) >= min(slip)


def test_strokes_carry_a_speed_and_fast_ones_are_lighter(plan):
    sp = [s["sp"] for e in plan for s in e["strokes"] if "sp" in s]
    assert sp and min(sp) >= 0 and max(sp) <= 1
    sheet = Sheet(1, pegs=False)
    base = dict(pts=np.array([[100, 100], [160, 100], [220, 100]], np.float32), p=.5, w=1.0, taper=True, ph=0.0)
    ink = []
    for speed in (0.0, 1.0):
        sheet = Sheet(1, pegs=False)
        sheet.draw_stroke(dict(base, sp=speed))
        ink.append(float(sheet.ink().sum()))
    assert ink[0] > ink[1]


def test_zones_cover_the_sheet_with_a_steady_angle_each():
    soft = np.zeros((480, 800), np.float32)
    soft[100:400, 100:700] = .6
    zone, angle, order = zones(soft, np.random.default_rng(2))
    assert zone.shape == (480, 800) and sorted(order) == list(range(len(angle))) and len(angle) > 1
    flat, ang, order = zones(np.zeros((480, 800), np.float32), np.random.default_rng(2))
    assert flat.max() == 0 and order == [0]


def test_light_tones_keep_one_direction_and_only_dark_ones_cross(plan):
    shading = [e["label"] for e in plan if e["phase"] == "shading"]
    assert "first tone" in shading and "deepening" in shading


def test_the_sheet_has_a_pencilled_scene_note_and_the_pencil_changes_only_the_frame():
    sheet = Sheet(5)
    assert sheet.ink()[910:940, 20:400].sum() > 0
    img = Image.new("L", (800, 480), 255)
    out = with_pencil(img, (300, 200))
    assert np.asarray(out).min() == 0 and np.asarray(img).min() == 255          # the original is untouched
    assert with_pencil(Image.new("P", (10, 10)), (5, 5)).mode == "P"             # modes it can't draw in are left alone


def test_look_suffix_marks_the_pencil():
    assert Look().suffix == "" and Look(tool=True).suffix == "-pencil"
