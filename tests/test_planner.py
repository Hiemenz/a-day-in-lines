import numpy as np

from day_in_lines.constants import TOTAL
from day_in_lines.planner import Look, build_plan, organic_groups
from conftest import make_png


def test_plan_has_exactly_720_events_in_phase_order(plan):
    assert len(plan) == TOTAL
    order = ["composition", "linework", "shading", "finishing"]
    phases = [order.index(e["phase"]) for e in plan]
    assert phases == sorted(phases)                  # the day unfolds in order
    assert set(phases) == {0, 1, 2, 3}
    # nominal sizes are ceilings: a simple picture may have fewer shapes than 40 construction lines
    assert phases.count(0) <= 40 and phases.count(1) <= 230 and phases.count(3) <= 50


def test_plan_is_deterministic_for_a_seed(tmp_path):
    img = make_png(tmp_path / "x.png")
    a, b, c = build_plan(img, 5), build_plan(img, 5), build_plan(img, 6)
    assert all(np.array_equal(s["pts"], t["pts"]) for e, f in zip(a, b) for s, t in zip(e["strokes"], f["strokes"]))
    assert any(len(e["strokes"]) != len(f["strokes"]) for e, f in zip(a, c)) or \
        not np.array_equal(a[300]["strokes"][0]["pts"], c[300]["strokes"][0]["pts"])


def test_blank_image_still_makes_a_full_day(tmp_path):
    from PIL import Image
    Image.new("L", (80, 50), 255).save(tmp_path / "blank.png")
    assert len(build_plan(tmp_path / "blank.png", 1)) == TOTAL


def test_detail_and_contrast_change_the_drawing(tmp_path):
    img = make_png(tmp_path / "x.png", (400, 240))
    base = sum(len(e["strokes"]) for e in build_plan(img, 5))
    more = sum(len(e["strokes"]) for e in build_plan(img, 5, look=Look(detail=1.8, contrast=1.4)))
    assert more > base


def test_look_suffix_is_empty_for_defaults():
    assert Look().suffix == ""
    assert Look(1.5, 1.2).suffix == "-d1.5-c1.2"


def items(n, rng):
    return [(float(x), float(y), [{"id": i}]) for i, (x, y) in enumerate(rng.uniform(0, 400, (n, 2)))]


def test_organic_groups_keep_every_stroke_once():
    rng = np.random.default_rng(1)
    its = items(500, rng)
    groups = organic_groups(its, area=40 * 1600, rng=rng)
    ids = sorted(s["id"] for g in groups for s in g)
    assert ids == list(range(500))
    assert all(groups)


def test_organic_groups_are_not_a_grid_sweep():
    """Patches are blobs visited by a wandering walk. A raster sweep would visit them
    in strictly increasing row-then-column order, which this must not."""
    rng = np.random.default_rng(2)
    its = items(800, rng)
    groups = organic_groups(its, area=50 * 1600, rng=rng)
    centres = [np.mean([[its[s["id"]][0], its[s["id"]][1]] for s in g], axis=0) for g in groups]
    rows = [int(c[1] // 40) for c in centres]
    assert rows != sorted(rows)
    # and a patch is not confined to one 40 px square
    spans = [np.ptp([its[s["id"]][0] for s in g]) for g in groups if len(g) > 3]
    assert max(spans) > 40


def test_organic_groups_handles_nothing_and_one():
    rng = np.random.default_rng(3)
    assert organic_groups([], 100, rng) == []
    assert len(organic_groups([(5., 5., [{"id": 0}])], 10_000, rng)) == 1
