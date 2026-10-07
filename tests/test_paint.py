import json
import sys

import numpy as np
import pytest
from PIL import Image

from day_in_lines.constants import H, SS, TOTAL, W
from day_in_lines.export import viewer_plan
from day_in_lines.media import DEFAULT_GAMMA, new_sheet, plan
from day_in_lines.paint import PaintSheet, build_paint_plan, load_colour, panel_tones
from day_in_lines.planner import Look
from day_in_lines.render import dither_1bit
from conftest import make_png

PAINT = Look(medium="paint")


def slabs(path, size=(400, 240), seed=0):
    """A painting-like picture: slabs of colour with ridges and streaks, nothing like a photo."""
    r = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size[1], 0:size[0]]
    img = np.zeros((*yy.shape, 3), np.float32) + .6
    for _ in range(40):
        cx, cy = r.uniform(0, size[0]), r.uniform(0, size[1])
        a, b, ang = r.uniform(15, 70), r.uniform(6, 20), r.uniform(0, np.pi)
        u = (xx - cx) * np.cos(ang) + (yy - cy) * np.sin(ang)
        v = (yy - cy) * np.cos(ang) - (xx - cx) * np.sin(ang)
        m = ((u / a) ** 2 + (v / b) ** 2 < 1)[..., None]
        streak = .06 * np.sin(v * 1.7 + r.uniform(0, 6))[..., None]
        img = np.where(m, r.uniform(.05, .95, 3) + streak, img)
    Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(path)
    return path


@pytest.fixture(scope="module")
def painting(tmp_path_factory):
    return slabs(tmp_path_factory.mktemp("p") / "slabs.png")


@pytest.fixture(scope="module")
def events(painting):
    return build_paint_plan(painting, 3)


@pytest.fixture(scope="module")
def finished(events):
    sh = PaintSheet(3)
    for e in events:
        sh.draw_event(e)
    return sh


def test_a_day_of_720_events_in_four_phases(events):
    assert len(events) == TOTAL
    order = ["composition", "linework", "shading", "finishing"]
    phases = [order.index(e["phase"]) for e in events]
    assert phases == sorted(phases) and set(phases) == {0, 1, 2, 3}
    assert all(e["strokes"] for e in events)                 # every minute adds paint


def test_the_finished_frame_is_the_painting_you_gave_it(painting, finished):
    """The day works backwards from the end result, so the last frame is that picture."""
    _, target = load_colour(painting, "crop")
    final = np.asarray(finished.paper((W, H)), np.float32) / 255
    assert np.abs(final - target).mean() < .01
    assert np.abs(final - target).max() < .6                 # and nowhere is badly wrong


def test_early_frames_are_coarser_than_late_ones(painting, events):
    _, target = load_colour(painting, "crop")
    sh, errors = PaintSheet(3), {}
    for k, e in enumerate(events, 1):
        sh.draw_event(e)
        if k in (100, 250, 450, 720):
            errors[k] = np.abs(np.asarray(sh.paper((W, H)), np.float32) / 255 - target).mean()
    assert errors[100] > errors[250] > errors[450] > errors[720]


def test_strokes_stay_on_the_canvas_and_have_what_the_viewer_needs(events):
    for e in events:
        for s in e["strokes"]:
            x0, y0, x1, y1 = s["box"]
            assert 0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H
            assert s["mask"].shape == (y1 - y0, x1 - x0) and s["mask"].any()
            assert s["rgb"].shape == ((y1 - y0) * SS, (x1 - x0) * SS, 3)
    plan_json = json.loads(viewer_plan(events))               # the exposure-sheet encoder accepts paint strokes
    assert len(plan_json) == TOTAL


def test_plan_is_deterministic(painting):
    a, b = build_paint_plan(painting, 3), build_paint_plan(painting, 3)
    assert all(np.array_equal(s["mask"], t["mask"]) and np.array_equal(s["rgb"], t["rgb"])
               for e, f in zip(a, b) for s, t in zip(e["strokes"], f["strokes"]))


def test_the_sheet_starts_with_nothing_and_paint_only_accumulates(events):
    sh = PaintSheet(3)
    assert np.asarray(sh.gray()).min() == 255 and sh.ink().max() == 0     # a blank canvas, no pegs, no marks
    last, marks = 0.0, {}
    for k, e in enumerate(events[:260], 1):
        sh.draw_event(e)
        if k % 20 == 0:
            ink = float((sh.ink() > .5).mean())               # the fraction of the canvas with paint on it
            assert ink >= last
            last, marks[k] = ink, ink
    assert marks[20] < .5 < marks[260]
    assert marks[260] > .97                                   # blocking in and the underpainting cover the canvas


def test_a_stroke_lays_down_its_own_pixels_and_later_strokes_cover_earlier_ones():
    sh = PaintSheet(1)

    def stroke(value, ph):
        return dict(mask=np.ones((12, 12), bool), box=(5, 5, 17, 17), a=1.0, ph=ph,
                    rgb=np.full((12 * SS, 12 * SS, 3), int(value * 255), np.uint8))
    sh.draw_stroke(stroke(.2, .3))
    assert abs(np.asarray(sh.paper())[11 * SS, 11 * SS].mean() / 255 - .2) < .02
    sh.draw_stroke(stroke(.9, .4))
    assert abs(np.asarray(sh.paper())[11 * SS, 11 * SS].mean() / 255 - .9) < .02


def test_the_same_stroke_always_paints_the_same_way(events):
    a, b = PaintSheet(5), PaintSheet(5)
    for e in events[:30]:
        a.draw_event(e)
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
    changed = np.argwhere(before != after)
    assert len(changed)
    x0 = min(s["box"][0] for s in new["strokes"]); x1 = max(s["box"][2] for s in new["strokes"])
    y0 = min(s["box"][1] for s in new["strokes"]); y1 = max(s["box"][3] for s in new["strokes"])
    assert changed[:, 1].min() >= x0 and changed[:, 1].max() < x1
    assert changed[:, 0].min() >= y0 and changed[:, 0].max() < y1


def test_a_dark_painting_gets_its_midtones_lifted_and_a_light_one_does_not():
    r = np.random.default_rng(0)
    dark = np.where(r.random((60, 100)) < .85, r.normal(.1, .03, (60, 100)), r.normal(.9, .05, (60, 100)))   # mostly dark, a few lights
    dark = np.clip(dark, 0, 1)
    light = np.clip(r.normal(.7, .12, (60, 100)), 0, 1)
    flat = np.full((60, 100), .5)
    assert panel_tones(dark)[2] < .95
    assert panel_tones(light)[2] == 1.0
    assert panel_tones(flat) == (0.0, 1.0, 1.0)


def odd_inputs(tmp_path):
    out = {}
    for name, im in {"flat grey": Image.new("L", (300, 200), 128), "white": Image.new("RGB", (300, 200), (255, 255, 255)),
                     "black": Image.new("RGB", (300, 200), (0, 0, 0)), "tiny": Image.new("RGB", (24, 16), (200, 60, 30))}.items():
        im.save(tmp_path / f"{name}.png")
        out[name] = tmp_path / f"{name}.png"
    yy, xx = np.mgrid[0:300, 0:100]
    Image.fromarray(np.stack([xx * 2, yy * .8, 255 - xx * 2], -1).clip(0, 255).astype(np.uint8)).save(tmp_path / "tall.png")
    out["portrait"] = tmp_path / "tall.png"
    g = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    g.paste(Image.new("RGBA", (80, 60), (200, 40, 40, 255)), (60, 30))
    g.save(tmp_path / "cut.png")
    out["transparent"] = tmp_path / "cut.png"
    Image.open(make_png(tmp_path / "gt.png")).convert("L").save(tmp_path / "gt.png")
    out["grayscale"] = tmp_path / "gt.png"
    return out


@pytest.mark.parametrize("name, fit", [("flat grey", "crop"), ("white", "pad"), ("black", "crop"), ("tiny", "pad"),
                                        ("portrait", "crop"), ("portrait", "pad"), ("transparent", "pad"),
                                        ("grayscale", "crop")])
def test_any_input_makes_a_full_day_that_renders_and_ends_on_the_picture(tmp_path, name, fit):
    image = odd_inputs(tmp_path)[name]
    ev = build_paint_plan(image, 1, fit)
    assert len(ev) == TOTAL
    sh = PaintSheet(1)
    for e in ev:
        sh.draw_event(e)
    assert np.isfinite(sh.canvas).all() and 0 <= sh.canvas.min() and sh.canvas.max() <= 1
    _, target = load_colour(image, fit)
    assert np.abs(np.asarray(sh.paper((W, H)), np.float32) / 255 - target).mean() < .02


def test_detail_makes_more_and_smaller_strokes(painting):
    base = sum(len(e["strokes"]) for e in build_paint_plan(painting, 3))
    more = sum(len(e["strokes"]) for e in build_paint_plan(painting, 3, look=Look(detail=1.8, medium="paint")))
    assert more > base


def test_painting_needs_scikit_image_and_says_so(painting, monkeypatch):
    monkeypatch.setitem(sys.modules, "skimage.segmentation", None)
    with pytest.raises(RuntimeError, match="scikit-image"):
        build_paint_plan(painting, 3)


def test_medium_is_chosen_by_the_look(painting):
    assert type(new_sheet(PAINT, 1)).__name__ == "PaintSheet"
    assert type(new_sheet(Look(), 1)).__name__ == "Sheet"
    assert "mask" in plan(painting, 3, "crop", PAINT)[0]["strokes"][0]
    assert "mask" not in plan(painting, 3, "crop", Look())[0]["strokes"][0]
    assert Look(medium="paint").suffix == "-paint" and Look(1.5, 1.0, "paint").suffix == "-paint-d1.5-c1"
