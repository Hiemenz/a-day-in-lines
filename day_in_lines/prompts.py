"""Text-to-image prompts that scaffold a new picture in the style of a given one: a style
line read from the picture, then one prompt per drawing phase, from the first loose
construction lines to the finished sheet."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .constants import PHASES, TOTAL
from .media import PHASE_NAMES
from .planner import Look

PAINT_PHASES = (("composition", 80), ("linework", 170), ("shading", 280), ("finishing", 190))

STAGES = {
    "pencil": {
        "composition": "only faint, loose construction lines blocking in the big shapes; no shading, the paper is mostly blank",
        "linework": "confident contour lines, strongest first, then thin dark lines for fine details; no shading yet",
        "shading": "contours plus layered pencil hatching, light to dark, in several angles that follow the form; tone is building",
        "finishing": "the finished drawing: darkest accents, re-stated strong edges, a few stray construction marks left visible",
    },
    "paint": {
        "composition": "a blank canvas with only big flat slabs of colour blocking in the broad masses",
        "linework": "an underpainting: smaller flat slabs correcting the masses, still no fine detail",
        "shading": "medium palette-knife strokes building form, with some canvas texture showing",
        "finishing": "the finished painting: the smallest strokes, details and highlights, visible hand-cut stroke edges",
    },
}
MEDIUM_STYLE = {
    "pencil": "graphite pencil drawing on cream animation paper, hand-drawn wobble, tapered strokes, paper grain",
    "paint": "opaque palette-knife painting on canvas, flat strokes with slightly hand-cut edges, visible brushwork",
}


def phase_minutes(medium: str):
    """[(phase, first minute, last minute)] for the medium's four phases."""
    sizes = PAINT_PHASES if medium == "paint" else tuple(
        (k, n if n else TOTAL - sum(m or 0 for _, m in PHASES)) for k, n in PHASES)
    out, t = [], 0
    for k, n in sizes:
        out.append((k, t + 1, t + n))
        t += n
    return out


HUES = ("red", "orange", "yellow", "green", "teal", "blue", "purple", "pink")


def describe(path: Path) -> list[str]:
    """Plain-words tone, line and (for colour pictures) palette descriptors measured from the picture."""
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
        sheet = Image.new("RGBA", rgba.size, (255, 255, 255, 255))      # transparent areas are blank paper
        rgb = Image.alpha_composite(sheet, rgba).convert("RGB").resize((200, 120), Image.LANCZOS)
    g = np.asarray(rgb.convert("L"), np.float32) / 255
    hsv = np.asarray(rgb.convert("HSV"), np.float32) / 255
    mean, spread = float(g.mean()), float(g.std())
    gy, gx = np.gradient(g)
    edges = float((np.hypot(gx, gy) > 0.08).mean())
    tone = "dark, moody" if mean < .35 else "light, airy" if mean > .65 else "mid-toned"
    contrast = "high contrast" if spread > .28 else "soft, low contrast" if spread < .15 else "moderate contrast"
    busy = "dense with fine detail" if edges > .12 else "sparse, with large calm areas" if edges < .04 else "balanced between detail and calm areas"
    traits = [tone, contrast, busy]
    colourful = hsv[..., 1] > .25
    if colourful.mean() > .2:                       # a palette, only if the picture really has colour
        hue = np.bincount((hsv[..., 0][colourful] * len(HUES)).astype(int) % len(HUES), minlength=len(HUES))
        traits.append(f"mostly {HUES[int(hue.argmax())]} hues")
    return traits


def build(medium: str, subject: str, traits: list[str], look: Look = Look()) -> list[tuple[str, str, str]]:
    """[(phase name, minutes, prompt)] followed by the finished picture's prompt."""
    finish = ("landscape 5:3 sheet, strong value structure so it still reads in black and white on a 1-bit e-ink display"
              if medium == "paint" else "landscape 5:3 sheet, black and white, suits a 1-bit e-ink display")
    style = [MEDIUM_STYLE[medium], ", ".join(traits), finish]
    if look.detail > 1.2:
        style.append("extra fine lines and tight hatching")
    elif look.detail < .8:
        style.append("sparse, economical marks")
    if look.contrast > 1.2:
        style.append("deep, heavy darks")
    style = "; ".join(style)
    subject = subject.strip() or "a new scene"
    out = []
    for i, (phase, a, b) in enumerate(phase_minutes(medium)):
        same = "the same picture and composition as the previous stage, " if i else ""
        out.append((PHASE_NAMES[medium][phase], f"minutes {a}-{b}",
                    f"{subject}, {same}{STAGES[medium][phase]}. Style: {style}."))
    return out


def render(medium, subject, traits, look: Look = Look()) -> str:
    lines = [f"Style read from the picture: {', '.join(traits)}", ""]
    for i, (name, mins, text) in enumerate(build(medium, subject, traits, look), 1):
        lines += [f"{i}. {name.capitalize()} ({mins})", f"   {text}", ""]
    lines.append("Generate the four stages in order, each from the previous one, to scaffold the drawing.")
    return "\n".join(lines)
