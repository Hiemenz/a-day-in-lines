"""Painting: the picture you give it is the finished painting, and the day replays it
from a blank canvas, stroke by stroke, working backwards from the end result.

The picture is taken apart into stroke-shaped regions that follow its own edges and
ridges, at four scales, and the day lays them down from coarse to fine:

  composition   blocking in: big slabs of the broad masses of colour
  linework      underpainting: smaller slabs, covering what is still wrong
  shading       building form: medium strokes
  finishing     details: the smallest strokes, ending on the picture's real pixels

Each stroke puts down the painting's own pixels inside its region: blurred at the
coarse scales, sharp at the last. At every scale only regions that still differ from
the target get a stroke, so the final frame is the picture you gave it. Planned and
rendered in colour; the 1-bit panel shows the luminance.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage as ndi

from .constants import H, SS, TOTAL, W
from .planner import Look, ev, fit, organic_groups

LUMA = np.array([.299, .587, .114], np.float32)

# phase, label, events (None = whatever is left), regions, blur in px for finding them, error that earns
# a stroke, SLIC compactness (lower follows the picture's colour edges more closely), opacity (paint is
# opaque; a stroke hides what is under it), and how much of the painting's own fine texture a slab keeps
# (None = the exact pixels)
LEVELS = (
    ("composition", "block in", 80, 110, 10.0, 0.0, 4, 1.0, .5),
    ("linework", "underpainting", 170, 380, 4.0, .040, 4, 1.0, .7),
    ("shading", "build form", None, 1000, 1.5, .028, 4, 1.0, .9),
    ("finishing", "detail", 190, 2600, 0.0, .014, 4, 1.0, None),
)
EVENTS = {phase: n for phase, _, n, *_ in LEVELS if n}
EVENTS["shading"] = TOTAL - sum(EVENTS.values())


# ==========================================================================
# Reading the painting
# ==========================================================================
def load_colour(path, fit_mode="crop", contrast=1.0):
    """(rgb_ss, rgb): the picture as floats 0-1, at the render size and at the logical size."""
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("RGB")
    if fit_mode == "pad":
        big = ImageOps.pad(img, (W * SS, H * SS), color=(255, 255, 255))
    else:
        big = ImageOps.fit(img, (W * SS, H * SS), Image.LANCZOS)
    out = []
    for im in (big, big.resize((W, H), Image.LANCZOS)):
        a = np.asarray(im, np.float32) / 255
        out.append(np.clip(.5 + (a - .5) * contrast, 0, 1) if contrast != 1 else a)
    return out[0], out[1]


def blur_rgb(rgb, sigma):
    return np.stack([ndi.gaussian_filter(rgb[..., i], sigma) for i in range(3)], -1) if sigma else rgb


def regions(img, count, compactness):
    """Stroke-shaped regions that follow the picture's own edges and ridges (SLIC superpixels)."""
    try:
        from skimage.segmentation import slic
    except ImportError:
        raise RuntimeError("painting needs scikit-image (sudo apt install python3-skimage, or pip install scikit-image)")
    kw = dict(n_segments=max(4, count), compactness=compactness, sigma=0, start_label=0, enforce_connectivity=True)
    try:
        return slic(img.astype(np.float64), channel_axis=-1, **kw)
    except TypeError:                                           # older scikit-image
        return slic(img.astype(np.float64), multichannel=True, **kw)


def panel_tones(gray):
    """(lo, hi, curve): stretch the picture's tones to the full range, then lift the mid-tones of a dark
    picture (curve < 1) so a 1-bit panel shows more than a black mass. Light pictures are left alone."""
    lo, hi = (float(v) for v in np.percentile(gray, (1, 99)))
    if hi - lo <= .08:
        return 0.0, 1.0, 1.0
    mid = float(np.median(np.clip((gray - lo) / (hi - lo) * .92 + .04, 0, 1)))
    return lo, hi, float(np.clip(np.log(.48) / np.log(max(mid, .02)), .62, 1.0))


# ==========================================================================
# One stroke: a region of the painting
# ==========================================================================
def region_stroke(mask, box, src_ss, level, rng):
    """A stroke that lays down `src_ss` inside `mask` (cropped to `box` = x0, y0, x1, y1, logical px)."""
    x0, y0, x1, y1 = box
    ys, xs = np.nonzero(mask)
    centre = np.array([xs.mean() + x0, ys.mean() + y0])
    cov = np.cov(np.stack([xs, ys])) if len(xs) > 2 else np.eye(2)
    vals, vecs = np.linalg.eigh(cov + np.eye(2) * 1e-3)            # the region's long axis stands in for the stroke's path
    axis, half = vecs[:, 1], 1.4 * float(np.sqrt(max(vals[1], 1e-3)))
    opacity = level[7]
    return dict(mask=mask, box=box, rgb=(src_ss[y0 * SS:y1 * SS, x0 * SS:x1 * SS] * 255 + .5).astype(np.uint8),
                a=opacity, p=opacity, w=float(max(2, 3 * np.sqrt(max(vals[0], 1e-3)))), taper=False,
                pts=np.array([centre - axis * half, centre, centre + axis * half], np.float32), ph=float(rng.random()))


# ==========================================================================
# Building the day, working backwards from the finished painting
# ==========================================================================
def build_paint_plan(image, seed: int, fit_mode="crop", look: Look = Look()):
    rng = np.random.default_rng([seed, 21])
    rgb_ss, rgb = load_colour(image, fit_mode, look.contrast)
    canvas = np.ones((H, W, 3), np.float32)                     # what the painting looks like so far
    texture, texture_ss = rgb - blur_rgb(rgb, 2.0), rgb_ss - blur_rgb(rgb_ss, 2.0 * SS)    # ridges, streaks, bristle marks
    phases = {}
    for level in LEVELS:
        phase, label, _, count, sigma, threshold, compactness = level[:7]
        seg = regions(blur_rgb(rgb, sigma), int(count * look.detail), compactness)
        n = int(seg.max()) + 1
        if level[8] is None:                                    # the last scale: the picture's own pixels
            src, src_ss = rgb, rgb_ss
        else:                                                   # a slab: the region's flat colour plus the painting's real texture
            ids = np.arange(n)
            flat = np.stack([ndi.mean(rgb[..., c], seg, index=ids) for c in range(3)], -1)
            src = np.clip(flat[seg] + level[8] * texture, 0, 1)
            src_ss = np.clip(flat[np.kron(seg, np.ones((SS, SS), seg.dtype))] + level[8] * texture_ss, 0, 1)
        off = ndi.mean(np.abs(canvas - src).mean(-1), seg, index=np.arange(n))     # how far each region is from the target
        boxes = ndi.find_objects(seg + 1)
        items, painted = [], np.zeros(n, bool)
        for lab in np.flatnonzero(off > threshold):
            sl = boxes[lab]
            if sl is None:
                continue
            y0, y1 = max(sl[0].start - 2, 0), min(sl[0].stop + 2, H)
            x0, x1 = max(sl[1].start - 2, 0), min(sl[1].stop + 2, W)
            mask = ndi.binary_dilation(seg[y0:y1, x0:x1] == lab, iterations=1)     # a pixel of overlap hides the seams
            s = region_stroke(mask, (x0, y0, x1, y1), src_ss, level, rng)
            items.append((float(s["pts"][1, 0]), float(s["pts"][1, 1]), [s]))
            painted[lab] = True
        canvas = np.where(painted[seg][..., None], src, canvas)
        area = float(painted[seg].sum()) or 1.0
        groups = organic_groups(items, area, rng, patch_area=area / EVENTS[phase]) if items else []
        phases[phase] = [ev(phase, label, g) for g in groups]

    events = [e for phase, *_ in LEVELS for e in fit(phases[phase], EVENTS[phase])]
    if len(events) < TOTAL:                                     # a scale with little left to fix: split the busiest events
        events = fit(events, TOTAL)
    events += [ev("finishing", "pause", []) for _ in range(TOTAL - len(events))]    # only if there is almost nothing to paint
    events[0] = dict(events[0], view=panel_tones(rgb @ LUMA))   # how the panel should show this picture's tones
    return events


# ==========================================================================
# Paint on canvas
# ==========================================================================
class PaintSheet:
    """A blank white canvas that takes strokes of the painting's own pixels. Same interface as the
    pencil's Sheet, so the baking, viewer and previews don't care which they get."""

    def __init__(self, seed, pegs=True):               # pegs: the pencil's animation-paper holes; not for canvas
        self.seed = seed
        self.canvas = np.ones((H * SS, W * SS, 3), np.float32)
        self.cover = np.zeros((H * SS, W * SS), np.float32)
        self.view = (0.0, 1.0, 1.0)

    def draw_event(self, e):
        if "view" in e:
            self.view = e["view"]
        for s in e["strokes"]:
            self.draw_stroke(s)

    def draw_stroke(self, s):
        """Lay the painting's pixels down inside the stroke's region, with a hand-cut edge."""
        r = np.random.default_rng(int(s["ph"] * 2 ** 31))      # the same stroke always paints the same way
        x0, y0 = s["box"][:2]
        m = np.kron(s["mask"].astype(np.float32), np.ones((SS, SS), np.float32))
        noise = ndi.gaussian_filter(r.normal(0, 1, m.shape), 1.5)
        a = np.clip((ndi.gaussian_filter(m, 1.0) - .5 + .12 * noise / (noise.std() + 1e-6)) * 3 + .5, 0, 1) * s["a"]
        reg = (slice(y0 * SS, y0 * SS + m.shape[0]), slice(x0 * SS, x0 * SS + m.shape[1]))
        patch = s["rgb"].astype(np.float32) / 255
        self.canvas[reg] = self.canvas[reg] * (1 - a[..., None]) + patch * a[..., None]
        np.maximum(self.cover[reg], a, out=self.cover[reg])

    def ink(self):
        """How much paint is on the sheet, 0-1 per pixel."""
        return self.cover

    def gray(self, size=(W, H)) -> Image.Image:
        lum = self.canvas @ LUMA
        lo, hi, curve = self.view
        if hi - lo > .08:                                       # use the whole tonal range on the 1-bit panel
            lum = np.where(self.cover > .02, np.clip((lum - lo) / (hi - lo) * .92 + .04, 0, 1) ** curve, lum)
        return Image.fromarray(np.clip(lum * 255, 0, 255).astype(np.uint8), "L").resize(size, Image.LANCZOS)

    def paper(self, size=None) -> Image.Image:
        img = Image.fromarray(np.clip(self.canvas * 255, 0, 255).astype(np.uint8), "RGB")
        return img.resize(size, Image.LANCZOS) if size else img
