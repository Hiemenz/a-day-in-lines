"""Display back-ends: a PNG file, or a Waveshare e-ink panel."""
from __future__ import annotations

from pathlib import Path

from PIL import ImageChops

from .render import dither_1bit


class FileDisplay:
    def __init__(self, path, size):
        self.path, self.size = Path(path), size

    def show(self, img, full=False):
        img.save(self.path)


class WaveshareDisplay:
    """Adapter over Waveshare's `waveshare_epd` python package."""

    def __init__(self, module, gamma=2.4):
        import importlib
        epd_class = importlib.import_module(f"waveshare_epd.{module}").EPD
        if module == "epd7in5_V2" and not hasattr(epd_class, "display_Partial"):
            from .epd_partial import with_partial       # older driver: add its partial refresh
            epd_class = with_partial(epd_class)
        self.epd = epd_class()
        self.size = (self.epd.width, self.epd.height)
        self.gamma = gamma
        self.last = None                 # what the panel shows, to find what changed

    def show(self, img, full=False):
        epd = self.epd
        if img.mode != "1":              # greyscale frames: same local dither, not Pillow's error diffusion
            img = dither_1bit(img.convert("L"), self.gamma)
        last, self.last = self.last, img
        if full or not hasattr(epd, "display_Partial") or last is None or last.size != img.size:
            epd.init()
            epd.display(epd.getbuffer(img))
            epd.sleep()
            return
        box = ImageChops.difference(last.convert("L"), img.convert("L")).getbbox()
        if box is None:                  # nothing changed: leave the panel alone
            return
        x0, y0, x1, y1 = box[0] // 8 * 8, box[1], -(-box[2] // 8) * 8, box[3]     # x on byte boundaries
        buf = bytearray(img.crop((x0, y0, x1, y1)).tobytes("raw"))
        for i in range(len(buf)):        # PIL 0=black, panel 1=black
            buf[i] ^= 0xFF
        (getattr(epd, "init_part", None) or epd.init)()
        epd.display_Partial(buf, x0, y0, x1, y1)     # only the box that changed, as the mlb scoreboard does
        epd.sleep()
