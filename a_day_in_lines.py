#!/usr/bin/env python3
"""
A Day in Lines
==============

Give it any image. Over 12 hours it redraws that image on an e-ink display as a
graphite pencil drawing, one stroke (or small connected group of strokes) per
minute: 720 drawing events, nothing ever erased or redrawn.

The day unfolds the way an animator works at a desk:

  minutes   1-40    composition    faint, loose construction lines of the big shapes
  minutes  41-260   linework       contours, coarse to fine, strongest first
  minutes 261-670   shading        hatching built up in layers, light to dark
  minutes 671-720   finishing      darkest accents, re-stated edges, small marks

The plan is deterministic for a given image + seed, so after a reboot the sheet
is rebuilt stroke-for-stroke. By default the seed is the date, so the same
picture drawn on a different day comes out in a slightly different hand.

Commands
--------
  python3 a_day_in_lines.py plan    --image ship.png
  python3 a_day_in_lines.py frame   --image ship.png 360 -o halfway.png [--mono]
  python3 a_day_in_lines.py preview --image ship.png -o preview/
  python3 a_day_in_lines.py run     --image ship.png --display file
  python3 a_day_in_lines.py run     --image ~/pictures/ --display waveshare:epd7in5_V2

--image can be a folder: a different picture is chosen each day.

Requires numpy, scipy and Pillow (on Raspberry Pi OS: apt install python3-numpy
python3-scipy python3-pil).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from scipy import ndimage as ndi

W, H = 800, 480          # logical sheet (5:3, matches 7.5" 800x480 panels)
SS = 2                   # supersampling for the graphite render
TOTAL = 720              # drawing events per day
PHASES = (("composition", 40), ("linework", 230), ("shading", None), ("finishing", 50))
PAPER_RGB = np.array([0.965, 0.948, 0.905], np.float32)   # preview only
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


# ==========================================================================
# Geometry helpers
# ==========================================================================
def resample(pts, step):
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return np.vstack([pts, pts])
    d = np.hypot(*np.diff(pts, axis=0).T)
    L = np.concatenate([[0], np.cumsum(d)])
    if L[-1] < 1e-6:
        return pts[:1].repeat(2, 0)
    n = max(2, int(L[-1] / step) + 1)
    s = np.linspace(0, L[-1], n)
    return np.stack([np.interp(s, L, pts[:, 0]), np.interp(s, L, pts[:, 1])], 1)


def length(pts):
    pts = np.asarray(pts)
    return float(np.hypot(*np.diff(pts, axis=0).T).sum()) if len(pts) > 1 else 0.0


def arc(cx, cy, rx, ry, a0, a1, n=48):
    a = np.radians(np.linspace(a0, a1, n))
    return np.stack([cx + rx * np.cos(a), cy + ry * np.sin(a)], 1)


# ==========================================================================
# The pencil: ideal path -> hand-drawn stroke
# ==========================================================================
class Pen:
    def __init__(self, rng):
        self.rng = rng

    def _wobble(self, pts, amp):
        p = resample(pts, 2.0)
        n = len(p)
        if n < 3 or amp <= 0:
            return p
        tang = np.gradient(p, axis=0)
        tang /= np.linalg.norm(tang, axis=1, keepdims=True) + 1e-9
        nor = np.stack([-tang[:, 1], tang[:, 0]], 1)
        L = length(p)
        s = np.linspace(0, 1, n)
        off = np.zeros(n)
        for _ in range(3):
            cycles = L / self.rng.uniform(35, 140)
            off += self.rng.normal(0, amp / 2) * np.sin(2 * np.pi * (cycles * s + self.rng.random()))
        return p + nor * off[:, None]

    def line(self, pts, p=0.5, w=1.0, wob=0.5, over=0.0, taper=True, jitter=0.0):
        r = self.rng
        pts = np.array(pts, float)
        if len(pts) < 2:
            return []
        if jitter:
            pts = pts + r.normal(0, jitter, pts.shape)
        if over:
            a = pts[1] - pts[0]; b = pts[-1] - pts[-2]
            a /= np.linalg.norm(a) + 1e-9; b /= np.linalg.norm(b) + 1e-9
            pts = np.vstack([pts[0] - a * over * r.uniform(0.3, 1.2), pts,
                             pts[-1] + b * over * r.uniform(0.3, 1.2)])
        q = self._wobble(pts, wob)
        if length(q) < 2.0:
            return []
        return [dict(pts=q.astype(np.float32), p=float(np.clip(p * r.uniform(0.9, 1.1), 0, 1)),
                     w=float(w), taper=bool(taper), ph=float(r.random()))]


# ==========================================================================
# Reading the image
# ==========================================================================
def load_target(path: Path, fit="crop"):
    """Return (tone, gray): tone 0 = bare paper, 1 = darkest graphite."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("L")
    if fit == "pad":
        img = ImageOps.pad(img, (W, H), color=int(np.percentile(np.asarray(img), 95)))
    else:
        img = ImageOps.fit(img, (W, H), Image.LANCZOS)
    g = np.asarray(img, np.float32) / 255
    paper = np.percentile(g, 92)
    ink = np.percentile(g, 0.7)
    tone = np.clip((paper - g) / max(paper - ink, 1e-3), 0, 1)
    tone = ndi.gaussian_filter(tone, 1.0)
    return tone ** 0.95, g


def canny(gray, sigma, lo_pct, hi_pct):
    g = ndi.gaussian_filter(gray, sigma)
    gx, gy = ndi.sobel(g, 1), ndi.sobel(g, 0)
    mag = np.hypot(gx, gy)
    ang = (np.degrees(np.arctan2(gy, gx)) + 180) % 180
    q = ((ang + 22.5) // 45).astype(int) % 4
    nms = np.zeros_like(mag)
    for k, (dy, dx) in enumerate(((0, 1), (1, 1), (1, 0), (1, -1))):
        a = np.roll(mag, (dy, dx), (0, 1)); b = np.roll(mag, (-dy, -dx), (0, 1))
        sel = (q == k) & (mag >= a) & (mag >= b)
        nms[sel] = mag[sel]
    nz = nms[nms > 0]
    if nz.size == 0:
        return np.zeros_like(mag, bool), mag
    hi, lo = np.percentile(nz, hi_pct), np.percentile(nz, lo_pct)
    weak = nms > lo
    lab, n = ndi.label(weak, np.ones((3, 3)))
    keep = np.zeros(n + 1, bool)
    keep[np.unique(lab[nms > hi])] = True
    keep[0] = False
    e = keep[lab]
    e[:2, :] = e[-2:, :] = False
    e[:, :2] = e[:, -2:] = False
    return e, mag


NB = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


def trace(edge):
    """Thin edge map -> list of polylines (x, y)."""
    e = edge.copy()
    deg = ndi.convolve(e.astype(int), np.ones((3, 3), int), mode="constant") - 1
    deg[~e] = 0
    ys, xs = np.nonzero(e)
    order = sorted(zip(ys, xs), key=lambda p: deg[p] != 1)   # endpoints first
    seen = np.zeros_like(e)
    paths = []
    for y0, x0 in order:
        if seen[y0, x0]:
            continue
        path = [(x0, y0)]
        seen[y0, x0] = True
        y, x = y0, x0
        while True:
            nxt = None
            for dy, dx in NB:
                yy, xx = y + dy, x + dx
                if e[yy, xx] and not seen[yy, xx]:
                    nxt = (yy, xx)
                    break
            if nxt is None:
                break
            y, x = nxt
            seen[y, x] = True
            path.append((x, y))
        if len(path) >= 4:
            paths.append(np.array(path, float))
    return paths


def smooth(pts, k=5):
    if len(pts) < k + 2:
        return pts
    ker = np.ones(k) / k
    pad = np.pad(pts, ((k // 2, k // 2), (0, 0)), mode="edge")
    return np.stack([np.convolve(pad[:, i], ker, "valid") for i in range(2)], 1)


def split_long(pts, maxlen, rng):
    out, cur = [], [pts[0]]
    run = 0.0
    lim = maxlen * rng.uniform(.7, 1.2)
    for a, b in zip(pts[:-1], pts[1:]):
        run += float(np.hypot(*(b - a)))
        cur.append(b)
        if run > lim:
            out.append(np.array(cur)); cur = [b]; run = 0.0
            lim = maxlen * rng.uniform(.7, 1.2)
    if len(cur) > 2:
        out.append(np.array(cur))
    return out


def sample(img, pts):
    xi = np.clip(np.round(pts[:, 0]).astype(int), 0, W - 1)
    yi = np.clip(np.round(pts[:, 1]).astype(int), 0, H - 1)
    return img[yi, xi]


def contour_paths(gray, tone, sigma, lo, hi, minlen, maxlen, rng):
    e, mag = canny(gray, sigma, lo, hi)
    paths = []
    for p in trace(e):
        p = smooth(p, 5 if sigma > 2 else 3)
        if length(p) < minlen:
            continue
        for piece in split_long(p, maxlen, rng):
            if length(piece) >= minlen * .6:
                strength = float(sample(mag, piece).mean())
                dark = float(sample(tone, piece).mean())
                paths.append(dict(pts=piece, score=length(piece) * strength, dark=dark))
    return paths, e


def ridge_paths(gray, tone, rng, pct=96.5, minlen=8, maxlen=70):
    """Thin dark lines (masts, rigging, fine detail) that edge detection splits in two."""
    try:
        from skimage.morphology import skeletonize
    except ImportError:
        return []
    dark = 1 - ndi.gaussian_filter(gray, 0.7)
    th = ndi.white_tophat(dark, size=7)
    m = (th > np.percentile(th, pct)) & (tone > .2)
    m = ndi.binary_opening(m, structure=np.ones((2, 2))) | (th > np.percentile(th, 99.3))
    lab, n = ndi.label(m, np.ones((3, 3)))
    if n:
        area = ndi.sum(m, lab, index=np.arange(1, n + 1))
        m = np.concatenate([[False], area >= 12])[lab]
    sk = skeletonize(m)
    sk[:2, :] = sk[-2:, :] = False
    sk[:, :2] = sk[:, -2:] = False
    out = []
    for p in trace(sk):
        p = smooth(p, 3)
        if length(p) < minlen:
            continue
        for piece in split_long(p, maxlen, rng):
            if length(piece) >= minlen * .6:
                out.append(dict(pts=piece, score=length(piece) * float(sample(th, piece).mean()) * 40,
                                dark=float(sample(tone, piece).mean())))
    return out


def orientation_field(gray, sigma=6):
    """Local isophote angle (degrees) and coherence from the structure tensor."""
    g = ndi.gaussian_filter(gray, 1.5)
    gx, gy = ndi.sobel(g, 1), ndi.sobel(g, 0)
    jxx = ndi.gaussian_filter(gx * gx, sigma)
    jyy = ndi.gaussian_filter(gy * gy, sigma)
    jxy = ndi.gaussian_filter(gx * gy, sigma)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy)        # gradient direction
    coh = np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / (jxx + jyy + 1e-9)
    return np.degrees(theta) + 90, coh                   # +90: along the edge


# ==========================================================================
# Building the day
# ==========================================================================
def ev(phase, label, strokes):
    return dict(phase=phase, label=label, strokes=[s for s in strokes if s])


def group_paths(paths, budget_len, radius, rng):
    """Seed from the strongest remaining path, gather neighbours into one event."""
    remaining = sorted(paths, key=lambda p: -p["score"])
    mids = {id(p): p["pts"][len(p["pts"]) // 2] for p in remaining}
    groups = []
    while remaining:
        seed = remaining.pop(0)
        grp, tot = [seed], length(seed["pts"])
        c = mids[id(seed)]
        if tot < budget_len:
            near = sorted(remaining, key=lambda p: np.hypot(*(mids[id(p)] - c)))
            for p in near:
                if tot >= budget_len or np.hypot(*(mids[id(p)] - c)) > radius:
                    break
                grp.append(p); tot += length(p["pts"])
            ids = {id(p) for p in grp}
            remaining = [p for p in remaining if id(p) not in ids]
        groups.append(grp)
    return groups


def build_plan(image: Path, seed: int, fit_mode="crop"):
    rng = np.random.default_rng([seed, 1])
    pen = Pen(rng)
    tone, gray = load_target(image, fit_mode)
    phases = {}

    # ---- composition: the big shapes, loosely --------------------------
    coarse, _ = contour_paths(gray, tone, 6.0, 70, 92, 30, 160, rng)
    coarse = sorted(coarse, key=lambda p: -p["score"])[:90]
    comp = []
    for grp in group_paths(coarse, 150, 70, rng):
        st = []
        for p in grp:
            st += pen.line(p["pts"][::3], p=.15 + .08 * p["dark"], w=.9, wob=1.4, over=7)
        comp.append(ev("composition", "construction line", st))
    phases["composition"] = comp

    # ---- linework: contours coarse -> fine --------------------------------
    med, med_e = contour_paths(gray, tone, 2.6, 72, 93, 14, 110, rng)
    fine, fine_e = contour_paths(gray, tone, 1.3, 80, 95, 10, 70, rng)
    near_med = ndi.binary_dilation(med_e, iterations=2)
    fine = [p for p in fine if sample(near_med.astype(float), p["pts"]).mean() < .5]
    ridges = ridge_paths(gray, tone, rng)
    lines = []
    for label, pool, pbase, budget in (("contour", med, .4, 120), ("thin line", ridges, .45, 60),
                                       ("detail line", fine, .36, 70)):
        for grp in group_paths(pool, budget, 40, rng):
            st = []
            for p in grp:
                st += pen.line(p["pts"], p=pbase + .32 * p["dark"], w=1.0, wob=.45, over=1.5)
            lines.append(ev("linework", label, st))
    phases["linework"] = lines

    # ---- shading + finishing: hatching layers -----------------------------
    theta, coh = orientation_field(gray)
    layers = [
        # (threshold, spacing, angle offset, pressure, max stroke length, follow-image weight)
        (.12, 4.4, 0, .26, 30, .8),
        (.20, 4.0, 58, .30, 28, .6),
        (.34, 3.8, -46, .34, 26, .4),
        (.48, 3.5, 88, .38, 24, .3),
        (.62, 3.3, 22, .44, 22, .3),
        (.76, 3.1, -20, .5, 20, .3),
        (.86, 3.0, 70, .62, 16, .2),      # -> finishing: darkest accents
    ]
    names = ["first tone", "cross-hatching", "third layer", "deepening", "dark masses",
             "deep shadow", "darkest accent"]
    noise = ndi.gaussian_filter(np.random.default_rng([seed, 9]).normal(0, 1, (H, W)), 6)
    noise = noise / (noise.std() + 1e-9) * .035
    soft = ndi.gaussian_filter(tone, 2.0)
    shade, accent = [], []
    cell = 40
    base_angle = 50.0
    for li, (thr, spc, aoff, pr, maxlen, follow) in enumerate(layers):
        mask = ndi.binary_opening(soft > thr + noise, iterations=2)
        lab, n = ndi.label(mask)
        if n:
            area = ndi.sum(mask, lab, index=np.arange(1, n + 1))
            mask = np.concatenate([[False], area >= 150])[lab]
        cells = hatch_layer(pen, rng, mask, theta, coh, base_angle + aoff, spc, pr, maxlen, follow, cell)
        is_acc = li == len(layers) - 1
        for key in sorted(cells, key=lambda k: (k[1] // 3, k[0], k[1])):
            (accent if is_acc else shade).append(ev("finishing" if is_acc else "shading", names[li], cells[key]))
    # finishing also re-states the strongest dark edges with real pressure
    darkest = sorted(med + fine + ridges, key=lambda p: -(p["dark"] * 2 + p["score"] / 4000))[:40]
    for grp in group_paths(darkest, 90, 50, rng):
        st = []
        for p in grp:
            st += pen.line(p["pts"], p=.82, w=1.15, wob=.35)
        accent.append(ev("finishing", "accent line", st))
    for p in rng.choice(len(coarse), size=min(6, len(coarse)), replace=False) if coarse else []:
        accent.append(ev("finishing", "restated construction", pen.line(coarse[p]["pts"][::3] + rng.normal(0, 1.5, 2),
                                                                        p=.2, wob=1.6, over=6)))
    rng.shuffle(accent)
    phases["finishing"] = accent

    # ---- fit everything into exactly 720 minutes -------------------------
    sizes = {k: n for k, n in PHASES if n}
    out = {}
    for k in ("composition", "linework", "finishing"):
        out[k] = fit(phases[k], sizes[k])
    out["shading"] = fit(shade, TOTAL - sum(len(v) for v in out.values()))
    events = [e for k, _ in PHASES for e in out[k]]
    if len(events) != TOTAL:   # tiny/empty images: pad with quiet restatements
        while len(events) < TOTAL:
            events.append(ev("finishing", "pause", []))
        events = events[:TOTAL]
    return events


def hatch_layer(pen, rng, mask, theta, coh, angle, spc, pr, maxlen, follow, cell):
    """One layer of hatching: continuous parallel lines across the sheet, cut into
    hand-length strokes inside `mask`, each nudged toward the image's local flow.
    Returns {cell: strokes} so each minute can draw one patch."""
    a = math.radians(angle)
    d = np.array([math.cos(a), math.sin(a)])
    n = np.array([-d[1], d[0]])
    c = np.array([W / 2, H / 2])
    R = math.hypot(W, H) / 2 + 4
    cells = {}
    off = -R + rng.uniform(0, spc)
    while off < R:
        base = c + n * off
        q = resample([base - d * R, base + d * R], 1.0)
        inside = (q[:, 0] >= 0) & (q[:, 0] < W - .5) & (q[:, 1] >= 0) & (q[:, 1] < H - .5)
        ok = np.zeros(len(q), bool)
        qi = np.round(q[inside]).astype(int)
        ok[inside] = mask[qi[:, 1], qi[:, 0]]
        idx = np.flatnonzero(np.diff(np.concatenate([[0], ok.astype(int), [0]])))
        for s, e in zip(idx[::2], idx[1::2]):
            seg = q[s:e]
            if len(seg) < 3:
                continue
            k = max(1, int(math.ceil(len(seg) / (maxlen * rng.uniform(.75, 1.25)))))
            for piece in np.array_split(seg, k):
                if len(piece) < 3:
                    continue
                m = piece[len(piece) // 2]
                L = length(piece) + rng.uniform(-1, 1.5)
                yi, xi = int(m[1]), int(m[0])
                w = follow * min(1.0, coh[yi, xi] * 2)
                diff = ((theta[yi, xi] - angle + 90) % 180) - 90
                aa = math.radians(angle + w * diff + rng.normal(0, 3))
                dd = np.array([math.cos(aa), math.sin(aa)])
                st = pen.line([m - dd * L / 2, m + dd * L / 2], p=pr, w=.9, wob=.3, jitter=.25)
                cells.setdefault((xi // cell, yi // cell), []).extend(st)
        off += spc * rng.uniform(.85, 1.15)
    return cells


def fit(events, n):
    """Merge or split events so there are exactly n (when possible)."""
    events = [e for e in events if e["strokes"]]
    if not events or n <= 0:
        return events[:max(n, 0)]
    while len(events) < n:
        i = max(range(len(events)), key=lambda k: len(events[k]["strokes"]))
        s = events[i]["strokes"]
        if len(s) < 2:
            break
        h = len(s) // 2
        events[i:i + 1] = [dict(events[i], strokes=s[:h]), dict(events[i], strokes=s[h:])]
    if len(events) > n:
        events = [dict(events[g[0]], strokes=sum([events[k]["strokes"] for k in g], []))
                  for g in np.array_split(np.arange(len(events)), n)]
    return events


# ==========================================================================
# The library: every image is broken down once, keyed by its contents
# ==========================================================================
def image_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def seed_for(h: str) -> int:
    """Each image gets its own hand, fixed for good, so its plan never needs redoing."""
    return int(h[:8], 16)


class Library:
    """Watches the picture folder (and uploads), bakes each new image's 720-minute
    plan exactly once, and decides which picture each day gets.

      cache/library.json          what's known: images, when added, when drawn, which day got which
      cache/plans/<hash>-<fit>.pkl.gz   the baked plans (renames and moves never trigger a rebake)
    """

    def __init__(self, cache: Path, src: Path, fit: str, fixed_seed=None,
                 size=(W, H), gamma=2.4, mono=True):
        import threading
        self.cache, self.src, self.fit, self.fixed_seed = cache, src, fit, fixed_seed
        self.size, self.gamma, self.mono = tuple(size), gamma, mono
        self.plans = cache / "baked"
        self.plans.mkdir(parents=True, exist_ok=True)
        self.uploads = (src if src.is_dir() else cache / "uploads")
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.file = cache / "library.json"
        self.lock = threading.RLock()
        self._mem = {}
        self.baking = set()
        try:
            self.data = json.loads(self.file.read_text())
        except Exception:
            self.data = {}
        for k in ("images", "files", "days"):
            self.data.setdefault(k, {})

    # ---- bookkeeping ----
    def save(self):
        with self.lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.file)

    def sources(self):
        dirs = [self.src] if self.src.is_dir() else []
        if self.uploads not in dirs:
            dirs.append(self.uploads)
        files = [] if self.src.is_dir() else [self.src]
        for d in dirs:
            if d.is_dir():
                files += sorted(p for p in d.iterdir() if p.suffix.lower() in IMG_EXT and not p.name.startswith("."))
        return files

    def scan(self):
        """Find new or changed files. Unchanged files (same size and mtime) are not re-read."""
        added = []
        with self.lock:
            seen = set()
            for p in self.sources():
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                key, sig = str(p.resolve()), [st.st_size, int(st.st_mtime)]
                seen.add(key)
                rec = self.data["files"].get(key)
                if rec and rec["sig"] == sig:
                    h = rec["hash"]
                else:
                    if time.time() - st.st_mtime < 3:      # still being copied in; next scan
                        continue
                    h = image_hash(p)
                    self.data["files"][key] = {"sig": sig, "hash": h}
                img = self.data["images"].get(h)
                if img is None:
                    self.data["images"][h] = {"path": key, "name": p.stem, "added": dt.datetime.now().isoformat(timespec="seconds"),
                                              "drawn": []}
                    added.append(h)
                elif not Path(img["path"]).exists():
                    img["path"], img["name"] = key, p.stem       # moved or renamed: same plan
            for key in list(self.data["files"]):
                if key not in seen:
                    del self.data["files"][key]
            if added:
                self.save()
        return added

    def present(self, h):
        img = self.data["images"].get(h)
        return img is not None and Path(img["path"]).exists()

    def dir(self, h):
        w, hh = self.size
        return self.plans / f"{h}-{self.fit}-{w}x{hh}-{'mono' if self.mono else 'gray'}-g{self.gamma:g}"

    def ready(self, h):
        return (self.dir(h) / "meta.json").exists()

    def frame(self, h, k) -> Path:
        return self.dir(h) / "frames" / f"{k:03d}.png"

    def meta(self, h):
        if self._mem.get("meta", (None,))[0] != h:
            self._mem["meta"] = (h, json.loads((self.dir(h) / "meta.json").read_text()))
        return self._mem["meta"][1]

    def seed(self, h):
        return self.fixed_seed if self.fixed_seed is not None else seed_for(h)

    def unbaked(self):
        with self.lock:
            hs = [h for h in self.data["images"] if self.present(h) and not self.ready(h)]
        return sorted(hs, key=lambda h: self.data["images"][h]["added"])

    # ---- baking ----
    def bake(self, h, background=False):
        """Break the image down into its plan, once. In the background it runs as a
        separate low-priority process so the display's minute ticks never wait on it."""
        if self.ready(h):
            return True
        with self.lock:
            busy = h in self.baking
            if not busy:
                self.baking.add(h)
        if busy:                                   # the other thread is on it; wait for its result
            for _ in range(900):
                if self.ready(h) or h not in self.baking:
                    break
                time.sleep(1)
            return self.ready(h)
        try:
            return self._bake(h, background)
        finally:
            with self.lock:
                self.baking.discard(h)

    def _bake(self, h, background):
        img = Path(self.data["images"][h]["path"])
        out = self.dir(h)
        if background:
            import subprocess
            cmd = [sys.executable, str(Path(__file__).resolve()), "--image", str(img), "--fit", self.fit,
                   "--seed", str(self.seed(h)), "bake", "--to", str(out),
                   "--size", "x".join(map(str, self.size)), "--gamma", str(self.gamma)] + ([] if self.mono else ["--gray"])
            r = subprocess.run(cmd, preexec_fn=lambda: os.nice(15), capture_output=True, text=True)
            if r.returncode:
                print(f"could not break down {img.name}: {r.stderr.strip().splitlines()[-1:]}", flush=True)
            return self.ready(h)
        bake_image(img, self.seed(h), self.fit, out, self.size, self.gamma, self.mono)
        return True

    def ensure(self, h):
        """Results exist, or get created now (only ever once per image)."""
        if not self.ready(h) and not self.bake(h):
            raise RuntimeError(f"could not break down {self.data['images'][h]['name']}")

    # ---- the daily pick ----
    def pick(self, day: dt.date):
        """Which picture this day gets. Decided once per day and remembered, so reboots
        agree. New pictures go first, then whichever was drawn longest ago."""
        with self.lock:
            d = str(day)
            h = self.data["days"].get(d)
            if h and self.present(h):
                return h
            cands = [h for h in self.data["images"] if self.present(h)]
            if not cands:
                return None
            ready = [h for h in cands if self.ready(h)] or cands[:1]   # first boot: bake one now
            info = self.data["images"]
            h = min(ready, key=lambda h: (bool(info[h]["drawn"]), max(info[h]["drawn"] or [""]), info[h]["added"]))
            self.data["days"][d] = h
            if d not in info[h]["drawn"]:
                info[h]["drawn"] = (info[h]["drawn"] + [d])[-30:]
            for old in sorted(self.data["days"])[:-60]:
                del self.data["days"][old]
            self.save()
            return h

    def add_upload(self, data: bytes, name: str) -> str:
        stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(name).stem)[:60] or "upload"
        dest = self.uploads / f"{stem}{Path(name).suffix.lower() or '.jpg'}"
        n = 2
        while dest.exists():
            dest = self.uploads / f"{stem}-{n}{Path(name).suffix.lower() or '.jpg'}"
            n += 1
        tmp = self.uploads / f".{dest.name}.part"
        tmp.write_bytes(data)
        try:
            Image.open(tmp).verify()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise ValueError("That file is not an image the display can read.")
        h = hashlib.sha1(data).hexdigest()[:16]
        with self.lock:
            if h in self.data["images"] and self.present(h):     # already have it
                tmp.unlink()
                return h
            tmp.replace(dest)
            st = dest.stat()
            self.data["files"][str(dest.resolve())] = {"sig": [st.st_size, int(st.st_mtime)], "hash": h}
            self.data["images"][h] = {"path": str(dest.resolve()), "name": dest.stem,
                                      "added": dt.datetime.now().isoformat(timespec="seconds"), "drawn": []}
            self.save()
        return h

    def watch(self, every: int, on_change=None):
        """Background loop: notice new pictures and bake them, oldest first."""
        import threading

        def loop():
            while True:
                try:
                    new = self.scan()
                    for h in new:
                        print(f"new picture: {self.data['images'][h]['name']}", flush=True)
                    for h in self.unbaked():
                        name = self.data["images"][h]["name"]
                        t0 = time.time()
                        if self.bake(h, background=True):
                            print(f"broke down {name} in {time.time() - t0:.0f}s (kept for good)", flush=True)
                            if on_change:
                                on_change()
                except Exception as e:
                    print(f"watcher: {e}", flush=True)
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()


def bake_image(image: Path, seed: int, fit: str, out: Path, size=(W, H), gamma=2.4, mono=True):
    """Do all the work for one image up front and store it as static files:

      frames/000.png ... 720.png   the finished display frame for every minute
      viewer.json                  the strokes, for the web viewer
      meta.json                    name and the exposure sheet (written last = done)

    Displaying is then just opening frames/<minute>.png."""
    import shutil
    tmp = out.with_name(f".{out.name}.{os.getpid()}.part")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "frames").mkdir(parents=True)
    events = build_plan(image, seed, fit)
    (tmp / "viewer.json").write_text(viewer_plan(events))
    sh = Sheet(seed)
    for k in range(TOTAL + 1):
        if k:
            sh.draw_event(events[k - 1])
        g = sh.gray(tuple(size))
        (dither_1bit(g, gamma) if mono else g).save(tmp / "frames" / f"{k:03d}.png")
    meta = {"name": image.stem, "seed": seed, "size": list(size), "baked": dt.datetime.now().isoformat(timespec="seconds"),
            "events": [[e["phase"], e["label"], len(e["strokes"])] for e in events]}
    (tmp / "meta.json").write_text(json.dumps(meta))
    if out.exists():
        shutil.rmtree(out)
    tmp.replace(out)                       # atomic: a half-baked folder is never read


# ==========================================================================
# Graphite on paper
# ==========================================================================
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


# ==========================================================================
# Displays
# ==========================================================================
class FileDisplay:
    def __init__(self, path, size):
        self.path, self.size = Path(path), size

    def show(self, img, full=False):
        img.save(self.path)


class WaveshareDisplay:
    """Adapter over Waveshare's `waveshare_epd` python package."""

    def __init__(self, module):
        import importlib
        self.epd = importlib.import_module(f"waveshare_epd.{module}").EPD()
        self.size = (self.epd.width, self.epd.height)

    def show(self, img, full=False):
        epd = self.epd
        buf = epd.getbuffer(img.convert("1"))
        if full or not hasattr(epd, "display_Partial"):
            epd.init()
            epd.display(buf)
        else:
            (getattr(epd, "init_part", None) or epd.init)()
            try:
                epd.display_Partial(buf, 0, 0, epd.width, epd.height)
            except TypeError:
                epd.display_Partial(buf)
        epd.sleep()


# ==========================================================================
# The clock
# ==========================================================================
def pick_image(src: Path, day: dt.date) -> Path:
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXT)
        if not files:
            sys.exit(f"no images in {src}")
        return files[day.toordinal() % len(files)]
    return src


# ==========================================================================
# The exposure sheet: an image broken down into its 720 line additions
# ==========================================================================
def viewer_plan(events) -> str:
    """Compact JSON the viewer page reads: [phase, label, [[p, w, taper, delta-encoded pts x2], ...]]."""
    out = []
    for e in events:
        ss = []
        for s in e["strokes"]:
            p = np.round(np.asarray(s["pts"]) * 2).astype(int)
            flat = [int(p[0, 0]), int(p[0, 1])] + np.diff(p, axis=0).ravel().tolist()
            ss.append([round(s["p"], 2), round(s["w"], 2), 1 if s["taper"] else 0, flat])
        out.append([e["phase"][0], e["label"], ss])
    return json.dumps(out, separators=(",", ":"))


def viewer_html(events, live=None, plan_json=None) -> str:
    tpl = (Path(__file__).with_name("viewer.html")).read_text()
    tpl = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
           '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>' + tpl + '</body></html>')
    return tpl.replace("/*PLAN*/[]", plan_json if plan_json is not None else viewer_plan(events)).replace("/*LIVE*/null", json.dumps(live))


def write_xsheet(events, path: Path, start: dt.time):
    t0 = dt.datetime.combine(dt.date.today(), start)
    with open(path, "w") as f:
        f.write("minute,time,phase,lines_added,strokes\n")
        for i, e in enumerate(events):
            t = t0 + dt.timedelta(minutes=i + 1)
            f.write(f'{i + 1},{t:%H:%M},{e["phase"]},"{e["label"]}",{len(e["strokes"])}\n')


def breakdown(image, seed, fit_mode, outdir: Path, start: dt.time, frames_every: int):
    """Everything the viewer shows, as files: the exposure sheet, an offline viewer
    page, and optionally the sheet at each iteration with that minute's new lines in blue."""
    outdir.mkdir(parents=True, exist_ok=True)
    events = build_plan(image, seed, fit_mode)
    write_xsheet(events, outdir / "exposure_sheet.csv", start)
    (outdir / "day_in_lines.html").write_text(viewer_html(events))
    if frames_every:
        fd = outdir / "frames"
        fd.mkdir(exist_ok=True)
        sh = Sheet(seed)
        blue = np.array([0.18, 0.43, 0.84], np.float32)
        for i, e in enumerate(events):
            sh.draw_event(e)
            m = i + 1
            if m % frames_every and m != TOTAL:
                continue
            new = Sheet(seed, pegs=False)
            new.draw_event(e)
            ink = (1 - new.canvas)[..., None]
            img = np.asarray(sh.paper(), np.float32) / 255
            img = img * (1 - ink * .85) + blue * ink * .85
            Image.fromarray(np.clip(img * 255, 0, 255).astype(np.uint8)).resize((W, H), Image.LANCZOS) \
                .save(fd / f"{m:03d}.png")
    total = sum(len(e["strokes"]) for e in events)
    print(f"{image.name}: {total} strokes in {TOTAL} minutes -> {outdir}")
    return events


# ==========================================================================
# The clock (with uploads that can take over the schedule)
# ==========================================================================
def drawing_day(now, start):
    """(day, events that should be on the sheet). Before start time, show yesterday's finished sheet."""
    s = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if now < s:
        return (now - dt.timedelta(days=1)).date(), TOTAL
    return now.date(), min(TOTAL, int((now - s).total_seconds() // 60))


def next_start(after: dt.datetime, start: dt.time) -> dt.datetime:
    s = dt.datetime.combine(after.date(), start)
    return s if s >= after else s + dt.timedelta(days=1)


def schedule(now, start, cache: Path, lib: Library):
    """What should be on the sheet now: (key, image hash, k, start datetime)."""
    ov_f = cache / "override.json"
    if ov_f.exists():
        try:
            ov = json.loads(ov_f.read_text())
            os_, h = dt.datetime.fromisoformat(ov["start"]), ov["hash"]
            if now >= os_:
                if now < next_start(os_ + dt.timedelta(minutes=TOTAL), start) and lib.present(h):
                    k = min(TOTAL, int((now - os_).total_seconds() // 60))
                    return f"upload:{ov['start']}:{h}", h, k, os_
                ov_f.unlink()            # finished; back to the daily schedule
        except Exception:
            ov_f.unlink(missing_ok=True)
    day, k = drawing_day(now, start)
    h = lib.pick(day)
    if h is None:
        return None, None, 0, None
    return f"day:{day}:{h}", h, k, dt.datetime.combine(day, start)


class Live:
    """What the display is drawing, shared with the web page."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.key = None
        self.hash = None
        self.k = 0
        self.name = ""
        self.started = None
        self.html = None


def serve(live: Live, port: int, cache: Path, start: dt.time, lib: Library):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, unquote

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = urlparse(self.path).path
            with live.lock:
                if path in ("/", "/index.html"):
                    if live.key is None:
                        return self._send(503, "<p>The display is still breaking down its first picture. Reload in a minute.</p>",
                                          "text/html")
                    if live.html is None or live.html[0] != live.key:
                        info = dict(key=live.key, k=live.k, image=live.name,
                                    start=live.started.strftime("%H:%M"), startMin=live.started.hour * 60 + live.started.minute,
                                    next=start.strftime("%H:%M"))
                        plan = (lib.dir(live.hash) / "viewer.json").read_text()
                        live.html = (live.key, viewer_html(None, info, plan))
                    return self._send(200, live.html[1], "text/html; charset=utf-8")
                if path == "/state.json":
                    return self._send(200, json.dumps(dict(key=live.key, k=live.k)))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/upload":
                return self._send(404, '{"error":"not found"}')
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 40_000_000:
                return self._send(413, '{"error":"Images up to 40 MB, please."}')
            data = self.rfile.read(n)
            name = Path(unquote(self.headers.get("X-Filename", "upload.jpg"))).name or "upload.jpg"
            try:
                h = lib.add_upload(data, name)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            when = parse_qs(u.query).get("start", ["now"])[0]
            now = dt.datetime.now().replace(second=0, microsecond=0)
            begin = now if when == "now" else next_start(now + dt.timedelta(minutes=1), start)
            (cache / "override.json").write_text(json.dumps({"hash": h, "start": begin.isoformat()}))
            live.wake.set()
            self._send(200, json.dumps({"ok": True, "start": begin.strftime("%a %H:%M"), "now": when == "now"}))

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"viewer at http://<this-pi>:{port}/", flush=True)


def run(args):
    cache = Path(args.cache).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if args.display == "file":
        disp = FileDisplay(args.out, tuple(map(int, args.size.split("x"))))
    elif args.display.startswith("waveshare:"):
        disp = WaveshareDisplay(args.display.split(":", 1)[1])
    else:
        sys.exit(f"unknown display {args.display}")
    start = dt.datetime.strptime(args.start, "%H:%M").time()
    lib = Library(cache, Path(args.image).expanduser(), args.fit, args.seed, disp.size, args.gamma, args.mono)
    lib.scan()
    live = Live()
    lib.watch(args.scan_every)
    if args.serve:
        serve(live, args.serve, cache, start, lib)

    key, shown, since_full = None, -1, 0
    while True:
        now = dt.datetime.now()
        new_key, h, k, started = schedule(now, start, cache, lib)
        if new_key is None:
            print(f"no pictures yet in {lib.src}; waiting", flush=True)
            live.wake.wait(30); live.wake.clear()
            continue
        lib.ensure(h)                    # normally already baked by the watcher
        full = new_key != key
        key = new_key
        if k != shown or full:
            since_full += 1
            if args.full_refresh_every and since_full >= args.full_refresh_every:
                full, since_full = True, 0
            with Image.open(lib.frame(h, k)) as img:          # static: just open this minute's frame
                img.load()
                disp.show(img, full=full)
            shown = k
            meta = lib.meta(h)
            label = meta["events"][k - 1][1] if k else "blank sheet"
            print(f"{now:%H:%M}  {k:3d}/{TOTAL}  {meta['name']}  {label}", flush=True)
        with live.lock:
            live.key, live.hash, live.k, live.name, live.started = key, h, k, lib.meta(h)["name"], started
        live.wake.wait(max(1, 60 - dt.datetime.now().second + 0.5))
        live.wake.clear()


def preview(image, seed, fit_mode, outdir: Path, every: int):
    outdir.mkdir(parents=True, exist_ok=True)
    events = build_plan(image, seed, fit_mode)
    sh = Sheet(seed)
    hours = {60 * h: None for h in range(1, 13)}
    gif = []
    for k in range(TOTAL + 1):
        if k:
            sh.draw_event(events[k - 1])
        if k in hours:
            hours[k] = sh.paper((W, H))
        if k % every == 0:
            gif.append(sh.paper((W // 2, H // 2)).convert("P", palette=Image.ADAPTIVE, colors=64))
    sh.paper().save(outdir / "final.png")
    dither_1bit(sh.gray()).save(outdir / "final_eink.png")
    gif += [gif[-1]] * 40
    gif[0].save(outdir / "timelapse.gif", save_all=True, append_images=gif[1:], duration=50, loop=0)
    tw, th, cols = W // 2, H // 2, 4
    cs = Image.new("RGB", (cols * tw + (cols + 1) * 10, 3 * (th + 26) + 10), (40, 40, 42))
    dd = ImageDraw.Draw(cs)
    for i, k in enumerate(sorted(hours)):
        x, y = 10 + (i % cols) * (tw + 10), 10 + (i // cols) * (th + 26)
        cs.paste(hours[k].resize((tw, th), Image.LANCZOS), (x, y))
        dd.text((x, y + th + 6), f"hour {k // 60}  -  {events[k - 1]['phase']}", fill=(225, 225, 225))
    cs.save(outdir / "contact_sheet.png")
    print("wrote", ", ".join(p.name for p in sorted(outdir.iterdir())))


def main():
    ap = argparse.ArgumentParser(description="A Day in Lines: a drawing that takes all day.")
    ap.add_argument("--image", required=True, help="image file, or a folder (one picture per day)")
    ap.add_argument("--seed", type=int, default=None, help="fixed seed (default: the date)")
    ap.add_argument("--fit", choices=["crop", "pad"], default="crop", help="fill the sheet, or keep the whole image")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan", help="print the 720-minute schedule")
    sp.add_argument("--start", default="07:00")
    sb = sub.add_parser("breakdown", help="exposure sheet, viewer page, and per-minute frames for an image")
    sb.add_argument("-o", "--outdir", default="breakdown")
    sb.add_argument("--start", default="07:00")
    sb.add_argument("--frames", type=int, default=0, metavar="N",
                    help="also save the sheet every N minutes with that minute's new lines in blue (1 = all 720)")
    sk = sub.add_parser("bake", help="break down an image (or every image in a folder) ahead of time")
    sk.add_argument("--to", default=None, help=argparse.SUPPRESS)
    sk.add_argument("--size", default="800x480", help="display size the frames are made for")
    sk.add_argument("--gamma", type=float, default=2.4)
    sk.add_argument("--gray", action="store_true", help="greyscale frames instead of 1-bit")
    sk.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sf = sub.add_parser("frame", help="the sheet after k minutes")
    sf.add_argument("k", type=int); sf.add_argument("-o", "--out", default="frame.png")
    sf.add_argument("--mono", action="store_true"); sf.add_argument("--gamma", type=float, default=2.4)
    sv = sub.add_parser("preview", help="final frame, hourly contact sheet, timelapse")
    sv.add_argument("-o", "--outdir", default="preview")
    sv.add_argument("--every", type=int, default=6)
    sr = sub.add_parser("run", help="drive the display")
    sr.add_argument("--display", default="file", help="'file' or 'waveshare:<module>', e.g. waveshare:epd7in5_V2")
    sr.add_argument("--out", default="current.png")
    sr.add_argument("--size", default="800x480")
    sr.add_argument("--start", default="07:00", help="when the drawing begins each day")
    sr.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sr.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the viewer on this port: watch live, upload any image from your phone")
    sr.add_argument("--scan-every", type=int, default=60, metavar="SEC",
                    help="how often to look for new pictures in the folder")
    sr.add_argument("--mono", action=argparse.BooleanOptionalAction, default=True)
    sr.add_argument("--gamma", type=float, default=2.4, help="darken mid-tones before 1-bit dithering")
    sr.add_argument("--full-refresh-every", type=int, default=0,
                    help="full (blinking) refresh every N updates to clear ghosting; 0 = never")
    args = ap.parse_args()

    today = dt.date.today()
    seed = args.seed if args.seed is not None else today.toordinal()
    if args.cmd == "run":
        return run(args)
    if args.cmd == "bake":
        size = tuple(map(int, args.size.split("x")))
        if args.to:                                   # one image, called by the watcher
            bake_image(Path(args.image), seed, args.fit, Path(args.to), size, args.gamma, not args.gray)
            return
        lib = Library(Path(args.cache).expanduser(), Path(args.image).expanduser(), args.fit, args.seed,
                      size, args.gamma, not args.gray)
        lib.scan()
        todo = lib.unbaked()
        print(f"{len(lib.data['images'])} pictures, {len(todo)} to break down")
        for h in todo:
            t0 = time.time()
            lib.bake(h)
            print(f"  {lib.data['images'][h]['name']}: {time.time() - t0:.0f}s")
        return
    image = pick_image(Path(args.image).expanduser(), today)
    if args.cmd == "plan":
        t0 = dt.datetime.combine(today, dt.datetime.strptime(args.start, "%H:%M").time())
        for i, e in enumerate(build_plan(image, seed, args.fit)):
            print(f"{i + 1:3d}  {t0 + dt.timedelta(minutes=i + 1):%H:%M}  {e['phase']:<12} "
                  f"{e['label']:<24} {len(e['strokes']):3d} strokes")
    elif args.cmd == "breakdown":
        breakdown(image, seed, args.fit, Path(args.outdir),
                  dt.datetime.strptime(args.start, "%H:%M").time(), args.frames)
    elif args.cmd == "frame":
        events = build_plan(image, seed, args.fit)
        sh = Sheet(seed)
        for e in events[:args.k]:
            sh.draw_event(e)
        (dither_1bit(sh.gray(), args.gamma) if args.mono else sh.paper()).save(args.out)
        print(args.out)
    elif args.cmd == "preview":
        preview(image, seed, args.fit, Path(args.outdir), args.every)
    elif args.cmd == "run":
        run(args)


if __name__ == "__main__":
    main()
