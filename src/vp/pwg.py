"""PWG Raster (PWG 5102.4) decoder.

File = b"RaS2" then, per page, a 1796-byte big-endian header and the
compressed pixel data. Compression: each row group starts with a repeat
byte (rows = n + 1); the row is a series of codes: 0..127 repeat the next
pixel c+1 times, 129..255 copy 257-c literal pixels, 128 fills the rest of
the row with white.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SYNC = b"RaS2"
HEADER_SIZE = 1796
MAX_PIXELS = 400_000_000  # refuse absurd headers (a 1200 dpi A3 page is ~270M)

COLOR_SPACES = {0: "w", 1: "rgb", 3: "k", 18: "sgray", 19: "srgb", 20: "adobe-rgb", 22: "device1"}


class RasterError(ValueError):
    pass


@dataclass
class Page:
    width: int
    height: int
    bits_per_color: int
    bits_per_pixel: int
    bytes_per_line: int
    color_space: int
    num_colors: int
    resolution: tuple[int, int]
    duplex: bool
    tumble: bool
    num_copies: int
    page_size_pts: tuple[int, int]
    media_type: str
    page_size_name: str
    total_page_count: int
    pixels: bytes  # height * bytes_per_line

    @property
    def color_space_name(self) -> str:
        return COLOR_SPACES.get(self.color_space, str(self.color_space))

    @property
    def type_name(self) -> str:
        """PWG raster type keyword, e.g. sgray_8, srgb_8."""
        return f"{self.color_space_name}_{self.bits_per_color}"


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("latin-1")


def _u32(h: bytes, off: int) -> int:
    return struct.unpack_from(">I", h, off)[0]


def parse_header(h: bytes) -> dict:
    if len(h) != HEADER_SIZE:
        raise RasterError("truncated page header")
    if _cstr(h[:64]) != "PwgRaster":
        raise RasterError("page header is not PwgRaster")
    return {
        "media_type": _cstr(h[128:192]),
        "duplex": bool(_u32(h, 272)),
        "resolution": (_u32(h, 276), _u32(h, 280)),
        "num_copies": _u32(h, 340),
        "page_size_pts": (_u32(h, 352), _u32(h, 356)),
        "tumble": bool(_u32(h, 368)),
        "width": _u32(h, 372),
        "height": _u32(h, 376),
        "bits_per_color": _u32(h, 384),
        "bits_per_pixel": _u32(h, 388),
        "bytes_per_line": _u32(h, 392),
        "color_space": _u32(h, 400),
        "num_colors": _u32(h, 420),
        "total_page_count": _u32(h, 452),
        "page_size_name": _cstr(h[1732:1796]),
    }


def _read_row(data: memoryview, pos: int, bpp: int, bpl: int) -> tuple[bytes, int]:
    row = bytearray()
    n = len(data)
    while len(row) < bpl:
        if pos >= n:
            raise RasterError("truncated pixel data")
        c = data[pos]
        pos += 1
        if c < 128:
            pixel = bytes(data[pos : pos + bpp])
            if len(pixel) != bpp:
                raise RasterError("truncated pixel data")
            pos += bpp
            row += pixel * (c + 1)
        elif c == 128:
            row += b"\xff" * (bpl - len(row))
        else:
            count = 257 - c
            chunk = bytes(data[pos : pos + count * bpp])
            if len(chunk) != count * bpp:
                raise RasterError("truncated pixel data")
            pos += count * bpp
            row += chunk
    if len(row) != bpl:
        raise RasterError("row overran bytes-per-line")
    return bytes(row), pos


def decode(data: bytes | memoryview) -> list[Page]:
    """Decode a whole PWG raster document. Raises RasterError if malformed."""
    mv = memoryview(data)
    if bytes(mv[:4]) != SYNC:
        raise RasterError("missing RaS2 sync word")
    pos = 4
    pages: list[Page] = []
    while pos < len(mv):
        hdr = parse_header(bytes(mv[pos : pos + HEADER_SIZE]))
        pos += HEADER_SIZE
        w, h, bpp, bpl = hdr["width"], hdr["height"], hdr["bits_per_pixel"], hdr["bytes_per_line"]
        if bpp == 0 or bpp % 8 or w * h > MAX_PIXELS or bpl < w * bpp // 8:
            raise RasterError(f"unsupported page geometry {w}x{h} at {bpp} bits per pixel")
        pixel_bytes = bpp // 8
        rows: list[bytes] = []
        while len(rows) < h:
            if pos >= len(mv):
                raise RasterError("truncated pixel data")
            repeat = mv[pos] + 1
            pos += 1
            row, pos = _read_row(mv, pos, pixel_bytes, bpl)
            rows.extend([row] * min(repeat, h - len(rows)))
        pages.append(Page(pixels=b"".join(rows), **hdr))
    if not pages:
        raise RasterError("no pages")
    return pages
