"""The library: every image is broken down once, keyed by its contents."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

from .constants import BAKE_VERSION, H, IMG_EXT, MAX_UPLOAD_PIXELS, MIN_FREE_MB, MODES, TOTAL, UPLOAD_EXT, W
from .export import viewer_plan
from .media import DEFAULT_GAMMA, medium_name, new_sheet, plan
from .planner import Look
from .render import frame_for

try:                                   # optional: iPhone photos (pip install pillow-heif)
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC = True
except ImportError:
    HEIC = False

STYLE_KEYS = ("medium", "detail", "contrast", "fit")


def image_exts() -> set:
    """File types the picture folder is scanned for."""
    return IMG_EXT | ({".heic", ".heif"} if HEIC else set())


def clean_style(style) -> dict:
    """How one drawing is made, where it differs from the picture's usual: any of medium,
    detail, contrast and fit. Returns only the parts given, checked. Raises ValueError."""
    out = {}
    for k in STYLE_KEYS:
        v = (style or {}).get(k)
        if v in (None, ""):
            continue
        if k == "medium":
            try:
                v = medium_name(v)
            except ValueError:
                raise ValueError("The medium is pencil or paint.")
        elif k == "fit":
            if v not in ("crop", "pad"):
                raise ValueError("Fit is crop or pad.")
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError("Detail and contrast must be numbers between 0.5 and 2.")
            if not 0.5 <= v <= 2:
                raise ValueError("Detail and contrast must be between 0.5 and 2.")
        out[k] = v
    return out


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

      cache/library.json   what's known: images, when added, when drawn, which day got which
      cache/baked/<hash>-<fit>-<size>-<mono|gray>-g<gamma>-s<seed>/
                           the baked frames (renames and moves never trigger a rebake)
    """

    def __init__(self, cache: Path, src: Path, fit: str, fixed_seed=None,
                 size=(W, H), gamma=2.4, mono=True, look: Look = Look(), mode=None, min_free_mb=MIN_FREE_MB):
        import threading
        self.cache, self.src, self.fit, self.fixed_seed = cache, src, fit, fixed_seed
        self.mode = mode or ("mono" if mono else "gray")       # what the stored frames are (see MODES)
        if self.mode not in MODES:
            raise ValueError(f"unknown frame mode {self.mode}")
        self.size, self.gamma, self.mono, self.look = tuple(size), gamma, self.mode == "mono", look
        self.min_free_mb = min_free_mb
        self.plans = cache / "baked"
        self.plans.mkdir(parents=True, exist_ok=True)
        self.uploads = (src if src.is_dir() else cache / "uploads")
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.file = cache / "library.json"
        self.lock = threading.RLock()
        self._mem = {}
        self.baking = set()
        self.failed = set()                        # couldn't be broken down this session; skipped
        self.failures = {}                         # hash -> why, for telling whoever sent it
        self.failed_at = {}                        # hash -> when it failed, so it is tried again later
        self._disk_warned = False
        try:
            self.data = json.loads(self.file.read_text())
        except Exception:
            self.data = {}
        for k in ("images", "files", "days", "looks", "weights"):
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
                files += sorted(p for p in d.iterdir() if p.suffix.lower() in image_exts() and not p.name.startswith("."))
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
        """The picture's file is still there and still holds this picture (a file replaced in
        place by a different picture no longer counts as the old one)."""
        img = self.data["images"].get(h)
        if img is None or not Path(img["path"]).exists():
            return False
        rec = self.data["files"].get(str(Path(img["path"]).resolve()))
        return rec is None or rec["hash"] == h

    def usable(self, h):
        return self.present(h) and h not in self.failed

    def look_for(self, h, style=None) -> Look:
        """How this picture is drawn: the display's setting, unless the picture was given its
        own, unless this one drawing (`style`) was given its own."""
        o, s, d = self.data["looks"].get(h) or {}, style or {}, self.look
        return Look(*(s.get(k) or o.get(k) or getattr(d, k) for k in ("detail", "contrast", "medium")))

    def fit_for(self, h, style=None) -> str:
        return (style or {}).get("fit") or (self.data["looks"].get(h) or {}).get("fit") or self.fit

    def gamma_for(self, h, style=None) -> float:
        """A picture given the other medium gets that medium's usual gamma."""
        m = self.look_for(h, style).medium
        return self.gamma if m == self.look.medium else DEFAULT_GAMMA[m]

    def tune(self, h, medium=None, detail=None, contrast=None, fit=None):
        """Give a picture its own medium, detail, contrast or fit for good (None keeps the
        current one). Its frames are made again for the new look. Raises ValueError for bad values."""
        new = clean_style(dict(medium=medium, detail=detail, contrast=contrast, fit=fit))
        cur, cur_fit = self.look_for(h), self.fit_for(h)
        look, fit = self.look_for(h, new), self.fit_for(h, new)
        if (look, fit) == (cur, cur_fit):
            return
        with self.lock:
            if (look, fit) == (self.look, self.fit):
                self.data["looks"].pop(h, None)
            else:
                self.data["looks"][h] = {"detail": look.detail, "contrast": look.contrast, "medium": look.medium}
                if fit != self.fit:
                    self.data["looks"][h]["fit"] = fit
            self.failed.discard(h)
            self.save()

    def dir(self, h, style=None):
        w, hh = self.size
        return self.plans / (f"{h}-{self.fit_for(h, style)}-{w}x{hh}-{self.mode}-g{self.gamma_for(h, style):g}"
                             f"-s{self.seed(h)}{self.look_for(h, style).suffix}-{BAKE_VERSION}")

    def ready(self, h, style=None):
        return (self.dir(h, style) / "meta.json").exists()

    def frame(self, h, k, style=None) -> Path:
        return self.dir(h, style) / "frames" / f"{k:03d}.png"

    def meta(self, h, style=None):
        d = self.dir(h, style)
        if self._mem.get("meta", (None,))[0] != d:
            self._mem["meta"] = (d, json.loads((d / "meta.json").read_text()))
        return self._mem["meta"][1]

    # ---- how often a picture comes round ----
    def weight(self, h) -> str:
        """"normal", "favourite" (comes round twice as often) or "never" (only when sent)."""
        return self.data["weights"].get(h, "normal")

    def set_weight(self, h, weight):
        if weight not in ("normal", "favourite", "never"):
            raise ValueError("A picture is normal, favourite or never.")
        with self.lock:
            if weight == "normal":
                self.data["weights"].pop(h, None)
            else:
                self.data["weights"][h] = weight
            self.save()

    def _turn(self, h, day: dt.date):
        """Sort key for whose turn it is: never-drawn pictures first (oldest added first), then
        the longest since it was last drawn, a favourite's wait counting double."""
        i = self.data["images"][h]
        if not i["drawn"]:
            return (0, 0, i["added"])
        try:
            gap = (day - dt.date.fromisoformat(max(i["drawn"]))).days
        except ValueError:
            gap = 0
        return (1, -gap * (2 if self.weight(h) == "favourite" else 1), i["added"])

    RETRY_AFTER = 1800                             # seconds before a picture that failed is tried again

    def retry_failed(self, now=None):
        """Let pictures that failed a while ago be tried again (a full disk or a bad moment is
        not the picture's fault). The reason stays in `failures`, so it is only reported once."""
        now = now or time.time()
        with self.lock:
            for h in [h for h, t in self.failed_at.items() if now - t >= self.RETRY_AFTER]:
                del self.failed_at[h]
                self.failed.discard(h)

    def low_disk(self) -> bool:
        import shutil
        try:
            return self.min_free_mb > 0 and shutil.disk_usage(self.plans).free < self.min_free_mb * 1_000_000
        except OSError:
            return False

    def seed(self, h):
        return self.fixed_seed if self.fixed_seed is not None else seed_for(h)

    def unbaked(self):
        with self.lock:
            hs = [h for h in self.data["images"] if self.usable(h) and not self.ready(h)]
        return sorted(hs, key=lambda h: self.data["images"][h]["added"])

    # ---- baking ----
    def bake(self, h, background=False, style=None):
        """Break the image down into its plan, once. In the background it runs as a
        separate low-priority process so the display's minute ticks never wait on it."""
        if self.ready(h, style):
            return True
        name = self.dir(h, style).name
        if self.low_disk():                        # not a fault of the picture: try again once there is room
            self.failures[h] = f"the disk is nearly full (under {self.min_free_mb} MB free)"
            if not self._disk_warned:
                print(f"not breaking down pictures: {self.failures[h]}", flush=True)
                self._disk_warned = True
            return False
        self._disk_warned = False
        with self.lock:
            busy = name in self.baking
            if not busy:
                self.baking.add(name)
        if busy:                                   # the other thread is on it; wait for its result
            while not self.ready(h, style) and name in self.baking:
                time.sleep(1)
            return self.ready(h, style)
        ok, why = False, "it could not be read"
        try:
            ok, why = self._bake(h, background, style)
        except Exception as e:
            why = str(e) or type(e).__name__
            print(f"could not break down {self.data['images'][h]['name']}: {e}", flush=True)
        finally:
            with self.lock:
                self.baking.discard(name)
                if not ok:
                    self.failed.add(h)
                    self.failed_at[h] = time.time()
                    self.failures[h] = why
                else:
                    self.failures.pop(h, None)
        return ok

    def _bake(self, h, background, style=None):
        img = Path(self.data["images"][h]["path"])
        out, look, fit = self.dir(h, style), self.look_for(h, style), self.fit_for(h, style)
        if background:
            import subprocess
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH")]))
            cmd = [sys.executable, "-m", "day_in_lines", "--image", str(img), "--fit", fit,
                   "--seed", str(self.seed(h)),
                   "--detail", str(look.detail), "--contrast", str(look.contrast), "--medium", look.medium,
                   "bake", "--to", str(out), "--mode", self.mode,
                   "--size", "x".join(map(str, self.size)), "--gamma", str(self.gamma_for(h, style))]
            r = subprocess.run(cmd, preexec_fn=lambda: os.nice(15), capture_output=True, text=True, env=env)
            err = ""
            if r.returncode:
                err = (r.stderr.strip().splitlines() or [f"exit code {r.returncode}"])[-1]
                print(f"could not break down {img.name}: {err}", flush=True)
            return self.ready(h, style), err or "it could not be read"
        bake_image(img, self.seed(h), fit, out, self.size, self.gamma_for(h, style), self.mode, look)
        return True, ""

    def recent_picks(self):
        with self.lock:
            return set(self.data["days"].values())

    # ---- housekeeping ----
    def prune(self, keep=10, protect=(), variants=()):
        """Free disk by deleting stored frames that can never be shown again:

          - folders made with older settings or an older program version;
          - folders whose picture has gone from the picture folder, apart from the
            `keep` most recent (in case it comes back);
          - half-finished bakes abandoned for over a day.

        keep < 0 turns pruning off. `variants` are (hash, style) pairs still wanted: drawings
        queued with their own look.

        Pictures still in the folder are never touched, so nothing is made again.
        Returns the names removed."""
        import shutil
        removed = []
        if keep < 0:
            return removed
        with self.lock:
            wanted = {self.dir(h).name: h for h in self.data["images"]}
            gone = [self.plans / n for n, h in wanted.items() if not self.present(h) and h not in set(protect)]
            wanted.update({self.dir(h, st).name: h for h, st in variants if h in self.data["images"]})
            busy = set(self.baking)
        try:
            entries = list(self.plans.iterdir())
        except FileNotFoundError:
            return removed
        stale = []
        for d in entries:
            if d.name.startswith("."):
                if d.name.endswith(".part") and time.time() - d.stat().st_mtime > 86400:
                    stale.append(d)
            elif d.is_dir() and d.name not in wanted and d.name not in busy:
                stale.append(d)
        gone = sorted((d for d in gone if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        for d in stale + gone[keep:]:
            shutil.rmtree(d, ignore_errors=True)
            d.with_name(f".{d.name}.lock").unlink(missing_ok=True)
            removed.append(d.name)
        return removed

    def ensure(self, h, style=None):
        """Results exist, or get created now (only ever once per image and look)."""
        if not self.ready(h, style) and not self.bake(h, style=style):
            raise RuntimeError(f"could not break down {self.data['images'][h]['name']}: {self.failures.get(h, 'unknown reason')}")

    # ---- the daily pick ----
    def pick(self, day: dt.date):
        """Which picture this day gets. Decided once per day and remembered, so reboots
        agree. New pictures go first, then whichever was drawn longest ago."""
        with self.lock:
            d = str(day)
            h = self.data["days"].get(d)
            if h and self.usable(h):
                return h
            cands = [h for h in self.data["images"] if self.usable(h) and self.weight(h) != "never"]
            if not cands:
                return None
            ready = [h for h in cands if self.ready(h)] or cands[:1]   # first boot: bake one now
            info = self.data["images"]
            h = min(ready, key=lambda h: self._turn(h, day))
            self.data["days"][d] = h
            if d not in info[h]["drawn"]:
                info[h]["drawn"] = (info[h]["drawn"] + [d])[-30:]
            for old in sorted(self.data["days"])[:-60]:
                del self.data["days"][old]
            self.save()
            return h

    def add_upload(self, data: bytes, name: str, by=None) -> str:
        import io
        if self.low_disk():
            raise ValueError("The display's disk is nearly full. Delete some pictures first.")
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format
                if im.width * im.height > MAX_UPLOAD_PIXELS:
                    raise ValueError(f"That picture is too large (over {MAX_UPLOAD_PIXELS // 1_000_000} megapixels).")
                im.verify()
        except ValueError:
            raise
        except Exception:                          # includes Pillow's decompression-bomb error
            raise ValueError("That file is not an image the display can read.")
        ext = UPLOAD_EXT.get(fmt)
        if ext is None:                            # e.g. GIF: store as PNG so the folder scan picks it up
            with Image.open(io.BytesIO(data)) as im:
                buf, im = io.BytesIO(), ImageOps.exif_transpose(im)
                if fmt in ("HEIF", "AVIF") and im.mode in ("RGB", "L"):      # photos: a PNG would be enormous
                    im.save(buf, "JPEG", quality=92)
                    ext = ".jpg"
                else:
                    im.save(buf, "PNG")
                    ext = ".png"
            data = buf.getvalue()
        stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(name).stem)[:60] or "upload"
        h = hashlib.sha1(data).hexdigest()[:16]
        with self.lock:
            if h in self.data["images"] and self.present(h):     # already have it
                return h
            dest, n = self.uploads / f"{stem}{ext}", 2
            while dest.exists():
                dest, n = self.uploads / f"{stem}-{n}{ext}", n + 1
            tmp = self.uploads / f".{dest.name}.part"
            tmp.write_bytes(data)
            tmp.replace(dest)
            st = dest.stat()
            key = str(dest.resolve())
            self.data["files"][key] = {"sig": [st.st_size, int(st.st_mtime)], "hash": h}
            img = self.data["images"].get(h)
            if img:                                # known picture whose file had gone: keep its history
                img["path"], img["name"] = key, dest.stem
            else:
                self.data["images"][h] = {"path": key, "name": dest.stem,
                                          "added": dt.datetime.now().isoformat(timespec="seconds"), "drawn": []}
                if by:
                    self.data["images"][h]["by"] = by
            self.failed.discard(h)
            self.save()
        return h

    def reroll(self, day: dt.date):
        """Give `day` a different picture than the one it has (the least recently drawn of the
        others). Returns the new hash, or None when there is no other to choose."""
        with self.lock:
            d, info = str(day), self.data["images"]
            cur = self.data["days"].get(d)
            cands = [h for h in info if self.usable(h) and h != cur and self.ready(h) and self.weight(h) != "never"]
            if not cands:
                return None
            h = min(cands, key=lambda h: self._turn(h, day))
            self.data["days"][d] = h
            if d not in info[h]["drawn"]:
                info[h]["drawn"] = (info[h]["drawn"] + [d])[-30:]
            self.save()
            return h

    def delete(self, h):
        """Remove a picture: its file, its history and its stored frames. Returns its name.
        Raises KeyError if unknown."""
        import shutil
        with self.lock:
            img = self.data["images"].pop(h)
            # Every copy of it in the folder, or the next scan would find one and add it back as new.
            # Files whose recorded hash is another picture's (replaced in place) are not this one's.
            for k in [k for k, v in self.data["files"].items() if v["hash"] == h]:
                Path(k).unlink(missing_ok=True)
                del self.data["files"][k]
            if img["path"] not in self.data["files"]:          # not scanned yet; and never one now holding another picture
                Path(img["path"]).unlink(missing_ok=True)
            for d in [d for d, v in self.data["days"].items() if v == h]:
                del self.data["days"][d]
            stored = list(self.plans.glob(f"{h}-*"))      # one per look it was ever made with
            self.data["looks"].pop(h, None)
            self.data["weights"].pop(h, None)
            self.failures.pop(h, None)
            self.failed.discard(h)
            self.save()
        for d in stored:
            shutil.rmtree(d, ignore_errors=True)
        return img["name"]

    def watch(self, every: int, on_change=None, keep=10, protect=lambda: (), variants=lambda: ()):
        """Background loop: notice new pictures and bake them, oldest first."""
        import threading

        def loop():
            while True:
                try:
                    self.retry_failed()
                    new = self.scan()
                    for h in new:
                        print(f"new picture: {self.data['images'][h]['name']}", flush=True)
                    queued = [(h, st) for h, st in variants() if self.usable(h)]
                    for name in self.prune(keep, protect(), queued):
                        print(f"removed stored frames {name}", flush=True)
                    todo = [(h, None) for h in self.unbaked()] + [(h, st) for h, st in queued if not self.ready(h, st)]
                    for h, st in todo:
                        name = self.data["images"][h]["name"]
                        t0 = time.time()
                        if self.bake(h, background=True, style=st):
                            print(f"broke down {name} in {time.time() - t0:.0f}s (kept for good)", flush=True)
                            if on_change:
                                on_change()
                except Exception as e:
                    print(f"watcher: {e}", flush=True)
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()


def bake_image(image: Path, seed: int, fit: str, out: Path, size=(W, H), gamma=2.4, mono=True, look: Look = Look()):
    """Do all the work for one image up front and store it as static files:

      frames/000.png ... 720.png   the finished display frame for every minute
      viewer.json                  the strokes, for the web viewer
      meta.json                    name and the exposure sheet (written last = done)

    Displaying is then just opening frames/<minute>.png. `mono` is True or False (1-bit or
    grey frames), or the name of a frame mode."""
    return _locked_bake(image, seed, fit, out, size, gamma, mono if isinstance(mono, str) else ("mono" if mono else "gray"), look)


def _locked_bake(image, seed, fit, out, size, gamma, mode, look):
    import fcntl
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.with_name(f".{out.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)      # another process (e.g. a manual `bake`) may be on it
        if not (out / "meta.json").exists():
            _bake_image(image, seed, fit, out, size, gamma, mode, look)


def _bake_image(image, seed, fit, out, size, gamma, mode, look):
    import shutil
    tmp = out.with_name(f".{out.name}.{os.getpid()}.part")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "frames").mkdir(parents=True)
    events = plan(image, seed, fit, look)
    (tmp / "viewer.json").write_text(viewer_plan(events))
    sh = new_sheet(look, seed)
    for k in range(TOTAL + 1):
        if k:
            sh.draw_event(events[k - 1])
        frame_for(sh, size, mode, gamma).save(tmp / "frames" / f"{k:03d}.png")
    meta = {"name": image.stem, "seed": seed, "medium": look.medium, "fit": fit, "mode": mode, "size": list(size), "baked": dt.datetime.now().isoformat(timespec="seconds"),
            "events": [[e["phase"], e["label"], len(e["strokes"])] for e in events]}
    (tmp / "meta.json").write_text(json.dumps(meta))
    if out.exists():
        shutil.rmtree(out)
    tmp.replace(out)                       # atomic: a half-baked folder is never read
