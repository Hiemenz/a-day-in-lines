"""Display back-ends: a PNG file, or a Waveshare e-ink panel."""
from __future__ import annotations

from pathlib import Path

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
        self.epd = importlib.import_module(f"waveshare_epd.{module}").EPD()
        self.size = (self.epd.width, self.epd.height)
        self.gamma = gamma

    def show(self, img, full=False):
        epd = self.epd
        if img.mode != "1":              # greyscale frames: same local dither, not Pillow's error diffusion
            img = dither_1bit(img.convert("L"), self.gamma)
        buf = epd.getbuffer(img)
        if full or not hasattr(epd, "display_Partial"):
            epd.init()
            epd.display(buf)
        else:
            (getattr(epd, "init_part", None) or epd.init)()
            try:
                epd.display_Partial(buf, 0, 0, epd.width, epd.height)
            except TypeError:
                epd.display_Partial(buf)
        epd.sleep()
