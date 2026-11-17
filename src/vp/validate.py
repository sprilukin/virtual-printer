"""Job validation against the attributes a profile advertises.

Only `*-supported` data is consulted. Real printers are more lenient than
this (the Brother answers "substituted" to values it does not advertise);
the simulator models the advertised contract, nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import ipp

OK = 0x0000
OK_SUBSTITUTED = 0x0001
UNSUPPORTED_VALUES = 0x040B
UNSUPPORTED_FORMAT = 0x040A

# Carry no printing semantics; accepted without a `-supported` counterpart.
ALWAYS_OK = {
    "job-name",
    "ipp-attribute-fidelity",
    "document-name",
    "document-natural-language",
    "job-originating-user-name",
    "requesting-user-name",
    "last-document",
    "printer-uri",
    "job-uri",
    "job-id",
    "attributes-charset",
    "attributes-natural-language",
    "status-message",
    "document-charset",
    "job-impressions",
    "job-media-sheets",
    "job-k-octets",
}
OPERATION_CHECKED = {"document-format", "compression"}


@dataclass
class Outcome:
    code: int = OK
    unsupported: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.code < 0x0400

    def to_json(self) -> dict:
        return {
            "status": ipp.status_name(self.code),
            "accepted": self.accepted,
            "unsupported": [a["name"] for a in self.unsupported],
            "notes": self.notes,
        }


def _key(v: object) -> str:
    return json.dumps(v, sort_keys=True)


def _in_range(v: object, ranges: list[dict]) -> bool:
    return isinstance(v, int) and any(r["lower"] <= v <= r["upper"] for r in ranges)


def value_supported(supported: dict, value: object) -> bool:
    """Is `value` allowed by the `*-supported` attribute?"""
    tag = supported["tag"]
    if tag == "rangeOfInteger":
        if isinstance(value, dict):
            return any(
                r["lower"] <= value["lower"] and value["upper"] <= r["upper"]
                for r in supported["values"]
            )
        return _in_range(value, supported["values"])
    if tag in ("boolean", "collection"):
        return True
    return _key(value) in {_key(v) for v in supported["values"]}


class Validator:
    def __init__(self, profile: dict):
        self.attrs = {a["name"]: a for a in profile["attributes"]}
        self.mode = profile.get("options", {}).get("validation", "strict")

    def supported(self, name: str) -> dict | None:
        return self.attrs.get(f"{name}-supported")

    def _check_media_size(self, size: dict) -> bool:
        db = self.attrs.get("media-size-supported")
        if db is None:
            return True
        want = {m["name"]: m["values"][0] for m in size["members"] if m["values"]}
        for entry in db["values"]:
            have = {m["name"]: m for m in entry["members"]}
            ok = True
            for dim in ("x-dimension", "y-dimension"):
                m = have.get(dim)
                if m is None or dim not in want:
                    ok = False
                    break
                if m["tag"] == "rangeOfInteger":
                    ok = ok and _in_range(want[dim], m["values"])
                else:
                    ok = ok and want[dim] in m["values"]
            if ok:
                return True
        return False

    def _check_media_col(self, col: dict) -> bool:
        member_names = self.attrs.get("media-col-supported")
        for m in col["members"]:
            n, vals = m["name"], m["values"]
            if member_names and n not in member_names["values"]:
                return False
            if n == "media-size":
                if not all(self._check_media_size(v) for v in vals):
                    return False
                continue
            sup = self.supported(n)
            if sup is not None and not all(value_supported(sup, v) for v in vals):
                return False
        return True

    def _check_attr(self, a: dict) -> bool:
        name = a["name"]
        if a["tag"] in ipp.OUT_OF_BAND.values():
            return True
        if name == "media-col":
            return all(self._check_media_col(v) for v in a["values"])
        sup = self.supported(name)
        if sup is not None:
            return all(value_supported(sup, v) for v in a["values"])
        jcas = self.attrs.get("job-creation-attributes-supported")
        if jcas is not None:
            return name in jcas["values"]
        return True

    def validate(self, request: dict) -> Outcome:
        out = Outcome()
        bad_format = False
        for g in request["groups"]:
            if g["tag"] not in ("operation-attributes", "job-attributes"):
                continue
            for a in g["attributes"]:
                name = a["name"]
                if g["tag"] == "operation-attributes":
                    if name not in OPERATION_CHECKED:
                        continue
                    sup = self.supported(name)
                    if sup is not None and not all(value_supported(sup, v) for v in a["values"]):
                        out.notes.append(f"{name}={_describe(a)} not advertised")
                        if name == "document-format":
                            bad_format = True
                        else:
                            out.unsupported.append(a)
                    continue
                if name in ALWAYS_OK:
                    continue
                if not self._check_attr(a):
                    out.unsupported.append(a)
                    out.notes.append(f"{name}={_describe(a)} not advertised")
        if bad_format and self.mode == "strict":
            out.code = UNSUPPORTED_FORMAT
        elif out.unsupported:
            out.code = UNSUPPORTED_VALUES if self.mode == "strict" else OK_SUBSTITUTED
        return out


def _describe(a: dict) -> str:
    return ",".join(_key(v).strip('"') for v in a["values"])
