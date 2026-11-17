import copy
from pathlib import Path

import pytest

from vp import ipp, profile
from vp.validate import Validator

PROFILES = Path(__file__).parent.parent / "profiles"


def request(*job_attrs, op_attrs=()):
    r = ipp.make_request(
        "Print-Job", "ipp://x/ipp/print", [{"tag": "job-attributes", "attributes": list(job_attrs)}]
    )
    r["groups"][0]["attributes"].extend(op_attrs)
    return r


def media_col(x, y, margin=423):
    size = {
        "members": [ipp.attr("x-dimension", "integer", x), ipp.attr("y-dimension", "integer", y)]
    }
    return ipp.attr(
        "media-col",
        "collection",
        {
            "members": [
                ipp.attr("media-size", "collection", size),
                ipp.attr("media-top-margin", "integer", margin),
            ]
        },
    )


@pytest.fixture
def brother():
    return profile.load("brother-dcp-l1630w", PROFILES)


def test_baseline_job_is_accepted(brother):
    out = Validator(brother).validate(
        request(
            ipp.attr("copies", "integer", 1),
            ipp.attr("sides", "keyword", "one-sided"),
            ipp.attr("printer-resolution", "resolution", ipp.resolution(600)),
            ipp.attr("print-color-mode", "keyword", "monochrome"),
            ipp.attr("media", "keyword", "iso_a4_210x297mm"),
            media_col(21000, 29700),
        )
    )
    assert out.accepted and out.code == 0


@pytest.mark.parametrize(
    "a",
    [
        ipp.attr("sides", "keyword", "two-sided-long-edge"),
        ipp.attr("printer-resolution", "resolution", ipp.resolution(720)),
        ipp.attr("print-color-mode", "keyword", "color"),
        ipp.attr("media", "keyword", "iso_a3_297x420mm"),
        ipp.attr("copies", "integer", 1000),
        media_col(29700, 42000),
        media_col(21000, 29700, margin=0),
        ipp.attr("page-delivery", "keyword", "reverse-order"),
    ],
)
def test_strict_rejects_unadvertised(brother, a):
    out = Validator(brother).validate(request(a))
    assert out.code == 0x040B
    assert [u["name"] for u in out.unsupported] == [a["name"]]


def test_lenient_substitutes(brother):
    p = copy.deepcopy(brother)
    p["options"]["validation"] = "lenient"
    out = Validator(p).validate(request(ipp.attr("sides", "keyword", "two-sided-long-edge")))
    assert out.accepted and out.code == 0x0001 and out.unsupported


def test_document_format(brother):
    bad = request(op_attrs=[ipp.attr("document-format", "mimeMediaType", "application/pdf")])
    assert Validator(brother).validate(bad).code == 0x040A
    ok = request(op_attrs=[ipp.attr("document-format", "mimeMediaType", "image/pwg-raster")])
    assert Validator(brother).validate(ok).code == 0


def test_overlays_change_what_is_accepted():
    duplex = profile.load("duplex-color-laser-300dpi", PROFILES)
    v = Validator(duplex)
    assert v.validate(request(ipp.attr("sides", "keyword", "two-sided-long-edge"))).accepted
    assert v.validate(request(ipp.attr("print-color-mode", "keyword", "color"))).accepted
    assert not v.validate(
        request(ipp.attr("printer-resolution", "resolution", ipp.resolution(600)))
    ).accepted
