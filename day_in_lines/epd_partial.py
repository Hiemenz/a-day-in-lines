"""Partial refresh for the 7.5" V2 panel on Waveshare's older driver.

Older copies of `waveshare_epd/epd7in5_V2.py` have no `display_Partial`, so every
update is a full, blinking refresh. `with_partial` subclasses that driver and adds the
init, full-refresh and partial-refresh routines from Waveshare's newer driver. Both
sets of routines are needed together: a partial refresh compares the new frame with the
one held in the controller's "old" RAM, and only the newer full refresh fills that.
"""
from __future__ import annotations


def with_partial(base):
    """Subclass of the driver's EPD class with init_part and display_Partial added."""
    from waveshare_epd import epdconfig

    class EPDPartial(base):
        def init(self):
            if epdconfig.module_init() != 0:
                return -1
            self.reset()
            self.send_command(0x06)                 # booster soft start
            for b in (0x17, 0x17, 0x28, 0x17):      # try 0x38 for the third if the panel misbehaves
                self.send_data(b)
            self.send_command(0x01)                 # power setting
            for b in (0x07, 0x07, 0x3F, 0x3F):
                self.send_data(b)
            self.send_command(0x04)                 # power on
            epdconfig.delay_ms(100)
            self.ReadBusy()
            self.send_command(0x00)                 # panel setting: KW, waveform from OTP
            self.send_data(0x1F)
            self.send_command(0x61)                 # resolution 800x480
            for b in (0x03, 0x20, 0x01, 0xE0):
                self.send_data(b)
            self.send_command(0x15)
            self.send_data(0x00)
            self.send_command(0x50)                 # vcom and data interval
            self.send_data(0x10)
            self.send_data(0x07)
            self.send_command(0x60)                 # tcon
            self.send_data(0x22)
            return 0

        def init_part(self):
            if epdconfig.module_init() != 0:
                return -1
            self.reset()
            self.send_command(0x00)
            self.send_data(0x1F)
            self.send_command(0x04)
            epdconfig.delay_ms(100)
            self.ReadBusy()
            self.send_command(0xE0)                 # cascade setting, then the partial waveform
            self.send_data(0x02)
            self.send_command(0xE5)
            self.send_data(0x6E)
            return 0

        def display(self, image):
            """Full refresh. Also fills the old-frame RAM, which partial refresh diffs against."""
            self.send_command(0x10)
            self.send_data2(bytes(b ^ 0xFF for b in image))
            self.send_command(0x13)
            self.send_data2(image)
            self.send_command(0x12)
            epdconfig.delay_ms(100)
            self.ReadBusy()

        def display_Partial(self, image, x0, y0, x1, y1):
            """Refresh the window x0..x1, y0..y1; `image` is that window's bytes from getbuffer
            (with x0 and x1 on multiples of 8)."""
            x0, x1 = x0 // 8 * 8, (x1 + 7) // 8 * 8
            self.send_command(0x50)
            self.send_data(0xA9)
            self.send_data(0x07)
            self.send_command(0x91)                 # enter partial mode
            self.send_command(0x90)                 # window
            for v in (x0 // 256, x0 % 256, (x1 - 1) // 256, (x1 - 1) % 256,
                      y0 // 256, y0 % 256, (y1 - 1) // 256, (y1 - 1) % 256, 0x01):
                self.send_data(v)
            self.send_command(0x13)
            self.send_data2(bytes(b ^ 0xFF for b in image))
            self.send_command(0x12)
            epdconfig.delay_ms(100)
            self.ReadBusy()

    return EPDPartial
