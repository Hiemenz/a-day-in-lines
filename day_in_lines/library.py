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

from .constants import BAKE_VERSION, H, IMG_EXT, MAX_UPLOAD_PIXELS, TOTAL, UPLOAD_EXT, W
from .export import viewer_plan
from .planner import Look, build_plan
from .render import Sheet, dither_1bit


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
                 size=(W, H), gamma=2.4, mono=True, look: Look = Look()):
        import threading
        self.cache, self.src, self.fit, self.fixed_seed = cache, src, fit, fixed_seed
        self.size, self.gamma, self.mono, self.look = tuple(size), gamma, mono, look
        self.plans = cache / "baked"
        self.plans.mkdir(parents=True, exist_ok=True)
        self.uploads = (src if src.is_dir() else cache / "uploads")
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.file = cache / "library.json"
        self.lock = threading.RLock()
        self._mem = {}
        self.baking = set()
        self.failed = set()                        # couldn't be broken down this session; skipped
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

    def usable(self, h):
        return self.present(h) and h not in self.failed

    def dir(self, h):
        w, hh = self.size
        return self.plans / f"{h}-{self.fit}-{w}x{hh}-{'mono' if self.mono else 'gray'}-g{self.gamma:g}-s{self.seed(h)}{self.look.suffix}-{BAKE_VERSION}"

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
            hs = [h for h in self.data["images"] if self.usable(h) and not self.ready(h)]
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
            while not self.ready(h) and h in self.baking:
                time.sleep(1)
            return self.ready(h)
        ok = False
        try:
            ok = self._bake(h, background)
        except Exception as e:
            print(f"could not break down {self.data['images'][h]['name']}: {e}", flush=True)
        finally:
            with self.lock:
                self.baking.discard(h)
                if not ok:
                    self.failed.add(h)
        return ok

    def _bake(self, h, background):
        img = Path(self.data["images"][h]["path"])
        out = self.dir(h)
        if background:
            import subprocess
            cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "a_day_in_lines.py"), "--image", str(img), "--fit", self.fit,
                   "--seed", str(self.seed(h)),
                   "--detail", str(self.look.detail), "--contrast", str(self.look.contrast),
                   "bake", "--to", str(out),
                   "--size", "x".join(map(str, self.size)), "--gamma", str(self.gamma)] + ([] if self.mono else ["--gray"])
            r = subprocess.run(cmd, preexec_fn=lambda: os.nice(15), capture_output=True, text=True)
            if r.returncode:
                err = (r.stderr.strip().splitlines() or [f"exit code {r.returncode}"])[-1]
                print(f"could not break down {img.name}: {err}", flush=True)
            return self.ready(h)
        bake_image(img, self.seed(h), self.fit, out, self.size, self.gamma, self.mono, self.look)
        return True

    def recent_picks(self):
        with self.lock:
            return set(self.data["days"].values())

    # ---- housekeeping ----
    def prune(self, keep=10, protect=()):
        """Free disk by deleting stored frames that can never be shown again:

          - folders made with older settings or an older program version;
          - folders whose picture has gone from the picture folder, apart from the
            `keep` most recent (in case it comes back);
          - half-finished bakes abandoned for over a day.

        keep < 0 turns pruning off.

        Pictures still in the folder are never touched, so nothing is made again.
        Returns the names removed."""
        import shutil
        removed = []
        if keep < 0:
            return removed
        with self.lock:
            wanted = {self.dir(h).name: h for h in self.data["images"]}
            gone = [self.plans / n for n, h in wanted.items() if not self.present(h) and h not in set(protect)]
            busy = {self.dir(h).name for h in self.baking}
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
            if h and self.usable(h):
                return h
            cands = [h for h in self.data["images"] if self.usable(h)]
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
        import io
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
                buf = io.BytesIO()
                ImageOps.exif_transpose(im).save(buf, "PNG")
            data, ext = buf.getvalue(), ".png"
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
            self.failed.discard(h)
            self.save()
        return h

    def watch(self, every: int, on_change=None, keep=10, protect=lambda: ()):
        """Background loop: notice new pictures and bake them, oldest first."""
        import threading

        def loop():
            while True:
                try:
                    new = self.scan()
                    for h in new:
                        print(f"new picture: {self.data['images'][h]['name']}", flush=True)
                    for name in self.prune(keep, protect()):
                        print(f"removed stored frames {name}", flush=True)
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


def bake_image(image: Path, seed: int, fit: str, out: Path, size=(W, H), gamma=2.4, mono=True, look: Look = Look()):
    """Do all the work for one image up front and store it as static files:

      frames/000.png ... 720.png   the finished display frame for every minute
      viewer.json                  the strokes, for the web viewer
      meta.json                    name and the exposure sheet (written last = done)

    Displaying is then just opening frames/<minute>.png."""
    import fcntl
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.with_name(f".{out.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)      # another process (e.g. a manual `bake`) may be on it
        if not (out / "meta.json").exists():
            _bake_image(image, seed, fit, out, size, gamma, mono, look)


def _bake_image(image, seed, fit, out, size, gamma, mono, look):
    import shutil
    tmp = out.with_name(f".{out.name}.{os.getpid()}.part")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "frames").mkdir(parents=True)
    events = build_plan(image, seed, fit, look)
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
