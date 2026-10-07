import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_png(path: Path, size=(200, 120), seed=0) -> Path:
    """A small picture with a dark disc and some texture, so every phase has something to do."""
    yy, xx = np.mgrid[0:size[1], 0:size[0]]
    g = 235 - 40 * (xx / size[0])
    g -= 170 * (np.hypot(xx - size[0] * .45, yy - size[1] * .55) < size[1] * .3)
    g += np.random.default_rng(seed).normal(0, 6, g.shape)
    Image.fromarray(np.clip(g, 0, 255).astype(np.uint8)).save(path)
    old = path.stat().st_mtime - 60              # the scanner skips files still being written
    os.utime(path, (old, old))
    return path


@pytest.fixture
def picture(tmp_path):
    return make_png(tmp_path / "disc.png")


@pytest.fixture(scope="session")
def plan(tmp_path_factory):
    from day_in_lines.planner import build_plan
    return build_plan(make_png(tmp_path_factory.mktemp("img") / "disc.png", (400, 240)), seed=7)
