"""Painting: a palette-knife painting built up from a blank canvas over the day.

The same 720-event day as the pencil, but the sheet starts completely blank and paint
goes on in layers, the way a painter works:

  composition   blocking in: a few huge knife sweeps of the big masses of colour
  linework      underpainting: broad strokes until the canvas is covered
  shading       building form: medium strokes that follow the flow of the image
  finishing     details at the edges, darkest accents, highlights, splatter

Paint is opaque and streaky, so later strokes cover earlier ones. It is planned and
rendered in colour; the 1-bit panel shows the luminance.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage as ndi

from .constants import H, SS, TOTAL, W
from .pen import resample
from .planner import Look, ev, fit, orientation_field, organic_groups
from .render import make_grain

SIZES = {"composition": 90, "linework": 200, "finishing": 130}     # shading takes the rest
LUMA = np.array([.299, .587, .114], np.float32)


# ==========================================================================
# Reading the image in colour
# ==========================================================================
def load_colour(path, fit_mode="crop", contrast=1.0):
    """(rgb, gray) as floats 0-1. contrast > 1 pushes darks darker and lights lighter."""
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("RGB")
    if fit_mode == "pad":
        img = ImageOps.pad(img, (W, H), color=(255, 255, 255))
    else:
        img = ImageOps.fit(img, (W, H), Image.LANCZOS)
    rgb = np.asarray(img, np.float32) / 255
    lum = rgb @ LUMA
    lo, hi = np.percentile(lum, (1, 99))              # use the whole tonal range, however muted the original,
    if hi - lo > .08:                                 # so the 1-bit panel has something to show (not for flat pictures)
        rgb = np.clip((rgb - lo) / (hi - lo) * .92 + .04, 0, 1)
    rgb = np.clip(.5 + (rgb - .5) * contrast, 0, 1)
    return rgb, rgb @ LUMA


def blur_rgb(rgb, sigma):
    return np.stack([ndi.gaussian_filter(rgb[..., i], sigma) for i in range(3)], -1)


# ==========================================================================
# One stroke: a centre line that drifts with the image's flow
# ==========================================================================
def flow_line(centre, base, length, theta, coh, follow, rng, bend=0.0, step=3.0):
    """Centre line of a stroke through `centre`, heading `base` degrees and drifting
    toward the local flow of the image (theta, coh from orientation_field)."""
    def walk(sign):
        p = np.array(centre, float)
        h = np.array([math.cos(math.radians(base)), math.sin(math.radians(base))]) * sign
        pts, run, turn = [p.copy()], 0.0, rng.normal(0, bend) if bend else 0.0
        while run < length / 2:
            xi, yi = int(np.clip(p[0], 0, W - 1)), int(np.clip(p[1], 0, H - 1))
            a = math.radians(theta[yi, xi])
            d = np.array([math.cos(a), math.sin(a)])
            if d @ h < 0:
                d = -d
            wt = .3 * follow * min(1.0, coh[yi, xi] * 2)
            h = h * (1 - wt) + d * wt
            ang = math.radians(turn) * step / 50                  # `bend` is degrees of drift per 50 px
            c, s = math.cos(ang), math.sin(ang)
            h = np.array([h[0] * c - h[1] * s, h[0] * s + h[1] * c])
            h /= np.hypot(*h) + 1e-9
            q = p + h * step
            if not (0 <= q[0] < W - 1 and 0 <= q[1] < H - 1):
                break
            p = q
            pts.append(p.copy())
            run += step
        return pts
    back = walk(-1)[::-1]
    return np.array(back + walk(1)[1:])


def make_stroke(rng, x, y, w, length, field, base, follow, opacity, kind, dry, tone, bend=6.0, wobble=.03):
    """A paint stroke dict, coloured from the image under it (`tone` is a blurred RGB image)."""
    theta, coh = field
    length *= rng.uniform(.75, 1.25)
    pts = flow_line((x, y), base + rng.normal(0, 12), length, theta, coh, follow, rng, bend, step=min(3.0, length / 6))
    if len(pts) < 3:
        return None
    xi = np.clip(np.round(pts[:, 0]).astype(int), 0, W - 1)
    yi = np.clip(np.round(pts[:, 1]).astype(int), 0, H - 1)
    colour = tone[yi, xi].mean(0) * (1 + rng.normal(0, wobble))
    return dict(pts=pts.astype(np.float32), w=float(w), c=tuple(float(v) for v in np.clip(colour, 0, 1)),
                p=float(opacity), a=float(opacity), kind=kind, dry=float(dry), taper=False, ph=float(rng.random()))


def midpoint(s):
    m = s["pts"][len(s["pts"]) // 2]
    return float(m[0]), float(m[1])


# ==========================================================================
# Where strokes go
# ==========================================================================
def cover_canvas(rng, target, maker, max_strokes=1500):
    """Strokes from random bare spots until `target` of the canvas is covered. Starting
    each stroke where nothing has been painted yet fills the sheet without any lattice."""
    seen = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(seen)
    out, attempts = [], 0
    ys = xs = None
    while len(out) < max_strokes and attempts < max_strokes * 4:
        if ys is None or attempts % 6 == 0:
            bare = np.asarray(seen) == 0
            if bare.mean() <= 1 - target:
                break
            ys, xs = np.nonzero(bare)
        attempts += 1
        i = int(rng.integers(len(xs)))
        s = maker(float(xs[i]), float(ys[i]))
        if s is None:
            continue
        out.append(s)
        draw.line([tuple(p) for p in s["pts"]], fill=255, width=max(1, int(s["w"])), joint="curve")
    return out


def pick_spots(rng, weights, n):
    """n positions, more likely where `weights` is high."""
    p = weights.ravel().astype(np.float64)
    p = p / p.sum() if p.sum() > 0 else np.full(p.size, 1 / p.size)
    idx = rng.choice(p.size, size=n, p=p)
    return [(float(np.clip(i % W + rng.uniform(-.5, .5), 0, W - 1)), float(np.clip(i // W + rng.uniform(-.5, .5), 0, H - 1)))
            for i in idx]


def as_items(strokes):
    return [(*midpoint(s), [s]) for s in strokes if s]


def patches(strokes, events, rng, area=W * H):
    """Gather strokes into about `events` patches, visited in a wandering order."""
    return organic_groups(as_items(strokes), area, rng, patch_area=area / max(events, 1))


# ==========================================================================
# Building the day
# ==========================================================================
def build_paint_plan(image, seed: int, fit_mode="crop", look: Look = Look()):
    rng = np.random.default_rng([seed, 21])
    rgb, gray = load_colour(image, fit_mode, look.contrast)
    d = look.detail
    coarse = orientation_field(ndi.gaussian_filter(gray, 5), sigma=20)
    medium = orientation_field(gray, sigma=10)
    fine = orientation_field(gray, sigma=5)
    out = {}

    # ---- composition: blocking in, a few huge sweeps of the big masses ----
    tone = blur_rgb(rgb, 12)
    block = cover_canvas(rng, .92, lambda x, y: make_stroke(
        rng, x, y, rng.uniform(40, 70), rng.uniform(90, 170), coarse, 55, .35, .97, "knife", .12, tone, bend=4))
    out["composition"] = [ev("composition", "block in", g) for g in patches(block, SIZES["composition"], rng)]

    # ---- linework: underpainting, broad strokes until nothing is bare ----
    tone = blur_rgb(rgb, 5)
    under = cover_canvas(rng, .9995, lambda x, y: make_stroke(
        rng, x, y, rng.uniform(24, 40), rng.uniform(55, 110), coarse, 40, .5, .96, "knife", .15, tone, bend=6), 2500)
    out["linework"] = [ev("linework", "underpainting", g) for g in patches(under, SIZES["linework"], rng)]

    # ---- shading: building form, medium strokes that follow the flow, mostly where there is something to see ----
    tone = blur_rgb(rgb, 2.2)
    grad = ndi.gaussian_gradient_magnitude(gray, 2.0)
    interest = .15 + grad / (grad.max() + 1e-9)
    size = 1 / d ** .5
    n_build = int(700 * d)
    build = [make_stroke(rng, x, y, rng.uniform(12, 26) * size, rng.uniform(28, 70) * size, medium, 30, .75, .96,
                         "knife" if rng.random() < .6 else "brush", rng.uniform(.1, .45), tone, bend=8)
             for x, y in pick_spots(rng, interest, n_build)]
    n_shading = TOTAL - sum(len(v) for v in out.values()) - SIZES["finishing"]
    out["shading"] = [ev("shading", "build form", g) for g in patches(build, n_shading, rng)]

    # ---- finishing: details, darkest darks, lights, splatter ----
    sharp = np.clip(.5 + (blur_rgb(rgb, 1.0) - .5) * 1.12, 0, 1)
    edge = grad ** 1.4
    dark_at = ndi.gaussian_filter((gray < np.percentile(gray, 9)).astype(float), 3)
    light_at = ndi.gaussian_filter((gray > np.percentile(gray, 96)).astype(float), 3)
    detail = [make_stroke(rng, x, y, rng.uniform(5, 12) / d ** .5, rng.uniform(12, 34) / d ** .5, fine, 20, .8, .97,
                          "knife" if rng.random() < .5 else "brush", rng.uniform(.1, .5), sharp, bend=8)
              for x, y in pick_spots(rng, edge, int(520 * d))]
    darks = [make_stroke(rng, x, y, rng.uniform(6, 12), rng.uniform(12, 28), fine, 70, .6, .98, "knife", .1,
                         sharp, bend=6)
             for x, y in pick_spots(rng, dark_at + 1e-6, int(36 * d))]
    lights = [make_stroke(rng, x, y, rng.uniform(5, 13), rng.uniform(14, 40), fine, 35, .5, .98, "knife", .25,
                          .55 * sharp + .45, bend=6)
              for x, y in pick_spots(rng, light_at + 1e-6, int(32 * d))]
    flicks = []
    for x, y in pick_spots(rng, dark_at * grad + 1e-6, 14):                    # spatter near dark areas
        wd = rng.uniform(2, 5)                                                  # a dot of flung paint
        flicks.append(make_stroke(rng, x, y, wd, wd * rng.uniform(1, 1.8), fine, rng.uniform(0, 180), 0, .97,
                                  "dab", 0, sharp * .3, bend=0))
    budget = SIZES["finishing"]
    total = sum(len(v) for v in (detail, darks, lights, flicks)) or 1
    fin = []
    for label, strokes in (("detail stroke", detail), ("dark accent", darks), ("highlight", lights),
                           ("splatter", flicks)):
        events = max(1, round(budget * len(strokes) / total))
        fin += [ev("finishing", label, g) for g in patches([s for s in strokes if s], events, rng)]
    out["finishing"] = fin

    # ---- fit exactly 720 events ----
    for k in ("composition", "linework", "finishing"):
        out[k] = fit(out[k], SIZES[k])
    out["shading"] = fit(out["shading"], TOTAL - sum(len(out[k]) for k in ("composition", "linework", "finishing")))
    events = [e for k in ("composition", "linework", "shading", "finishing") for e in out[k]]
    while len(events) < TOTAL:
        events.append(ev("finishing", "pause", []))
    return events[:TOTAL]


# ==========================================================================
# Paint on canvas
# ==========================================================================
class PaintSheet:
    """A blank white canvas that takes opaque, streaky strokes. Same interface as the
    pencil's Sheet, so the baking, viewer and previews don't care which they get."""

    def __init__(self, seed, pegs=True):               # pegs: the pencil's animation-paper holes; not for canvas
        self.seed = seed
        self.canvas = np.ones((H * SS, W * SS, 3), np.float32)
        self.cover = np.zeros((H * SS, W * SS), np.float32)
        self.grain = make_grain(seed)

    def draw_event(self, e):
        for s in e["strokes"]:
            self.draw_stroke(s)

    def draw_stroke(self, s):
        """One loaded stroke: a solid ribbon with hand-cut edges, split into a few wide
        streaks that peel away at the dry end, then lit from the top left like thick paint."""
        r = np.random.default_rng(int(s["ph"] * 2 ** 31))           # the same stroke always paints the same way
        pts = resample(np.asarray(s["pts"], np.float32) * SS, 1.5 if s["kind"] == "dab" else 2.0)
        n = len(pts)
        if n < 3:
            return
        tang = np.gradient(pts, axis=0)
        tang /= np.linalg.norm(tang, axis=1, keepdims=True) + 1e-9
        nor = np.stack([-tang[:, 1], tang[:, 0]], 1)
        w, kind, dry = s["w"] * SS, s["kind"], s["dry"]
        pad = int(w) + 8
        x0, y0 = np.floor(pts.min(0)).astype(int) - pad
        x1, y1 = np.ceil(pts.max(0)).astype(int) + pad
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W * SS), min(y1, H * SS)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        t = np.linspace(0, 1, n)
        prof = .8 + .2 * np.sqrt(np.clip(t / .06, 0, 1))              # a blunt start, full width almost at once
        if kind == "brush":
            prof = prof * (.55 + .45 * np.clip((1 - t) / .2, 0, 1) ** .6)     # a brush lifts off and thins
        elif kind == "dab":
            prof = .15 + .85 * np.sqrt(np.clip(np.sin(np.pi * t), 0, 1))      # a round blob
        k = min(9, max(3, n // 4) | 1)

        def edge():                                                    # hand-cut edge: slow, small wobble
            v = np.convolve(r.normal(0, 1, n + k - 1), np.ones(k) / k, "valid")
            return 1 + .07 * v / (v.std() + 1e-6)
        half = (w / 2 * prof * edge(), w / 2 * prof * edge())          # left and right half-widths

        def at(u):                                                     # offset curve at u in [-1, 1] across the ribbon
            return pts + nor * (np.where(u < 0, half[0], half[1]) * u)[:, None]

        nb = 1 if kind == "dab" else int(np.clip(round(w / (14 if kind == "knife" else 9)), 1, 8))
        colour = np.asarray(s["c"], np.float32)
        img = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        o = np.array([x0, y0], np.float32)
        for i in range(nb):
            ua, ub = -1 + 2 * i / nb - (.12 / nb if i else 0), -1 + 2 * (i + 1) / nb
            t0 = int(n * r.uniform(0, .03)) if i in (0, nb - 1) else 0
            t1 = n - int(n * dry * .55 * r.random() ** 1.3)            # streaks drop away at different points
            if t1 - t0 < 3:
                continue
            band = np.vstack([at(np.full(n, ua))[t0:t1], at(np.full(n, ub))[t0:t1][::-1]]) - o
            col = np.clip(colour + r.normal(0, .03 if kind == "knife" else .05) + r.normal(0, .01, 3), 0, 1)
            rgba = tuple(int(v) for v in col * 255) + (int(255 * s["a"] * r.uniform(.94, 1)),)
            draw.polygon([tuple(p) for p in band], fill=rgba)
        arr = np.asarray(img, np.float32) / 255
        alpha = arr[..., 3]
        valley = np.clip((.55 - self.grain[y0:y1, x0:x1]) / .45, 0, 1)    # the canvas weave shows through dry paint
        a = alpha * (1 - valley * (.05 + .5 * dry))
        reg = (slice(y0, y1), slice(x0, x1))
        self.canvas[reg] = self.canvas[reg] * (1 - a[..., None]) + arr[..., :3] * a[..., None]
        relief = ndi.gaussian_filter(alpha, 1.4)                       # raised paint: lit edge top-left, shadow bottom-right
        gy, gx = np.gradient(relief)
        self.canvas[reg] += ((gx + gy) * .16 * np.clip(alpha * 1.5, 0, 1))[..., None]
        np.clip(self.canvas[reg], 0, 1, out=self.canvas[reg])
        np.maximum(self.cover[reg], a, out=self.cover[reg])

    def ink(self):
        """How much paint is on the sheet, 0-1 per pixel."""
        return self.cover

    def gray(self, size=(W, H)) -> Image.Image:
        lum = self.canvas @ LUMA
        return Image.fromarray(np.clip(lum * 255, 0, 255).astype(np.uint8), "L").resize(size, Image.LANCZOS)

    def paper(self, size=None) -> Image.Image:
        img = Image.fromarray(np.clip(self.canvas * 255, 0, 255).astype(np.uint8), "RGB")
        return img.resize(size, Image.LANCZOS) if size else img
