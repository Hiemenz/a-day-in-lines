"""Display back-ends: a PNG file, a Waveshare e-ink panel, or a Pimoroni Inky."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

from .render import dither_1bit


class FileDisplay:
    def __init__(self, path, size):
        self.path, self.size = Path(path), size

    def show(self, img, full=False):
        img.save(self.path)


class WaveshareDisplay:
    """Adapter over Waveshare's `waveshare_epd` python package. `mode` is what the stored
    frames are: "mono" (1-bit, partial refreshes), "gray4" (four greys, on panels whose driver
    has them) or "colour" (seven-colour ACeP panels). Grey and colour refreshes are always full."""

    def __init__(self, module, gamma=2.4, mode="mono"):
        import importlib
        epd_class = importlib.import_module(f"waveshare_epd.{module}").EPD
        if module == "epd7in5_V2" and not hasattr(epd_class, "display_Partial"):
            from .epd_partial import with_partial       # older driver: add its partial refresh
            epd_class = with_partial(epd_class)
        self.epd = epd_class()
        self.size = (self.epd.width, self.epd.height)
        self.gamma, self.mode = gamma, mode
        self.last = None                 # what the panel shows, to find what changed
        if mode == "gray4":
            self.init4 = getattr(self.epd, "init_4Gray", None) or getattr(self.epd, "Init_4Gray", None)
            if not (self.init4 and hasattr(self.epd, "getbuffer_4Gray") and hasattr(self.epd, "display_4Gray")):
                raise ValueError(f"the {module} driver has no four-grey mode; use --mode mono")

    def show(self, img, full=False):
        epd = self.epd
        if self.mode == "gray4":
            self.init4()
            epd.display_4Gray(epd.getbuffer_4Gray(img.convert("L")))
            return epd.sleep()
        if self.mode == "colour":
            epd.init()
            epd.display(epd.getbuffer(img.convert("RGB")))
            return epd.sleep()
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


class InkyDisplay:
    """Adapter over Pimoroni's `inky` package; the board is found from its EEPROM. Inky panels
    have no partial refresh, so every update is a full one: use --update-every to space them out."""

    def __init__(self, kind="auto", gamma=2.4):
        if kind != "auto":
            raise ValueError("use inky:auto (the board is detected)")
        from inky.auto import auto
        self.inky = auto()
        self.size = tuple(self.inky.resolution)
        self.gamma = gamma
        self.multi = getattr(self.inky, "colour", "") == "multi"       # Impression: seven colours

    def show(self, img, full=False):
        if self.multi:
            self.inky.set_image(img.convert("RGB"))
        else:                            # black/white(/red) boards take palette indexes: 0 white, 1 black
            if img.mode != "1":
                img = dither_1bit(img.convert("L"), self.gamma)
            black = (np.asarray(img.convert("L")) < 128).astype(np.uint8)
            self.inky.set_image(Image.fromarray(black, "P"))
        self.inky.show()


class Rotated:
    """Any display, turned: 180 for a panel mounted upside down, 90 or 270 for one on its side."""

    def __init__(self, display, degrees):
        self.display, self.degrees = display, degrees % 360
        w, h = display.size
        self.size = (h, w) if self.degrees in (90, 270) else (w, h)

    def __getattr__(self, name):                      # the panel driver and the rest, as the display has them
        return getattr(self.display, name)

    def show(self, img, full=False):
        self.display.show(img.rotate(self.degrees, expand=True) if self.degrees else img, full=full)


def caption(img: Image.Image, text: str) -> Image.Image:
    """A small label in the bottom right corner of a frame, in the frame's own mode."""
    if not text:
        return img
    out = img.convert("RGB") if img.mode == "P" else img.copy()
    try:
        font = ImageFont.load_default(size=max(11, out.height // 34))
    except TypeError:                                 # older Pillow: one size only
        font = ImageFont.load_default()
    d = ImageDraw.Draw(out)
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
    w, h, pad = x1 - x0, y1 - y0, 5
    x, y = out.width - w - 3 * pad, out.height - h - 3 * pad
    white, black = (255, 0) if out.mode in ("1", "L") else ((255, 255, 255), (0, 0, 0))
    d.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], fill=white)
    d.text((x - x0, y - y0), text, font=font, fill=black)
    return out
