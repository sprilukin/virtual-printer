from pathlib import Path

from vp import mdns, profile

PROFILES = Path(__file__).parent.parent / "profiles"


def test_overlays_are_distinct_devices():
    base = profile.load("brother-dcp-l1630w", PROFILES)
    uuids = set()
    for n in ("brother-dcp-l1630w", "duplex-color-laser-300dpi", "borderless-inkjet-letter"):
        p = profile.load(n, PROFILES)
        a = {x["name"]: x for x in p["attributes"]}
        assert a["printer-name"]["values"][0].startswith("[VIRTUAL] ")
        txt = dict(p["dnssd"]["txt"])
        assert a["printer-uuid"]["values"][0] == "urn:uuid:" + txt["UUID"]
        uuids.add(txt["UUID"])
    assert len(uuids) == 3
    assert (
        base["dnssd"]["txt"] != profile.load("duplex-color-laser-300dpi", PROFILES)["dnssd"]["txt"]
    )


def test_borderless_overlay_margins_and_media():
    p = profile.load("borderless-inkjet-letter", PROFILES)
    a = {x["name"]: x for x in p["attributes"]}
    assert a["media-default"]["values"] == ["na_letter_8.5x11in"]
    for s in ("top", "bottom", "left", "right"):
        assert a[f"media-{s}-margin-supported"]["values"] == [0]


def test_service_info_replays_txt_verbatim():
    p = profile.load("brother-dcp-l1630w", PROFILES)
    info = mdns.service_info(p, 6310, ["192.0.2.7"], "vp-x")
    assert info.name == "[VIRTUAL] Brother DCP-L1630W._ipp._tcp.local."
    assert info.port == 6310 and info.parsed_addresses() == ["192.0.2.7"]
    assert {k.decode(): (v or b"").decode() for k, v in info.properties.items()} == dict(
        p["dnssd"]["txt"]
    )
