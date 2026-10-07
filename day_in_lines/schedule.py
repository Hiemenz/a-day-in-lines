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

    def add(self, h, begin: dt.datetime, fin: dt.datetime, fixed: bool):
        with self.lock:
            ovs = self._load()
            new = {"hash": h, "start": begin.isoformat(), "end": fin.isoformat(), "fixed": fixed}
            if not fixed:                  # joins the back of the queue, so uploads already promised a time keep it
                waiting = [self._hands_back(o) for o in ovs if not o.get("fixed")]
                if waiting and begin < max(waiting):
                    self._move(new, next_start(max(waiting), self.start))
            ovs.append(new)
            self._save(self._chain(ovs))
            return self._t(new, "start"), self._t(new, "end")

    def hashes(self):
        """Pictures waiting for, or holding, the sheet."""
        with self.lock:
            return {o["hash"] for o in self._load()}

    def current(self, now: dt.datetime, lib: Library):
        """The upload on the sheet now, as (hash, start, end), or None. Drops finished
        ones, and ones a later fixed upload has cut short."""
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
            return active["hash"], self._t(active, "start"), self._t(active, "end")
        return None


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
