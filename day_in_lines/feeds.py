"""Pictures that arrive by themselves: RSS or Atom feeds, plain lists of image addresses, or one
address whose picture changes. New pictures are saved into the picture folder like any upload."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .constants import MAX_UPLOAD_BYTES
from .library import Library

MAX_LISTING = 5_000_000
IMAGE_URL = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|tiff?|heic)(?:$|[?#])", re.I)


def get(url: str, limit: int) -> tuple[bytes, str]:
    """(body, content type). Only http and https; a body over `limit` is refused."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"not a web address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "a-day-in-lines"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if len(data) > limit:
        raise ValueError(f"{url} is too large")
    return data, ctype


def image_links(body: bytes, base: str) -> list:
    """The pictures a feed or list names, in the order it gives them."""
    text = body.decode("utf-8", "replace")
    found = []
    if re.search(r"<(?:rss|feed|rdf:RDF)\b", text[:2000]):
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise ValueError(f"could not read the feed: {e}")
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            url = el.get("url") or el.get("href") or ""
            kind = el.get("type", "") + " " + el.get("medium", "")
            if tag in ("enclosure", "content", "link") and url and ("image" in kind or IMAGE_URL.search(url)):
                if tag != "link" or el.get("rel") == "enclosure":
                    found.append(url)
            elif tag in ("description", "encoded", "content", "summary") and el.text:
                found += re.findall(r"""<img[^>]+src=["']([^"']+)""", el.text)
    else:                                              # a plain list: one address a line
        found = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("http://", "https://"))]
    out = []
    for u in found:
        u = urljoin(base, u.strip())
        if urlparse(u).scheme in ("http", "https") and u not in out:
            out.append(u)
    return out


class Feeds:
    """Looks at each address every so often and stores pictures it has not seen: cache/feeds.json
    remembers which. At most `limit` new pictures per address each time, so a long feed joins the
    rotation gradually rather than all at once."""

    def __init__(self, lib: Library, cache: Path, urls, limit=5):
        self.lib, self.file, self.urls, self.limit = lib, cache / "feeds.json", list(urls), limit
        try:
            self.seen = json.loads(self.file.read_text())
        except Exception:
            self.seen = {}

    def _save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen))
        tmp.replace(self.file)

    def check(self, url: str) -> list:
        """Fetch one address; returns the hashes of the pictures it added."""
        body, ctype = get(url, MAX_UPLOAD_BYTES)
        if ctype.startswith("image/"):                 # the address is the picture; a new one is a new hash
            name = Path(urlparse(url).path).name or "feed.jpg"
            before = set(self.lib.data["images"])
            h = self.lib.add_upload(body, name, by=None)
            return [h] if h not in before else []
        if len(body) > MAX_LISTING:
            raise ValueError(f"{url} is too large to be a feed")
        seen, added = self.seen.setdefault(url, []), []
        for link in image_links(body, url):
            if link in seen:
                continue
            if len(added) >= self.limit:
                break
            try:
                data, _ = get(link, MAX_UPLOAD_BYTES)
                before = set(self.lib.data["images"])
                h = self.lib.add_upload(data, Path(urlparse(link).path).name or "feed.jpg")
                if h not in before:
                    added.append(h)
            except Exception as e:                     # one bad picture shouldn't hold up the rest
                print(f"feed: skipped {link}: {e}", flush=True)
            seen.append(link)
        del seen[:-2000]
        self._save()
        return added

    def check_all(self) -> list:
        added = []
        for url in self.urls:
            try:
                new = self.check(url)
                if new:
                    print(f"feed: {len(new)} new picture(s) from {url}", flush=True)
                added += new
            except Exception as e:
                print(f"feed: {url}: {e}", flush=True)
        return added

    def start(self, every_minutes=60):
        def loop():
            while True:
                self.check_all()
                time.sleep(max(1, every_minutes) * 60)
        threading.Thread(target=loop, daemon=True).start()
