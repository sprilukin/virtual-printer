"""Regenerates the hand-built overlay profiles from code (overlays are verbose JSON).

Run from the repo root: .venv/bin/python tools/make_overlays.py
"""

import json
from pathlib import Path

from vp.ipp import attr as A
from vp.ipp import resolution

kw = lambda n, *v: A(n, "keyword", *v)  # noqa: E731
CMYK = [("Black", "#000000"), ("Cyan", "#00FFFF"), ("Magenta", "#FF00FF"), ("Yellow", "#FFFF00")]


def markers(colors):
    n = len(colors)
    return [
        A("marker-colors", "nameWithoutLanguage", *[c[1] for c in colors]),
        A("marker-names", "nameWithoutLanguage", *[c[0] for c in colors]),
        A("marker-levels", "integer", *[80] * n),
        A("marker-high-levels", "integer", *[100] * n),
        A("marker-low-levels", "integer", *[10] * n),
        kw("marker-types", *["toner"] * n),
    ]


def ident(title):
    return [
        A("printer-name", "nameWithoutLanguage", f"[VIRTUAL] {title}"),
        A("printer-info", "textWithoutLanguage", f"[VIRTUAL] {title}"),
        A("printer-dns-sd-name", "nameWithoutLanguage", f"[VIRTUAL] {title}"),
        A("printer-make-and-model", "textWithoutLanguage", f"Virtual {title}"),
    ]


FORMATS = [
    A("document-format-supported", "mimeMediaType", "application/octet-stream", "image/pwg-raster"),
    A("document-format-preferred", "mimeMediaType", "image/pwg-raster"),
]


def txt(title, color, duplex):
    return {
        "pdl": "application/octet-stream,image/pwg-raster",
        "ty": f"Virtual {title}",
        "product": f"(Virtual {title})",
        "usb_MFG": "Virtual",
        "usb_MDL": title,
        "usb_CMD": "PWGRaster",
        "Color": color,
        "Duplex": duplex,
        "URF": None,
        "mopria-certified": None,
        "print_wfds": None,
    }


def devid(t):
    return A(
        "printer-device-id",
        "textWithoutLanguage",
        f"MFG:Virtual;CMD:PWGRaster;MDL:{t};CLS:PRINTER;",
    )


T3 = "Office IPPS Basic Auth"
T1 = "Duplex Color Laser 300dpi"
T2 = "Borderless Inkjet Letter"
OVERLAYS = {
    "duplex-color-laser-300dpi": {
        "base": "brother-dcp-l1630w",
        "description": "Auto duplex, colour, 300 dpi only (overlay on the Brother capture)",
        "dnssd_name": f"[VIRTUAL] {T1}",
        "dnssd_txt": txt(T1, "T", "T"),
        "remove": ["urf-supported", "printer-icons"],
        "set": [
            *ident(T1),
            devid(T1),
            *FORMATS,
            kw("sides-supported", "one-sided", "two-sided-long-edge", "two-sided-short-edge"),
            kw("print-color-mode-supported", "auto", "color", "monochrome", "auto-monochrome"),
            kw("print-color-mode-default", "auto"),
            kw("output-mode-supported", "auto", "color", "monochrome", "auto-monochrome"),
            kw("output-mode-default", "auto"),
            A("color-supported", "boolean", True),
            A("pages-per-minute", "integer", 24),
            A("pages-per-minute-color", "integer", 24),
            A("printer-resolution-supported", "resolution", resolution(300)),
            A("printer-resolution-default", "resolution", resolution(300)),
            A("pwg-raster-document-resolution-supported", "resolution", resolution(300)),
            kw("pwg-raster-document-type-supported", "sgray_8", "srgb_8"),
            kw("pwg-raster-document-sheet-back", "flipped"),
            *markers(CMYK),
        ],
        "options": {"validation": "strict"},
    },
    "borderless-inkjet-letter": {
        "base": "brother-dcp-l1630w",
        "description": "Zero margins, Letter default, colour inkjet (overlay on the Brother capture)",
        "dnssd_name": f"[VIRTUAL] {T2}",
        "dnssd_txt": txt(T2, "T", "F"),
        "remove": ["urf-supported", "printer-icons"],
        "default_media": "na_letter_8.5x11in",
        "margins": {"default": 0, "supported": [0]},
        "set": [
            *ident(T2),
            devid(T2),
            *FORMATS,
            kw("print-color-mode-supported", "auto", "color", "monochrome"),
            kw("print-color-mode-default", "color"),
            kw("output-mode-supported", "auto", "color", "monochrome"),
            kw("output-mode-default", "color"),
            A("color-supported", "boolean", True),
            A("pages-per-minute", "integer", 12),
            A("pages-per-minute-color", "integer", 8),
            A("printer-resolution-supported", "resolution", resolution(300), resolution(600)),
            A("printer-resolution-default", "resolution", resolution(600)),
            A(
                "pwg-raster-document-resolution-supported",
                "resolution",
                resolution(300),
                resolution(600),
            ),
            kw("pwg-raster-document-type-supported", "sgray_8", "srgb_8"),
            kw("media-type-supported", "stationery", "photographic-glossy"),
            *markers(CMYK),
        ],
        "options": {"validation": "strict"},
    },
    "ipps-auth-office": {
        "base": "duplex-color-laser-300dpi",
        "description": (
            "ipps:// (TLS) with HTTP Basic auth, user office / password print-me "
            "(overlay on duplex-color-laser-300dpi)"
        ),
        "dnssd_name": f"[VIRTUAL] {T3}",
        "dnssd_service_type": "_ipps._tcp",
        "dnssd_txt": {**txt(T3, "T", "T"), "TLS": "1.2", "air": "username,password"},
        "set": [*ident(T3), devid(T3)],
        "options": {
            "validation": "strict",
            "tls": {"mode": "self-signed"},
            "auth": {"type": "basic", "realm": "Office printer", "users": {"office": "print-me"}},
        },
    },
}

if __name__ == "__main__":
    for name, overlay in OVERLAYS.items():
        d = Path("profiles") / name
        d.mkdir(exist_ok=True)
        (d / "overlay.json").write_text(json.dumps(overlay, indent=2) + "\n")
        print("wrote", d / "overlay.json")
