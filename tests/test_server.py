import http.client
import io
import json
import datetime as dt

import pytest
from PIL import Image

from day_in_lines.library import Library
from day_in_lines.schedule import Overrides
from day_in_lines.server import Live, serve


def png(size=(60, 40), color=120, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, color if mode != "RGB" else (color,) * 3).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def server(tmp_path):
    src = tmp_path / "pictures"
    src.mkdir()
    lib = Library(tmp_path / "cache", src, "crop")
    live = Live()
    start, dur = dt.time(7), dt.timedelta(hours=12)
    ov = Overrides(tmp_path / "cache", start, dur)
    srv = serve(live, 0, ov, start, dur, lib, upload_key="sesame", bind="127.0.0.1")
    srv.lib, srv.ov, srv.live = lib, ov, live
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def upload(srv, body, key="sesame", query="", name="pic.png"):
    q = f"?key={key}" if key is not None else "?"
    return call(srv, "POST", "/upload" + q + query, body, {"X-Filename": name, "Content-Length": str(len(body))})


def test_upload_with_the_key_is_accepted_and_queued(server):
    status, data = upload(server, png())
    assert status == 200
    out = json.loads(data)
    assert out["ok"] and out["now"] is True
    assert len(server.lib.data["images"]) == 1
    assert len(server.ov.hashes()) == 1


def test_upload_for_the_next_start_is_queued_not_started(server):
    status, data = upload(server, png(), query="&start=next")
    assert status == 200 and json.loads(data)["now"] is False


@pytest.mark.parametrize("key", [None, "", "wrong", "sesam", "sesame-and-more"])
def test_upload_without_the_right_key_is_refused(server, key):
    status, _ = upload(server, png(), key=key)
    assert status == 403
    assert server.lib.data["images"] == {}


def test_upload_that_is_not_an_image_is_a_400(server):
    status, data = upload(server, b"hello there")
    assert status == 400 and "error" in json.loads(data)


def test_upload_of_a_huge_picture_is_a_400(server):
    status, data = upload(server, png((9000, 9000), 0, "1"))
    assert status == 400 and "too large" in json.loads(data)["error"]


def test_upload_with_bad_times_is_a_400(server):
    assert upload(server, png(), query="&start=teatime")[0] == 400
    assert upload(server, png(color=9), query="&end=25:99")[0] == 400


def test_empty_and_malformed_content_length(server):
    assert call(server, "POST", "/upload?key=sesame", b"", {"Content-Length": "0"})[0] == 413
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    c.putrequest("POST", "/upload?key=sesame")
    c.putheader("Content-Length", "lots")
    c.endheaders()
    assert c.getresponse().status == 400
    c.close()


def test_oversized_content_length_is_refused_before_reading(server):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    c.putrequest("POST", "/upload?key=sesame")
    c.putheader("Content-Length", str(10 ** 9))
    c.endheaders()
    assert c.getresponse().status == 413
    c.close()


def test_unknown_paths_are_404(server):
    assert call(server, "GET", "/nope")[0] == 404
    assert call(server, "POST", "/elsewhere", b"x", {"Content-Length": "1"})[0] == 404


def test_viewer_waits_for_the_first_picture(server):
    status, data = call(server, "GET", "/")
    assert status == 503
    status, data = call(server, "GET", "/state.json")
    assert status == 200 and json.loads(data) == {"key": None, "k": 0}


def test_viewer_page_is_served_for_the_live_picture(server, tmp_path):
    import datetime
    lib, live = server.lib, server.live
    h = "abc123"
    lib.data["images"][h] = {"path": "x", "name": "x", "added": "", "drawn": []}
    (lib.dir(h)).mkdir(parents=True)
    (lib.dir(h) / "viewer.json").write_text("[]")
    with live.lock:
        live.key, live.hash, live.k, live.name = "day:1", h, 5, "ship"
        live.started = datetime.datetime(2026, 3, 10, 7)
        live.finish = datetime.datetime(2026, 3, 10, 19)
    status, data = call(server, "GET", "/")
    assert status == 200 and b"ship" in data and b"/*LIVE*/null" not in data
    assert json.loads(call(server, "GET", "/state.json")[1]) == {"key": "day:1", "k": 5}


def test_a_stalled_upload_does_not_block_other_requests(server):
    import socket
    s = socket.create_connection(("127.0.0.1", server.server_address[1]))
    s.sendall(b"POST /upload?key=sesame HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n\r\nabc")
    assert call(server, "GET", "/state.json")[0] == 200       # threads: others are served meanwhile
    s.close()
