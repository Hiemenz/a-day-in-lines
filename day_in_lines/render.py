"""Graphite on paper, and the 1-bit dither for the panel."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .constants import H, PAPER_RGB, SS, W
from .pen import Pen, arc


def make_grain(seed):
    r = np.random.default_rng([seed, 7])
    g = r.random((H * SS, W * SS)).astype(np.float32)
    g = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.9)), np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6) * 0.2 + 0.5
    return np.clip(g, 0, 1)


class Sheet:
    def __init__(self, seed, pegs=True):
        self.seed = seed
        self.canvas = np.ones((H * SS, W * SS), np.float32)
        self.grain = make_grain(seed)
        if not pegs:
            return
        pen = Pen(np.random.default_rng([seed, 3]))     # animation paper's peg holes:
        for cx, rx in ((400, 7), (352, 10), (448, 10)):  # the "almost" in almost blank
            for s in pen.line(arc(cx, 468, rx, 3.2, 0, 361), p=.15, w=.8, wob=0, taper=False):
                self.draw_stroke(s)

    def draw_event(self, e):
        for s in e["strokes"]:
            self.draw_stroke(s)

    def draw_stroke(self, s):
        pts = np.asarray(s["pts"], np.float32) * SS
        if len(pts) < 2:
            return
        x0, y0 = np.floor(pts.min(0)).astype(int) - 6
        x1, y1 = np.ceil(pts.max(0)).astype(int) + 6
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W * SS), min(y1, H * SS)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        img = Image.new("L", (x1 - x0, y1 - y0), 0)
        d = ImageDraw.Draw(img)
        n = len(pts)
        t = np.linspace(0, 1, n)
        if s["taper"]:
            tp = 0.3 + 0.7 * np.sqrt(np.clip(np.minimum(t / 0.14, (1 - t) / 0.14), 0, 1))
        else:
            tp = np.ones(n)
        pr = np.clip(s["p"] * tp * (0.86 + 0.14 * np.sin(2 * np.pi * (t * 2.3 + s["ph"]))), 0, 1)
        wpx = s["w"] * SS * 1.1
        o = np.array([x0, y0], np.float32)
        for i in range(n - 1):
            a = tuple((pts[i] - o).tolist()); b = tuple((pts[i + 1] - o).tolist())
            wi = max(1, int(round(wpx * (0.7 + 0.3 * tp[i]))))
            d.line([a, b], fill=int(255 * pr[i]), width=wi)
            if wi > 1:
                r = wi / 2
                d.ellipse([b[0] - r + .5, b[1] - r + .5, b[0] + r - .5, b[1] + r - .5], fill=int(255 * pr[i]))
        img = img.filter(ImageFilter.GaussianBlur(0.55))
        a = np.asarray(img, np.float32) / 255
        a = a * np.clip(0.3 + 1.0 * self.grain[y0:y1, x0:x1], 0, 1.2)   # paper tooth
        self.canvas[y0:y1, x0:x1] *= (1 - np.clip(a * 0.95, 0, 0.94))

    def gray(self, size=(W, H)) -> Image.Image:
        img = Image.fromarray(np.clip(self.canvas * 255, 0, 255).astype(np.uint8), "L")
        return img.resize(size, Image.LANCZOS)

    def paper(self, size=None) -> Image.Image:
        r = np.random.default_rng(5)
        mott = np.asarray(Image.fromarray((r.random((H // 4, W // 4)) * 255).astype(np.uint8))
                          .resize((W * SS, H * SS), Image.BICUBIC), np.float32) / 255
        tint = PAPER_RGB * (0.985 + 0.03 * mott[..., None]) * (0.97 + 0.03 * self.grain[..., None])
        rgb = tint * self.canvas[..., None] ** 1.05
        out = Image.fromarray(np.clip(rgb * 255, 0, 255).astype(np.uint8), "RGB")
        return out.resize(size, Image.LANCZOS) if size else out


def bayer(n):
    m = np.array([[0, 2], [3, 1]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


def dither_1bit(gray: Image.Image, gamma=2.4) -> Image.Image:
    """Ordered (Bayer) dither. It's local, so adding a stroke only flips pixels
    under that stroke: partial refreshes show exactly the new marks, nothing else."""
    g = (np.asarray(gray, np.float32) / 255) ** gamma
    h, w = g.shape
    t = np.tile(bayer(8), (h // 8 + 1, w // 8 + 1))[:h, :w]
    return Image.fromarray(((g > t) * 255).astype(np.uint8), "L").convert("1")
