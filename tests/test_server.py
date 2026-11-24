import asyncio
import json
from pathlib import Path

from vp import ipp, jobs, png, profile
from vp.server import Printer

from .helpers import blank_page, ipp_call, make_pwg, status

PROFILES = Path(__file__).parent.parent / "profiles"
A = ipp.attr


def run(coro):
    return asyncio.run(coro)


async def with_printer(name, fn, tmp_path, lenient=False):
    prof = profile.load(name, PROFILES)
    if lenient:
        prof["options"]["validation"] = "lenient"
    p = Printer(prof, tmp_path / "spool")
    await p.start()
    try:
        return await fn(p)
    finally:
        await p.stop()


def names(resp, group="printer-attributes"):
    return {a["name"]: a for g in resp["groups"] if g["tag"] == group for a in g["attributes"]}


def test_get_printer_attributes_replays_profile(tmp_path):
    async def go(p):
        r = await ipp_call(p, "Get-Printer-Attributes")
        assert status(r) == "successful-ok"
        a = names(r)
        assert a["printer-name"]["values"] == ["[VIRTUAL] Brother DCP-L1630W"]
        assert a["printer-state"]["values"] == [3]
        assert a["printer-uri-supported"]["values"] == [f"ipp://127.0.0.1:{p.port}/ipp/print"]
        assert all("vp-brother" not in v for v in a["printer-icons"]["values"])
        assert "media-col-database" in a
        assert a["printer-current-time"]["tag"] == "dateTime"
        return r

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_requested_attributes_and_unknown(tmp_path):
    async def go(p):
        r = await ipp_call(
            p,
            "Get-Printer-Attributes",
            [A("requested-attributes", "keyword", "sides-supported", "no-such-attribute")],
        )
        assert status(r) == "successful-ok-ignored-or-substituted-attributes"
        assert set(names(r)) == {"sides-supported"}
        assert "no-such-attribute" in names(r, "unsupported-attributes")
        r = await ipp_call(
            p, "Get-Printer-Attributes", [A("requested-attributes", "keyword", "job-template")]
        )
        got = set(names(r))
        assert "sides-default" in got and "printer-name" not in got

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_print_job_produces_receipt_and_pngs(tmp_path):
    doc = make_pwg([blank_page(300, 400), blank_page(300, 400)])

    async def go(p):
        r = await ipp_call(
            p,
            "Print-Job",
            [
                A("document-format", "mimeMediaType", "image/pwg-raster"),
                A("job-name", "nameWithoutLanguage", "hello"),
            ],
            [
                A("copies", "integer", 2),
                A("sides", "keyword", "one-sided"),
                A("printer-resolution", "resolution", ipp.resolution(600)),
            ],
            doc,
        )
        assert status(r) == "successful-ok"
        job = names(r, "job-attributes")
        jid = job["job-id"]["values"][0]
        assert job["job-state"]["values"][0] in (jobs.PROCESSING, jobs.COMPLETED)
        await p.wait_idle()
        r = await ipp_call(p, "Get-Job-Attributes", [A("job-id", "integer", jid)])
        assert names(r, "job-attributes")["job-state"]["values"] == [jobs.COMPLETED]
        d = tmp_path / "spool" / f"job-{jid:04d}"
        receipt = json.loads((d / "receipt.json").read_text())
        assert receipt["page_count"] == 2 and receipt["copies"] == 2
        assert receipt["sides"] == "one-sided" and receipt["resolution_dpi"] == [600, 600]
        assert receipt["pixel_dimensions"] == [300, 400] and receipt["validation"]["accepted"]
        assert receipt["job_name"] == "hello"
        assert (d / "document.pwg").read_bytes() == doc
        w, h, rows = png.read((d / "page-002.png").read_bytes())
        assert (w, h) == (300, 400)
        assert tuple(rows[0][:3])[1] < 255 and tuple(rows[200][450:453]) == (
            255,
            255,
            255,
        )  # margins shaded
        # Get-Jobs
        r = await ipp_call(p, "Get-Jobs", [A("which-jobs", "keyword", "completed")])
        assert [
            a["values"][0] for a in names(r, "job-attributes").values() if a["name"] == "job-id"
        ] == [jid]

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_print_job_strict_rejects_and_lenient_accepts(tmp_path):
    doc = make_pwg([blank_page(50, 50)])
    sides = [A("sides", "keyword", "two-sided-long-edge")]
    ops = [A("document-format", "mimeMediaType", "image/pwg-raster")]

    async def strict(p):
        r = await ipp_call(p, "Print-Job", ops, sides, doc)
        assert status(r) == "client-error-attributes-or-values-not-supported"
        assert "sides" in names(r, "unsupported-attributes")
        assert not p.store.jobs
        r = await ipp_call(p, "Validate-Job", ops, sides)
        assert status(r) == "client-error-attributes-or-values-not-supported"
        assert status(await ipp_call(p, "Validate-Job", ops)) == "successful-ok"

    async def lenient(p):
        r = await ipp_call(p, "Print-Job", ops, sides, doc)
        assert status(r) == "successful-ok-ignored-or-substituted-attributes"
        await p.wait_idle()
        assert p.store.get(1).state == jobs.COMPLETED

    run(with_printer("brother-dcp-l1630w", strict, tmp_path / "a"))
    run(with_printer("brother-dcp-l1630w", lenient, tmp_path / "b", lenient=True))


def test_duplex_overlay_receipt(tmp_path):
    doc = make_pwg([blank_page(60, 80, duplex=1, dpi=300), blank_page(60, 80, duplex=1, dpi=300)])

    async def go(p):
        r = await ipp_call(
            p,
            "Print-Job",
            [A("document-format", "mimeMediaType", "image/pwg-raster")],
            [A("sides", "keyword", "two-sided-long-edge")],
            doc,
        )
        assert status(r) == "successful-ok"
        await p.wait_idle()
        rc = p.store.get(1).receipt
        assert (
            rc["sides"] == "two-sided-long-edge"
            and rc["sheet_count"] == 1
            and rc["page_count"] == 2
        )

    run(with_printer("duplex-color-laser-300dpi", go, tmp_path))


def test_garbage_document_aborts_job(tmp_path):
    async def go(p):
        r = await ipp_call(
            p,
            "Print-Job",
            [A("document-format", "mimeMediaType", "image/pwg-raster")],
            (),
            b"not a raster",
        )
        assert status(r) == "successful-ok"
        await p.wait_idle()
        j = p.store.get(1)
        assert j.state == jobs.ABORTED and "document-format-error" in j.reasons

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_create_job_send_document_cancel(tmp_path):
    doc = make_pwg([blank_page(40, 40)])

    async def go(p):
        r = await ipp_call(p, "Create-Job")
        jid = names(r, "job-attributes")["job-id"]["values"][0]
        assert names(r, "job-attributes")["job-state"]["values"] == [jobs.PENDING_HELD]
        r = await ipp_call(
            p,
            "Send-Document",
            [
                A("job-id", "integer", jid),
                A("last-document", "boolean", True),
                A("document-format", "mimeMediaType", "image/pwg-raster"),
            ],
            (),
            doc,
        )
        assert status(r) == "successful-ok"
        await p.wait_idle()
        assert p.store.get(jid).state == jobs.COMPLETED
        r = await ipp_call(p, "Cancel-Job", [A("job-id", "integer", jid)])
        assert status(r) == "client-error-not-possible"
        r = await ipp_call(p, "Get-Job-Attributes", [A("job-id", "integer", 99)])
        assert status(r) == "client-error-not-found"

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_protocol_errors(tmp_path):
    async def go(p):
        assert (
            status(await ipp_call(p, "Get-Printer-Attributes", version="3.0"))
            == "server-error-version-not-supported"
        )
        p.accepting = False
        r = await ipp_call(
            p, "Print-Job", [A("document-format", "mimeMediaType", "image/pwg-raster")], (), b"x"
        )
        assert status(r) == "server-error-not-accepting-jobs"
        r = await ipp_call(p, "Get-Printer-Attributes")
        assert names(r)["printer-is-accepting-jobs"]["values"] == [False]
        # operation the profile does not advertise
        r = await ipp_call(p, "Get-Printer-Attributes")
        bad = ipp.encode({**ipp.make_request("Get-Printer-Attributes", "ipp://x"), "code": 0x0012})
        assert ipp.decode(await p.handle_ipp(bad, "x"))["code"] == 0x0501
        assert ipp.decode(await p.handle_ipp(b"\x02\x00garbage", "x"))["code"] == 0x0400

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_http_chunked_upload_with_expect(tmp_path):
    doc = make_pwg([blank_page(30, 30)])

    async def go(p):
        uri = f"ipp://127.0.0.1:{p.port}{p.resource}"
        req = ipp.make_request("Print-Job", uri)
        req["groups"][0]["attributes"].append(
            A("document-format", "mimeMediaType", "image/pwg-raster")
        )
        body = ipp.encode(req, doc)
        reader, writer = await asyncio.open_connection("127.0.0.1", p.port)
        writer.write(
            (
                f"POST {p.resource} HTTP/1.1\r\nHost: h:1\r\nContent-Type: application/ipp\r\n"
                "Transfer-Encoding: chunked\r\nExpect: 100-continue\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 100")
        await reader.readline()
        for i in range(0, len(body), 700):
            c = body[i : i + 700]
            writer.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        assert "200 OK" in head
        n = int(
            next(
                l.split(":")[1] for l in head.splitlines() if l.lower().startswith("content-length")
            )
        )
        resp = ipp.decode(await reader.readexactly(n))
        assert status(resp) == "successful-ok"
        # keep-alive: a second request on the same connection, wrong path -> 404
        writer.write(
            b"POST /nope HTTP/1.1\r\nHost: h\r\nContent-Type: application/ipp\r\nContent-Length: 0\r\n\r\n"
        )
        await writer.drain()
        assert b"404" in await reader.readline()
        writer.close()
        await p.wait_idle()
        assert p.store.get(1).state == jobs.COMPLETED

    run(with_printer("brother-dcp-l1630w", go, tmp_path))


def test_fixed_state_overlays(tmp_path):
    async def reasons_and_state(p):
        a = names(await ipp_call(p, "Get-Printer-Attributes"))
        return a["printer-state-reasons"]["values"], a["printer-state"]["values"]

    async def toner(p):
        assert await reasons_and_state(p) == (["toner-low-report"], [3])
        p.reset()
        assert await reasons_and_state(p) == (["toner-low-report"], [3])

    async def stopped(p):
        assert await reasons_and_state(p) == (["none"], [5])
        p.reset()
        assert await reasons_and_state(p) == (["none"], [5])

    run(with_printer("toner-low-only", toner, tmp_path))
    run(with_printer("stopped-no-reason", stopped, tmp_path))


# --- epson-et3950-inferred: an INFERRED profile (not recorded from a real printer) ---


def _http(p, method, path, body=None, headers=None):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def test_epson_inferred_404_everywhere_but_ipp(tmp_path):
    async def go(p):
        assert p.resource == "/ipp"
        for method in ("GET", "POST"):
            for path in ("/ipp/print", "/", "/other"):
                body = b"x" if method == "POST" else None
                st, h, data = await asyncio.to_thread(
                    _http, p, method, path, body, {"Content-Type": "application/ipp"}
                )
                assert st == 404, (method, path)
                assert h["Server"] == "EPSON_Linux UPnP/1.0"
                assert h["Content-Type"] == "text/html"
                assert data == b""

    run(with_printer("epson-et3950-inferred", go, tmp_path))


def test_epson_inferred_ipp_attributes(tmp_path):
    async def go(p):
        r = await ipp_call(p, "Get-Printer-Attributes")
        assert status(r) == "successful-ok"
        a = names(r)
        assert a["printer-make-and-model"]["values"] == ["EPSON ET-3950 Series"]
        assert a["printer-name"]["values"][0].startswith("[VIRTUAL] ")
        assert "inferred, not recorded" in a["printer-name"]["values"][0]
        assert a["printer-uuid"]["values"][0].startswith("urn:uuid:")
        assert {"image/pwg-raster", "image/urf", "application/pdf"} <= set(
            a["document-format-supported"]["values"]
        )
        assert {"x": 300, "y": 300, "units": "dpi"} in a[
            "pwg-raster-document-resolution-supported"
        ]["values"]
        assert "srgb_8" in a["pwg-raster-document-type-supported"]["values"]
        assert {"iso_a4_210x297mm", "na_letter_8.5x11in"} <= set(a["media-supported"]["values"])
        assert "one-sided" in a["sides-supported"]["values"]
        assert a["printer-state"]["values"] == [3]
        assert a["printer-uri-supported"]["values"] == [f"ipp://127.0.0.1:{p.port}/ipp"]

    run(with_printer("epson-et3950-inferred", go, tmp_path))


def test_epson_inferred_uuid_is_stable_and_labelled():
    a = profile.load("epson-et3950-inferred", PROFILES)
    b = profile.load("epson-et3950-inferred", PROFILES)
    ids = [x["values"] for x in a["attributes"] if x["name"] == "printer-uuid"]
    assert ids == [x["values"] for x in b["attributes"] if x["name"] == "printer-uuid"]
    assert "INFERRED, NOT RECORDED" in a["description"]


def test_real_model_name_is_opt_in():
    import pytest

    prof = profile.load("duplex-color-laser-300dpi", PROFILES)
    for x in prof["attributes"]:
        if x["name"] == "printer-make-and-model":
            x["values"] = ["EPSON ET-3950 Series"]
    with pytest.raises(profile.ProfileError, match="printer-make-and-model"):
        profile.validate(prof)
    prof["options"]["real_model_name"] = True
    profile.validate(prof)


def test_request_log_lines(tmp_path):
    lines: list[str] = []

    async def go():
        prof = profile.load("brother-dcp-l1630w", PROFILES)
        p = Printer(prof, tmp_path / "spool", request_log=lines.append)
        await p.start()
        try:
            await ipp_call(p, "Get-Printer-Attributes")
            reader, writer = await asyncio.open_connection(p.host, p.port)
            writer.write(b"GET /nope HTTP/1.1\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
        finally:
            await p.stop()

    run(go())
    assert any(
        "POST /ipp/print Get-Printer-Attributes" in x and "successful-ok" in x for x in lines
    )
    assert any("GET /nope" in x and "HTTP 404" in x for x in lines)
