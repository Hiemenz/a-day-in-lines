import json

import numpy as np
import pytest
from PIL import Image

from day_in_lines.constants import H, TOTAL, W
from day_in_lines.export import viewer_plan
from day_in_lines.media import DEFAULT_GAMMA, new_sheet, plan
from day_in_lines.paint import PaintSheet, build_paint_plan
from day_in_lines.planner import Look
from day_in_lines.render import dither_1bit
from conftest import make_png

PAINT = Look(medium="paint")


def colourful(path, size=(400, 240), seed=0):
    """Blobs of strong colour with soft edges: nothing like a pencil subject."""
    r = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size[1], 0:size[0]]
    img = np.zeros((*yy.shape, 3), np.float32) + .6
    for _ in range(9):
        cx, cy, rad = r.uniform(0, size[0]), r.uniform(0, size[1]), r.uniform(25, 90)
        m = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * rad ** 2))[..., None]
        img = img * (1 - m) + r.uniform(0, 1, 3) * m
    Image.fromarray((img * 255).astype(np.uint8)).save(path)
    return path


@pytest.fixture(scope="module")
def painting(tmp_path_factory):
    return colourful(tmp_path_factory.mktemp("p") / "blobs.png")


@pytest.fixture(scope="module")
def events(painting):
    return build_paint_plan(painting, 3)


def test_a_day_of_720_events_in_four_phases(events):
    assert len(events) == TOTAL
    order = ["composition", "linework", "shading", "finishing"]
    phases = [order.index(e["phase"]) for e in events]
    assert phases == sorted(phases) and set(phases) == {0, 1, 2, 3}
    assert phases.count(0) <= 90 and phases.count(1) <= 200 and phases.count(3) <= 130
    assert all(e["strokes"] for e in events)               # every minute adds paint


def test_strokes_stay_on_the_canvas_and_have_what_the_viewer_needs(events):
    for e in events:
        for s in e["strokes"]:
            pts = np.asarray(s["pts"])
            assert pts[:, 0].min() >= 0 and pts[:, 0].max() < W and pts[:, 1].min() >= 0 and pts[:, 1].max() < H
            assert s["kind"] in ("knife", "brush", "dab") and 0 <= s["dry"] <= 1
            assert len(s["c"]) == 3 and all(0 <= v <= 1 for v in s["c"])
    json.loads(viewer_plan(events))                          # the exposure-sheet encoder accepts paint strokes


def test_plan_is_deterministic(painting):
    a, b = build_paint_plan(painting, 3), build_paint_plan(painting, 3)
    assert all(np.array_equal(s["pts"], t["pts"]) and s["c"] == t["c"]
               for e, f in zip(a, b) for s, t in zip(e["strokes"], f["strokes"]))


def test_the_sheet_starts_with_nothing_and_paint_only_accumulates(events):
    sh = PaintSheet(3)
    assert np.asarray(sh.gray()).min() == 255 and sh.ink().max() == 0     # a blank canvas, no pegs, no marks
    last, marks = 0.0, {}
    for k, e in enumerate(events[:300], 1):
        sh.draw_event(e)
        if k % 25 == 0:
            ink = float((sh.ink() > .5).mean())               # the fraction of the canvas with paint on it
            assert ink >= last
            last, marks[k] = ink, ink
    assert marks[25] < .35 < marks[300]
    assert marks[300] > .97                                   # the underpainting has covered the canvas by then


def test_paint_is_opaque_so_later_strokes_cover_earlier_ones():
    sh = PaintSheet(1)
    pts = np.array([[100, 100], [200, 100], [300, 100]], np.float32)

    def stroke(colour, ph):
        return dict(pts=pts, w=20.0, c=colour, p=1.0, a=1.0, kind="knife", dry=0.0, taper=False, ph=ph)
    sh.draw_stroke(stroke((.1, .1, .1), .2))
    sh.draw_stroke(stroke((.9, .9, .9), .4))
    centre = np.asarray(sh.paper())[200, 400]                # (y, x) at 2x supersampling
    assert centre.min() > 200                                  # the white paint hides the dark underneath


def test_the_same_stroke_always_paints_the_same_way(events):
    a, b = PaintSheet(5), PaintSheet(5)
    for e in events[:20]:
        a.draw_event(e)
    for e in events[:20]:
        b.draw_event(e)
    assert np.array_equal(a.canvas, b.canvas)


def test_one_bit_frames_only_change_where_paint_went(events):
    sh = PaintSheet(2)
    for e in events[:60]:
        sh.draw_event(e)
    before = np.asarray(dither_1bit(sh.gray(), DEFAULT_GAMMA["paint"]))
    new = events[60]
    sh.draw_event(new)
    after = np.asarray(dither_1bit(sh.gray(), DEFAULT_GAMMA["paint"]))
    xs = np.concatenate([np.asarray(s["pts"])[:, 0] for s in new["strokes"]])
    ys = np.concatenate([np.asarray(s["pts"])[:, 1] for s in new["strokes"]])
    pad = max(s["w"] for s in new["strokes"]) + 6
    changed = np.argwhere(before != after)
    assert len(changed)
    assert changed[:, 1].min() >= xs.min() - pad and changed[:, 1].max() <= xs.max() + pad
    assert changed[:, 0].min() >= ys.min() - pad and changed[:, 0].max() <= ys.max() + pad


def odd_inputs(tmp_path):
    out = {}
    Image.new("L", (300, 200), 128).save(tmp_path / "flat_grey.png"); out["flat grey"] = tmp_path / "flat_grey.png"
    Image.new("RGB", (300, 200), (255, 255, 255)).save(tmp_path / "white.png"); out["white"] = tmp_path / "white.png"
    Image.new("RGB", (300, 200), (0, 0, 0)).save(tmp_path / "black.png"); out["black"] = tmp_path / "black.png"
    Image.new("RGB", (24, 16), (200, 60, 30)).save(tmp_path / "tiny.png"); out["tiny"] = tmp_path / "tiny.png"
    yy, xx = np.mgrid[0:300, 0:100]
    Image.fromarray(np.stack([xx * 2, yy * .8, 255 - xx * 2], -1).clip(0, 255).astype(np.uint8)).save(tmp_path / "tall.png")
    out["portrait"] = tmp_path / "tall.png"
    g = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    g.paste(Image.new("RGBA", (80, 60), (200, 40, 40, 255)), (60, 30)); g.save(tmp_path / "cut.png"); out["transparent"] = tmp_path / "cut.png"
    Image.open(make_png(tmp_path / "gray_texture.png")).convert("L").save(tmp_path / "gray_texture.png"); out["grayscale"] = tmp_path / "gray_texture.png"
    return out


@pytest.mark.parametrize("name, fit", [("flat grey", "crop"), ("white", "pad"), ("black", "crop"), ("tiny", "pad"),
                                        ("portrait", "crop"), ("portrait", "pad"), ("transparent", "pad"),
                                        ("grayscale", "crop")])
def test_any_input_makes_a_full_day_that_renders(tmp_path, name, fit):
    image = odd_inputs(tmp_path)[name]
    ev = build_paint_plan(image, 1, fit)
    assert len(ev) == TOTAL
    sh = PaintSheet(1)
    for e in ev[::10]:                                         # a spread of the day is enough here
        sh.draw_event(e)
    assert np.isfinite(sh.canvas).all() and 0 <= sh.canvas.min() and sh.canvas.max() <= 1


def test_detail_makes_smaller_and_more_numerous_strokes(painting):
    base = sum(len(e["strokes"]) for e in build_paint_plan(painting, 3))
    more = sum(len(e["strokes"]) for e in build_paint_plan(painting, 3, look=Look(detail=1.8, medium="paint")))
    assert more > base


def test_medium_is_chosen_by_the_look(painting):
    assert type(new_sheet(PAINT, 1)).__name__ == "PaintSheet"
    assert type(new_sheet(Look(), 1)).__name__ == "Sheet"
    assert plan(painting, 3, "crop", PAINT)[0]["strokes"][0]["kind"] in ("knife", "brush")
    assert "kind" not in plan(painting, 3, "crop", Look())[0]["strokes"][0]
    assert Look(medium="paint").suffix == "-paint" and Look(1.5, 1.0, "paint").suffix == "-paint-d1.5-c1"
