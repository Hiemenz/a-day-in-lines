"""The two media, behind one interface: pencil on animation paper, or palette-knife paint on canvas."""
from __future__ import annotations

from .paint import PaintSheet, build_paint_plan
from .planner import Look, build_plan
from .render import Sheet

MEDIA = ("pencil", "paint")
ALIASES = {"draw": "pencil", "drawing": "pencil", "painting": "paint"}   # one path for drawing, one for painting


def medium_name(text):
    """argparse type: accept `draw` for the pencil path as well as `pencil`."""
    name = ALIASES.get(text, text)
    if name not in MEDIA:
        raise ValueError(text)
    return name
DEFAULT_GAMMA = {"pencil": 2.4, "paint": 1.6}      # how much to darken mid-tones before 1-bit dithering
PHASE_NAMES = {                                    # the four phases, as each medium calls them
    "pencil": {"composition": "composition", "linework": "linework", "shading": "shading", "finishing": "finishing"},
    "paint": {"composition": "blocking in", "linework": "underpainting", "shading": "building form",
              "finishing": "details & highlights"},
}


def plan(image, seed, fit_mode="crop", look: Look = Look()):
    return (build_paint_plan if look.medium == "paint" else build_plan)(image, seed, fit_mode, look)


def new_sheet(look: Look, seed, pegs=True):
    return (PaintSheet if look.medium == "paint" else Sheet)(seed, pegs)
