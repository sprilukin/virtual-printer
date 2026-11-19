"""Profile loading: base + overlay merge, and validation of the result.

profiles/<name>/printer.json   a scrubbed real capture (base)
profiles/<name>/overlay.json   {"base": "<other>", "set": [attr...],
                                "remove": [names], "dnssd_txt": {k: v|null},
                                "options": {...}, "description": "..."}
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any, cast

from . import faults, ipp
from .scrub import VIRTUAL_MODEL_PREFIX, VIRTUAL_PREFIX

REQUIRED_ATTRS = [
    "printer-name",
    "printer-state",
    "printer-state-reasons",
    "printer-is-accepting-jobs",
    "ipp-versions-supported",
    "operations-supported",
    "document-format-supported",
    "charset-supported",
    "natural-language-configured",
]
# default must be one of the advertised values, when both are present
DEFAULT_IN_SUPPORTED = {
    "media-default": "media-supported",
    "sides-default": "sides-supported",
    "print-color-mode-default": "print-color-mode-supported",
    "document-format-default": "document-format-supported",
    "printer-resolution-default": "printer-resolution-supported",
}
VALIDATION_MODES = {"strict", "lenient"}
TLS_MODES = {"self-signed", "ca", "files"}


class ProfileError(Exception):
    pass


def load(name: str, profiles_dir: Path, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    if name in _seen:
        raise ProfileError(f"overlay cycle: {' -> '.join((*_seen, name))}")
    d = profiles_dir / name
    if (d / "printer.json").exists():
        profile = json.loads((d / "printer.json").read_text())
    elif (d / "overlay.json").exists():
        overlay = json.loads((d / "overlay.json").read_text())
        base = load(overlay["base"], profiles_dir, (*_seen, name))
        profile = apply_overlay(base, overlay, name)
    else:
        raise ProfileError(f"no profile {name!r} in {profiles_dir}")
    validate(profile)
    return cast("dict[str, Any]", profile)


def apply_overlay(base: dict[str, Any], overlay: dict[str, Any], name: str) -> dict[str, Any]:
    p = copy.deepcopy(base)
    p["name"] = name
    p["description"] = overlay.get("description", f"Overlay on {overlay['base']}")
    p["base"] = overlay["base"]
    removed = set(overlay.get("remove", []))
    p["attributes"] = [a for a in p["attributes"] if a["name"] not in removed]
    index = {a["name"]: i for i, a in enumerate(p["attributes"])}
    for a in overlay.get("set", []):
        if a["name"] in index:
            p["attributes"][index[a["name"]]] = a
        else:
            index[a["name"]] = len(p["attributes"])
            p["attributes"].append(a)
    if "dnssd_txt" in overlay and p.get("dnssd"):
        txt = dict(p["dnssd"]["txt"])
        for k, v in overlay["dnssd_txt"].items():
            if v is None:
                txt.pop(k, None)
            else:
                txt[k] = v
        p["dnssd"]["txt"] = [[k, v] for k, v in txt.items()]
    if "dnssd_name" in overlay and p.get("dnssd"):
        p["dnssd"]["service_name"] = overlay["dnssd_name"]
    if "dnssd_service_type" in overlay and p.get("dnssd"):
        p["dnssd"]["service_type"] = overlay["dnssd_service_type"]
    if "default_media" in overlay:
        _set_default_media(p, overlay["default_media"])
    if "margins" in overlay:
        _set_margins(p, overlay["margins"])
    p["options"] = {**p.get("options", {}), **overlay.get("options", {})}
    # an overlay is a different (virtual) device: give it its own identity
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, f"virtual-printer/{name}"))
    _replace(p, ipp.attr("printer-uuid", "uri", f"urn:uuid:{ident}"))
    if p.get("dnssd"):
        txt = dict(p["dnssd"]["txt"])
        if "UUID" in txt:
            txt["UUID"] = ident
            p["dnssd"]["txt"] = [[k, v] for k, v in txt.items()]
    return p


def _replace(p: dict[str, Any], new: dict[str, Any]) -> None:
    for i, a in enumerate(p["attributes"]):
        if a["name"] == new["name"]:
            p["attributes"][i] = new
            return
    p["attributes"].append(new)


def _get(p: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in p["attributes"] if a["name"] == name), None)


def media_dimensions(name: str) -> tuple[int, int]:
    """Size in 1/100 mm from a PWG self-describing media name."""
    m = re.search(r"_([\d.]+)x([\d.]+)(mm|in)$", name)
    if not m:
        raise ProfileError(f"cannot read dimensions from media name {name!r}")
    k = 100.0 if m.group(3) == "mm" else 2540.0
    return round(float(m.group(1)) * k), round(float(m.group(2)) * k)


def _set_default_media(p: dict[str, Any], media: str) -> None:
    x, y = media_dimensions(media)
    for n in ("media-default", "media-ready"):
        _replace(p, ipp.attr(n, "keyword", media))
    for n in ("media-col-default", "media-col-ready"):
        a = _get(p, n)
        for col in a["values"] if a else []:
            for m in col["members"]:
                if m["name"] == "media-size":
                    m["values"] = [
                        {
                            "members": [
                                ipp.attr("x-dimension", "integer", x),
                                ipp.attr("y-dimension", "integer", y),
                            ]
                        }
                    ]


def _set_margins(p: dict[str, Any], spec: dict[str, Any]) -> None:
    """spec: {"default": 0, "supported": [0, 340]} in 1/100 mm."""
    sides = ("top", "bottom", "left", "right")

    def walk(attrs: list[dict[str, Any]]) -> None:
        for a in attrs:
            if a["tag"] == "collection":
                for v in a["values"]:
                    walk(v["members"])
            elif a["name"] in {f"media-{s}-margin" for s in sides}:
                a["values"] = [spec["default"]]

    walk(p["attributes"])
    for s in sides:
        n = f"media-{s}-margin-supported"
        if _get(p, n) is not None:
            _replace(p, ipp.attr(n, "integer", *spec["supported"]))


def _validate_value(tag: str, v: Any, where: str, errors: list[str]) -> None:
    if tag in ipp.OUT_OF_BAND.values():
        errors.append(f"{where}: out-of-band tag {tag} carries a value")
    elif tag in ("integer", "enum"):
        if not isinstance(v, int) or isinstance(v, bool):
            errors.append(f"{where}: {tag} expects int")
    elif tag == "boolean":
        if not isinstance(v, bool):
            errors.append(f"{where}: boolean expects bool")
    elif tag == "resolution":
        if not (isinstance(v, dict) and {"x", "y", "units"} <= v.keys()):
            errors.append(f"{where}: bad resolution")
    elif tag == "rangeOfInteger":
        if not (isinstance(v, dict) and {"lower", "upper"} <= v.keys()):
            errors.append(f"{where}: bad range")
    elif tag == "collection":
        if not (isinstance(v, dict) and isinstance(v.get("members"), list)):
            errors.append(f"{where}: bad collection")
        else:
            for m in v["members"]:
                _validate_attr(m, f"{where}.{m.get('name', '?')}", errors)
    elif tag in ("textWithLanguage", "nameWithLanguage"):
        if not (isinstance(v, dict) and {"lang", "value"} <= v.keys()):
            errors.append(f"{where}: bad language-tagged string")
    elif tag == "octetString":
        if not (isinstance(v, dict) and "base64" in v):
            errors.append(f"{where}: octetString expects base64")
    elif tag in ipp.STRING_TAGS.values() and not isinstance(v, str):
        errors.append(f"{where}: {tag} expects string")


def _validate_attr(a: dict[str, Any], where: str, errors: list[str]) -> None:
    if not isinstance(a, dict) or not {"name", "tag", "values"} <= a.keys():
        errors.append(f"{where}: malformed attribute")
        return
    tag = a["tag"]
    if tag not in ipp.TAG_BY_NAME:
        errors.append(f"{where}: unknown tag {tag!r}")
        return
    if tag not in ipp.OUT_OF_BAND.values() and not a["values"]:
        errors.append(f"{where}: no values")
    if tag in ipp.OUT_OF_BAND.values() and a["values"]:
        errors.append(f"{where}: out-of-band attribute with values")
        return
    for i, v in enumerate(a["values"]):
        if tag not in ipp.OUT_OF_BAND.values():
            _validate_value(tag, v, f"{where}[{i}]", errors)


def _flat(a: dict[str, Any]) -> list[Any]:
    return [json.dumps(v, sort_keys=True) for v in a["values"]]


def _validate_security(options: dict[str, Any]) -> list[str]:
    """`options.tls` and `options.auth`."""
    errors: list[str] = []
    if "tls" in options:
        tls = options["tls"]
        if not isinstance(tls, dict) or tls.get("mode") not in TLS_MODES:
            errors.append(f"options.tls.mode must be one of {sorted(TLS_MODES)}")
        elif tls["mode"] == "files" and not (
            isinstance(tls.get("cert"), str) and isinstance(tls.get("key"), str)
        ):
            errors.append("options.tls with mode 'files' needs string 'cert' and 'key' paths")
    if "auth" in options:
        auth = options["auth"]
        users = auth.get("users") if isinstance(auth, dict) else None
        if not isinstance(auth, dict) or auth.get("type") != "basic":
            errors.append("options.auth.type must be 'basic'")
        elif not (
            isinstance(users, dict)
            and users
            and all(isinstance(k, str) and isinstance(v, str) for k, v in users.items())
        ):
            errors.append("options.auth.users must be a non-empty {user: password} object")
        elif not isinstance(auth.get("realm", ""), str):
            errors.append("options.auth.realm must be a string")
    return errors


def validate(profile: dict[str, Any]) -> None:
    errors: list[str] = []
    if profile.get("format") != 1:
        errors.append("format must be 1")
    attrs = {}
    for a in profile.get("attributes", []):
        if a.get("name") in attrs:
            errors.append(f"duplicate attribute {a['name']}")
        attrs[a.get("name")] = a
        _validate_attr(a, a.get("name", "?"), errors)
    errors.extend(f"missing required attribute {n}" for n in REQUIRED_ATTRS if n not in attrs)
    pn = attrs.get("printer-name")
    if pn and pn["values"]:
        v = pn["values"][0]
        text = v["value"] if isinstance(v, dict) else v
        if not text.startswith(VIRTUAL_PREFIX):
            errors.append(f"printer-name must start with {VIRTUAL_PREFIX.strip()!r}")
    mm = attrs.get("printer-make-and-model")
    if mm and mm["values"]:
        v = mm["values"][0]
        text = v["value"] if isinstance(v, dict) else v
        if not text.startswith(VIRTUAL_MODEL_PREFIX):
            errors.append(
                f"printer-make-and-model must start with {VIRTUAL_MODEL_PREFIX.strip()!r}"
            )
    dns = profile.get("dnssd")
    if dns and not dns.get("service_name", "").startswith(VIRTUAL_PREFIX):
        errors.append(f"dnssd service_name must start with {VIRTUAL_PREFIX.strip()!r}")
    for d, s in DEFAULT_IN_SUPPORTED.items():
        if (
            d in attrs
            and s in attrs
            and attrs[d]["values"]
            and _flat(attrs[d])[0] not in _flat(attrs[s])
        ):
            errors.append(f"{d} is not among {s}")
    mode = profile.get("options", {}).get("validation", "strict")
    if mode not in VALIDATION_MODES:
        errors.append(f"options.validation must be one of {sorted(VALIDATION_MODES)}")
    if "faults" in profile.get("options", {}):
        try:
            faults.validate_list(profile["options"]["faults"])
        except faults.FaultError as exc:
            errors.append(str(exc))
    opts = profile.get("options", {})
    reasons = opts.get("state_reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        errors.append("options.state_reasons must be a list of keywords")
    if opts.get("printer_state", "stopped") != "stopped":
        errors.append("options.printer_state must be 'stopped' when present")
    errors.extend(_validate_security(opts))
    if errors:
        raise ProfileError(
            f"profile {profile.get('name', '?')!r} invalid:\n  " + "\n  ".join(errors)
        )
