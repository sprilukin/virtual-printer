"""Test helpers: a PWG raster encoder and an IPP client for the async server."""

from __future__ import annotations

import asyncio
import struct

from vp import ipp


def pwg_header(
    width, height, dpi=600, bpp=8, color_space=18, duplex=0, tumble=0, copies=1
) -> bytes:
    h = bytearray(1796)
    h[0:9] = b"PwgRaster"
    h[128:138] = b"stationery"
    struct.pack_into(">I", h, 272, duplex)
    struct.pack_into(">II", h, 276, dpi, dpi)
    struct.pack_into(">I", h, 340, copies)
    struct.pack_into(">II", h, 352, round(width / dpi * 72), round(height / dpi * 72))
    struct.pack_into(">I", h, 368, tumble)
    struct.pack_into(">II", h, 372, width, height)
    struct.pack_into(">III", h, 384, 8, bpp, width * bpp // 8)
    struct.pack_into(">I", h, 400, color_space)
    struct.pack_into(">I", h, 420, bpp // 8)
    h[1732:1745] = b"iso_a4_210x297mm"[:13]
    return bytes(h)


def encode_row(row: bytes, bpp_bytes: int) -> bytes:
    """Run-length encode one row: repeats for runs, literals otherwise."""
    px = [row[i : i + bpp_bytes] for i in range(0, len(row), bpp_bytes)]
    out, i = bytearray(), 0
    while i < len(px):
        run = 1
        while i + run < len(px) and px[i + run] == px[i] and run < 128:
            run += 1
        if run > 1:
            out += bytes([run - 1]) + px[i]
            i += run
        else:
            j = i
            while j < len(px) and j - i < 128 and (j + 1 >= len(px) or px[j + 1] != px[j]):
                j += 1
            j = max(j, i + 1)
            out += bytes([257 - (j - i)]) + b"".join(px[i:j])
            i = j
    return bytes(out)


def make_pwg(pages: list[dict]) -> bytes:
    """pages: [{"rows": [bytes, ...], "dpi": 300, "bpp": 8, ...}]; consecutive equal rows are repeated."""
    out = bytearray(b"RaS2")
    for p in pages:
        rows, bpp = p["rows"], p.get("bpp", 8)
        w = len(rows[0]) * 8 // bpp
        out += pwg_header(
            w,
            len(rows),
            p.get("dpi", 600),
            bpp,
            p.get("color_space", 18),
            p.get("duplex", 0),
            p.get("tumble", 0),
        )
        i = 0
        while i < len(rows):
            n = 1
            while i + n < len(rows) and rows[i + n] == rows[i] and n < 256:
                n += 1
            out += bytes([n - 1]) + encode_row(rows[i], bpp // 8)
            i += n
    return bytes(out)


def blank_page(w=60, h=80, **kw) -> dict:
    return {"rows": [b"\xff" * (w * kw.get("bpp", 8) // 8)] * h, **kw}


async def ipp_call(
    printer, operation, op_attrs=(), job_attrs=(), data=b"", version="2.0", target=None
):
    """Send an IPP request to a running Printer and return (decoded response, http-less)."""
    uri = f"ipp://127.0.0.1:{printer.port}{printer.resource}"
    groups = []
    if job_attrs:
        groups.append({"tag": "job-attributes", "attributes": list(job_attrs)})
    req = ipp.make_request(operation, target or uri, groups)
    req["version"] = version
    req["groups"][0]["attributes"].extend(op_attrs)
    body = ipp.encode(req, data)
    raw = await asyncio.to_thread(ipp.send, uri, body)
    return ipp.decode(raw)


def status(resp: dict) -> str:
    return ipp.status_name(resp["code"])
