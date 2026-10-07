"""Command line: run the display, bake, preview, break an image down."""
from __future__ import annotations

import argparse
import datetime as dt
import secrets
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .constants import H, IMG_EXT, TOTAL, W
from .displays import FileDisplay, WaveshareDisplay
from .export import breakdown
from .library import Library, bake_image
from .media import DEFAULT_GAMMA, PHASE_NAMES, medium_name, new_sheet, plan
from .planner import Look
from .render import dither_1bit
from .schedule import Overrides, parse_end, schedule, window
from .server import Live, serve


def make_display(args):
    if args.display == "file":
        return FileDisplay(args.out, tuple(map(int, args.size.split("x"))))
    if args.display.startswith("waveshare:"):
        try:
            return WaveshareDisplay(args.display.split(":", 1)[1], gamma_of(args))
        except ImportError as e:
            sys.exit(f"could not load the panel driver ({e}). Is PYTHONPATH set to the e-Paper "
                     f"python/lib folder? See the README.")
    sys.exit(f"unknown display {args.display}")


def check(args):
    """Try the display end to end: a full refresh of a test card, then a partial refresh
    that adds one mark, the way the clock updates it. Takes under a minute."""
    disp = make_display(args)
    w, h = disp.size

    def card(extra=False):
        im = Image.new("L", (w, h), 255)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, w - 1, h - 1], outline=0, width=3)
        d.line([0, 0, w, h], fill=0, width=1)
        d.line([0, h, w, 0], fill=0, width=1)
        for i in range(w - 40):                                  # a grey ramp, to see the dither
            d.line([20 + i, h - 70, 20 + i, h - 30], fill=int(255 * i / (w - 40)))
        d.text((24, 20), f"A Day in Lines: display check {w}x{h}", fill=0)
        d.text((24, 40), "partial refresh adds the circle" if not extra else "partial refresh worked", fill=0)
        if extra:
            d.ellipse([w // 2 - 40, h // 2 - 40, w // 2 + 40, h // 2 + 40], outline=0, width=4)
        return dither_1bit(im, gamma_of(args))

    t0 = time.time()
    disp.show(card(), full=True)
    print(f"full refresh ok ({time.time() - t0:.1f}s)", flush=True)
    time.sleep(3)
    t0 = time.time()
    disp.show(card(extra=True), full=False)
    print(f"partial refresh ok ({time.time() - t0:.1f}s)", flush=True)
    if hasattr(disp, "epd") and not hasattr(disp.epd, "display_Partial"):
        print("note: this panel module has no partial refresh, so every minute will be a full (blinking) refresh")
    if args.display == "file":
        print(f"wrote {args.out}")
    print("If the panel showed the test card, then the circle without a full blink, you're ready to `run`.")


def upload_key_for(args, cache: Path):
    """Uploads need a key unless --open-uploads. With no --upload-key, one is made
    once and kept in the cache, so the address you bookmarked keeps working."""
    if args.open_uploads:
        return None
    if args.upload_key:
        return args.upload_key
    f = cache / "upload_key"
    try:
        return f.read_text().strip() or None
    except FileNotFoundError:
        key = secrets.token_urlsafe(9)
        f.write_text(key + "\n")
        f.chmod(0o600)
        return key


def look_of(args) -> Look:
    return Look(args.detail, args.contrast, getattr(args, "medium", "pencil"))


def gamma_of(args) -> float:
    """--gamma if given, else what suits the medium (paint needs less darkening than pencil)."""
    g = getattr(args, "gamma", None)
    return g if g is not None else DEFAULT_GAMMA[getattr(args, "medium", "pencil")]


def pick_image(src: Path, day: dt.date) -> Path:
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXT)
        if not files:
            sys.exit(f"no images in {src}")
        return files[day.toordinal() % len(files)]
    return src


def run(args):
    cache = Path(args.cache).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    disp = make_display(args)
    start = dt.datetime.strptime(args.start, "%H:%M").time()
    dur = window(start, parse_end(args.end))
    lib = Library(cache, Path(args.image).expanduser(), args.fit, args.seed, disp.size, gamma_of(args), args.mono, look_of(args))
    lib.scan()
    live = Live()
    overrides = Overrides(cache, start, dur)
    lib.watch(args.scan_every, keep=args.keep, protect=lambda: overrides.hashes() | lib.recent_picks())
    if args.serve:
        key = upload_key_for(args, cache)
        serve(live, args.serve, overrides, start, dur, lib, key, args.bind)
        if key:
            print(f"uploads need the key: open http://<this-pi>:{args.serve}/?key={key}", flush=True)
        else:
            print("uploads are open to anyone who can reach this port (--open-uploads)", flush=True)
    print(f"drawing each day {start:%H:%M}-{(dt.datetime.combine(dt.date.min, start) + dur):%H:%M}"
          f" ({dur.total_seconds() / 60 / TOTAL:.2g} min per drawing event)", flush=True)

    key, shown, since_full = None, -1, 0
    while True:
        try:
            key, shown, since_full = tick(args, disp, lib, live, overrides, start, dur, key, shown, since_full)
        except Exception as e:           # a bad picture or a display hiccup must not stop the clock
            print(f"error: {e}; trying again shortly", flush=True)
            key = None                   # redraw in full once it works again
            live.wake.wait(30)
        live.wake.clear()


def tick(args, disp, lib, live, overrides, start, dur, key, shown, since_full):
    """One minute of the clock: put the right frame up, then wait for the next minute."""
    now = dt.datetime.now()
    new_key, h, k, started, finish = schedule(now, start, dur, overrides, lib)
    if new_key is None:
        print(f"no pictures yet in {lib.src}; waiting", flush=True)
        live.wake.wait(30)
        return key, shown, since_full
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
        live.key, live.hash, live.k, live.name = key, h, k, lib.meta(h)["name"]
        live.started, live.finish = started, finish
    live.wake.wait(max(1, 60 - dt.datetime.now().second + 0.5))
    return key, shown, since_full


def preview(image, seed, fit_mode, outdir: Path, every: int, look: Look = Look(), gamma=2.4):
    outdir.mkdir(parents=True, exist_ok=True)
    events = plan(image, seed, fit_mode, look)
    sh = new_sheet(look, seed)
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
    dither_1bit(sh.gray(), gamma).save(outdir / "final_eink.png")
    gif += [gif[-1]] * 40
    gif[0].save(outdir / "timelapse.gif", save_all=True, append_images=gif[1:], duration=50, loop=0)
    tw, th, cols = W // 2, H // 2, 4
    cs = Image.new("RGB", (cols * tw + (cols + 1) * 10, 3 * (th + 26) + 10), (40, 40, 42))
    dd = ImageDraw.Draw(cs)
    for i, k in enumerate(sorted(hours)):
        x, y = 10 + (i % cols) * (tw + 10), 10 + (i // cols) * (th + 26)
        cs.paste(hours[k].resize((tw, th), Image.LANCZOS), (x, y))
        dd.text((x, y + th + 6), f"hour {k // 60}  -  {PHASE_NAMES[look.medium][events[k - 1]['phase']]}", fill=(225, 225, 225))
    cs.save(outdir / "contact_sheet.png")
    print("wrote", ", ".join(p.name for p in sorted(outdir.iterdir())))


def main():
    ap = argparse.ArgumentParser(description="A Day in Lines: a drawing that takes all day.")
    ap.add_argument("--image", help="image file, or a folder (one picture per day); not needed for `check`")
    ap.add_argument("--seed", type=int, default=None, help="fixed seed (default: the date)")
    ap.add_argument("--fit", choices=["crop", "pad"], default="crop", help="fill the sheet, or keep the whole image")
    ap.add_argument("--medium", type=medium_name, default="pencil", metavar="{draw,paint}",
                    help="two paths: `draw` (or `pencil`, the default) invents pencil marks for any image; "
                         "`paint` takes a painting apart into its own strokes and replays it on a blank canvas")
    ap.add_argument("--detail", type=float, default=1.0, metavar="X",
                    help="more fine lines and tighter hatching above 1.0, fewer below (0.5-2)")
    ap.add_argument("--contrast", type=float, default=1.0, metavar="X",
                    help="darker shading for flat, low-contrast pictures above 1.0 (0.5-2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan", help="print the 720-minute schedule")
    sp.add_argument("--start", default="07:00")
    sp.add_argument("--end", default=None, help="when the drawing should be finished (default: 12 hours after --start)")
    sb = sub.add_parser("breakdown", help="exposure sheet, viewer page, and per-minute frames for an image")
    sb.add_argument("-o", "--outdir", default="breakdown")
    sb.add_argument("--start", default="07:00")
    sb.add_argument("--end", default=None, help="when the drawing should be finished (default: 12 hours after --start)")
    sb.add_argument("--frames", type=int, default=0, metavar="N",
                    help="also save the sheet every N minutes with that minute's new lines in blue (1 = all 720)")
    sk = sub.add_parser("bake", help="break down an image (or every image in a folder) ahead of time")
    sk.add_argument("--to", default=None, help=argparse.SUPPRESS)
    sk.add_argument("--size", default="800x480", help="display size the frames are made for")
    sk.add_argument("--gamma", type=float, default=None)
    sk.add_argument("--gray", action="store_true", help="greyscale frames instead of 1-bit")
    sk.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sf = sub.add_parser("frame", help="the sheet after k minutes")
    sf.add_argument("k", type=int); sf.add_argument("-o", "--out", default="frame.png")
    sf.add_argument("--mono", action="store_true"); sf.add_argument("--gamma", type=float, default=None)
    sv = sub.add_parser("preview", help="final frame, hourly contact sheet, timelapse")
    sv.add_argument("-o", "--outdir", default="preview")
    sv.add_argument("--every", type=int, default=6)
    sc = sub.add_parser("check", help="try the display with a test card before the first long run")
    sc.add_argument("--display", default="file", help="'file' or 'waveshare:<module>'")
    sc.add_argument("--out", default="check.png")
    sc.add_argument("--size", default="800x480")
    sc.add_argument("--gamma", type=float, default=None)
    sr = sub.add_parser("run", help="drive the display")
    sr.add_argument("--display", default="file", help="'file' or 'waveshare:<module>', e.g. waveshare:epd7in5_V2")
    sr.add_argument("--out", default="current.png")
    sr.add_argument("--size", default="800x480")
    sr.add_argument("--start", default="07:00", help="when the drawing begins each day")
    sr.add_argument("--end", default=None, metavar="HH:MM",
                    help="when each day's drawing is finished (default: 12 hours after --start, one event a minute)")
    sr.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sr.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the viewer on this port: watch live, upload any image from your phone")
    sr.add_argument("--scan-every", type=int, default=60, metavar="SEC",
                    help="how often to look for new pictures in the folder")
    sr.add_argument("--mono", action=argparse.BooleanOptionalAction, default=True)
    sr.add_argument("--gamma", type=float, default=None, help="darken mid-tones before 1-bit dithering (default 2.4 pencil, 1.6 paint)")
    sr.add_argument("--upload-key", default=None, metavar="KEY",
                    help="the key uploads need, from the viewer opened as /?key=KEY "
                         "(default: one is made and kept in the cache, and printed at start-up)")
    sr.add_argument("--open-uploads", action="store_true", help="let anyone who can reach the port upload, no key")
    sr.add_argument("--bind", default="0.0.0.0", metavar="ADDR",
                    help="address the viewer listens on (default all; 127.0.0.1 = this machine only)")
    sr.add_argument("--keep", type=int, default=10, metavar="N",
                    help="free stored frames that can't be shown again: those made with older settings, and those of "
                         "pictures deleted from the folder beyond the newest N (-1 = never delete anything)")
    sr.add_argument("--full-refresh-every", type=int, default=120,
                    help="full (blinking) refresh every N updates to clear ghosting (default 120 = every two hours); 0 = never")
    args = ap.parse_args()
    if args.cmd != "check" and not args.image:
        ap.error("--image is required")
    if not (0.5 <= args.detail <= 2 and 0.5 <= args.contrast <= 2):
        ap.error("--detail and --contrast must be between 0.5 and 2")

    today = dt.date.today()
    seed = args.seed if args.seed is not None else today.toordinal()
    if args.cmd == "run":
        return run(args)
    if args.cmd == "check":
        return check(args)
    if args.cmd == "bake":
        size = tuple(map(int, args.size.split("x")))
        if args.to:                                   # one image, called by the watcher
            bake_image(Path(args.image), seed, args.fit, Path(args.to), size, gamma_of(args), not args.gray, look_of(args))
            return
        lib = Library(Path(args.cache).expanduser(), Path(args.image).expanduser(), args.fit, args.seed,
                      size, gamma_of(args), not args.gray, look_of(args))
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
        st = dt.datetime.strptime(args.start, "%H:%M").time()
        t0, dur = dt.datetime.combine(today, st), window(st, parse_end(args.end))
        for i, e in enumerate(plan(image, seed, args.fit, look_of(args))):
            print(f"{i + 1:3d}  {t0 + dur * (i + 1) / TOTAL:%H:%M}  {e['phase']:<12} "
                  f"{e['label']:<24} {len(e['strokes']):3d} strokes")
    elif args.cmd == "breakdown":
        st = dt.datetime.strptime(args.start, "%H:%M").time()
        breakdown(image, seed, args.fit, Path(args.outdir), st, args.frames, window(st, parse_end(args.end)), look_of(args))
    elif args.cmd == "frame":
        events = plan(image, seed, args.fit, look_of(args))
        sh = new_sheet(look_of(args), seed)
        for e in events[:args.k]:
            sh.draw_event(e)
        (dither_1bit(sh.gray(), gamma_of(args)) if args.mono else sh.paper((W, H))).save(args.out)
        print(args.out)
    elif args.cmd == "preview":
        preview(image, seed, args.fit, Path(args.outdir), args.every, look_of(args), gamma_of(args))
    elif args.cmd == "run":
        run(args)


if __name__ == "__main__":
    main()
