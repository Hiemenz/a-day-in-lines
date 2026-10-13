import sys
import types

from PIL import Image

from day_in_lines.displays import WaveshareDisplay


def fake_driver(monkeypatch):
    """A stand-in for Waveshare's older epd7in5_V2 driver: no display_Partial, records the bus."""
    log = []
    pkg = types.ModuleType("waveshare_epd")
    cfg = types.ModuleType("waveshare_epd.epdconfig")
    cfg.module_init = lambda: 0
    cfg.module_exit = lambda: None
    cfg.delay_ms = lambda ms: None
    mod = types.ModuleType("waveshare_epd.epd7in5_V2")

    class EPD:
        width, height = 800, 480

        def reset(self): pass
        def ReadBusy(self): pass
        def send_command(self, c): log.append(("cmd", c))
        def send_data(self, d): log.append(("data", d))
        def send_data2(self, d): log.append(("bulk", bytes(d)))
        def getbuffer(self, img): return bytearray(b ^ 0xFF for b in img.convert("1").tobytes())
        def sleep(self): log.append(("sleep",))

    mod.EPD, pkg.epdconfig, pkg.epd7in5_V2 = EPD, cfg, mod
    for name, m in (("waveshare_epd", pkg), ("waveshare_epd.epdconfig", cfg), ("waveshare_epd.epd7in5_V2", mod)):
        monkeypatch.setitem(sys.modules, name, m)
    return log


def test_old_driver_gets_partial_refresh(monkeypatch):
    log = fake_driver(monkeypatch)
    disp = WaveshareDisplay("epd7in5_V2")
    assert hasattr(disp.epd, "display_Partial")
    img = Image.new("1", (800, 480), 1)

    disp.show(img, full=True)
    cmds = [e[1] for e in log if e[0] == "cmd"]
    assert cmds.count(0x10) == 1 and cmds.count(0x13) == 1 and 0x91 not in cmds     # full: both RAMs, no partial mode

    log.clear()
    disp.show(img, full=False)                                                       # unchanged: panel untouched
    assert log == []

    img2 = img.copy()
    img2.paste(0, (13, 100, 30, 120))                                                # a small change
    disp.show(img2, full=False)
    cmds = [e[1] for e in log if e[0] == "cmd"]
    assert 0x91 in cmds and 0x90 in cmds and 0x10 not in cmds and cmds.count(0x12) == 1
    window = [e[1] for e in log if e[0] == "data"][-9:]
    assert window == [0, 8, 0, 31, 0, 100, 0, 119, 1]                                # x 8..32 (byte aligned), y 100..120
    assert log[-1] == ("sleep",)


def test_other_modules_are_left_alone(monkeypatch):
    fake_driver(monkeypatch)
    sys.modules["waveshare_epd.epd7in5_V2"].EPD.display_Partial = lambda *a: None
    sys.modules["waveshare_epd.epd7in5_V2"].EPD.init = lambda self: None
    assert type(WaveshareDisplay("epd7in5_V2").epd).__name__ == "EPD"
