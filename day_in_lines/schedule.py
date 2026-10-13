"""The clock: when each drawing starts and finishes, and uploads that take over the schedule."""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

from .constants import TOTAL
from .library import Library


def parse_end(text):
    return dt.datetime.strptime(text, "%H:%M").time() if text else None


def window(start: dt.time, end: dt.time | None) -> dt.timedelta:
    """How long a day's drawing takes: start to end (across midnight if need be).
    No end: the classic 12 hours, one event a minute."""
    if end is None:
        return dt.timedelta(minutes=TOTAL)
    d = (dt.datetime.combine(dt.date.min, end) - dt.datetime.combine(dt.date.min, start)) % dt.timedelta(days=1)
    return d or dt.timedelta(days=1)


def progress(now: dt.datetime, began: dt.datetime, dur: dt.timedelta) -> int:
    """Events on the sheet: the 720 are spread evenly from `began` over `dur`."""
    if now <= began:
        return 0
    return min(TOTAL, int((now - began) / dur * TOTAL))


def drawing_day(now, start, dur=dt.timedelta(minutes=TOTAL)):
    """(day, events that should be on the sheet). Before today's start, it is still
    yesterday's drawing: finished, or still going if it runs past midnight."""
    s = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if now < s:
        s -= dt.timedelta(days=1)
    return s.date(), progress(now, s, dur)


def next_start(after: dt.datetime, start: dt.time) -> dt.datetime:
    s = dt.datetime.combine(after.date(), start)
    return s if s >= after else s + dt.timedelta(days=1)


def override_end(began: dt.datetime, fin: dt.datetime, start: dt.time, dur: dt.timedelta) -> dt.datetime:
    """When an uploaded drawing that finishes at `fin` hands the sheet back to the
    daily schedule. Normally it stays up, finished, until the next start time, like
    any day's drawing. If it finishes early in a day's drawing time, it hands back
    straight away and the day's drawing joins in progress, rather than leaving the
    panel idle for a day."""
    s = dt.datetime.combine(fin.date(), start)
    if s > fin:
        s -= dt.timedelta(days=1)
    return fin if fin - s < dur / 2 else next_start(fin, start)


class Overrides:
    """Uploads waiting for, or holding, the sheet: cache/override.json, a list of
    {"hash", "start", "end", "fixed"}. A fixed upload (start now, or at a chosen
    time) takes the sheet at its start, cutting short whatever upload is drawing.
    A queued one (at the next start time) waits until the sheet is free."""

    def __init__(self, cache: Path, start: dt.time, dur: dt.timedelta):
        self.file, self.start, self.dur = cache / "override.json", start, dur
        self.lock = threading.Lock()
        self.repeats = Repeats(self, cache / "repeats.json")

    def _load(self):
        try:
            ovs = json.loads(self.file.read_text())
        except FileNotFoundError:
            return []
        except Exception:
            self.file.unlink(missing_ok=True)
            return []
        ovs = ovs if isinstance(ovs, list) else [ovs]          # older single-override file
        ovs = [o for o in ovs if isinstance(o, dict) and "hash" in o and "start" in o]
        for o in ovs:                                          # older entries: the default length
            o.setdefault("end", (dt.datetime.fromisoformat(o["start"]) + self.dur).isoformat())
        return ovs

    def _save(self, ovs):
        if not ovs:
            self.file.unlink(missing_ok=True)
            return
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(ovs))
        tmp.replace(self.file)

    @staticmethod
    def _t(o, k):
        return dt.datetime.fromisoformat(o[k])

    def _hands_back(self, o):
        return override_end(self._t(o, "start"), self._t(o, "end"), self.start, self.dur)

    def _move(self, o, to):
        o["end"], o["start"] = (to + (self._t(o, "end") - self._t(o, "start"))).isoformat(), to.isoformat()

    def _chain(self, ovs):
        """Queued uploads wait until the sheet is free, including for fixed uploads
        that would otherwise cut them short."""
        for _ in range(4 * len(ovs) + 1):
            ovs.sort(key=lambda o: self._t(o, "start"))
            moved = False
            for prev, o in zip(ovs, ovs[1:]):
                free = self._hands_back(prev)
                if self._t(o, "start") >= free:
                    continue
                if not o.get("fixed"):
                    self._move(o, next_start(free, self.start)); moved = True; break
                if not prev.get("fixed") and self._t(prev, "end") > self._t(o, "start"):
                    self._move(prev, next_start(self._hands_back(o), self.start)); moved = True; break
            if not moved:
                break
        return ovs

    def add(self, h, begin: dt.datetime, fin: dt.datetime, fixed: bool, first: int = 0, **extra):
        """`first` is the spot in the 720 to start from: the sheet already holds that many
        events when the upload takes it, and the rest are spread from `begin` to `fin`."""
        with self.lock:
            ovs = self._load()
            new = {"hash": h, "start": begin.isoformat(), "end": fin.isoformat(), "fixed": fixed}
            if first:
                new["first"] = first
            new.update({k: v for k, v in extra.items() if v})       # who sent it, which repeat made it
            if not fixed:                  # joins the back of the queue, so uploads already promised a time keep it
                waiting = [self._hands_back(o) for o in ovs if not o.get("fixed")]
                if waiting and begin < max(waiting):
                    self._move(new, next_start(max(waiting), self.start))
            ovs.append(new)
            self._save(self._chain(ovs))
            return self._t(new, "start"), self._t(new, "end")

    @staticmethod
    def entry_id(o):
        return f"{o['hash']}@{o['start']}"

    def entries(self, now: dt.datetime, lib: Library):
        """Everything waiting for or holding the sheet, soonest first, each with an `id` and
        `drawing` (is it on the sheet now)."""
        self.current(now, lib)                       # drops finished and cut-short ones first
        with self.lock:
            ovs = self._load()
        out = []
        for o in sorted(ovs, key=lambda o: self._t(o, "start")):
            out.append(dict(o, id=self.entry_id(o), drawing=self._t(o, "start") <= now))
        return out

    def cancel(self, eid):
        """Remove one entry by its id; returns it, or None if it has gone already."""
        with self.lock:
            ovs = self._load()
            gone = next((o for o in ovs if self.entry_id(o) == eid), None)
            if gone:
                ovs.remove(gone)
                self._save(self._chain(ovs))
        if gone and gone.get("repeat"):
            self.repeats.skip(gone["repeat"], gone["start"])
        return gone

    def hashes(self):
        """Pictures waiting for, or holding, the sheet."""
        with self.lock:
            return {o["hash"] for o in self._load()}

    def current(self, now: dt.datetime, lib: Library):
        """The upload on the sheet now, as (hash, start, end), or None. Drops finished
        ones, and ones a later fixed upload has cut short."""
        self.repeats.apply(now, lib)
        with self.lock:
            ovs = self._load()
            keep = [o for o in ovs if lib.usable(o["hash"]) and now < self._hands_back(o)]
            started = [o for o in keep if self._t(o, "start") <= now]
            active = max(started, key=lambda o: self._t(o, "start")) if started else None
            if active:
                keep = [o for o in keep if o is active or self._t(o, "start") > now]
            if keep != ovs:
                self._save(self._chain(keep))
        if active:
            began, fin = self._t(active, "start"), self._t(active, "end")
            first = min(max(int(active.get("first", 0)), 0), TOTAL - 1)
            # Seen from the viewer's side the drawing began earlier, so `progress` gives `first` at `began`
            return active["hash"], began - (fin - began) * first / (TOTAL - first), fin
        return None


DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class Repeats:
    """Pictures that come round again: cache/repeats.json, a list of {"id", "hash", "days"
    (0 = Monday), "time", "until", "first", "by", "skip"}. Each occurrence is put on the
    upload schedule as a fixed upload a day before it is due."""

    AHEAD = dt.timedelta(days=1)

    def __init__(self, overrides, file: Path):
        self.ov, self.file = overrides, file
        self.lock = threading.Lock()

    def _load(self):
        try:
            rs = json.loads(self.file.read_text())
        except FileNotFoundError:
            return []
        except Exception:
            self.file.unlink(missing_ok=True)
            return []
        return [r for r in rs if isinstance(r, dict) and "hash" in r and "id" in r] if isinstance(rs, list) else []

    def _save(self, rs):
        if not rs:
            self.file.unlink(missing_ok=True)
            return
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rs))
        tmp.replace(self.file)

    def add(self, h, days, at: str, until="", first=0, by=None):
        """Repeat a picture on these weekdays at this time ("HH:MM"). Returns the repeat's id."""
        dt.datetime.strptime(at, "%H:%M")
        with self.lock:
            rs = self._load()
            r = {"id": f"{h}-{len(rs)}-{int(dt.datetime.now().timestamp())}", "hash": h, "days": sorted(set(days)),
                 "time": at, "until": until, "first": first}
            if by:
                r["by"] = by
            self._save(rs + [r])
            return r["id"]

    def all(self):
        with self.lock:
            return self._load()

    def remove(self, rid):
        with self.lock:
            rs = self._load()
            keep = [r for r in rs if r["id"] != rid]
            self._save(keep)
            return len(keep) != len(rs)

    def skip(self, rid, occurrence_iso):
        """Leave out one occurrence (it was cancelled); the repeat carries on after it."""
        with self.lock:
            rs = self._load()
            for r in rs:
                if r["id"] == rid:
                    r["skip"] = occurrence_iso
            self._save(rs)

    @staticmethod
    def next_time(r, now: dt.datetime) -> dt.datetime:
        t = dt.datetime.strptime(r["time"], "%H:%M").time()
        for i in range(8):
            c = dt.datetime.combine(now.date() + dt.timedelta(days=i), t)
            if c >= now and c.weekday() in r["days"]:
                return c
        return dt.datetime.combine(now.date() + dt.timedelta(days=7), t)

    def apply(self, now: dt.datetime, lib: Library):
        """Put occurrences coming up within a day on the schedule (once each)."""
        rs = self.all()
        if not rs:
            return
        now = now.replace(second=0, microsecond=0)
        have = {(o["hash"], o["start"]) for o in self.ov._load()}
        for r in rs:
            if not lib.usable(r["hash"]):
                continue
            occ = self.next_time(r, now)
            if occ - now > self.AHEAD or occ.isoformat() == r.get("skip") or (r["hash"], occ.isoformat()) in have:
                continue
            first = r.get("first", 0)
            fin = (next_start(occ + dt.timedelta(minutes=1), dt.datetime.strptime(r["until"], "%H:%M").time())
                   if r.get("until") else occ + self.ov.dur * (TOTAL - first) / TOTAL)
            self.ov.add(r["hash"], occ, fin, True, first, repeat=r["id"], by=r.get("by"))


def schedule(now, start, dur, overrides: Overrides, lib: Library):
    """What should be on the sheet now: (key, image hash, k, start datetime, end datetime)."""
    cur = overrides.current(now, lib)
    if cur:
        h, began, fin = cur
        return f"upload:{began.isoformat()}:{h}", h, progress(now, began, fin - began), began, fin
    day, k = drawing_day(now, start, dur)
    h = lib.pick(day)
    if h is None:
        return None, None, 0, None, None
    began = dt.datetime.combine(day, start)
    return f"day:{day}:{h}", h, k, began, began + dur
