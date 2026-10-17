"""Putting a picture on the schedule: shared by the web upload and the Slack channel."""
from __future__ import annotations

import datetime as dt

from .constants import TOTAL
from .library import Library
from .schedule import DAYS, Overrides, next_start


class BadRequest(ValueError):
    """Something the sender can fix; the message is shown to them."""


def submit(lib: Library, overrides: Overrides, start: dt.time, dur: dt.timedelta, data: bytes | None, name: str,
           when="now", until="", first=0, now=None, by=None, tune=None, every=None) -> dict:
    """Store the picture and queue it. `when` is "now", "next" or "HH:MM"; `until` is
    "" or the "HH:MM" it should be finished by; `first` (0 to 719) is the spot in the
    sequence it starts from. With `data` None, `name` is the hash of a picture the
    library already holds. `by` is who sent it. `tune` is a dict of medium, detail and
    contrast for this picture. `every` is a list of weekdays (0 = Monday) to repeat it on,
    at the time in `when` (the daily start if that is not a clock time). Raises BadRequest."""
    now = now or dt.datetime.now().replace(second=0, microsecond=0)
    try:
        first = int(first)
    except (TypeError, ValueError):
        raise BadRequest("The starting spot must be a number from 0 to 719.")
    if not 0 <= first < TOTAL:
        raise BadRequest(f"The starting spot must be from 0 to {TOTAL - 1}.")
    try:
        if every:
            at = when if when not in ("now", "next") else start.strftime("%H:%M")
            dt.datetime.strptime(at, "%H:%M")
            if until:
                dt.datetime.strptime(until, "%H:%M")
        elif when == "now":
            begin, fixed = now, True
        elif when == "next":
            begin, fixed = next_start(now + dt.timedelta(minutes=1), start), False
        else:                                      # a chosen time: the next time it comes round
            begin, fixed = next_start(now, dt.datetime.strptime(when, "%H:%M").time()), True
        if not every:
            fin = (next_start(begin + dt.timedelta(minutes=1), dt.datetime.strptime(until, "%H:%M").time())
                   if until else begin + dur * (TOTAL - first) / TOTAL)
    except ValueError:
        raise BadRequest("Times must look like 14:30.")
    if data is None:
        h = name
        if not lib.usable(h):
            raise BadRequest("That picture is no longer available.")
    else:
        try:
            h = lib.add_upload(data, name, by)
        except ValueError as e:
            raise BadRequest(str(e))
    try:
        lib.tune(h, **(tune or {}))
    except ValueError as e:
        raise BadRequest(str(e))
    out = dict(hash=h, first=first, name=lib.data["images"][h]["name"], repeat=None)
    if every:
        rid = overrides.repeats.add(h, every, at, until, first, by)
        r = next(r for r in overrides.repeats.all() if r["id"] == rid)
        overrides.repeats.apply(now, lib)
        return dict(out, repeat=rid, days=r["days"], at=at, begin=overrides.repeats.next_time(r, now), end=None, now=False)
    begin, fin = overrides.add(h, begin, fin, fixed, first, by=by)
    return dict(out, begin=begin, end=fin, now=begin <= now)


def describe_days(days) -> str:
    days = sorted(days)
    if len(days) == 7:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "every weekday"
    if days == [5, 6]:
        return "every weekend"
    return "every " + ", ".join(DAYS[d] for d in days)
