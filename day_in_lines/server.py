"""The web viewer and upload endpoint."""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

from .constants import MAX_UPLOAD_BYTES, TOTAL
from .export import fill_live, viewer_page
from .library import Library
from .schedule import Overrides, next_start


class Live:
    """What the display is drawing, shared with the web page."""
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.key = None
        self.hash = None
        self.k = 0
        self.name = ""
        self.started = None
        self.finish = None
        self.html = None


def serve(live: Live, port: int, overrides: Overrides, start: dt.time, dur: dt.timedelta, lib: Library,
          upload_key=None, bind="0.0.0.0"):
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, unquote

    class H(BaseHTTPRequestHandler):
        timeout = 30                      # a stalled upload must not hold a thread forever

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = urlparse(self.path).path
            with live.lock:
                if path in ("/", "/index.html"):
                    if live.key is None:
                        return self._send(503, "<p>The display is still breaking down its first picture. Reload in a minute.</p>",
                                          "text/html")
                    if live.html is None or live.html[0] != live.key:
                        plan = (lib.dir(live.hash) / "viewer.json").read_text()
                        live.html = (live.key, viewer_page(plan))
                    info = dict(key=live.key, k=live.k, image=live.name,          # the minute is filled in fresh
                                start=live.started.strftime("%H:%M"), startMin=live.started.hour * 60 + live.started.minute,
                                finish=live.finish.strftime("%H:%M"),
                                perMin=(live.finish - live.started).total_seconds() / 60 / TOTAL,
                                next=start.strftime("%H:%M"), needsKey=bool(upload_key))
                    return self._send(200, fill_live(live.html[1], info), "text/html; charset=utf-8")
                if path == "/state.json":
                    return self._send(200, json.dumps(dict(key=live.key, k=live.k)))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/upload":
                return self._send(404, '{"error":"not found"}')
            q = parse_qs(u.query)
            if upload_key and not hmac.compare_digest(q.get("key", [""])[0].encode(), upload_key.encode()):
                return self._send(403, '{"error":"Sending pictures needs the key: open the page as /?key=..."}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, '{"error":"Bad Content-Length."}')
            if not 0 < n <= MAX_UPLOAD_BYTES:
                return self._send(413, json.dumps({"error": f"Images up to {MAX_UPLOAD_BYTES // 1_000_000} MB, please."}))
            try:
                data = self.rfile.read(n)
            except OSError:                       # timed out or dropped part-way
                return
            if len(data) != n:
                return self._send(400, '{"error":"The upload was cut short."}')
            name = Path(unquote(self.headers.get("X-Filename", "upload.jpg"))).name or "upload.jpg"
            try:
                h = lib.add_upload(data, name)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            when, until = q.get("start", ["now"])[0], q.get("end", [""])[0]
            now = dt.datetime.now().replace(second=0, microsecond=0)
            try:
                if when == "now":
                    begin, fixed = now, True
                elif when == "next":
                    begin, fixed = next_start(now + dt.timedelta(minutes=1), start), False
                else:                                  # a chosen time: the next time it comes round
                    begin, fixed = next_start(now, dt.datetime.strptime(when, "%H:%M").time()), True
                fin = (next_start(begin + dt.timedelta(minutes=1), dt.datetime.strptime(until, "%H:%M").time())
                       if until else begin + dur)
            except ValueError:
                return self._send(400, '{"error":"Times must look like 14:30."}')
            begin, fin = overrides.add(h, begin, fin, fixed)
            live.wake.set()
            self._send(200, json.dumps({"ok": True, "start": begin.strftime("%a %H:%M"), "end": fin.strftime("%a %H:%M"),
                                        "now": begin <= now}))

    srv = ThreadingHTTPServer((bind, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"viewer at http://<this-pi>:{srv.server_address[1]}/", flush=True)
    return srv
