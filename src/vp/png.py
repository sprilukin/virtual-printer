"""Per-page PNG rendering with the unprintable margins shaded.

Pure stdlib. Pixel work is done with bytes slicing and translate so a
600 dpi A4 page renders in about a second.
"""

from __future__ import annotations

import struct
import zlib

from .pwg import Page

SHADE = (255, 0, 0)  # margin tint
SHADE_ALPHA = 0.35


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _tint_table(channel: int) -> bytes:
    a = SHADE_ALPHA
    return bytes(round(v * (1 - a) + SHADE[channel] * a) for v in range(256))


_TINT = [_tint_table(c) for c in range(3)]


def _gray_of(page: Page, row: bytes) -> bytes:
    """One byte per pixel, 0 = black."""
    px = page.bits_per_pixel // 8
    w = page.width
    cs = page.color_space_name
    if px == 1:
        g = row[:w]
    elif px == 2:
        g = row[0 : w * 2 : 2]
    elif px == 3 or px == 6:
        step = px // 3
        r, gr, b = (row[i * step : w * px : px] for i in range(3))
        # cheap luma: average of three channels via two translate-free passes
        g = bytes((x + y + z) // 3 for x, y, z in zip(r, gr, b, strict=True))
    else:
        g = row[0 : w * px : px]
    return g.translate(bytes(255 - v for v in range(256))) if cs == "k" else g


def _rgb_of(page: Page, row: bytes) -> tuple[bytes, bytes, bytes]:
    px = page.bits_per_pixel // 8
    w = page.width
    if page.num_colors == 3 and px in (3, 6):
        step = px // 3
        return tuple(row[i * step : w * px : px] for i in range(3))  # type: ignore[return-value]
    g = _gray_of(page, row)
    return g, g, g


def render(page: Page, margins_pt: tuple[int, int, int, int]) -> bytes:
    """PNG bytes. margins_px = (top, bottom, left, right) in pixels."""
    top, bottom, left, right = margins_pt
    w, h = page.width, page.height
    comp = zlib.compressobj(3)
    out = [
        b"\x89PNG\r\n\x1a\n",
        _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)),
    ]
    idat = bytearray()
    cache: dict[tuple[bytes, bool], bytes] = {}
    bpl = page.bytes_per_line
    left, right = min(left, w), min(right, w - min(left, w))
    for y in range(h):
        src = page.pixels[y * bpl : (y + 1) * bpl]
        full_shade = y < top or y >= h - bottom
        key = (src, full_shade)
        scan = cache.get(key)
        if scan is None:
            r, g, b = _rgb_of(page, src)
            chans = [r, g, b]
            if full_shade:
                chans = [c.translate(_TINT[i]) for i, c in enumerate(chans)]
            elif left or right:
                chans = [
                    c[:left].translate(_TINT[i])
                    + c[left : w - right]
                    + c[w - right :].translate(_TINT[i])
                    for i, c in enumerate(chans)
                ]
            rgb = bytearray(3 * w + 1)
            for i, c in enumerate(chans):
                rgb[1 + i :: 3] = c
            scan = bytes(rgb)
            if len(cache) > 64:
                cache.clear()
            cache[key] = scan
        idat += comp.compress(scan)
        if len(idat) > 1 << 20:
            out.append(_chunk(b"IDAT", bytes(idat)))
            idat.clear()
    idat += comp.flush()
    if idat:
        out.append(_chunk(b"IDAT", bytes(idat)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def read(png: bytes) -> tuple[int, int, list[bytes]]:
    """Minimal reader for our own output (8-bit RGB, filter 0). For tests."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    h = 0
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos : pos + 4])
        kind, payload = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            idat += payload
    raw = zlib.decompress(idat)
    stride = 3 * w + 1
    return w, h, [raw[y * stride + 1 : (y + 1) * stride] for y in range(h)]
