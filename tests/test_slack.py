import datetime as dt
import io
import json

import pytest
from PIL import Image

from day_in_lines.library import Library
from day_in_lines.schedule import Overrides, schedule
from day_in_lines.server import Live
from day_in_lines.slack_app import Desk, browse_blocks, Options, options_from_form, parse_options, setup_modal
from day_in_lines.submit import BadRequest

D = dt.datetime


def png():
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (120, 120, 120)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def desk(tmp_path):
    (tmp_path / "pictures").mkdir()
    lib = Library(tmp_path / "cache", tmp_path / "pictures", "crop")
    start, dur = dt.time(7), dt.timedelta(hours=12)
    return Desk(lib, Overrides(tmp_path / "cache", start, dur), start, dur, Live())


def test_plain_message_has_no_options():
    assert parse_options("here you go") == Options()
    assert parse_options("<@U1> a picture").given is False


@pytest.mark.parametrize("text,when,until,first", [
    ("from 300", "now", "", 300),
    ("start from event 40 next", "next", "", 40),
    ("from shading", "now", "", 270),
    ("at 14:30", "14:30", "", 0),
    ("start at 9:15 finish by 22:00 from linework", "9:15", "22:00", 40),
    ("tomorrow from 100", "next", "", 100),
])
def test_options_in_a_message(text, when, until, first):
    o = parse_options(text)
    assert (o.when, o.until, o.first, o.given) == (when, until, first, True)


def test_phase_names_follow_the_medium():
    assert parse_options("from details", "paint").first == 530
    assert parse_options("from details", "pencil").first == 670


def test_unknown_spot_is_refused():
    with pytest.raises(BadRequest):
        parse_options("from banana")


def test_starting_spot_is_on_the_sheet_when_it_starts(desk):
    now = D(2026, 3, 10, 12, 0)
    desk.add(png(), "a.png", Options(first=300), now=now)
    _, h, k, began, fin = schedule(now, dt.time(7), dt.timedelta(hours=12), desk.overrides, desk.lib)
    assert k == 300
    # the remaining 420 events take 420 minutes at the usual pace
    assert schedule(now + dt.timedelta(minutes=60), dt.time(7), dt.timedelta(hours=12), desk.overrides, desk.lib)[2] == 360
    assert fin == now + dt.timedelta(minutes=420)
    assert schedule(fin - dt.timedelta(minutes=1), dt.time(7), dt.timedelta(hours=12), desk.overrides, desk.lib)[2] == 719


def test_spot_out_of_range_is_refused(desk):
    for bad in (-1, 720):
        with pytest.raises(BadRequest):
            desk.add(png(), "a.png", Options(first=bad))


def test_replies_say_what_will_happen(desk):
    now = D(2026, 3, 10, 12, 0)
    assert "Started" in desk.add(png(), "a.png", Options(first=300), now=now)
    assert "event 300" in desk.add(png(), "b.png", Options(first=300), now=now)
    assert "Queued" in desk.add(png(), "c.png", Options(when="next"), now=now)


def test_not_an_image_is_refused(desk):
    with pytest.raises(BadRequest):
        desk.add(b"nope", "a.png", Options())


def test_form_values_become_options():
    v = {"when": {"v": {"selected_option": {"value": "at"}}}, "at": {"v": {"selected_time": "14:30"}},
         "until": {"v": {"selected_time": "22:00"}}, "phase": {"v": {"selected_option": {"value": "270"}}},
         "event": {"v": {"value": None}}}
    o = options_from_form(v)
    assert (o.when, o.until, o.first) == ("14:30", "22:00", 270)
    v["event"]["v"]["value"] = "123"
    assert options_from_form(v).first == 123
    v["at"]["v"]["selected_time"] = None
    with pytest.raises(BadRequest):
        options_from_form(v)


def test_modal_is_well_formed():
    m = setup_modal("paint", {"file": "F1", "thread": "1.2"})
    assert [b["block_id"] for b in m["blocks"]] == ["when", "at", "until", "phase", "event", "repeat", "medium",
                                                     "detail", "contrast"]


def test_browse_pages_through_the_library(desk):
    for c in range(7):
        desk.lib.add_upload(_png(c), f"p{c}.png")
    rows, prev, more, page, total = desk.pictures(0)
    assert (len(rows), prev, more, total) == (5, False, True, 7)
    rows2, prev2, more2, page2, _ = desk.pictures(1)
    assert (len(rows2), prev2, more2) == (2, True, False)
    assert desk.pictures(99)[3] == 1                       # past the end shows the last page
    assert {h for h, _ in rows}.isdisjoint(h for h, _ in rows2)


def test_browse_blocks_and_choosing_a_known_picture(desk):
    assert "No pictures" in browse_blocks(desk, 0)[0]["text"]["text"]
    h = desk.lib.add_upload(_png(5), "mine.png")
    blocks = browse_blocks(desk, 0)
    assert [e["action_id"] for e in blocks[2]["elements"]] == ["pick", "preview", "delete"]
    assert blocks[2]["elements"][0]["value"] == h
    now = D(2026, 3, 10, 12, 0)
    reply = desk.add_known(h, Options(first=100), now=now)
    assert "mine" in reply and "event 100" in reply
    assert schedule(now, dt.time(7), dt.timedelta(hours=12), desk.overrides, desk.lib)[1] == h
    with pytest.raises(BadRequest):
        desk.add_known("nope", Options())


def _png(c):
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (c * 30, 90, 90)).save(buf, "PNG")
    return buf.getvalue()


# ---- the rest of the channel's features ----
from day_in_lines.slack_app import Progress, queue_blocks  # noqa: E402

DUR = dt.timedelta(hours=12)
START = dt.time(7)


def sched(desk, now):
    return schedule(now, START, DUR, desk.overrides, desk.lib)


def test_options_for_repeats_and_tuning():
    o = parse_options("every friday at 18:00 as paint detail 1.5 contrast 0.8 from details")
    assert (o.every, o.when, o.medium, o.detail, o.contrast, o.first) == ([4], "18:00", "paint", 1.5, 0.8, 530)
    assert parse_options("every mon, wed and fri").every == [0, 2, 4]
    assert parse_options("daily").every == list(range(7))
    assert parse_options("every weekday").every == [0, 1, 2, 3, 4]
    for bad in ("every blursday", "detail 5", "as paint contrast 0.1"):
        with pytest.raises(BadRequest):
            parse_options(bad)
    assert parse_options("my painting").given is False          # not a request to change the medium


def test_picture_given_its_own_look_is_made_again_for_it(desk):
    h = desk.lib.add_upload(_png(1), "a.png")
    before = desk.lib.dir(h)
    desk.add_known(h, Options(medium="paint", detail=1.5), now=D(2026, 3, 10, 12))
    assert desk.lib.look_for(h).medium == "paint" and desk.lib.look_for(h).detail == 1.5
    assert desk.lib.dir(h) != before and desk.medium_of(h) == "paint"
    assert desk.lib.gamma_for(h) == 1.6                           # paint's usual, not pencil's
    desk.add_known(h, Options(medium="pencil", detail=1.0))
    assert desk.lib.dir(h) == before and h not in desk.lib.data["looks"]
    with pytest.raises(BadRequest):
        desk.add_known(h, Options(detail=9))


def test_repeat_puts_each_occurrence_on_the_schedule_a_day_ahead(desk):
    h = desk.lib.add_upload(_png(2), "weekly.png")
    friday = D(2026, 3, 13, 18)                                  # a Friday
    r = desk.add_known(h, Options(when="18:00", every=[4], first=100), now=D(2026, 3, 11, 12))
    assert "every Friday at 18:00" in r
    assert desk.overrides.entries(D(2026, 3, 11, 12), desk.lib) == []          # more than a day away
    entries = desk.overrides.entries(D(2026, 3, 12, 19), desk.lib)
    assert [e["start"] for e in entries] == [friday.isoformat()] and entries[0]["first"] == 100
    desk.overrides.repeats.apply(D(2026, 3, 12, 20), desk.lib)                 # idempotent
    assert len(desk.overrides.entries(D(2026, 3, 12, 20), desk.lib)) == 1
    assert sched(desk, friday)[2] == 100                                       # it opens at its spot


def test_cancelling_one_occurrence_leaves_the_repeat(desk):
    h = desk.lib.add_upload(_png(2), "weekly.png")
    desk.add_known(h, Options(when="18:00", every=list(range(7))), now=D(2026, 3, 11, 12))
    now = D(2026, 3, 11, 17)
    (e,) = desk.overrides.entries(now, desk.lib)               # today's 18:00
    assert "repeat carries on" in desk.cancel(e["id"])
    assert desk.overrides.entries(now, desk.lib) == []                         # not put straight back
    assert len(desk.overrides.entries(D(2026, 3, 11, 19), desk.lib)) == 1      # but tomorrow's is
    rid = desk.overrides.repeats.all()[0]["id"]
    assert "Stopped" in desk.stop_repeat(rid, D(2026, 3, 11, 19))
    assert desk.overrides.entries(D(2026, 3, 11, 19), desk.lib) == []
    assert "already" in desk.stop_repeat(rid)


def test_queue_lists_and_cancels(desk):
    now = D(2026, 3, 10, 12)
    assert "Nothing queued" in queue_blocks(desk, now)[0]["text"]["text"]
    a = desk.lib.add_upload(_png(1), "a.png")
    b = desk.lib.add_upload(_png(2), "b.png")
    desk.add_known(a, Options(), now=now, by="U9")
    desk.add_known(b, Options(when="next"), now=now)
    blocks = queue_blocks(desk, now)
    text = " ".join(x["text"]["text"] for x in blocks)
    assert "on the display now" in text and "<@U9>" in text and "starts" in text
    ids = [x["accessory"]["value"] for x in blocks[1:]]
    assert "Cancelled" in desk.cancel(ids[1])
    assert "already" in desk.cancel(ids[1])
    assert len(desk.overrides.entries(now, desk.lib)) == 1


def test_skip_drops_the_upload_on_the_sheet(desk):
    now = D(2026, 3, 10, 12)
    desk.add_known(desk.lib.add_upload(_png(1), "a.png"), Options(), now=now)
    assert "Skipped" in desk.skip(now)
    assert desk.overrides.entries(now, desk.lib) == []


def test_skip_without_an_upload_changes_the_days_picture(desk, tmp_path):
    a, b = desk.lib.add_upload(_png(1), "a.png"), desk.lib.add_upload(_png(2), "b.png")
    now = D(2026, 3, 10, 12)
    with pytest.raises(BadRequest):                       # nothing is baked, so there is nothing to switch to
        desk.skip(now)
    for h in (a, b):
        (desk.lib.dir(h)).mkdir(parents=True)
        (desk.lib.dir(h) / "meta.json").write_text("{}")
    cur = desk.lib.pick(now.date())
    desk.skip(now)
    assert desk.lib.pick(now.date()) not in (None, cur)


def test_restart_begins_the_displayed_picture_again(desk):
    now = D(2026, 3, 10, 12)
    with pytest.raises(BadRequest):
        desk.restart(now)
    h = desk.lib.add_upload(_png(1), "a.png")
    desk.live.key, desk.live.hash = "k", h
    desk.restart(now)
    assert sched(desk, now)[1:3] == (h, 0)


def test_delete_removes_a_picture_but_not_one_in_use(desk):
    h = desk.lib.add_upload(_png(1), "gone.png")
    path = desk.lib.data["images"][h]["path"]
    desk.add_known(h, Options(), now=D(2026, 3, 10, 12))
    with pytest.raises(BadRequest):
        desk.delete(h)                                    # queued
    desk.overrides.cancel(desk.overrides.entries(D(2026, 3, 10, 12), desk.lib)[0]["id"])
    assert desk.delete(h) == "gone"
    assert h not in desk.lib.data["images"] and not __import__("pathlib").Path(path).exists()
    with pytest.raises(BadRequest):
        desk.delete(h)


def test_sender_is_remembered(desk):
    h = desk.add(_png(3), "x.png", Options(), by="U7").hash
    assert desk.lib.data["images"][h]["by"] == "U7"
    assert "<@U7>" in desk.pictures(0)[0][0][1]


def test_previews_are_made_from_stored_frames(desk):
    h = desk.lib.add_upload(_png(1), "a.png")
    assert desk.spots(h) is None and desk.thumbnails(0) is None       # nothing prepared yet
    (desk.lib.dir(h) / "frames").mkdir(parents=True)
    for k in (180, 360, 540, 720):
        Image.new("1", (800, 480), 1).save(desk.lib.frame(h, k))
    png = Image.open(io.BytesIO(desk.spots(h)))
    assert png.width > 640 and png.height > 384
    assert desk.thumbnails(0) is not None


def test_progress_announces_changes_only(desk):
    live = desk.live
    p = Progress(live, desk.lib)
    assert p.poll() == []                                              # nothing on yet
    live.key, live.hash, live.name, live.k = "d1", "h", "Boats", 0
    assert p.poll() == []                                              # what was already going isn't announced
    live.k = 30
    assert p.poll() == []
    live.k = 41
    assert "moves on to linework" in p.poll()[0]
    live.k = 720
    assert "complete" in " ".join(p.poll()) and p.poll() == []
    live.key, live.k = "d2", 0
    assert p.poll() == ["Now drawing *Boats* on a blank sheet."]
    live.key, live.k = "u1", 300
    assert "already at event 300 (shading)" in p.poll()[0]


def test_form_with_repeat_and_look():
    v = {"when": {"v": {"selected_option": {"value": "next"}}}, "at": {"v": {"selected_time": "18:30"}},
         "repeat": {"v": {"selected_option": {"value": "4"}}}, "medium": {"v": {"selected_option": {"value": "paint"}}},
         "detail": {"v": {"value": "1.5"}}, "contrast": {"v": {"value": None}}}
    o = options_from_form(v)
    assert (o.when, o.every, o.medium, o.detail, o.contrast) == ("18:30", [4], "paint", 1.5, None)
    v["repeat"]["v"]["selected_option"] = {"value": "wd"}
    assert options_from_form(v).every == [0, 1, 2, 3, 4]


# ---- the Slack wiring, against a stand-in for slack_bolt ----
import sys  # noqa: E402
import types  # noqa: E402
from unittest import mock  # noqa: E402

from day_in_lines import slack_app  # noqa: E402


class FakeApp:
    def __init__(self, token):
        self.client, self.handlers = mock.MagicMock(), {}

    def _reg(self, kind):
        return lambda name: (lambda fn: self.handlers.__setitem__((kind, name), fn) or fn)

    def event(self, name): return self._reg("event")(name)
    def action(self, name): return self._reg("action")(name)
    def view(self, name): return self._reg("view")(name)


@pytest.fixture
def bot(desk, monkeypatch):
    holder = {}

    def make_app(token):
        holder["app"] = FakeApp(token)
        return holder["app"]

    monkeypatch.setitem(sys.modules, "slack_bolt", types.SimpleNamespace(App=make_app))
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode",
                        types.SimpleNamespace(SocketModeHandler=lambda app, tok: mock.MagicMock()))
    monkeypatch.setattr(slack_app, "download", lambda url, tok: _png(4))
    slack_app.start_slack(desk, "xoxb", "xapp", "C1")
    app = holder["app"]
    app.desk = desk
    app.say = lambda: [c.kwargs for c in app.client.chat_postMessage.call_args_list]
    return app


def message(text="", files=(), channel="C1", user="U1", **kw):
    return dict(channel=channel, user=user, text=text, ts="1.0", files=list(files), **kw)


PIC = {"id": "F1", "name": "boat.png", "mimetype": "image/png", "url_private": "https://x/boat.png"}


def test_picture_with_words_is_queued_from_the_spot(bot):
    bot.handlers["event", "message"](message("from shading every friday at 18:00", [PIC], subtype="file_share"))
    (msg,) = bot.say()
    assert "Repeating *boat*" in msg["text"] and "event 270" in msg["text"] and msg["thread_ts"] == "1.0"
    assert bot.desk.lib.data["images"][next(iter(bot.desk.lib.data["images"]))]["by"] == "U1"


def test_other_channels_and_bots_are_ignored(bot):
    h = bot.handlers["event", "message"]
    h(message("help", channel="C2"))
    h(message("help", bot_id="B1"))
    assert bot.say() == []


def test_bare_picture_gets_buttons_then_the_form(bot):
    bot.handlers["event", "message"](message("", [PIC], subtype="file_share"))
    (msg,) = bot.say()
    ids = [e["action_id"] for e in msg["blocks"][1]["elements"]]
    assert ids == ["start_now", "choose"]
    body = {"channel": {"id": "C1"}, "user": {"id": "U1"}, "message": {"ts": "1.0"}, "trigger_id": "T",
            "actions": [{"value": "F1"}]}
    bot.client.files_info.return_value = {"file": PIC}
    bot.handlers["action", "choose"](lambda: None, body)
    view = bot.client.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "setup" and json.loads(view["private_metadata"])["file"] == "F1"
    values = {"when": {"v": {"selected_option": {"value": "next"}}}, "event": {"v": {"value": "200"}}}
    errors = []
    bot.handlers["view", "setup"](lambda **kw: errors.append(kw), {"user": {"id": "U1"}},
                                  {"state": {"values": values}, "private_metadata": view["private_metadata"]})
    assert "Queued *boat* from event 200" in bot.say()[-1]["text"]


def test_commands(bot):
    h = bot.handlers["event", "message"]
    for word in ("help", "status", "queue", "browse", "?"):
        h(message(word))
    texts = [m["text"] for m in bot.say()]
    assert texts[0].startswith("Post a picture") and "still breaking down" in texts[1]
    assert texts[2] == "The queue" and texts[3] == "The pictures on the display" and texts[4].startswith("Post a picture")
    h(message("skip"))
    assert ":warning:" in bot.say()[-1]["text"]            # nothing to switch to; said so, no crash


def test_only_listed_users_can_change_the_display(desk, monkeypatch):
    holder = {}
    monkeypatch.setitem(sys.modules, "slack_bolt", types.SimpleNamespace(App=lambda token: holder.setdefault("a", FakeApp(token))))
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode",
                        types.SimpleNamespace(SocketModeHandler=lambda a, t: mock.MagicMock()))
    monkeypatch.setattr(slack_app, "download", lambda u, t: _png(4))
    slack_app.start_slack(desk, "x", "y", "C1", users=["U1"])
    app = holder["a"]
    app.handlers["event", "message"](message("now", [PIC], user="U2", subtype="file_share"))
    assert "Only <@U1>" in app.client.chat_postMessage.call_args.kwargs["text"]
    assert desk.lib.data["images"] == {}
    app.handlers["event", "message"](message("status", user="U2"))          # looking is open to all
    assert "still breaking down" in app.client.chat_postMessage.call_args.kwargs["text"]


def test_delete_asks_first(bot):
    h = bot.desk.lib.add_upload(_png(8), "old.png")
    body = {"channel": {"id": "C1"}, "user": {"id": "U1"}, "message": {"ts": "1.0"}, "actions": [{"value": h}]}
    bot.handlers["action", "delete"](lambda: None, body)
    assert h in bot.desk.lib.data["images"]                 # not yet
    assert bot.say()[-1]["blocks"][1]["elements"][0]["confirm"]
    bot.handlers["action", "delete_yes"](lambda: None, body)
    assert h not in bot.desk.lib.data["images"] and "Deleted *old*" in bot.say()[-1]["text"]


def test_plural_day_names():
    assert parse_options("every fridays at 18:00").every == [4]
    assert parse_options("every tuesdays and thursdays").every == [1, 3]
