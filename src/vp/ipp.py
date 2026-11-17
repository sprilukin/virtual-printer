"""IPP wire codec and a minimal client.

The canonical attribute tree is plain JSON-able data:

    message = {"version": "2.0", "code": 0, "name": "successful-ok",
               "request_id": 1, "groups": [group, ...], "data_hex": ""?}
    group   = {"tag": "printer-attributes", "attributes": [attr, ...]}
    attr    = {"name": str, "tag": str, "values": [value, ...]}

Value shape depends on the value tag (see VALUE_TAGS): integers/enums are
ints, booleans bools, character strings str, resolution
{"x","y","units"}, rangeOfInteger {"lower","upper"}, collection
{"members": [attr, ...]}, language-tagged strings {"lang","value"},
octetString {"base64"}, dateTime/unknown tags {"hex"}. Out-of-band tags
(unsupported, unknown, no-value, ...) have an empty values list.
Encoding and decoding are exact inverses for well-formed messages.
"""

from __future__ import annotations

import base64
import http.client
import struct
from typing import Any
from urllib.parse import urlsplit

GROUP_TAGS = {
    0x01: "operation-attributes",
    0x02: "job-attributes",
    0x04: "printer-attributes",
    0x05: "unsupported-attributes",
    0x06: "subscription-attributes",
    0x07: "event-notification-attributes",
    0x08: "resource-attributes",
    0x09: "document-attributes",
    0x0A: "system-attributes",
}
END_TAG = 0x03

OUT_OF_BAND = {
    0x10: "unsupported",
    0x12: "unknown",
    0x13: "no-value",
    0x15: "not-settable",
    0x16: "delete-attribute",
    0x17: "admin-define",
}
INT_TAGS = {0x21: "integer", 0x23: "enum"}
STRING_TAGS = {
    0x41: "textWithoutLanguage",
    0x42: "nameWithoutLanguage",
    0x44: "keyword",
    0x45: "uri",
    0x46: "uriScheme",
    0x47: "charset",
    0x48: "naturalLanguage",
    0x49: "mimeMediaType",
}
VALUE_TAGS = {
    **OUT_OF_BAND,
    **INT_TAGS,
    0x22: "boolean",
    0x30: "octetString",
    0x31: "dateTime",
    0x32: "resolution",
    0x33: "rangeOfInteger",
    0x34: "collection",
    0x35: "textWithLanguage",
    0x36: "nameWithLanguage",
    **STRING_TAGS,
}
TAG_BY_NAME = {v: k for k, v in VALUE_TAGS.items()}
GROUP_BY_NAME = {v: k for k, v in GROUP_TAGS.items()}

BEG_COLLECTION = 0x34
END_COLLECTION = 0x37
MEMBER_NAME = 0x4A

RES_UNITS = {3: "dpi", 4: "dpcm"}

OPERATIONS = {
    "Print-Job": 0x0002,
    "Validate-Job": 0x0004,
    "Create-Job": 0x0005,
    "Send-Document": 0x0006,
    "Cancel-Job": 0x0008,
    "Get-Job-Attributes": 0x0009,
    "Get-Jobs": 0x000A,
    "Get-Printer-Attributes": 0x000B,
}

STATUS_NAMES = {
    0x0000: "successful-ok",
    0x0001: "successful-ok-ignored-or-substituted-attributes",
    0x0002: "successful-ok-conflicting-attributes",
    0x0400: "client-error-bad-request",
    0x0401: "client-error-forbidden",
    0x0402: "client-error-not-authenticated",
    0x0403: "client-error-not-authorized",
    0x0404: "client-error-not-possible",
    0x0405: "client-error-timeout",
    0x0406: "client-error-not-found",
    0x0407: "client-error-gone",
    0x0408: "client-error-request-entity-too-large",
    0x0409: "client-error-request-value-too-long",
    0x040A: "client-error-document-format-not-supported",
    0x040B: "client-error-attributes-or-values-not-supported",
    0x040C: "client-error-uri-scheme-not-supported",
    0x040D: "client-error-charset-not-supported",
    0x040E: "client-error-conflicting-attributes",
    0x0500: "server-error-internal-error",
    0x0501: "server-error-operation-not-supported",
    0x0502: "server-error-service-unavailable",
    0x0503: "server-error-version-not-supported",
    0x0504: "server-error-device-error",
    0x0505: "server-error-temporary-error",
    0x0506: "server-error-not-accepting-jobs",
    0x0507: "server-error-busy",
    0x0508: "server-error-job-canceled",
}


class IppError(ValueError):
    """Malformed IPP message."""


# ---------------------------------------------------------------- decoding


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise IppError(f"truncated message at offset {self.pos}")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise IppError("truncated message")
        return self.data[self.pos]


def _decode_value(tag: int, raw: bytes) -> object:
    if tag in OUT_OF_BAND:
        return None
    if tag in INT_TAGS:
        if len(raw) != 4:
            raise IppError("bad integer length")
        return struct.unpack(">i", raw)[0]
    if tag == 0x22:
        if len(raw) != 1:
            raise IppError("bad boolean length")
        return raw[0] != 0
    if tag == 0x32:
        if len(raw) != 9:
            raise IppError("bad resolution length")
        x, y, u = struct.unpack(">iib", raw)
        return {"x": x, "y": y, "units": RES_UNITS.get(u, u)}
    if tag == 0x33:
        if len(raw) != 8:
            raise IppError("bad range length")
        lo, hi = struct.unpack(">ii", raw)
        return {"lower": lo, "upper": hi}
    if tag in STRING_TAGS:
        return raw.decode("utf-8", errors="surrogateescape")
    if tag in (0x35, 0x36):
        (ll,) = struct.unpack(">H", raw[:2])
        lang = raw[2 : 2 + ll].decode("utf-8")
        (vl,) = struct.unpack(">H", raw[2 + ll : 4 + ll])
        return {"lang": lang, "value": raw[4 + ll : 4 + ll + vl].decode("utf-8")}
    if tag == 0x30:
        return {"base64": base64.b64encode(raw).decode("ascii")}
    return {"hex": raw.hex()}


def _read_collection(r: _Reader) -> dict:
    """Read members after a begCollection value, up to endCollection."""
    members: list[dict] = []
    current: dict | None = None
    while True:
        tag = r.u8()
        name_len = r.u16()
        name = r.take(name_len)
        raw = r.take(r.u16())
        if tag == END_COLLECTION:
            return {"members": members}
        if tag == MEMBER_NAME:
            current = {"name": raw.decode("utf-8"), "tag": None, "values": []}
            members.append(current)
            continue
        if current is None or name:
            raise IppError("collection value without member name")
        if tag == BEG_COLLECTION:
            value: object = _read_collection(r)
        else:
            value = _decode_value(tag, raw)
        _add_value(current, tag, value)


def _add_value(attr: dict, tag: int, value: object) -> None:
    tag_name = VALUE_TAGS.get(tag)
    if tag_name is None:
        raise IppError(f"unknown value tag 0x{tag:02x}")
    if attr["tag"] is None:
        attr["tag"] = tag_name
    elif attr["tag"] != tag_name:
        # mixed tags in one attribute are legal for out-of-band, rare otherwise
        attr.setdefault("extra_tags", []).append(tag_name)
    if value is not None:
        attr["values"].append(value)


def decode(data: bytes) -> dict:
    """Decode an IPP message (request or response) into the canonical tree.

    Trailing document data, if any, is kept hex-encoded under "data_hex".
    Use decode_message() for requests that carry a document."""
    msg, body = decode_message(data)
    if body:
        msg["data_hex"] = bytes(body).hex()
    return msg


def decode_message(data: bytes) -> tuple[dict, memoryview]:
    """Decode an IPP message, returning (tree, trailing document bytes)."""
    r = _Reader(data)
    major, minor = r.u8(), r.u8()
    code = r.u16()
    request_id = struct.unpack(">I", r.take(4))[0]
    msg: dict = {
        "version": f"{major}.{minor}",
        "code": code,
        "request_id": request_id,
        "groups": [],
    }
    group: dict | None = None
    current: dict | None = None
    while True:
        tag = r.u8()
        if tag == END_TAG:
            break
        if tag < 0x10:
            group = {"tag": GROUP_TAGS.get(tag, f"group-0x{tag:02x}"), "attributes": []}
            msg["groups"].append(group)
            current = None
            continue
        if group is None:
            raise IppError("attribute outside of a group")
        name = r.take(r.u16()).decode("utf-8")
        raw = r.take(r.u16())
        if name:
            current = {"name": name, "tag": None, "values": []}
            group["attributes"].append(current)
        elif current is None:
            raise IppError("additional value without attribute")
        if tag == BEG_COLLECTION:
            value: object = _read_collection(r)
        else:
            value = _decode_value(tag, raw)
        _add_value(current, tag, value)
    return msg, memoryview(data)[r.pos :]


# ---------------------------------------------------------------- encoding


def _encode_value(tag_name: str, value: object) -> bytes:
    tag = TAG_BY_NAME[tag_name]
    if tag in OUT_OF_BAND:
        return b""
    if tag in INT_TAGS:
        return struct.pack(">i", value)
    if tag == 0x22:
        return b"\x01" if value else b"\x00"
    v: Any = value
    if tag == 0x32:
        units = {n: c for c, n in RES_UNITS.items()}.get(v["units"], v["units"])
        return struct.pack(">iib", v["x"], v["y"], units)
    if tag == 0x33:
        return struct.pack(">ii", v["lower"], v["upper"])
    if tag in STRING_TAGS:
        return str(value).encode("utf-8", errors="surrogateescape")
    if tag in (0x35, 0x36):
        lang = value["lang"].encode("utf-8")  # type: ignore[index]
        text = value["value"].encode("utf-8")  # type: ignore[index]
        return struct.pack(">H", len(lang)) + lang + struct.pack(">H", len(text)) + text
    if tag == 0x30:
        return base64.b64decode(value["base64"])  # type: ignore[index]
    return bytes.fromhex(value["hex"])  # type: ignore[index]


def _enc_attr_values(attr: dict, out: bytearray, named: bool) -> None:
    """Emit all values of one attribute. `named`: top level (name on first
    value) versus collection member (name length 0, MEMBER_NAME precedes)."""
    tag_name = attr["tag"]
    tag = TAG_BY_NAME[tag_name]
    values = attr["values"] or [None]
    for i, value in enumerate(values):
        name = attr["name"].encode("utf-8") if (named and i == 0) else b""
        if tag == BEG_COLLECTION:
            out += bytes([tag]) + struct.pack(">H", len(name)) + name + b"\x00\x00"
            for member in value["members"]:
                mname = member["name"].encode("utf-8")
                out += bytes([MEMBER_NAME]) + b"\x00\x00" + struct.pack(">H", len(mname)) + mname
                _enc_attr_values(member, out, named=False)
            out += bytes([END_COLLECTION]) + b"\x00\x00\x00\x00"
        else:
            raw = b"" if value is None else _encode_value(tag_name, value)
            out += bytes([tag]) + struct.pack(">H", len(name)) + name
            out += struct.pack(">H", len(raw)) + raw


def encode(msg: dict, data: bytes = b"") -> bytes:
    major, minor = (int(p) for p in msg["version"].split("."))
    out = bytearray([major, minor])
    out += struct.pack(">HI", msg["code"], msg["request_id"])
    for group in msg["groups"]:
        out.append(GROUP_BY_NAME[group["tag"]])
        for attr in group["attributes"]:
            _enc_attr_values(attr, out, named=True)
    out.append(END_TAG)
    out += data or bytes.fromhex(msg.get("data_hex", ""))
    return bytes(out)


# ---------------------------------------------------------------- helpers


def attr(name: str, tag: str, *values: object) -> dict:
    return {"name": name, "tag": tag, "values": list(values)}


def resolution(dpi: int) -> dict:
    return {"x": dpi, "y": dpi, "units": "dpi"}


def status_name(code: int) -> str:
    return STATUS_NAMES.get(code, f"0x{code:04x}")


def find_attr(msg: dict, name: str, group: str | None = None) -> dict | None:
    for g in msg["groups"]:
        if group and g["tag"] != group:
            continue
        for a in g["attributes"]:
            if a["name"] == name:
                return a
    return None


def make_request(
    operation: str,
    printer_uri: str,
    extra_groups: list[dict] | None = None,
    request_id: int = 1,
    requesting_user: str = "virtual-printer",
) -> dict:
    ops = [
        attr("attributes-charset", "charset", "utf-8"),
        attr("attributes-natural-language", "naturalLanguage", "en"),
        attr("printer-uri", "uri", printer_uri),
        attr("requesting-user-name", "nameWithoutLanguage", requesting_user),
    ]
    return {
        "version": "2.0",
        "code": OPERATIONS[operation],
        "request_id": request_id,
        "groups": [{"tag": "operation-attributes", "attributes": ops}, *(extra_groups or [])],
    }


# ---------------------------------------------------------------- client


def _http_target(uri: str) -> tuple[str, int, str, bool]:
    parts = urlsplit(uri)
    if parts.scheme not in ("ipp", "http", "ipps", "https"):
        raise ValueError(f"unsupported URI scheme: {uri}")
    tls = parts.scheme in ("ipps", "https")
    port = parts.port or (443 if tls else 631)
    path = parts.path or "/"
    return parts.hostname or "", port, path, tls


def send(uri: str, request: bytes, timeout: float = 10.0) -> bytes:
    """POST an encoded IPP request, return the raw response body."""
    host, port, path, tls = _http_target(uri)
    if tls:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # capture tool: printers use self-signed certs
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ctx
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", path, body=request, headers={"Content-Type": "application/ipp"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise IppError(f"HTTP {resp.status} {resp.reason}")
        return body
    finally:
        conn.close()
