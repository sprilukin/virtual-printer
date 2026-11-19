import copy
import json

import pytest

from tests.test_scrub import IDS, canonical
from vp import dnssd, ipp
from vp.profile import ProfileError, apply_overlay, load, validate
from vp.scrub import scrub


@pytest.fixture
def base():
    return scrub(canonical(), "acme-laser-9", IDS)


def write(tmp_path, name, fname, doc):
    d = tmp_path / name
    d.mkdir()
    (d / fname).write_text(json.dumps(doc))


def test_overlay_set_remove_and_txt(base):
    overlay = {
        "base": "acme-laser-9",
        "set": [ipp.attr("sides-supported", "keyword", "one-sided", "two-sided-long-edge")],
        "remove": ["printer-location"],
        "dnssd_txt": {"Duplex": "T", "note": None},
        "options": {"validation": "lenient"},
    }
    p = apply_overlay(base, overlay, "duplex")
    names = {a["name"]: a for a in p["attributes"]}
    assert names["sides-supported"]["values"] == ["one-sided", "two-sided-long-edge"]
    assert "printer-location" not in names
    txt = dict(map(tuple, p["dnssd"]["txt"]))
    assert txt["Duplex"] == "T" and "note" not in txt
    assert p["options"]["validation"] == "lenient"
    assert base["options"]["validation"] == "strict"  # base untouched
    validate(p)


def test_load_base_and_overlay(tmp_path, base):
    write(tmp_path, "acme-laser-9", "printer.json", base)
    write(tmp_path, "acme-duplex", "overlay.json", {"base": "acme-laser-9", "options": {}})
    assert load("acme-duplex", tmp_path)["base"] == "acme-laser-9"
    write(tmp_path, "a", "overlay.json", {"base": "b"})
    write(tmp_path, "b", "overlay.json", {"base": "a"})
    with pytest.raises(ProfileError, match="cycle"):
        load("a", tmp_path)
    with pytest.raises(ProfileError):
        load("missing", tmp_path)


def test_validation_errors(base):
    bad = copy.deepcopy(base)
    bad["attributes"] = [a for a in bad["attributes"] if a["name"] != "printer-state"]
    bad["attributes"].append(ipp.attr("copies-supported", "rangeOfInteger", 5))
    bad["attributes"].append(ipp.attr("sides-default", "keyword", "two-sided-long-edge"))
    bad["attributes"].append(ipp.attr("sides-supported", "keyword", "one-sided"))
    bad["options"]["validation"] = "maybe"
    with pytest.raises(ProfileError) as exc:
        validate(bad)
    msg = str(exc.value)
    for frag in ("printer-state", "bad range", "sides-default is not among", "validation"):
        assert frag in msg


def test_virtual_prefix_required(base):
    bad = copy.deepcopy(base)
    bad["dnssd"]["service_name"] = "Real Name"
    with pytest.raises(ProfileError, match="service_name"):
        validate(bad)


def test_parse_dnssd_text():
    text = (
        "Brother\\032DCP-L1630W._ipp._tcp.local. can be reached at BRN1.local.:631 (interface 24)\n"
        " txtvers=1 ty=Brother\\ DCP-L1630W product=\\(Brother\\ DCP\\) rp=ipp/print\n"
    )
    d = dnssd.parse_dnssd_text(text)
    assert d["service_name"] == "Brother DCP-L1630W"
    assert d["port"] == 631
    assert dict(map(tuple, d["txt"]))["product"] == "(Brother DCP)"
    assert dict(map(tuple, d["txt"]))["ty"] == "Brother DCP-L1630W"


def test_fixed_state_option_validation(base):
    ok = copy.deepcopy(base)
    ok["options"].update({"state_reasons": ["toner-low-report"], "printer_state": "stopped"})
    validate(ok)
    bad = copy.deepcopy(base)
    bad["options"].update({"state_reasons": "toner-low-report", "printer_state": "idle"})
    with pytest.raises(ProfileError) as exc:
        validate(bad)
    assert "state_reasons must be a list" in str(exc.value)
    assert "printer_state must be 'stopped'" in str(exc.value)
