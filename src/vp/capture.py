"""Read-only capture of a printer, and conversion to the canonical tree.

Capture sends exactly one Get-Printer-Attributes request and browses mDNS.
It never sends Print-Job, Validate-Job or any document data.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import dnssd, ipp

RAW_IPP = "get-printer-attributes.ipp"
RAW_TXT_JSON = "txt.json"
RAW_TXT_TEXT = "txt.txt"  # `dns-sd -L` output, accepted as an alternative
META = "capture.json"
CANONICAL = "canonical.json"


def capture(uri: str, raw_dir: Path, mdns_timeout: float = 5.0) -> dict[str, Any]:
    """Capture `uri` into raw_dir. Returns the meta dict."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    host = urlsplit(uri).hostname or ""
    try:
        ip = str(socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0])
    except OSError:
        ip = None

    req = ipp.make_request(
        "Get-Printer-Attributes",
        uri,
        # `all` already includes media-col-database on the Brother; naming it
        # is harmless and covers printers that only return it when asked.
    )
    req["groups"][0]["attributes"].append(
        ipp.attr("requested-attributes", "keyword", "all", "media-col-database")
    )
    body = ipp.send(uri, ipp.encode(req))
    (raw_dir / RAW_IPP).write_bytes(body)
    msg = ipp.decode(body)
    if msg["code"] >= 0x0400:
        raise RuntimeError(f"printer answered {ipp.status_name(msg['code'])}")

    txt = None
    try:
        txt = dnssd.browse_txt(host, ip, mdns_timeout)
    except Exception as exc:  # mDNS is best effort
        print(f"warning: mDNS lookup failed: {exc}")
    if txt:
        (raw_dir / RAW_TXT_JSON).write_text(json.dumps(txt, indent=2) + "\n")
    else:
        print(
            f"warning: no _ipp._tcp service found for {host}; save `dns-sd -L` "
            f"output as {raw_dir / RAW_TXT_TEXT} and re-run convert"
        )

    meta = {
        "uri": uri,
        "host": host,
        "ip": ip,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "bytes": len(body),
        "has_txt": bool(txt),
    }
    (raw_dir / META).write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def convert(raw_dir: Path) -> dict[str, Any]:
    """Raw capture directory to the canonical (unscrubbed) tree."""
    ipp_path = raw_dir / RAW_IPP
    if not ipp_path.exists():
        raise FileNotFoundError(f"{ipp_path} missing; run `vp capture` first")
    msg = ipp.decode(ipp_path.read_bytes())
    if msg["code"] >= 0x0400:
        raise ValueError(f"capture holds an error response: {ipp.status_name(msg['code'])}")
    printer_groups = [g for g in msg["groups"] if g["tag"] == "printer-attributes"]
    if not printer_groups:
        raise ValueError("capture has no printer-attributes group")
    attributes = [a for g in printer_groups for a in g["attributes"]]

    dns = None
    if (raw_dir / RAW_TXT_JSON).exists():
        dns = json.loads((raw_dir / RAW_TXT_JSON).read_text())
    elif (raw_dir / RAW_TXT_TEXT).exists():
        dns = dnssd.parse_dnssd_text((raw_dir / RAW_TXT_TEXT).read_text())
    meta = json.loads((raw_dir / META).read_text()) if (raw_dir / META).exists() else {}

    canonical = {
        "format": 1,
        "ipp_version": msg["version"],
        "attributes": attributes,
        "dnssd": dns,
        "capture_meta": meta,
    }
    (raw_dir / CANONICAL).write_text(json.dumps(canonical, indent=2) + "\n")
    return canonical
