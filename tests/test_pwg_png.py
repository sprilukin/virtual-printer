import pytest

from vp import png, pwg

from .helpers import blank_page, make_pwg


def test_decode_roundtrip_with_repeats_and_literals():
    rows = [bytes(range(40)), bytes(range(40)), b"\x00" * 40, bytes([i % 3 for i in range(40)])]
    pages = pwg.decode(make_pwg([{"rows": rows, "dpi": 600, "duplex": 1, "tumble": 1}]))
    assert len(pages) == 1
    p = pages[0]
    assert (p.width, p.height, p.resolution) == (40, 4, (600, 600))
    assert p.duplex and p.tumble
    assert p.type_name == "sgray_8"
    assert [p.pixels[i * 40 : (i + 1) * 40] for i in range(4)] == rows


def test_decode_rgb_and_multiple_pages():
    row = bytes([10, 20, 30] * 5)
    data = make_pwg([{"rows": [row] * 3, "bpp": 24, "color_space": 19}, blank_page(8, 2)])
    pages = pwg.decode(data)
    assert [p.type_name for p in pages] == ["srgb_8", "sgray_8"]
    assert pages[0].bytes_per_line == 15


def test_white_fill_code():
    # one row where code 128 fills the remainder with white
    data = bytearray(make_pwg([blank_page(10, 1)]))
    data[4 + 1796 :] = bytes([0, 0, 0x00, 128])  # repeat=0, one black pixel, then fill
    p = pwg.decode(bytes(data))[0]
    assert p.pixels == b"\x00" + b"\xff" * 9


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: b"XXXX" + d[4:],
        lambda d: d[: len(d) - 3],
        lambda d: d[:100],
    ],
)
def test_decode_rejects_damage(mutate):
    with pytest.raises(pwg.RasterError):
        pwg.decode(mutate(make_pwg([{"rows": [bytes(range(20))] * 5}])))


def test_png_shades_margins_only():
    page = pwg.decode(make_pwg([blank_page(30, 40)]))[0]
    w, h, rows = png.read(png.render(page, (4, 6, 3, 5)))
    assert (w, h) == (30, 40)
    px = lambda x, y: tuple(rows[y][3 * x : 3 * x + 3])  # noqa: E731
    assert px(15, 20) == (255, 255, 255)  # printable area untouched
    for x, y in [(15, 0), (15, 3), (15, 39), (0, 20), (2, 20), (29, 20), (25, 20)]:
        assert px(x, y)[1] < 255, (x, y)  # tinted
    assert px(3, 20) == (255, 255, 255) and px(24, 20) == (255, 255, 255)
    assert px(15, 4) == (255, 255, 255) and px(15, 33) == (255, 255, 255)


def test_png_rgb_pixels_preserved():
    row = bytes([200, 100, 50] * 6)
    page = pwg.decode(make_pwg([{"rows": [row] * 4, "bpp": 24, "color_space": 19}]))[0]
    _, _, rows = png.read(png.render(page, (0, 0, 0, 0)))
    assert rows[2] == row
