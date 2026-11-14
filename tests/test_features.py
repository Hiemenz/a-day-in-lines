"""Quiet hours, pausing, per-drawing looks, dated repeats, favourites, panel modes, feeds, the
disk guard, the status and health endpoints, and the clock change."""
import argparse
import datetime as dt
import http.client
import io
import json
import os
import sys
import time
import types

import numpy as np
import pytest
from PIL import Image

from conftest import make_png
from day_in_lines import cli, feeds
from day_in_lines.constants import GRAY4, PALETTE7, TOTAL
from day_in_lines.displays import InkyDisplay, Rotated, WaveshareDisplay, caption
from day_in_lines.export import timelapse_gif, viewer_page
from day_in_lines.library import Library, clean_style
from day_in_lines.render import Sheet, frame_for
from day_in_lines.schedule import Overrides, Repeats, progress, schedule_full
from day_in_lines.server import Live, serve
from day_in_lines.slack_app import Desk, Options, Troubles, parse_options
from day_in_lines.submit import BadRequest, describe_repeat, submit

D, T = dt.datetime, dt.time
START, DUR = T(7), dt.timedelta(hours=12)


def png(shade=120, size=(60, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, (shade,) * 3).save(buf, "PNG")
    return buf.getvalue()


def fake_bake(lib, h, style=None):
    """Stored frames without the work: enough for the clock, the page and the previews."""
    d = lib.dir(h, style)
    (d / "frames").mkdir(parents=True, exist_ok=True)
    for k in range(TOTAL + 1):
        if not lib.frame(h, k, style).exists():
            Image.new("1", (80, 48), 1).save(lib.frame(h, k, style))
    (d / "viewer.json").write_text("[]")
    (d / "meta.json").write_text(json.dumps({"name": lib.data["images"][h]["name"], "events": [["c", f"event {i}", 1] for i in range(TOTAL)]}))
    return d


@pytest.fixture
def desk(tmp_path):
    (tmp_path / "pictures").mkdir()
    lib = Library(tmp_path / "cache", tmp_path / "pictures", "crop")
    return Desk(lib, Overrides(tmp_path / "cache", START, DUR), START, DUR, Live())


# ---- the clock change ----
@pytest.fixture
def london(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_drawing_does_not_lose_marks_when_the_clocks_go_back(london):
    began = D(2026, 10, 24, 20, 0)                       # the clocks go back at 02:00 on the 25th
    first_pass, second_pass = D(2026, 10, 25, 1, 30), D(2026, 10, 25, 1, 30, fold=1)
    a, b = progress(first_pass, began, DUR), progress(second_pass, began, DUR)
    assert (a, b) == (304, 360)                          # 13 real hours to the 08:00 promised; an hour on, further on
    assert progress(D(2026, 10, 25, 1, 59), began, DUR) < progress(D(2026, 10, 25, 1, 0, fold=1), began, DUR)
    assert progress(D(2026, 10, 25, 7, 59), began, DUR) == TOTAL - 1    # and still complete at the time promised


# ---- pause and resume ----
def test_pausing_freezes_an_upload_and_resuming_carries_on_later(desk):
    lib, ov = desk.lib, desk.overrides
    h = lib.add_upload(png(), "a.png")
    now = D(2026, 3, 10, 12, 0)
    desk.add_known(h, Options(), now)
    assert schedule_full(now + dt.timedelta(hours=3), START, DUR, ov, lib)["k"] == 180
    assert "Paused" in desk.pause(now + dt.timedelta(hours=3))
    assert "already" in desk.pause(now + dt.timedelta(hours=4))
    s = schedule_full(now + dt.timedelta(hours=5), START, DUR, ov, lib)
    assert s["k"] == 180 and s["paused"]                  # two hours on, nothing has moved
    assert "Resumed" in desk.resume(now + dt.timedelta(hours=5))
    s = schedule_full(now + dt.timedelta(hours=5), START, DUR, ov, lib)
    assert s["k"] == 180 and not s["paused"]
    assert s["fin"] == now + DUR + dt.timedelta(hours=2)  # finishes later by the length of the pause
    assert schedule_full(now + dt.timedelta(hours=6), START, DUR, ov, lib)["k"] == 240
    assert "isn't paused" in desk.resume(now + dt.timedelta(hours=6))


def test_pausing_the_days_drawing_resumes_it_from_the_same_event(desk, tmp_path):
    lib, ov = desk.lib, desk.overrides
    make_png(tmp_path / "pictures" / "a.png")
    (h,) = lib.scan()
    fake_bake(lib, h)
    noon = D(2026, 3, 10, 13, 0)
    assert schedule_full(noon, START, DUR, ov, lib)["k"] == 360
    ov.pause(noon)
    later = noon + dt.timedelta(hours=4)
    assert schedule_full(later, START, DUR, ov, lib)["k"] == 360
    ov.resume(later, lib)
    s = schedule_full(later, START, DUR, ov, lib)
    assert (s["hash"], s["k"]) == (h, 360) and s["key"].startswith("upload:")
    assert s["fin"] == later + dt.timedelta(hours=6)      # the six hours it had left
    assert schedule_full(later + dt.timedelta(hours=3), START, DUR, ov, lib)["k"] == 540


def test_an_upload_due_during_a_pause_waits_behind_the_resumed_drawing(desk):
    lib, ov = desk.lib, desk.overrides
    a, b = lib.add_upload(png(1), "a.png"), lib.add_upload(png(2), "b.png")
    now = D(2026, 3, 10, 12, 0)
    desk.add_known(a, Options(), now)
    desk.add_known(b, Options(when="14:00"), now)
    ov.pause(now + dt.timedelta(hours=1))
    ov.resume(now + dt.timedelta(hours=3), lib)            # 15:00: b's 14:00 start has passed
    assert schedule_full(now + dt.timedelta(hours=3), START, DUR, ov, lib)["hash"] == a
    waiting = [e for e in ov.entries(now + dt.timedelta(hours=3), lib) if e["hash"] == b]
    assert waiting and not waiting[0]["drawing"] and not waiting[0]["fixed"]


# ---- a look for one drawing only ----
def test_a_look_for_one_drawing_leaves_the_picture_as_it_was(desk):
    lib = desk.lib
    h = lib.add_upload(png(), "a.png")
    usual = lib.dir(h)
    now = D(2026, 3, 10, 12, 0)
    reply = desk.add_known(h, Options(medium="paint", fit="pad", when="next"), now)
    assert "as paint" in reply and reply.style == {"medium": "paint", "fit": "pad"}
    assert lib.dir(h) == usual and lib.look_for(h).medium == "pencil" and lib.data["looks"] == {}
    (entry,) = desk.overrides.entries(now, lib)
    assert entry["style"] == {"medium": "paint", "fit": "pad"}
    styled = lib.dir(h, entry["style"])
    assert styled != usual and "-pad-" in styled.name and "paint" in styled.name
    assert lib.gamma_for(h, entry["style"]) == 1.6
    assert desk.overrides.variants() == [(h, entry["style"])]
    fake_bake(lib, h, entry["style"])
    s = schedule_full(D(2026, 3, 11, 8, 0), START, DUR, desk.overrides, lib)
    assert s["style"] == entry["style"] and "medium=paint" in s["key"]


def test_a_style_that_matches_the_usual_is_not_kept(desk):
    h = desk.lib.add_upload(png(), "a.png")
    assert desk.add_known(h, Options(medium="pencil", fit="crop"), D(2026, 3, 10, 12)).style is None


def test_queued_looks_are_kept_by_prune_and_bad_ones_refused(desk):
    lib = desk.lib
    h = lib.add_upload(png(), "a.png")
    style = {"medium": "paint"}
    d = fake_bake(lib, h, style)
    assert lib.prune(keep=0) == [d.name]                   # nobody wants it
    d = fake_bake(lib, h, style)
    assert lib.prune(keep=0, variants=[(h, style)]) == [] and d.exists()
    for bad in ({"medium": "oil"}, {"detail": 9}, {"fit": "stretch"}, {"contrast": "lots"}):
        with pytest.raises(ValueError):
            clean_style(bad)
    with pytest.raises(BadRequest):
        submit(lib, desk.overrides, START, DUR, None, h, style={"detail": 3})


def test_fit_can_be_kept_for_a_picture(desk):
    lib = desk.lib
    h = lib.add_upload(png(), "a.png")
    lib.tune(h, fit="pad")
    assert lib.fit_for(h) == "pad" and "-pad-" in lib.dir(h).name and lib.look_for(h).medium == "pencil"
    lib.tune(h, fit="crop")
    assert lib.data["looks"] == {}


def test_words_for_fit_always_and_dates():
    o = parse_options("as paint fit pad always")
    assert (o.medium, o.fit, o.always) == ("paint", "pad", True)
    assert parse_options("fit crop").always is False
    assert parse_options("every 25 december at 09:00").every == {"month": 12, "day": 25}
    assert parse_options("every dec 25").every == {"month": 12, "day": 25}
    assert parse_options("monthly on the 1st").every == {"day": 1}
    assert parse_options("every 3rd at 18:00").every == {"day": 3}
    o = parse_options("every friday at 18:00")             # still a weekday, and the time is still the time
    assert (o.every, o.when) == ([4], "18:00")
    with pytest.raises(BadRequest):
        parse_options("every 31 february")


# ---- dated repeats ----
def test_a_yearly_and_a_monthly_repeat_come_round_on_their_dates(desk):
    lib, ov = desk.lib, desk.overrides
    h = lib.add_upload(png(), "tree.png")
    now = D(2026, 11, 9, 12, 0)
    reply = desk.add_known(h, Options(every={"month": 12, "day": 25}, when="08:00"), now)
    assert "every 25 December at 08:00" in reply and "Fri 25 Dec 08:00" in reply
    (r,) = ov.repeats.all()
    assert Repeats.next_time(r, now) == D(2026, 12, 25, 8, 0)
    assert Repeats.next_time(r, D(2026, 12, 25, 8, 1)) == D(2027, 12, 25, 8, 0)
    assert ov.entries(now, lib) == []                       # not within a day yet
    eve = D(2026, 12, 24, 9, 0)
    assert [e["start"] for e in ov.entries(eve, lib)] == ["2026-12-25T08:00:00"]
    monthly = {"id": "m", "hash": h, "days": [], "on": {"day": 31}, "time": "07:00"}
    assert Repeats.next_time(monthly, D(2026, 2, 1)) == D(2026, 3, 31, 7, 0)      # February has no 31st
    assert describe_repeat(monthly) == "on the 31st of every month"
    assert Repeats.next_time({"hash": h, "on": {"month": 2, "day": 29}, "time": "07:00"}, D(2026, 3, 1)) == D(2028, 2, 29, 7)
    with pytest.raises(BadRequest):
        desk.add_known(h, Options(every={"month": 2, "day": 30}), now)


# ---- favourites ----
def test_a_favourite_comes_round_twice_as_often_and_never_is_left_out(tmp_path):
    src = tmp_path / "pictures"
    src.mkdir()
    lib = Library(tmp_path / "cache", src, "crop")
    for i in range(3):
        make_png(src / f"{i}.png", seed=i)
    a, b, c = lib.scan()
    for h in (a, b, c):
        fake_bake(lib, h)
    lib.set_weight(a, "favourite")
    lib.set_weight(c, "never")
    picks = [lib.pick(dt.date(2026, 3, 1) + dt.timedelta(days=i)) for i in range(9)]
    assert c not in picks
    assert picks.count(a) == 6 and picks.count(b) == 3
    assert lib.reroll(dt.date(2026, 3, 9)) in (a, b)
    lib.set_weight(c, "normal")
    assert lib.pick(dt.date(2026, 3, 10)) == c              # never drawn: it goes first
    with pytest.raises(ValueError):
        lib.set_weight(a, "sometimes")


def test_the_channel_can_change_a_pictures_turn(desk):
    h = desk.lib.add_upload(png(), "a.png")
    assert "favourite" in desk.cycle_weight(h) and desk.lib.weight(h) == "favourite"
    assert "only when sent" in desk.cycle_weight(h) and "only when sent" in desk.pictures(0)[0][0][1]
    assert "usual turn" in desk.cycle_weight(h) and desk.lib.data["weights"] == {}


# ---- the disk guard ----
def test_a_nearly_full_disk_stops_uploads_and_processing_without_blaming_the_picture(tmp_path, capsys):
    src = tmp_path / "pictures"
    src.mkdir()
    lib = Library(tmp_path / "cache", src, "crop", min_free_mb=10 ** 9)
    with pytest.raises(ValueError, match="nearly full"):
        lib.add_upload(png(), "a.png")
    make_png(src / "b.png")
    (h,) = lib.scan()
    assert lib.bake(h) is False and lib.bake(h) is False
    assert capsys.readouterr().out.count("disk is nearly full") == 1          # said once
    assert lib.usable(h) and "nearly full" in lib.failures[h]                 # tried again when there is room
    assert Library(tmp_path / "c2", src, "crop", min_free_mb=0).low_disk() is False


# ---- panel modes ----
def test_four_grey_and_colour_frames_use_only_the_panels_values():
    sh = Sheet(1)
    sh.canvas[100:400, 100:900] = np.linspace(0.05, 0.95, 800, dtype=np.float32)
    g4 = frame_for(sh, (400, 240), "gray4")
    assert g4.mode == "L" and set(np.unique(np.asarray(g4))) == set(GRAY4)
    assert frame_for(sh, (400, 240), "mono").mode == "1" and frame_for(sh, (400, 240), "gray").mode == "L"
    from day_in_lines.paint import PaintSheet
    ps = PaintSheet(1)
    ps.canvas[:, :800] = (0.9, 0.1, 0.1)
    ps.cover[:] = 1
    col = frame_for(ps, (400, 240), "colour")
    assert col.mode == "P" and col.size == (400, 240)
    used = {tuple(col.getpalette()[i * 3:i * 3 + 3]) for i in np.unique(np.asarray(col))}
    assert used <= set(PALETTE7) and (255, 0, 0) in used
    assert frame_for(sh, (400, 240), "colour").mode == "P"  # pencil on a colour panel: its black and white


def test_frames_for_each_mode_are_stored_apart(tmp_path):
    src = tmp_path / "pictures"
    src.mkdir()
    make_png(src / "a.png")
    names = set()
    for mode in ("mono", "gray", "gray4", "colour"):
        lib = Library(tmp_path / "cache", src, "crop", mode=mode)
        (h,) = lib.scan() or list(lib.data["images"])
        names.add(lib.dir(h).name)
    assert len(names) == 4
    assert Library(tmp_path / "cache", src, "crop", mono=False).mode == "gray"
    with pytest.raises(ValueError):
        Library(tmp_path / "cache", src, "crop", mode="sepia")


class Recorder:
    size = (80, 48)

    def __init__(self):
        self.shown = []

    def show(self, img, full=False):
        self.shown.append((img.copy(), full))


def test_rotation_turns_the_frame_and_swaps_the_size():
    inner = Recorder()
    img = Image.new("1", (80, 48), 1)
    img.putpixel((0, 0), 0)
    up = Rotated(inner, 180)
    assert up.size == (80, 48)
    up.show(img, full=True)
    assert inner.shown[-1][0].getpixel((79, 47)) == 0 and inner.shown[-1][1] is True
    side = Rotated(inner, 90)
    assert side.size == (48, 80)
    side.show(Image.new("1", (48, 80), 1))
    assert inner.shown[-1][0].size == (80, 48)
    assert side.shown is inner.shown                        # the rest of the display shows through


def test_caption_marks_only_the_corner_and_keeps_the_mode():
    img = Image.new("1", (400, 240), 1)
    out = caption(img, "Boats, 9 November 2026")
    assert out.mode == "1" and out.size == img.size and img.getbbox() == (0, 0, 400, 240)
    dark = np.argwhere(np.asarray(out) == 0)
    assert len(dark) and dark[:, 0].min() > 200 and dark[:, 1].min() > 200
    assert caption(img, "") is img
    assert caption(Image.new("P", (400, 240)), "x").mode == "RGB"
    assert cli.caption_text("{name}, {date} ({day})", "Boats", D(2026, 11, 9, 8)) == "Boats, 9 November 2026 (Monday)"
    assert cli.caption_text("{oops}", "Boats", D(2026, 11, 9)) == "{oops}"


def test_waveshare_four_grey_and_colour_use_the_drivers_own_calls(monkeypatch):
    calls = []

    class EPD:
        width, height = 80, 48
        def init(self): calls.append("init")
        def init_4Gray(self): calls.append("init4")
        def getbuffer(self, img): calls.append(("buf", img.mode)); return b""
        def getbuffer_4Gray(self, img): calls.append(("buf4", img.mode)); return b""
        def display(self, buf): calls.append("display")
        def display_4Gray(self, buf): calls.append("display4")
        def sleep(self): calls.append("sleep")

    monkeypatch.setitem(sys.modules, "waveshare_epd", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "waveshare_epd.fake", types.SimpleNamespace(EPD=EPD))
    WaveshareDisplay("fake", mode="gray4").show(Image.new("L", (80, 48)))
    assert calls == ["init4", ("buf4", "L"), "display4", "sleep"]
    calls.clear()
    WaveshareDisplay("fake", mode="colour").show(Image.new("P", (80, 48)))
    assert calls == ["init", ("buf", "RGB"), "display", "sleep"]
    del EPD.init_4Gray
    with pytest.raises(ValueError, match="four-grey"):
        WaveshareDisplay("fake", mode="gray4")


def test_inky_boards_get_what_they_expect(monkeypatch):
    class Board:
        resolution, colour = (80, 48), "black"
        def set_image(self, img): self.img = img
        def show(self): self.showed = True

    board = Board()
    monkeypatch.setitem(sys.modules, "inky", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "inky.auto", types.SimpleNamespace(auto=lambda: board))
    d = InkyDisplay()
    img = Image.new("1", (80, 48), 1)
    img.putpixel((3, 4), 0)
    d.show(img)
    arr = np.asarray(board.img)
    assert d.size == (80, 48) and board.showed and arr[4, 3] == 1 and arr.sum() == 1      # 1 is black on an Inky
    board.colour = "multi"
    InkyDisplay().show(Image.new("P", (80, 48)))
    assert board.img.mode == "RGB"
    with pytest.raises(ValueError):
        InkyDisplay("what")


# ---- the clock loop: quiet hours, spacing updates, captions ----
@pytest.fixture
def clock(desk, tmp_path, monkeypatch):
    lib = desk.lib
    make_png(tmp_path / "pictures" / "a.png")
    (h,) = lib.scan()
    fake_bake(lib, h)
    disp, now = Recorder(), {"t": D(2026, 3, 10, 7, 0)}

    class Fake(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now["t"]

    monkeypatch.setattr(cli, "dt", types.SimpleNamespace(datetime=Fake))
    monkeypatch.setattr(desk.live.wake, "wait", lambda *a: None)
    args = argparse.Namespace(full_refresh_every=0, quiet_hours=None, update_every=1, caption=None)
    st = {"key": None, "shown": -1, "since_full": 0, "held": False}

    def at(hour, minute=0, day=10):
        now["t"] = D(2026, 3, day, hour, minute)
        cli.tick(args, disp, lib, desk.live, desk.overrides, START, DUR, st)
        return st["shown"]

    return types.SimpleNamespace(at=at, args=args, disp=disp, live=desk.live, st=st)


def test_quiet_hours_hold_the_panel_then_catch_up_with_a_full_refresh(clock):
    clock.args.quiet_hours = cli.parse_quiet("12:00-14:00")
    assert clock.at(11, 59) == 299 and len(clock.disp.shown) == 1
    assert clock.at(12, 30) == 299 and len(clock.disp.shown) == 1        # held
    assert clock.live.k == 330 and clock.live.quiet                      # but the page knows the truth
    assert clock.at(14, 0) == 420
    assert clock.disp.shown[-1][1] is True and not clock.live.quiet      # a full refresh to catch up
    assert clock.at(14, 1) == 421 and clock.disp.shown[-1][1] is False


def test_quiet_hours_across_midnight_and_a_fresh_start(clock):
    quiet = cli.parse_quiet("22:00-07:00")
    assert [cli.in_quiet(quiet, D(2026, 3, 10, h)) for h in (21, 22, 3, 7)] == [False, True, True, False]
    assert cli.in_quiet(None, D(2026, 3, 10, 3)) is False
    clock.args.quiet_hours = cli.parse_quiet("00:00-23:59")
    assert clock.at(9) == 120                                            # nothing shown yet: show it once anyway
    assert clock.at(10) == 120
    with pytest.raises(SystemExit):
        cli.parse_quiet("late")


def test_updates_can_be_spaced_out(clock):
    clock.args.update_every = 15
    assert clock.at(7, 7) == 0                                           # the clock is rounded down to :00, :15 ...
    assert clock.at(7, 14) == 0 and clock.at(7, 15) == 15 and clock.at(7, 29) == 15
    assert clock.at(19, 0) == TOTAL                                      # and the finished one


def test_a_caption_is_drawn_on_the_frame_shown(clock):
    clock.args.caption = "{name}"
    clock.at(9)
    assert np.asarray(clock.disp.shown[-1][0]).min() == 0


# ---- the web: status, health, the queue ----
@pytest.fixture
def web(desk):
    srv = serve(desk.live, 0, desk.overrides, START, DUR, desk.lib, upload_key="sesame", bind="127.0.0.1", desk=desk)
    srv.desk = desk
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path, body=body, headers={"Content-Length": str(len(body or b""))})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data)


def test_status_lists_the_queue_and_repeats_without_a_key(web):
    status, s = call(web, "GET", "/status.json")
    assert status == 200 and s["queue"] == [] and s["key"] is None and s["paused"] is False
    assert call(web, "POST", "/upload?key=sesame&start=next&medium=paint&fit=pad&from=40", png())[0] == 200
    assert call(web, "POST", "/upload?key=sesame&every=0,4&start=18:00", png(9))[0] == 200
    s = call(web, "GET", "/status.json")[1]
    assert [(e["name"], e["first"], e["style"]) for e in s["queue"]][0] == ("upload", 40, {"medium": "paint", "fit": "pad"})
    assert [(r["when"], r["time"]) for r in s["repeats"]] == [("every Monday, Friday", "18:00")]
    assert len(s["preparing"]) == 2


def test_upload_options_are_checked(web):
    for bad in ("&medium=oil", "&detail=7", "&fit=stretch", "&every=9", "&every=sometimes", "&every=02-30", "&every=day-0"):
        assert call(web, "POST", "/upload?key=sesame" + bad, png())[0] == 400, bad
    assert call(web, "POST", "/upload?key=sesame&every=12-25", png(3))[0] == 200
    assert call(web, "POST", "/upload?key=sesame&every=day-1&always=1&medium=paint", png(4))[0] == 200
    lib = web.desk.lib
    assert [look["medium"] for look in lib.data["looks"].values()] == ["paint"]     # `always` changed the picture itself


def test_the_queue_can_be_changed_from_the_page_only_with_the_key(web):
    call(web, "POST", "/upload?key=sesame&start=next", png())
    eid = call(web, "GET", "/status.json")[1]["queue"][0]["id"]
    for path in ("/cancel?id=" + eid, "/pause", "/resume", "/skip", "/restart", "/stop_repeat?id=x"):
        assert call(web, "POST", path)[0] == 403, path
    from urllib.parse import quote
    status, out = call(web, "POST", f"/cancel?key=sesame&id={quote(eid)}")
    assert status == 200 and "Cancelled upload" in out["text"]
    assert call(web, "GET", "/status.json")[1]["queue"] == []
    assert "Paused" in call(web, "POST", "/pause?key=sesame")[1]["text"]
    assert call(web, "GET", "/status.json")[1]["paused"] is True
    assert "Resumed" in call(web, "POST", "/resume?key=sesame")[1]["text"]
    assert call(web, "POST", "/restart?key=sesame")[0] == 400          # nothing on the display yet
    assert call(web, "POST", "/skip?key=sesame")[0] in (200, 400)      # the answer depends on the time of day (see test_slack)


def test_health_turns_bad_after_the_loop_has_failed_for_a_while(web):
    live = web.desk.live
    assert call(web, "GET", "/health") == (200, live.health()[1])
    live.error, live.error_since = "panel not answering", time.time() - 30
    assert call(web, "GET", "/health")[0] == 200                       # a blip
    live.error_since = time.time() - 600
    status, info = call(web, "GET", "/health")
    assert status == 503 and info["error"] == "panel not answering" and info["failing_for"] >= 600
    live.error = live.error_since = None
    live.last_ok = time.time() - 900                                   # the loop has stopped altogether
    assert call(web, "GET", "/health")[0] == 503
    live.last_ok = time.time()
    assert call(web, "GET", "/health")[0] == 200


def test_the_channel_hears_about_failures_once_and_about_recovery(desk):
    lib, live = desk.lib, desk.live
    h = lib.add_upload(png(), "a.png", by="U7")
    t = Troubles(live, lib)
    assert t.poll() == []
    lib.failures[h] = "cannot identify image file"
    (text,) = t.poll()
    assert "<@U7>" in text and "*a*" in text and "cannot identify" in text
    assert t.poll() == []
    live.error, live.error_since = "no panel", 1000.0
    assert t.poll(now=1100.0) == []                                    # not long enough to mention
    assert "failing for 10 minutes: no panel" in t.poll(now=1600.0)[0]
    assert t.poll(now=1700.0) == []
    live.error = live.error_since = None
    assert t.poll(now=1800.0) == ["The display is working again."]


def test_status_says_when_the_display_is_paused(desk):
    live = desk.live
    live.key, live.hash, live.name, live.k, live.paused = "d", "h", "Boats", 100, True
    live.finish = D(2026, 3, 10, 19)
    assert desk.status().endswith("Paused.")


# ---- timelapse, the packaged page ----
def test_timelapse_is_a_gif_of_the_stored_frames(desk):
    h = desk.lib.add_upload(png(), "a.png")
    assert timelapse_gif([desk.lib.frame(h, k) for k in (0, 360, 720)]) is None      # nothing stored yet
    fake_bake(desk.lib, h)
    for k in range(0, 721, 90):                           # each a little further on
        Image.new("L", (80, 48), 255 - k // 3).save(desk.lib.frame(h, k))
    gif = Image.open(io.BytesIO(timelapse_gif([desk.lib.frame(h, k) for k in range(0, 721, 90)], hold=3)))
    assert gif.format == "GIF" and gif.n_frames == 9 and gif.size == (400, 240)        # the hold is one long last frame


def test_the_viewer_page_ships_inside_the_package():
    page = viewer_page("[]")
    assert 'id="queue"' in page and 'id="more"' in page and "status.json" in page


# ---- feeds ----
RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><enclosure url="https://x.test/one.jpg" type="image/jpeg"/></item>
<item><media:content url="/two.png" medium="image"/></item>
<item><description>&lt;p&gt;&lt;img src="https://x.test/three.webp"&gt;&lt;/p&gt;</description></item>
<item><enclosure url="https://x.test/talk.mp3" type="audio/mpeg"/></item>
<item><enclosure url="ftp://x.test/four.jpg" type="image/jpeg"/></item>
</channel></rss>"""


def test_pictures_are_found_in_feeds_and_lists():
    assert feeds.image_links(RSS, "https://x.test/feed") == ["https://x.test/one.jpg", "https://x.test/two.png",
                                                             "https://x.test/three.webp"]
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="enclosure" type="image/png" href="a.png"/>' \
           b'<link rel="alternate" href="page.png"/></entry></feed>'
    assert feeds.image_links(atom, "https://x.test/f/") == ["https://x.test/f/a.png"]
    listing = b"# mine\nhttps://x.test/a.jpg\n\nnot a link\nhttps://x.test/a.jpg\nhttp://x.test/b.jpg\n"
    assert feeds.image_links(listing, "https://x.test/") == ["https://x.test/a.jpg", "http://x.test/b.jpg"]
    with pytest.raises(ValueError):
        feeds.image_links(b"<rss><channel>", "https://x.test/")
    with pytest.raises(ValueError):
        feeds.get("file:///etc/passwd", 100)


def test_a_feed_adds_new_pictures_a_few_at_a_time_and_only_once(desk, tmp_path, monkeypatch):
    lib = desk.lib
    pages = {"https://x.test/feed": (RSS, "application/rss+xml"), "https://x.test/one.jpg": (png(1), "image/png"),
             "https://x.test/two.png": (b"not a picture", "image/png"), "https://x.test/three.webp": (png(3), "image/png"),
             "https://x.test/today.jpg": (png(7), "image/jpeg")}
    fetched = []

    def get(url, limit):
        fetched.append(url)
        return pages[url]

    monkeypatch.setattr(feeds, "get", get)
    f = feeds.Feeds(lib, tmp_path / "cache", ["https://x.test/feed", "https://x.test/today.jpg"], limit=1)
    assert len(f.check_all()) == 2                         # one.jpg, and the picture at today.jpg
    assert len(f.check_all()) == 1                         # two.png was not a picture; three.webp, held back the first time
    fetched.clear()
    assert f.check_all() == [] and fetched == ["https://x.test/feed", "https://x.test/today.jpg"]
    pages["https://x.test/today.jpg"] = (png(8), "image/jpeg")
    assert len(f.check_all()) == 1                         # the address has a new picture today
    assert len(lib.data["images"]) == 4
    again = feeds.Feeds(lib, tmp_path / "cache", ["https://x.test/feed"])
    assert again.check_all() == []                         # what was seen is remembered across restarts


# ---- iPhone photos ----
def test_heic_files_are_scanned_only_when_they_can_be_opened(monkeypatch):
    from day_in_lines import library
    monkeypatch.setattr(library, "HEIC", False)
    assert ".heic" not in library.image_exts()
    monkeypatch.setattr(library, "HEIC", True)
    assert {".heic", ".heif", ".png"} <= library.image_exts()


def test_the_package_runs_as_a_module(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, "-m", "day_in_lines", "check", "--out", str(tmp_path / "c.png"), "--size", "100x60",
                        "--mode", "gray4", "--rotate", "180"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(cli.Path(cli.__file__).parent.parent)}, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert set(np.unique(np.asarray(Image.open(tmp_path / "c.png")))) <= set(GRAY4)
