"""The web viewer and upload endpoint."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

from .constants import MAX_UPLOAD_BYTES, TOTAL
from .export import fill_live, viewer_page
from .library import Library
from .schedule import Overrides
from .submit import BadRequest, submit


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
        self.style = None                 # the look this one drawing was given, if any
        self.paused = False
        self.quiet = False                # quiet hours: the panel is holding its last frame
        self.error = None                 # what the display loop last failed on, while it is failing
        self.error_since = None           # time.time() when it began failing
        self.last_ok = None               # time.time() of the last good tick

    def health(self, now=None, stale=300) -> tuple[bool, dict]:
        """Is the display loop alive and well: (ok, details)."""
        import time
        now = now or time.time()
        with self.lock:
            failing = self.error_since is not None and now - self.error_since >= stale
            stuck = self.last_ok is not None and now - self.last_ok >= stale and self.error_since is None
            info = dict(ok=not (failing or stuck), drawing=self.name or None, k=self.k, paused=self.paused,
                        error=self.error, failing_for=int(now - self.error_since) if self.error_since else 0,
                        last_ok_ago=int(now - self.last_ok) if self.last_ok else None)
        return info["ok"], info


def serve(live: Live, port: int, overrides: Overrides, start: dt.time, dur: dt.timedelta, lib: Library,
          upload_key=None, bind="0.0.0.0", desk=None):
    import hmac
    if desk is None:
        from .slack_app import Desk       # the same actions the Slack channel has; no Slack needed
        desk = Desk(lib, overrides, start, dur, live)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, unquote

    class H(BaseHTTPRequestHandler):
        timeout = 30                      # a stalled upload must not hold a thread forever

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):       # the client went away; nothing to tell it
                pass

        def do_GET(self):
            path = urlparse(self.path).path
            with live.lock:
                if path in ("/", "/index.html"):
                    if live.key is None:
                        return self._send(503, "<p>The display is still breaking down its first picture. Reload in a minute.</p>",
                                          "text/html")
                    if live.html is None or live.html[0] != live.key:
                        plan = (lib.dir(live.hash, live.style) / "viewer.json").read_text()
                        live.html = (live.key, viewer_page(plan))
                    info = dict(key=live.key, k=live.k, image=live.name,          # the minute is filled in fresh
                                start=live.started.strftime("%H:%M"), startMin=live.started.hour * 60 + live.started.minute,
                                finish=live.finish.strftime("%H:%M"),
                                perMin=(live.finish - live.started).total_seconds() / 60 / TOTAL,
                                next=start.strftime("%H:%M"), needsKey=bool(upload_key),
                                medium=lib.look_for(live.hash, live.style).medium, fit=lib.fit, defaults=dict(
                                    medium=lib.look.medium, detail=lib.look.detail, contrast=lib.look.contrast))
                    return self._send(200, fill_live(live.html[1], info), "text/html; charset=utf-8")
                if path == "/state.json":
                    return self._send(200, json.dumps(dict(key=live.key, k=live.k)))
                m = re.fullmatch(r"/frame/(\d{1,3})\.png", path)       # the stored frame, as the panel shows it
                if m and live.hash is not None and int(m.group(1)) <= TOTAL:
                    f = lib.frame(live.hash, int(m.group(1)), live.style)
                    if f.is_file():
                        return self._send(200, f.read_bytes(), "image/png")
            if path == "/status.json":                  # outside the lock: it reads the live state itself
                return self._send(200, json.dumps(desk.status_data()))
            if path == "/health":
                ok, info = live.health()
                return self._send(200 if ok else 503, json.dumps(info))
            self._send(404, '{"error":"not found"}')

        ACTIONS = {"/cancel": lambda q: desk.cancel(q.get("id", [""])[0]),
                   "/stop_repeat": lambda q: desk.stop_repeat(q.get("id", [""])[0]),
                   "/skip": lambda q: desk.skip(), "/restart": lambda q: str(desk.restart()),
                   "/pause": lambda q: desk.pause(), "/resume": lambda q: desk.resume()}

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/upload" and u.path not in self.ACTIONS:
                return self._send(404, '{"error":"not found"}')
            q = parse_qs(u.query)
            if upload_key and not hmac.compare_digest(q.get("key", [""])[0].encode(), upload_key.encode()):
                return self._send(403, '{"error":"Changing the display needs the key: open the page as /?key=..."}')
            if u.path in self.ACTIONS:                  # the queue and the sheet: same as the Slack commands
                try:
                    text = self.ACTIONS[u.path](q)
                except BadRequest as e:
                    return self._send(400, json.dumps({"error": str(e)}))
                return self._send(200, json.dumps({"ok": True, "text": text.replace("*", "").replace("`", "")}))
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
            one = lambda k, d="": q.get(k, [d])[0]          # noqa: E731
            style = {k: one(k) for k in ("medium", "detail", "contrast", "fit") if one(k)}
            try:
                every = self._every(one("every"))
                always = one("always") in ("1", "true", "on")
                r = submit(lib, overrides, start, dur, data, name, one("start", "now"), one("end"), one("from", "0"),
                           tune=style if always else None, every=every, style=None if always else style)
            except BadRequest as e:
                return self._send(400, json.dumps({"error": str(e)}))
            live.wake.set()
            self._send(200, json.dumps({"ok": True, "start": r["begin"].strftime("%a %-d %b %H:%M" if r["repeat"] else "%a %H:%M"),
                                        "end": r["end"].strftime("%a %H:%M") if r["end"] else "",
                                        "now": r["now"], "repeat": bool(r["repeat"]), "paused": bool(overrides.paused())}))

        @staticmethod
        def _every(text):
            """`every` in the address: weekdays as 0-6 (Monday first) separated by commas, or the
            words daily, weekdays, weekends; a date as day-N (monthly) or MM-DD (yearly)."""
            text = text.strip().lower()
            if not text:
                return None
            named = {"daily": list(range(7)), "weekdays": list(range(5)), "weekends": [5, 6]}
            if text in named:
                return named[text]
            try:
                if text.startswith("day-"):
                    return {"day": int(text[4:])}
                if re.fullmatch(r"\d{2}-\d{2}", text):
                    return {"month": int(text[:2]), "day": int(text[3:])}
                days = sorted({int(d) for d in text.split(",")})
            except ValueError:
                days = [-1]
            if not days or not all(0 <= d <= 6 for d in days):
                raise BadRequest("Repeat days are numbers from 0 (Monday) to 6 (Sunday).")
            return days

    srv = ThreadingHTTPServer((bind, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"viewer at http://<this-pi>:{srv.server_address[1]}/", flush=True)
    return srv
