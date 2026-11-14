"""A Slack channel for the display: drop a picture in, choose where in the sequence it starts.

The Pi connects out to Slack (Socket Mode), so it needs no public address. It listens to one
channel only. A picture posted there with options in its message is queued straight away; a
picture posted bare gets buttons, and "Choose..." opens a form. `browse` lists the pictures the
display already has, `queue` shows and cancels what is waiting, and the bot posts progress as
the drawing moves through its phases."""
from __future__ import annotations

import datetime as dt
import difflib
import io
import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from math import ceil

from .constants import H, MAX_UPLOAD_BYTES, TOTAL, W
from . import library
from .export import timelapse_gif
from .library import Library
from .media import PHASE_NAMES, PHASE_STARTS, medium_name, phase_at
from .schedule import DAYS, Overrides, drawing_day
from .server import Live
from .submit import MONTHS, BadRequest, describe_repeat, submit

HELP = (
    "Post a picture here and I'll put it on the display. Add words to the message to choose how:\n"
    "• `from 300` starts at event 300 of 720; `from shading` starts at that phase "
    "(composition, linework, shading, finishing; or blocking, underpainting, building, details for paint)\n"
    "• `now` (the default), `next` (the next daily start) or `at 14:30` for when it begins\n"
    "• `finish by 22:00` for when it should be complete\n"
    "• `every friday at 18:00`, `every weekday` or `daily` to repeat it; `every 25 december` or "
    "`monthly on the 1st` for a date\n"
    "• `as paint` or `as pencil`, `detail 1.5`, `contrast 1.2`, `fit pad` (the whole image) or `fit crop` to change "
    "how this one drawing is made; add `always` to keep that look for the picture\n"
    "Post a picture with no words and I'll offer buttons and a form.\n"
    "Commands: `browse` (pictures the display has, to send again, preview or delete), `queue` (what is waiting; "
    "cancel or stop repeats), `skip`, `restart`, `pause`, `resume`, `status`."
)
PAGE = 5                                  # pictures per page when browsing
TIME = r"(\d{1,2}:\d{2})"
# Each medium's phase names, plus the four generic ones, all mean the same four starting points.
PHASE_WORDS = {"composition": 0, "blocking": 0, "linework": 1, "underpainting": 1, "shading": 2, "building": 2,
               "finishing": 3, "details": 3}


@dataclass
class Options:
    when: str = "now"
    until: str = ""
    first: int = 0
    given: bool = False                    # did the message say anything at all
    medium: str | None = None              # these three change how this picture is made
    detail: float | None = None
    contrast: float | None = None
    every: list | dict | None = None       # weekdays to repeat on (0 = Monday), or a date: {"day": 1}, {"month": 12, "day": 25}
    fit: str | None = None                 # "crop" or "pad"
    always: bool = False                   # keep this look for the picture, not just this drawing

    def style(self):
        return {k: v for k in ("medium", "detail", "contrast", "fit") if (v := getattr(self, k))}

    def tune(self):
        return self.style()


class Reply(str):
    """The words to post, plus which picture, starting spot and look they are about."""
    hash = None
    first = 0
    style = None


def parse_date(t: str):
    """A date that comes round, from the words after it was asked for: (match, {"day"} or
    {"month", "day"}), or None."""
    nth = r"(\d{1,2})(?:st|nd|rd|th)?"

    def month(word):
        hits = [i + 1 for i, m in enumerate(MONTHS) if len(word) >= 3 and m.lower().startswith(word)]
        return hits[0] if hits else None

    for pattern, order in ((rf"\b(?:every|each)\s+(?:year\s+on\s+)?(?:the\s+)?{nth}\s+(?:of\s+)?([a-z]{{3,9}})\b", "dm"),
                           (rf"\b(?:every|each)\s+(?:year\s+on\s+)?([a-z]{{3,9}})\s+{nth}\b(?!:)", "md")):
        if m := re.search(pattern, t):
            d, mo = (m.group(1), m.group(2)) if order == "dm" else (m.group(2), m.group(1))
            if month(mo):
                return m, {"month": month(mo), "day": int(d)}
    if m := re.search(rf"\b(?:(?:every|each)\s+month|monthly)\s+on\s+(?:the\s+)?{nth}\b", t) \
            or re.search(r"\b(?:every|each)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", t):
        return m, {"day": int(m.group(1))}
    return None


def parse_days(text: str) -> list:
    days = set()
    for w in re.split(r"\s*,\s*|\s+and\s+|\s+", text.strip()):
        if len(w) > 3 and w.endswith("s") and w not in ("weekdays", "weekends"):
            w = w[:-1]                                   # "fridays"
        if w in ("day", "days", "daily", "night"):
            days |= set(range(7))
        elif w in ("weekday", "weekdays"):
            days |= set(range(5))
        elif w in ("weekend", "weekends"):
            days |= {5, 6}
        elif len(w) >= 3 and (m := [i for i, d in enumerate(DAYS) if d.lower().startswith(w[:9])]):
            days.add(m[0])
        elif w:
            raise BadRequest(f"I don't know the day '{w}'.")
    return sorted(days)


def parse_options(text: str, medium="pencil") -> Options:
    """Read the options out of a message. Raises BadRequest for ones it can't make sense of."""
    t = re.sub(r"<[^>]*>", " ", text or "").lower()          # user mentions, links
    o = Options()

    def take(pattern):
        nonlocal t
        m = re.search(pattern, t)
        if m:
            o.given = True
            t = t[:m.start()] + " " + t[m.end():]
        return m

    if m := take(r"\b(?:as|in|medium)\s+(paint(?:ing)?|pencil|draw(?:ing)?)\b"):
        o.medium = medium = medium_name(m.group(1))
    for key in ("detail", "contrast"):
        if m := take(rf"\b{key}\s+(\d+(?:\.\d+)?)\b"):
            setattr(o, key, float(m.group(1)))
            if not 0.5 <= getattr(o, key) <= 2:
                raise BadRequest(f"{key.title()} must be between 0.5 and 2.")
    if m := take(r"\bfit\s+(crop|pad)\b"):
        o.fit = m.group(1)
    if o.style() and take(r"\balways\b"):
        o.always = True
    if m := take(r"\b(?:finish(?:ed)?(?:\s+by)?|until|by|end(?:ing)?(?:\s+at)?)\s+" + TIME):
        o.until = m.group(1)
    if found := parse_date(t):
        m, o.every = found
        o.given, t = True, t[:m.start()] + " " + t[m.end():]
        try:
            dt.date(2024, o.every.get("month", 1), o.every["day"])
        except ValueError:
            raise BadRequest("That isn't a date that comes round.")
    elif m := take(r"\b(?:every|each)\s+([a-z]+(?:(?:\s*,\s*|\s+and\s+)[a-z]+)*)"):
        o.every = parse_days(m.group(1))
    elif take(r"\bdaily\b"):
        o.every = list(range(7))
    spot = r"\b(?:from|spot|event|minute|start(?:ing)?\s+(?:from|at\s+event))\s+(?:event\s+)?(\d+|[a-z]+)\b"
    for m in re.finditer(spot, t):                        # the first that is a real spot; others are just words
        w = m.group(1)
        if w.isdigit() or w in PHASE_WORDS:
            o.first = int(w) if w.isdigit() else PHASE_STARTS[medium][PHASE_WORDS[w]]
        elif difflib.get_close_matches(w, PHASE_WORDS, n=1, cutoff=0.7):     # "from shadng": a typo, so say so
            raise BadRequest(f"I don't know the spot '{w}'. Use a number from 0 to {TOTAL - 1} or a phase name.")
        else:                                                              # "from grandma", "from the beach": a caption
            continue
        o.given, t = True, t[:m.start()] + " " + t[m.end():]
        break
    if m := re.search(r"\b(?:at|start(?:ing)?(?:\s+at)?)\s+" + TIME, t):
        o.when, o.given = m.group(1), True
    elif re.search(r"\b(?:next(?!\s+(?:to|door|week|month|year|time|one|of)\b)|tomorrow|queue)\b", t):
        o.when, o.given = "next", True
    elif re.search(r"\bnow\b", t):
        o.given = True
    return o


class Desk:
    """What the channel can do, with no Slack in it, so it can be tested."""

    def __init__(self, lib: Library, overrides: Overrides, start: dt.time, dur: dt.timedelta, live: Live):
        self.lib, self.overrides, self.start, self.dur, self.live = lib, overrides, start, dur, live

    @property
    def medium(self):
        return self.lib.look.medium

    def medium_of(self, h, style=None):
        return self.lib.look_for(h, style).medium

    def now(self):
        return dt.datetime.now().replace(second=0, microsecond=0)

    def name_of(self, h):
        return self.lib.data["images"].get(h, {}).get("name", "that picture")

    # ---- sending ----
    def add(self, data: bytes | None, name: str, o: Options, now=None, label=None, by=None) -> Reply:
        """Queue a picture (or, with data None, the library picture whose hash is `name`);
        returns the reply. Raises BadRequest."""
        r = submit(self.lib, self.overrides, self.start, self.dur, data, name, o.when, o.until, o.first, now, by,
                   o.style() if o.always else None, o.every, None if o.always else o.style())
        self.live.wake.set()
        label = f"*{r['name']}*"
        spot = f" from event {r['first']} of {TOTAL} ({phase_at(r['medium'], r['first'])})" if r["first"] else ""
        look = (f" as {r['medium']}" if o.medium else "") + (" (and from now on)" if o.always and o.style() else "")
        if r["repeat"]:
            text = (f"Repeating {label}{look}{spot} {describe_repeat(r)} at {r['at']}"
                    f"{' (finishing by ' + o.until + ')' if o.until else ''}. Next: {r['begin']:%a %-d %b %H:%M}.")
        elif r["now"]:
            text = f"Started {label}{look} now{spot}. It will be complete at {r['end']:%a %H:%M}."
        else:
            text = f"Queued {label}{look}{spot}. It starts {r['begin']:%a %H:%M} and is complete at {r['end']:%a %H:%M}."
        if self.overrides.paused():
            text += " The display is paused; it will not start until you `resume`."
        reply = Reply(text)
        reply.hash, reply.first, reply.style = r["hash"], r["first"], r["style"]
        return reply

    def add_known(self, h: str, o: Options, now=None, by=None) -> Reply:
        """Queue a picture the library already has."""
        return self.add(None, h, o, now, by=by)

    # ---- browsing ----
    def pictures(self, page: int):
        """One page of the library, newest first: ([(hash, line)], has_previous, has_next, page, total)."""
        info = self.lib.data["images"]
        hs = sorted((h for h in info if self.lib.usable(h)), key=lambda h: info[h]["added"], reverse=True)
        page = min(max(page, 0), max(len(hs) - 1, 0) // PAGE)
        rows = []
        for n, h in enumerate(hs[page * PAGE:(page + 1) * PAGE], page * PAGE + 1):
            i = info[h]
            last = f"last drawn {max(i['drawn'])}" if i.get("drawn") else "not drawn yet"
            w = {"favourite": ", a favourite", "never": ", only when sent"}.get(self.lib.weight(h), "")
            rows.append((h, f"{n}. *{i['name']}*: added {i['added'][:10]}, {last}{w}"
                            + (f", sent by <@{i['by']}>" if i.get("by") else "")
                            + ("" if self.lib.ready(h) else ", being prepared")))
        return rows, page > 0, (page + 1) * PAGE < len(hs), page, len(hs)

    def strip(self, items, cols=3, tw=320) -> bytes | None:
        """A contact sheet of stored frames, [(label, hash, event)]; None if none is ready."""
        from PIL import Image, ImageDraw
        th = tw * H // W
        cols = max(1, min(cols, len(items)))
        sheet = Image.new("RGB", (cols * (tw + 8) + 8, ceil(len(items) / cols) * (th + 24) + 8), (40, 40, 42))
        d, shown = ImageDraw.Draw(sheet), 0
        for i, (label, h, k, *style) in enumerate(items):
            x, y = 8 + (i % cols) * (tw + 8), 8 + (i // cols) * (th + 24)
            f = self.lib.frame(h, k, style[0] if style else None)
            if f.is_file():
                with Image.open(f) as im:
                    sheet.paste(im.convert("RGB").resize((tw, th)), (x, y))
                shown += 1
            d.text((x, y + th + 5), label, fill=(225, 225, 225))
        if not shown:
            return None
        buf = io.BytesIO()
        sheet.save(buf, "PNG")
        return buf.getvalue()

    def thumbnails(self, page: int) -> bytes | None:
        """The finished pictures on a page of `pictures`, numbered like its lines."""
        rows, _, _, page, _ = self.pictures(page)
        return self.strip([(f"{n}. {self.name_of(h)}"[:44], h, TOTAL) for n, (h, _) in enumerate(rows, page * PAGE + 1)])

    def spots(self, h) -> bytes | None:
        """The picture at four points in its sequence, to help choose where to start."""
        return self.strip([(f"event {k} ({k * 100 // TOTAL}%)", h, k) for k in (180, 360, 540, TOTAL)], cols=2)

    def cycle_weight(self, h) -> str:
        """Normal, then favourite, then never in the rotation, then normal again."""
        if h not in self.lib.data["images"]:
            raise BadRequest("That picture has already gone.")
        new = {"normal": "favourite", "favourite": "never", "never": "normal"}[self.lib.weight(h)]
        self.lib.set_weight(h, new)
        return {"normal": f"*{self.name_of(h)}* takes its usual turn in the rotation.",
                "favourite": f"*{self.name_of(h)}* is a favourite: it comes round twice as often.",
                "never": f"*{self.name_of(h)}* is out of the rotation: it is drawn only when sent."}[new]

    def delete(self, h):
        """Remove a picture and its file for good. Refuses one that is on the display or queued."""
        busy = self.overrides.hashes() | {r["hash"] for r in self.overrides.repeats.all()}
        with self.live.lock:
            busy.add(self.live.hash)
        name = self.name_of(h)
        if h in busy:
            raise BadRequest(f"*{name}* is on the display or waiting in the queue or a repeat. Cancel those first.")
        try:
            return self.lib.delete(h)
        except KeyError:
            raise BadRequest("That picture has already gone.")

    # ---- the queue ----
    def queue(self, now=None):
        now = now or dt.datetime.now().replace(second=0, microsecond=0)
        return self.overrides.entries(now, self.lib), self.overrides.repeats.all()

    def cancel(self, eid, now=None) -> str:
        gone = self.overrides.cancel(eid)
        self.live.wake.set()
        if not gone:
            return "That one has already finished or been cancelled."
        more = " This one occurrence is skipped; the repeat carries on." if gone.get("repeat") else ""
        return f"Cancelled *{self.name_of(gone['hash'])}*.{more}"

    def stop_repeat(self, rid, now=None) -> str:
        r = next((r for r in self.overrides.repeats.all() if r["id"] == rid), None)
        if not r or not self.overrides.repeats.remove(rid):
            return "That repeat has already been stopped."
        now = now or dt.datetime.now().replace(second=0, microsecond=0)
        for e in self.overrides.entries(now, self.lib):      # its coming occurrence, if already scheduled
            if e.get("repeat") == rid and not e["drawing"]:
                self.overrides.cancel(e["id"])
        self.live.wake.set()
        return f"Stopped repeating *{self.name_of(r['hash'])}*."

    def skip(self, now=None) -> str:
        """Drop the upload on the sheet; or, with none, give today a different picture."""
        now = now or dt.datetime.now().replace(second=0, microsecond=0)
        drawing = [e for e in self.overrides.entries(now, self.lib) if e["drawing"]]
        if drawing:
            return "Skipped. " + self.cancel(drawing[-1]["id"])
        h = self.lib.reroll(drawing_day(now, self.start, self.dur)[0])
        if h is None:
            raise BadRequest("There's no other prepared picture to switch to.")
        self.live.wake.set()
        return f"Switched today's picture to *{self.name_of(h)}*; it joins in progress."

    def restart(self, now=None, by=None) -> Reply:
        """Begin what is on the display again from a blank sheet."""
        with self.live.lock:
            h, style = self.live.hash, self.live.style
        if h is None:
            raise BadRequest("Nothing is on the display yet.")
        return self.add(None, h, Options(**(style or {})), now, by=by)

    def pause(self, now=None) -> str:
        if not self.overrides.pause(now or self.now()):
            return "The display is already paused."
        self.live.wake.set()
        return "Paused. The sheet stays as it is until you `resume`."

    def resume(self, now=None) -> str:
        if not self.overrides.resume(now or self.now(), self.lib):
            return "The display isn't paused."
        self.live.wake.set()
        return "Resumed. The drawing carries on from where it stopped, and finishes that much later."

    def status(self) -> str:
        with self.live.lock:
            if self.live.key is None:
                return "The display is still breaking down its first picture."
            m = self.medium_of(self.live.hash, self.live.style)
            return (f"On the display: *{self.live.name}*, event {self.live.k} of {TOTAL}"
                    + (f" ({phase_at(m, self.live.k)})" if self.live.k else " (blank sheet)")
                    + (". Paused." if self.live.paused else f". Complete at {self.live.finish:%a %H:%M}.")
                    + (" Quiet hours: the panel is holding its last frame." if self.live.quiet else ""))

    def status_data(self, now=None) -> dict:
        """Everything about the display's state, for the web page: what is on, what is waiting,
        what repeats, what is being prepared and what failed."""
        now = now or self.now()
        entries, repeats = self.queue(now)
        with self.live.lock:
            lv = self.live
            out = dict(key=lv.key, k=lv.k, total=TOTAL, image=lv.name, paused=lv.paused, quiet=lv.quiet,
                       start=lv.started.isoformat() if lv.started else None,
                       finish=lv.finish.isoformat() if lv.finish else None,
                       medium=self.medium_of(lv.hash, lv.style) if lv.hash in self.lib.data["images"] else None,
                       error=lv.error)
        out["paused"] = out["paused"] or bool(self.overrides.paused())
        out["queue"] = [dict(id=e["id"], name=self.name_of(e["hash"]), start=e["start"], end=e["end"], drawing=e["drawing"],
                             first=e.get("first", 0), repeat=bool(e.get("repeat")), style=e.get("style")) for e in entries]
        out["repeats"] = [dict(id=r["id"], name=self.name_of(r["hash"]), when=describe_repeat(r), time=r["time"],
                               until=r.get("until", ""), first=r.get("first", 0), style=r.get("style")) for r in repeats]
        out["preparing"] = [self.name_of(h) for h in self.lib.unbaked()]
        out["failed"] = [dict(name=self.name_of(h), why=why) for h, why in list(self.lib.failures.items())]
        return out


class Progress:
    """Notices what the display has moved on to, for posting in the channel."""

    def __init__(self, live: Live, lib: Library):
        self.live, self.lib, self.seen = live, lib, None
        self.completed = None                       # (hash, style) of a drawing just announced complete

    def poll(self) -> list:
        with self.live.lock:
            key, h, k, name, style = self.live.key, self.live.hash, self.live.k, self.live.name, self.live.style
        if key is None:
            return []
        phase, done = phase_at(self.lib.look_for(h, style).medium, k), k >= TOTAL
        if self.seen is None:                       # first look: just remember, don't announce what was already going
            self.seen = (key, phase, done)
            return []
        okey, ophase, odone = self.seen
        out = []
        if key != okey:
            out.append(f"Now drawing *{name}*" + (f", already at event {k} ({phase})." if k else " on a blank sheet."))
            odone = False
        elif phase != ophase and k:
            out.append(f"*{name}* moves on to {phase} (event {k} of {TOTAL}).")
        if done and not odone:
            out.append(f"*{name}* is complete.")
            self.completed = (h, style)
        self.seen = (key, phase, done)
        return out


class Troubles:
    """Things worth telling the channel about once: pictures that could not be broken down,
    and a display loop that has been failing for a while (and when it recovers)."""

    AFTER = 300                                     # seconds of failing before saying so

    def __init__(self, live: Live, lib: Library):
        self.live, self.lib, self.told, self.down = live, lib, set(), False

    def poll(self, now=None) -> list:
        out = []
        for h, why in list(self.lib.failures.items()):
            if (h, why) in self.told:
                continue
            self.told.add((h, why))
            i = self.lib.data["images"].get(h, {})
            out.append(f":warning: {'<@' + i['by'] + '> ' if i.get('by') else ''}I couldn't break down "
                       f"*{i.get('name', 'a picture')}*: {why}. It has been left out.")
        with self.live.lock:
            since, error = self.live.error_since, self.live.error
        now = now or time.time()
        if since and now - since >= self.AFTER and not self.down:
            self.down = True
            out.append(f":warning: The display has been failing for {int((now - since) // 60)} minutes: {error}")
        elif not since and self.down:
            self.down = False
            out.append("The display is working again.")
        return out


# ---- the Slack side: blocks ----
def md(text, **extra):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}, **extra}


def button(text, action, value, **extra):
    return {"type": "button", "action_id": action, "value": value, "text": {"type": "plain_text", "text": text}, **extra}


def browse_blocks(desk: Desk, page: int) -> list:
    rows, prev, more, page, total = desk.pictures(page)
    if not rows:
        return [md("No pictures yet. Post one here.")]
    blocks = [md(f"{total} pictures, page {page + 1}. Choose one to send again:")]
    for h, line in rows:
        blocks.append(md(line))
        turn = {"normal": "Make favourite", "favourite": "Take out of rotation", "never": "Back in rotation"}[desk.lib.weight(h)]
        blocks.append({"type": "actions", "elements": [
            button("Choose...", "pick", h), button("Preview spots", "preview", h), button(turn, "weight", h),
            button("Delete...", "delete", h, style="danger")]})
    nav = [button(t, "page", str(page + d)) for d, t, ok in ((-1, "Previous", prev), (1, "More", more)) if ok]
    return blocks + ([{"type": "actions", "elements": nav}] if nav else [])


def queue_blocks(desk: Desk, now=None) -> list:
    entries, repeats = desk.queue(now)
    blocks = []
    for e in entries:
        t = dt.datetime.fromisoformat
        spot = f" from event {e['first']}" if e.get("first") else ""
        by = f", sent by <@{e['by']}>" if e.get("by") else ""
        when = (f"on the display now, complete {t(e['end']):%a %H:%M}" if e["drawing"]
                else f"starts {t(e['start']):%a %H:%M}, complete {t(e['end']):%a %H:%M}")
        blocks.append(md(f"*{desk.name_of(e['hash'])}*{spot}: {when}{by}",
                         accessory=button("Cancel", "cancel", e["id"])))
    for r in repeats:
        until = f", finishing by {r['until']}" if r.get("until") else ""
        spot = f" from event {r['first']}" if r.get("first") else ""
        blocks.append(md(f"*{desk.name_of(r['hash'])}*{spot}: {describe_repeat(r)} at {r['time']}{until}",
                         accessory=button("Stop repeating", "stop_repeat", r["id"])))
    if not blocks:
        blocks = [md(f"Nothing queued. The daily drawing starts at {desk.start:%H:%M}.")]
    else:
        blocks = [md("Queued and repeating:")] + blocks
    if desk.overrides.paused():
        blocks.insert(0, md(":double_vertical_bar: The display is paused. Say `resume` to carry on."))
    return blocks


def setup_modal(medium, meta: dict) -> dict:
    """The form: when it starts, when it finishes, where in the sequence, how it's made, whether it repeats."""
    def opts(pairs):
        return [{"text": {"type": "plain_text", "text": t}, "value": v} for v, t in pairs]

    def field(block, label, element, optional=True):
        return {"type": "input", "block_id": block, "optional": optional, "label": {"type": "plain_text", "text": label},
                "element": {"action_id": "v", **element}}

    when = opts((("now", "Now"), ("next", "At the next daily start"), ("at", "At a time...")))
    return {
        "type": "modal", "callback_id": "setup", "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Send to the display"},
        "submit": {"type": "plain_text", "text": "Send"},
        "blocks": [
            field("when", "Start", {"type": "static_select", "options": when, "initial_option": when[0]}, False),
            field("at", "...at this time (if 'At a time'; also the time of day for a repeat)", {"type": "timepicker"}),
            field("until", "Finish by (empty = the usual pace)", {"type": "timepicker"}),
            field("phase", "Start from the beginning of a phase", {"type": "static_select", "options": phase_options(medium)}),
            field("event", f"...or from an exact event (0 to {TOTAL - 1}; wins over the phase)",
                  {"type": "number_input", "is_decimal_allowed": False, "min_value": "0", "max_value": str(TOTAL - 1)}),
            field("repeat", "Repeat",
                  {"type": "static_select", "options": opts((("d", "Every day"), ("wd", "Every weekday"), ("we", "Every weekend"))
                                                          + tuple((str(i), f"Every {n}") for i, n in enumerate(DAYS)))}),
            field("medium", "Make it as",
                  {"type": "static_select", "options": opts((("pencil", "A pencil drawing"), ("paint", "A painting")))}),
            field("fit", "Fit", {"type": "static_select", "options": opts((("crop", "Fill the sheet (crop)"),
                                                                           ("pad", "Whole image (margins)")))}),
            field("always", "Keep this look", {"type": "checkboxes", "options": opts(
                (("always", "Always make this picture this way, not just this once"),))}),
            field("detail", "Detail (0.5 to 2; more is finer)",
                  {"type": "number_input", "is_decimal_allowed": True, "min_value": "0.5", "max_value": "2"}),
            field("contrast", "Contrast (0.5 to 2)",
                  {"type": "number_input", "is_decimal_allowed": True, "min_value": "0.5", "max_value": "2"}),
        ],
    }


def phase_options(medium):
    names = list(PHASE_NAMES[medium].values())
    return [{"text": {"type": "plain_text", "text": f"{n} (event {s})"}, "value": str(s)}
            for n, s in zip(names, PHASE_STARTS[medium])]


def options_from_form(values: dict) -> Options:
    def get(block, key):
        return values.get(block, {}).get("v", {}).get(key)
    when = (get("when", "selected_option") or {}).get("value", "now")
    if when == "at":
        when = get("at", "selected_time")
        if not when:
            raise BadRequest("Choose a time for 'At a time...'.")
    elif (t := get("at", "selected_time")) and get("repeat", "selected_option"):
        when = t                                      # a repeat takes its time of day from here
    o = Options(when=when, until=get("until", "selected_time") or "", given=True)
    event, phase = get("event", "value"), get("phase", "selected_option")
    o.first = int(event) if event not in (None, "") else int(phase["value"]) if phase else 0
    rep = (get("repeat", "selected_option") or {}).get("value")
    if rep:
        o.every = {"d": list(range(7)), "wd": list(range(5)), "we": [5, 6]}.get(rep) or [int(rep)]
    o.medium = (get("medium", "selected_option") or {}).get("value")
    o.fit = (get("fit", "selected_option") or {}).get("value")
    o.always = bool(get("always", "selected_options"))
    for key in ("detail", "contrast"):
        v = get(key, "value")
        setattr(o, key, float(v) if v not in (None, "") else None)
    return o


def download(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise BadRequest(f"Images up to {MAX_UPLOAD_BYTES // 1_000_000} MB, please.")
    return data


def start_slack(desk: Desk, bot_token: str, app_token: str, channel: str, users=None, timelapse=True):
    """Connect to Slack and answer in `channel` (its ID, like C0123ABCD). `users` limits who can
    change the display (Slack user IDs); everyone in the channel can look. Returns at once."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=bot_token)
    client = app.client
    users = set(users or ())

    def post(thread, text, **kw):
        client.chat_postMessage(channel=channel, thread_ts=thread, text=text, **kw)

    def upload(thread, png, name, title):
        if png:
            client.files_upload_v2(channel=channel, thread_ts=thread, file=png, filename=name, title=title)

    def allowed(user, thread):
        if users and user not in users:
            post(thread, ":lock: Only " + ", ".join(f"<@{u}>" for u in sorted(users)) + " can change the display.")
            return False
        return True

    def fetch(f) -> tuple[bytes, str]:
        if not str(f.get("mimetype", "image/")).startswith("image/"):
            raise BadRequest(f"{f.get('name', 'That file')} isn't a picture.")
        name = f.get("name") or "slack.png"
        if "hei" in str(f.get("mimetype", "")) + str(f.get("filetype", "")) and not library.HEIC:
            thumb = next((f[k] for k in ("thumb_1024", "thumb_960", "thumb_800", "thumb_720", "thumb_480") if f.get(k)), None)
            if thumb:                                # an iPhone photo this Pi can't open: Slack's own JPEG of it
                return download(thumb, bot_token), name.rsplit(".", 1)[0] + ".jpg"
        return download(f.get("url_private_download") or f["url_private"], bot_token), name

    def attempt(thread, fn):
        """Run fn; post its text (and a preview of the starting spot), or what went wrong."""
        try:
            reply = fn()
            post(thread, reply)
            if isinstance(reply, Reply) and reply.first:
                upload(thread, desk.strip([(f"event {reply.first}", reply.hash, reply.first, reply.style)]), "start.png",
                       "Where it starts")
        except BadRequest as e:
            post(thread, f":warning: {e}")
        except Exception as e:                       # a Slack or network hiccup shouldn't kill the listener
            print(f"slack: {e}", flush=True)
            post(thread, ":warning: Something went wrong. Try again in a minute.")

    def thread_of(body):
        m = body.get("message") or {}
        return m.get("thread_ts") or m.get("ts")

    def file_of(fid):
        return client.files_info(file=fid)["file"]

    def show_browse(thread, page=0):
        post(thread, "The pictures on the display", blocks=browse_blocks(desk, page))
        upload(thread, desk.thumbnails(page), "page.png", "This page")

    commands = {
        "browse": lambda thread, user: show_browse(thread),
        "queue": lambda thread, user: post(thread, "The queue", blocks=queue_blocks(desk)),
        "status": lambda thread, user: post(thread, desk.status()),
        "help": lambda thread, user: post(thread, HELP),
        "skip": lambda thread, user: allowed(user, thread) and attempt(thread, desk.skip),
        "restart": lambda thread, user: allowed(user, thread) and attempt(thread, lambda: desk.restart(by=user)),
        "pause": lambda thread, user: allowed(user, thread) and attempt(thread, desk.pause),
        "resume": lambda thread, user: allowed(user, thread) and attempt(thread, desk.resume),
    }

    @app.event("message")
    def on_message(event):
        if event.get("channel") != channel or event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        thread, text, user = event.get("thread_ts") or event["ts"], event.get("text", ""), event.get("user")
        images = [f for f in event.get("files", []) if str(f.get("mimetype", "")).startswith("image/")]
        if not images:
            word = re.sub(r"<[^>]*>", " ", text).strip().lower().rstrip("?") or "help"
            if word in commands:
                commands[word](thread, user)
            return
        if not allowed(user, thread):
            return
        f = images[0]
        extra = " I used the first picture; send one at a time." if len(images) > 1 else ""

        def go():
            reply = desk.add(*fetch(f), o, by=user)
            return _keep(reply, reply + extra) if extra else reply

        try:
            o = parse_options(text, desk.medium)
        except BadRequest as e:
            return post(thread, f":warning: {e}")
        if not o.given:
            return post(thread, "Got it. How should I start it?", blocks=[
                md(f"Got *{f.get('name', 'your picture')}*. How should I start it?"),
                {"type": "actions", "elements": [button("Start now", "start_now", f["id"], style="primary"),
                                                 button("Choose when and where...", "choose", f["id"])]}])
        attempt(thread, go)

    def _keep(reply, text):
        out = Reply(text)
        out.hash, out.first, out.style = reply.hash, reply.first, reply.style
        return out

    def guard(body, ack):
        """Common checks for a button: acknowledged, from our channel, from an allowed person."""
        ack()
        return body["channel"]["id"] == channel and allowed(body["user"]["id"], thread_of(body))

    @app.action("start_now")
    def on_now(ack, body):
        if guard(body, ack):
            attempt(thread_of(body), lambda: desk.add(*fetch(file_of(body["actions"][0]["value"])), Options(),
                                                      by=body["user"]["id"]))

    @app.action("choose")
    def on_choose(ack, body):
        if guard(body, ack):
            meta = {"file": body["actions"][0]["value"], "thread": thread_of(body)}
            client.views_open(trigger_id=body["trigger_id"], view=setup_modal(desk.medium, meta))

    @app.action("pick")
    def on_pick(ack, body):
        if guard(body, ack):
            h = body["actions"][0]["value"]
            meta = {"hash": h, "thread": thread_of(body)}
            client.views_open(trigger_id=body["trigger_id"], view=setup_modal(desk.medium_of(h), meta))

    @app.action("page")
    def on_page(ack, body, respond):
        ack()
        if body["channel"]["id"] == channel:
            page = int(body["actions"][0]["value"])
            respond(text="The pictures on the display", blocks=browse_blocks(desk, page), replace_original=True)
            upload(thread_of(body), desk.thumbnails(page), "page.png", "This page")

    @app.action("preview")
    def on_preview(ack, body):
        ack()
        if body["channel"]["id"] != channel:
            return
        h, thread = body["actions"][0]["value"], thread_of(body)
        png = desk.spots(h)
        if png:
            upload(thread, png, "spots.png", f"{desk.name_of(h)} at 25%, 50%, 75% and 100%")
        else:
            post(thread, f"*{desk.name_of(h)}* is still being prepared; try again in a minute.")

    @app.action("weight")
    def on_weight(ack, body):
        if guard(body, ack):
            attempt(thread_of(body), lambda: desk.cycle_weight(body["actions"][0]["value"]))

    @app.action("delete")
    def on_delete(ack, body):
        if guard(body, ack):
            h = body["actions"][0]["value"]
            post(thread_of(body), f"Delete *{desk.name_of(h)}* for good? Its picture file is removed too.", blocks=[
                md(f"Delete *{desk.name_of(h)}* for good? Its picture file is removed too."),
                {"type": "actions", "elements": [button("Delete it", "delete_yes", h, style="danger", confirm={
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "mrkdwn", "text": "This cannot be undone."},
                    "confirm": {"type": "plain_text", "text": "Delete"}, "deny": {"type": "plain_text", "text": "Keep"}})]}])

    @app.action("delete_yes")
    def on_delete_yes(ack, body):
        if guard(body, ack):
            attempt(thread_of(body), lambda: f"Deleted *{desk.delete(body['actions'][0]['value'])}*.")

    @app.action("cancel")
    def on_cancel(ack, body, respond):
        if guard(body, ack):
            attempt(thread_of(body), lambda: desk.cancel(body["actions"][0]["value"]))
            respond(text="The queue", blocks=queue_blocks(desk), replace_original=True)

    @app.action("stop_repeat")
    def on_stop(ack, body, respond):
        if guard(body, ack):
            attempt(thread_of(body), lambda: desk.stop_repeat(body["actions"][0]["value"]))
            respond(text="The queue", blocks=queue_blocks(desk), replace_original=True)

    @app.view("setup")
    def on_setup(ack, body, view):
        user = body["user"]["id"]
        try:
            o = options_from_form(view["state"]["values"])
        except BadRequest as e:
            return ack(response_action="errors", errors={"at": str(e)})
        ack()
        meta = json.loads(view["private_metadata"])
        if not allowed(user, meta["thread"]):
            return
        attempt(meta["thread"], (lambda: desk.add_known(meta["hash"], o, by=user)) if "hash" in meta else
                (lambda: desk.add(*fetch(file_of(meta["file"])), o, by=user)))

    progress, troubles = Progress(desk.live, desk.lib), Troubles(desk.live, desk.lib)

    def announce_once():
        for text in progress.poll() + troubles.poll():
            post(None, text)
        done, progress.completed = progress.completed, None
        if done and timelapse:                       # the whole day in a few seconds
            h, style = done
            gif = timelapse_gif([desk.lib.frame(h, k, style) for k in range(0, TOTAL + 1, 8)])
            if gif:
                client.files_upload_v2(channel=channel, file=gif, filename="timelapse.gif",
                                       title=f"{desk.name_of(h)}, start to finish")

    def announce():
        while True:
            try:
                announce_once()
            except Exception as e:
                print(f"slack progress: {e}", flush=True)
            time.sleep(20)

    threading.Thread(target=announce, daemon=True).start()
    handler = SocketModeHandler(app, app_token)
    handler.connect()
    print(f"slack: listening in channel {channel}", flush=True)
    handler.announce_once = announce_once            # for the tests
    return handler
