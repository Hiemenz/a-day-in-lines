"""Reading an image and planning its 720 drawing events."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage as ndi

from .constants import H, PHASES, TOTAL, W
from .pen import Pen, length, resample


def load_target(path: Path, fit="crop"):
    """Return (tone, gray): tone 0 = bare paper, 1 = darkest graphite."""
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")              # transparent areas are paper, not black
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("L")
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
