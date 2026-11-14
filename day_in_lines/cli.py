"""Command line: run the display, bake, preview, break an image down."""
from __future__ import annotations

import argparse
import datetime as dt
import secrets
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .constants import H, MIN_FREE_MB, MODES, TOTAL, W
from .displays import FileDisplay, InkyDisplay, Rotated, WaveshareDisplay, caption
from .export import breakdown
from .library import Library, bake_image, image_exts
from .media import DEFAULT_GAMMA, PHASE_NAMES, medium_name, new_sheet, plan
from .planner import Look
from .prompts import describe, render as render_prompts
from .render import dither_1bit, frame_for
from .schedule import Overrides, parse_end, progress, schedule_full, snap, window
from .server import Live, serve


def mode_of(args) -> str:
    """What the stored frames are: --mode, or 1-bit unless --no-mono asked for greys."""
    return getattr(args, "mode", None) or ("mono" if getattr(args, "mono", True) else "gray")


def make_display(args):
    disp = _make_display(args)
    turn = getattr(args, "rotate", 0)
    if turn:
        disp = Rotated(disp, turn)
        if disp.size[0] < disp.size[1]:
            print("note: the sheet is drawn 5:3 landscape; on an upright panel it is stretched to fit", flush=True)
    return disp


def _make_display(args):
    if args.display == "file":
        return FileDisplay(args.out, tuple(map(int, args.size.split("x"))))
    try:
        if args.display.startswith("waveshare:"):
            return WaveshareDisplay(args.display.split(":", 1)[1], gamma_of(args), mode_of(args))
        if args.display.startswith("inky"):
            return InkyDisplay(args.display.partition(":")[2] or "auto", gamma_of(args))
    except ImportError as e:
        sys.exit(f"could not load the panel driver ({e}). For Waveshare, is PYTHONPATH set to the e-Paper "
                 f"python/lib folder? For Inky, `pip install inky`. See the README.")
    except ValueError as e:
        sys.exit(str(e))
    sys.exit(f"unknown display {args.display}")


def parse_quiet(text):
    """"22:00-07:00" as (from, to), or None."""
    if not text:
        return None
    try:
        a, b = (dt.datetime.strptime(t.strip(), "%H:%M").time() for t in text.split("-"))
    except ValueError:
        sys.exit("--quiet looks like 22:00-07:00")
    return a, b


def in_quiet(quiet, now: dt.datetime) -> bool:
    if not quiet:
        return False
    a, b, t = quiet[0], quiet[1], now.time()
    return a <= t < b if a < b else (t >= a or t < b)


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
        if mode_of(args) == "mono":
            return dither_1bit(im, gamma_of(args))

        class Card:                                              # the card as a sheet, for the grey and colour modes
            gray = staticmethod(lambda size: im)
        return frame_for(Card, (w, h), mode_of(args), gamma_of(args))

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
        key = f.read_text().strip()
    except FileNotFoundError:
        key = ""
    if not key:                          # missing, or emptied by a power cut: make one, never leave uploads open
        import os
        key = secrets.token_urlsafe(9)
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # private from the start
        with os.fdopen(fd, "w") as out:
            out.write(key + "\n")
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(f)
    return key


def look_of(args) -> Look:
    return Look(args.detail, args.contrast, getattr(args, "medium", "pencil"))


def gamma_of(args) -> float:
    """--gamma if given, else what suits the medium (paint needs less darkening than pencil)."""
    g = getattr(args, "gamma", None)
    return g if g is not None else DEFAULT_GAMMA[getattr(args, "medium", "pencil")]


def pick_image(src: Path, day: dt.date) -> Path:
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in image_exts())
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
    args.quiet_hours = parse_quiet(args.quiet)
    lib = Library(cache, Path(args.image).expanduser(), args.fit, args.seed, disp.size, gamma_of(args), look=look_of(args),
                  mode=mode_of(args), min_free_mb=args.min_free)
    lib.scan()
    live = Live()
    overrides = Overrides(cache, start, dur)
    lib.watch(args.scan_every, keep=args.keep, protect=lambda: overrides.hashes() | lib.recent_picks(),
              variants=overrides.variants)
    if args.feed:
        from .feeds import Feeds
        Feeds(lib, cache, args.feed, args.feed_max).start(args.feed_every)
        print(f"watching {len(args.feed)} feed(s) every {args.feed_every} min", flush=True)
    from .slack_app import Desk               # what the web page and the Slack channel can both do
    desk = Desk(lib, overrides, start, dur, live)
    if args.serve:
        key = upload_key_for(args, cache)
        serve(live, args.serve, overrides, start, dur, lib, key, args.bind, desk)
        if key:
            print(f"uploads need the key: open http://<this-pi>:{args.serve}/?key={key}", flush=True)
        else:
            print("uploads are open to anyone who can reach this port (--open-uploads)", flush=True)
    if args.slack_channel:
        import os
        from .slack_app import start_slack
        tokens = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN")
        if not all(tokens):
            sys.exit("--slack-channel needs SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...) in the environment")
        try:
            start_slack(desk, *tokens, args.slack_channel,
                        [u for u in (args.slack_users or "").replace(",", " ").split() if u], args.slack_timelapse)
        except ImportError:
            sys.exit("--slack-channel needs slack_bolt: pip install -r requirements-slack.txt")
    print(f"drawing each day {start:%H:%M}-{(dt.datetime.combine(dt.date.min, start) + dur):%H:%M}"
          f" ({dur.total_seconds() / 60 / TOTAL:.2g} min per drawing event)", flush=True)

    if args.update_every > 1:
        print(f"updating the display every {args.update_every} minutes", flush=True)

    st = {"key": None, "shown": -1, "since_full": 0, "held": False}
    while True:
        try:
            tick(args, disp, lib, live, overrides, start, dur, st)
            with live.lock:
                live.error = live.error_since = None
                live.last_ok = time.time()
        except Exception as e:           # a bad picture or a display hiccup must not stop the clock
            print(f"error: {e}; trying again shortly", flush=True)
            st["key"] = None             # redraw in full once it works again
            with live.lock:
                live.error, live.error_since = str(e) or type(e).__name__, live.error_since or time.time()
            live.wake.wait(30)
        live.wake.clear()


def caption_text(template, name, now: dt.datetime) -> str:
    """--caption with {name}, {date} (9 November 2026), {day} (Monday) and {time} filled in."""
    if not template:
        return ""
    try:
        return template.format(name=name, date=f"{now.day} {now:%B %Y}", day=f"{now:%A}", time=f"{now:%H:%M}")
    except (KeyError, IndexError, ValueError):
        return template


def tick(args, disp, lib, live, overrides, start, dur, st):
    """One minute of the clock: put the right frame up, then wait for the next minute.
    `st` carries what the panel shows: key, shown (event), since_full, held (quiet hours)."""
    now = dt.datetime.now()
    s = schedule_full(now, start, dur, overrides, lib)
    if s["key"] is None:
        print(f"no pictures yet in {lib.src}; waiting", flush=True)
        live.wake.wait(30)
        return
    h, k, style = s["hash"], s["k"], s["style"]
    every = max(1, getattr(args, "update_every", 1) or 1)
    if every > 1 and not s["paused"]:        # advance only on every Nth minute of the clock
        k = min(k, progress(snap(now, every), s["began"], s["fin"] - s["began"]))
    lib.ensure(h, style)             # normally already baked by the watcher
    name = lib.data["images"].get(h, {}).get("name") or lib.meta(h, style)["name"]
    quiet = in_quiet(getattr(args, "quiet_hours", None), now) and st["shown"] != -1 and st["key"] is not None
    with live.lock:                  # the page and the channel see the true state, whatever the panel holds
        live.key, live.hash, live.k, live.name, live.style = s["key"], h, k, name, style
        live.started, live.finish, live.paused, live.quiet = s["began"], s["fin"], s["paused"], quiet
    if quiet:
        st["held"] = True            # the panel keeps its last frame; catch up with a full refresh afterwards
    else:
        full = s["key"] != st["key"] or st["held"]
        due = k != st["shown"]
        if full or due:
            st["since_full"] += 1
            if full or (args.full_refresh_every and st["since_full"] >= args.full_refresh_every):
                full, st["since_full"] = True, 0
            with Image.open(lib.frame(h, k, style)) as img:          # static: just open this minute's frame
                img.load()
                disp.show(caption(img, caption_text(getattr(args, "caption", None), name, now)), full=full)
            st.update(key=s["key"], shown=k, held=False)
            meta = lib.meta(h, style)
            label = meta["events"][k - 1][1] if k else "blank sheet"
            print(f"{now:%H:%M}  {k:3d}/{TOTAL}  {name}  {label}", flush=True)
    live.wake.wait(max(1, 60 - dt.datetime.now().second + 0.5))


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
    sk.add_argument("--mode", choices=MODES, default=None, help="what the frames are: mono (1-bit), gray, gray4, colour")
    sk.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sf = sub.add_parser("frame", help="the sheet after k minutes")
    sf.add_argument("k", type=int); sf.add_argument("-o", "--out", default="frame.png")
    sf.add_argument("--mono", action="store_true"); sf.add_argument("--gamma", type=float, default=None)
    sv = sub.add_parser("preview", help="final frame, hourly contact sheet, timelapse")
    sv.add_argument("-o", "--outdir", default="preview")
    sv.add_argument("--every", type=int, default=6)
    sq = sub.add_parser("prompts", help="text-to-image prompts that scaffold a new picture in this picture's style")
    sq.add_argument("--subject", default="a new scene", help="what the new picture shows")
    sq.add_argument("-o", "--out", default=None, help="write the prompts to this file instead of printing them")
    sc = sub.add_parser("check", help="try the display with a test card before the first long run")
    sc.add_argument("--display", default="file", help="'file', 'waveshare:<module>' or 'inky:auto'")
    sc.add_argument("--out", default="check.png")
    sc.add_argument("--size", default="800x480")
    sc.add_argument("--gamma", type=float, default=None)
    sc.add_argument("--mode", choices=MODES, default=None)
    sc.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    sr = sub.add_parser("run", help="drive the display")
    sr.add_argument("--display", default="file",
                    help="'file', 'waveshare:<module>' (e.g. waveshare:epd7in5_V2) or 'inky:auto' (Pimoroni Inky)")
    sr.add_argument("--mode", choices=MODES, default=None,
                    help="what the panel can show: mono (1-bit, the default), gray4 (four greys) or colour (seven-colour "
                         "panels; best with --medium paint). Grey and colour panels refresh in full every time")
    sr.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                    help="turn the picture: 180 for a panel mounted upside down, 90 or 270 for one on its side")
    sr.add_argument("--quiet", default=None, metavar="HH:MM-HH:MM",
                    help="quiet hours, e.g. 22:00-07:00: the panel holds its frame (no refreshes), then catches up")
    sr.add_argument("--caption", default=None, metavar="TEXT",
                    help="a small label in the corner; {name}, {date}, {day} and {time} are filled in")
    sr.add_argument("--feed", action="append", default=[], metavar="URL",
                    help="an RSS/Atom feed, a list of image addresses, or an image address to fetch pictures from "
                         "(can be given more than once)")
    sr.add_argument("--feed-every", type=int, default=60, metavar="MIN", help="how often to look at the feeds")
    sr.add_argument("--feed-max", type=int, default=5, metavar="N", help="most new pictures to take from a feed each time")
    sr.add_argument("--min-free", type=int, default=MIN_FREE_MB, metavar="MB",
                    help="stop storing and processing pictures when the disk has less than this free (0 = no check)")
    sr.add_argument("--slack-timelapse", action=argparse.BooleanOptionalAction, default=True,
                    help="post a timelapse GIF in the Slack channel when a drawing completes")
    sr.add_argument("--out", default="current.png")
    sr.add_argument("--size", default="800x480")
    sr.add_argument("--start", default="07:00", help="when the drawing begins each day")
    sr.add_argument("--end", default=None, metavar="HH:MM",
                    help="when each day's drawing is finished (default: 12 hours after --start, one event a minute)")
    sr.add_argument("--cache", default="~/.cache/a-day-in-lines")
    sr.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the viewer on this port: watch live, upload any image from your phone")
    sr.add_argument("--slack-channel", default=None, metavar="ID",
                    help="take pictures from this Slack channel (its ID, like C0123ABCD; needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN)")
    sr.add_argument("--slack-users", default=None, metavar="IDS",
                    help="only these Slack user IDs (comma-separated) can change the display from the channel; everyone there can look")
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
    sr.add_argument("--update-every", type=int, default=1, metavar="MIN",
                    help="advance the display only every MIN minutes (default 1: a partial refresh every minute)")
    sr.add_argument("--full-refresh-every", type=int, default=20,
                    help="full (blinking) refresh every N updates to clear ghosting (default 20); 0 = never")
    args = ap.parse_args()
    if args.cmd != "check" and not args.image:
        ap.error("--image is required")
    if getattr(args, "update_every", 1) < 1:
        ap.error("--update-every must be at least 1")
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
            bake_image(Path(args.image), seed, args.fit, Path(args.to), size, gamma_of(args),
                       args.mode or ("gray" if args.gray else "mono"), look_of(args))
            return
        lib = Library(Path(args.cache).expanduser(), Path(args.image).expanduser(), args.fit, args.seed,
                      size, gamma_of(args), look=look_of(args), mode=args.mode or ("gray" if args.gray else "mono"))
        lib.scan()
        todo = lib.unbaked()
        print(f"{len(lib.data['images'])} pictures, {len(todo)} to break down")
        for h in todo:
            t0 = time.time()
            lib.bake(h)
            print(f"  {lib.data['images'][h]['name']}: {time.time() - t0:.0f}s")
        return
    image = pick_image(Path(args.image).expanduser(), today)
    if args.cmd == "prompts":
        text = render_prompts(look_of(args).medium, args.subject, describe(image), look_of(args))
        Path(args.out).write_text(text + "\n") if args.out else print(text)
        return
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
