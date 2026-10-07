"""Geometry helpers and the simulated pencil."""
from __future__ import annotations

import numpy as np

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
