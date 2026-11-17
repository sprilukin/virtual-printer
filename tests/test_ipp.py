from vp import ipp


def sample_message():
    return {
        "version": "2.0",
        "code": 0,
        "request_id": 7,
        "groups": [
            {
                "tag": "operation-attributes",
                "attributes": [
                    ipp.attr("attributes-charset", "charset", "utf-8"),
                ],
            },
            {
                "tag": "printer-attributes",
                "attributes": [
                    ipp.attr("copies-supported", "rangeOfInteger", {"lower": 1, "upper": 999}),
                    ipp.attr(
                        "printer-resolution-supported",
                        "resolution",
                        ipp.resolution(300),
                        ipp.resolution(600),
                    ),
                    ipp.attr("printer-is-accepting-jobs", "boolean", True),
                    ipp.attr("printer-state", "enum", 3),
                    ipp.attr("printer-info", "textWithLanguage", {"lang": "en", "value": "hi"}),
                    ipp.attr("blob", "octetString", {"base64": "AAEC"}),
                    ipp.attr("when", "dateTime", {"hex": "07ea0b0500000000" + "2b0000"}),
                    ipp.attr("geo", "unknown"),
                    ipp.attr(
                        "media-col-default",
                        "collection",
                        {
                            "members": [
                                ipp.attr(
                                    "media-size",
                                    "collection",
                                    {
                                        "members": [
                                            ipp.attr("x-dimension", "integer", 21000),
                                            ipp.attr("y-dimension", "integer", 29700),
                                        ]
                                    },
                                ),
                                ipp.attr("media-type", "keyword", "stationery", "plain"),
                            ]
                        },
                        {"members": [ipp.attr("media-source", "keyword", "auto")]},
                    ),
                ],
            },
        ],
    }


def test_round_trip_is_exact():
    msg = sample_message()
    raw = ipp.encode(msg)
    assert ipp.decode(raw) == msg
    assert ipp.encode(ipp.decode(raw)) == raw


def test_collection_and_multivalue_decode():
    msg = ipp.decode(ipp.encode(sample_message()))
    col = ipp.find_attr(msg, "media-col-default")
    assert len(col["values"]) == 2
    size = col["values"][0]["members"][0]
    assert size["name"] == "media-size"
    assert size["values"][0]["members"][1]["values"] == [29700]
    assert ipp.find_attr(msg, "geo")["values"] == []


def test_trailing_data_preserved():
    raw = ipp.encode(sample_message(), data=b"RaS2")
    assert ipp.decode(raw)["data_hex"] == b"RaS2".hex()
    assert ipp.encode(ipp.decode(raw)) == raw


def test_truncated_message_rejected():
    import pytest

    raw = ipp.encode(sample_message())
    with pytest.raises(ipp.IppError):
        ipp.decode(raw[:-20])


def test_status_names():
    assert ipp.status_name(0x040B) == "client-error-attributes-or-values-not-supported"
    assert ipp.status_name(0x7777) == "0x7777"
