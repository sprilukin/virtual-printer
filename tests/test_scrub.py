import base64

import pytest

from vp import ipp, profile
from vp.scrub import Identifiers, LeakError, check_leaks, scrub

HOST = "PRN0123456789AB"
MAC = "00:1A:2B:3C:4D:5E"
IP = "10.20.30.40"
SERIAL = "SN-Q7W8E9R"


def canonical(extra=None):
    attrs = [
        ipp.attr("printer-name", "nameWithoutLanguage", HOST),
        ipp.attr("printer-make-and-model", "textWithoutLanguage", "Acme Laser 9"),
        ipp.attr("printer-state", "enum", 3),
        ipp.attr("printer-state-reasons", "keyword", "none"),
        ipp.attr("printer-is-accepting-jobs", "boolean", True),
        ipp.attr("ipp-versions-supported", "keyword", "2.0"),
        ipp.attr("operations-supported", "enum", 2, 4, 11),
        ipp.attr("document-format-supported", "mimeMediaType", "image/pwg-raster"),
        ipp.attr("charset-supported", "charset", "utf-8"),
        ipp.attr("natural-language-configured", "naturalLanguage", "en"),
        ipp.attr(
            "printer-uri-supported",
            "uri",
            f"ipp://{HOST}.local./ipp/print",
            f"ipp://{IP}:631/ipp/print",
        ),
        ipp.attr("printer-uuid", "uri", "urn:uuid:11111111-2222-3333-4444-001a2b3c4d5e"),
        ipp.attr("printer-device-id", "textWithoutLanguage", f"MFG:Acme;MDL:L9;SN:{SERIAL};"),
        ipp.attr("printer-location", "textWithoutLanguage", "Room 12, real building"),
        ipp.attr("printer-up-time", "integer", 1234),
        ipp.attr(
            "vendor-collection",
            "collection",
            {
                "members": [
                    ipp.attr(
                        "nested",
                        "collection",
                        {
                            "members": [
                                ipp.attr("mac", "keyword", MAC.lower().replace(":", "-")),
                            ]
                        },
                    ),
                ]
            },
        ),
    ]
    if extra:
        attrs.append(extra)
    return {
        "format": 1,
        "ipp_version": "2.0",
        "attributes": attrs,
        "dnssd": {
            "service_type": "_ipp._tcp",
            "service_name": "Acme",
            "txt": [
                ["UUID", "11111111-2222-3333-4444-001a2b3c4d5e"],
                ["adminurl", f"http://{HOST}.local./admin"],
                ["note", "second floor"],
                ["ty", "Acme Laser 9"],
            ],
        },
    }


IDS = Identifiers(hosts=[HOST + ".local"], ips=[IP], serials=[SERIAL], macs=[MAC])


def dump(doc):
    import json

    return json.dumps(doc).lower()


def test_scrub_removes_planted_identifiers_everywhere():
    out = scrub(canonical(), "acme-laser-9", IDS)
    text = dump(out)
    for secret in (HOST, IP, SERIAL, "001a2b3c4d5e", "00-1a-2b", "room 12", "second floor", "1234"):
        assert secret.lower() not in text, secret
    names = {a["name"] for a in out["attributes"]}
    assert "printer-up-time" not in names
    assert profile.load  # profile module importable
    profile.validate(out)


def test_scrub_is_deterministic_and_virtual_named():
    a = scrub(canonical(), "acme-laser-9", IDS)
    b = scrub(canonical(), "acme-laser-9", IDS)
    assert a == b
    assert a["dnssd"]["service_name"] == "[VIRTUAL] Acme Laser 9"
    name = next(x for x in a["attributes"] if x["name"] == "printer-name")
    assert name["values"] == ["[VIRTUAL] Acme Laser 9"]


@pytest.mark.parametrize("secret", [HOST, IP, SERIAL, MAC, MAC.replace(":", "").lower()])
def test_leak_check_catches_plain_nested_value(secret):
    doc = scrub(canonical(), "x", IDS)
    doc["attributes"].append(
        ipp.attr(
            "sneaky",
            "collection",
            {
                "members": [
                    ipp.attr(
                        "deep",
                        "collection",
                        {
                            "members": [
                                ipp.attr(
                                    "v", "textWithoutLanguage", f"prefix {secret.upper()} suffix"
                                )
                            ]
                        },
                    )
                ]
            },
        )
    )
    with pytest.raises(LeakError):
        check_leaks(doc, IDS)


def test_leak_check_catches_octet_string_and_attribute_name():
    doc = scrub(canonical(), "x", IDS)
    blob = base64.b64encode(f"junk{SERIAL}junk".encode()).decode()
    leaky = dict(
        doc, attributes=[*doc["attributes"], ipp.attr("blob", "octetString", {"base64": blob})]
    )
    with pytest.raises(LeakError):
        check_leaks(leaky, IDS)
    named = dict(doc, attributes=[*doc["attributes"], ipp.attr(f"x-{IP}", "keyword", "a")])
    with pytest.raises(LeakError):
        check_leaks(named, IDS)


def test_scrub_fails_when_leak_is_hidden_from_substitution():
    # octet strings are not rewritten by substitution, so this must fail the run
    blob = base64.b64encode(SERIAL.encode()).decode()
    c = canonical(ipp.attr("vendor-blob", "octetString", {"base64": blob}))
    with pytest.raises(LeakError) as exc:
        scrub(c, "x", IDS)
    assert SERIAL not in str(exc.value)  # the error never echoes the identifier


def test_short_identifier_rejected():
    with pytest.raises(ValueError):
        Identifiers(serials=["ab"]).variants()


def test_bad_mac_rejected():
    with pytest.raises(ValueError):
        Identifiers(macs=["12:34"]).variants()


def test_model_names_say_virtual():
    doc = scrub(canonical(), "acme", IDS)
    attrs = {a["name"]: a["values"][0] for a in doc["attributes"]}
    txt = dict(map(tuple, doc["dnssd"]["txt"]))
    assert attrs["printer-make-and-model"] == "Virtual Acme Laser 9"
    assert txt["ty"] == "Virtual Acme Laser 9"
