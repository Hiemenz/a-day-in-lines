import numpy as np
from PIL import Image

from day_in_lines.render import Sheet, blue_noise, dither_1bit


def spectrum(m, tone=.25):
    p = (tone > m).astype(float)
    power = np.abs(np.fft.fft2(p - p.mean())) ** 2
    power[0, 0] = 0
    f = np.fft.fftfreq(m.shape[0]) * m.shape[0]
    r = np.hypot(*np.meshgrid(f, f))
    return power[(r > 0) & (r <= 4)].mean() / power[r > 0].mean(), power.max() / power[r > 0].mean()


def test_blue_noise_uses_every_threshold_once():
    m = blue_noise()
    assert m.shape == (64, 64)
    assert np.allclose(np.sort(m.ravel()), (np.arange(m.size) + .5) / m.size)


def test_blue_noise_is_deterministic():
    assert np.array_equal(blue_noise(32, seed=3), blue_noise(32, seed=3))


def test_blue_noise_has_no_lattice():
    """Blue noise: almost no low-frequency energy and no spectral spikes. A Bayer
    matrix has a spike over a thousand times the average line; white noise has lots of low-frequency energy."""
    low, spike = spectrum(blue_noise())
    assert low < .2
    assert spike < 40


def test_dither_matches_the_tone():
    for tone in (.15, .5, .85):
        flat = Image.new("L", (256, 256), int(255 * tone ** (1 / 2.4)))
        white = np.asarray(dither_1bit(flat, 2.4)).mean()
        assert abs(white - tone) < .03


def test_dither_is_local():
    """Adding a mark only flips pixels under it, so a partial refresh shows just that mark."""
    base = np.full((120, 200), 230, np.uint8)
    marked = base.copy()
    marked[40:50, 60:120] = 90
    a = np.asarray(dither_1bit(Image.fromarray(base), 2.4))
    b = np.asarray(dither_1bit(Image.fromarray(marked), 2.4))
    changed = np.argwhere(a != b)
    assert len(changed)
    assert changed[:, 0].min() >= 40 and changed[:, 0].max() < 50
    assert changed[:, 1].min() >= 60 and changed[:, 1].max() < 120


def test_dither_output_is_one_bit():
    assert dither_1bit(Image.new("L", (30, 20), 128)).mode == "1"


def test_sheet_starts_nearly_blank_and_darkens():
    sh = Sheet(1)
    blank = np.asarray(sh.gray()).mean()
    assert blank > 250
    sh.draw_stroke(dict(pts=np.array([[50, 50], [300, 80]], np.float32), p=.9, w=1.0, taper=True, ph=0.0))
    assert np.asarray(sh.gray()).mean() < blank
