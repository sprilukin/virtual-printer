import asyncio
import json
import socket
import time
from pathlib import Path

import pytest

from vp import faults, ipp, jobs, profile
from vp.control import ControlServer
from vp.server import Printer

from .helpers import blank_page, make_pwg, status
from .test_server import names

PROFILES = Path(__file__).parent.parent / "profiles"
A = ipp.attr
DOC = make_pwg([blank_page(40, 40)])
PWG = [A("document-format", "mimeMediaType", "image/pwg-raster")]


async def with_control(tmp_path, fn, startup=None):
    prof = profile.load("brother-dcp-l1630w", PROFILES)
    if startup:
        prof["options"]["faults"] = startup
    p = Printer(prof, tmp_path / "spool")
    ctl = ControlServer(p)
    await p.start()
    await ctl.start()
    try:
        return await fn(p, ctl)
    finally:
        await ctl.stop()
        await p.stop()


async def http(ctl, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    reader, writer = await asyncio.open_connection("127.0.0.1", ctl.port)
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw
    )
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    code = int(head.split()[1])
    ctype = next(l for l in head.decode().splitlines() if l.lower().startswith("content-type"))
    return code, payload if "image/png" in ctype else json.loads(payload)


async def call(p, op, attrs=(), data=b""):
    # imported lazily: helpers.ipp_call is the shared IPP client
    from .helpers import ipp_call

    return await ipp_call(p, op, attrs, (), data)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- validation


def test_validate_fills_defaults_and_rejects_nonsense():
    assert faults.validate("reset-mid-upload", None)[1] == {"after_bytes": 1024}
    assert faults.validate("busy", {"count": 2})[1] == {"count": 2}
    assert faults.validate("media-jam", {"severity": "warning"})[1]["job_mode"] == "hold"
    for bad in [
        ("nope", {}),
        ("busy", {"seconds": 1}),
        ("busy", {"count": 0}),
        ("busy", {"count": True}),
        ("slow-completion", {"seconds": -1}),
        ("media-empty", {"severity": "loud"}),
        ("media-empty", {"count": 1}),
        ("busy", [1]),
    ]:
        with pytest.raises(faults.FaultError):
            faults.validate(*bad)


def test_profile_rejects_bad_startup_faults():
    prof = profile.load("brother-dcp-l1630w", PROFILES)
    prof["options"]["faults"] = [{"type": "nope"}]
    with pytest.raises(profile.ProfileError, match="unknown fault"):
        profile.validate(prof)


# ---------------------------------------------------------------- control API


def test_control_state_jobs_pages_and_errors(tmp_path):
    async def go(p, ctl):
        code, st = await http(ctl, "GET", "/state")
        assert code == 200 and st["printer_state"] == "idle" and st["faults"] == {}
        assert st["state_reasons"] == ["none"] and "busy" in st["fault_types"]
        r = await call(p, "Print-Job", PWG, DOC)
        jid = names(r, "job-attributes")["job-id"]["values"][0]
        await p.wait_idle()
        code, js = await http(ctl, "GET", "/jobs")
        assert code == 200 and [j["id"] for j in js] == [jid] and js[0]["state"] == "completed"
        code, one = await http(ctl, "GET", f"/jobs/{jid}")
        assert one["receipt"]["page_count"] == 1
        code, img = await http(ctl, "GET", f"/jobs/{jid}/pages/1.png")
        assert code == 200 and img[:8] == b"\x89PNG\r\n\x1a\n"
        assert (await http(ctl, "GET", f"/jobs/{jid}/pages/9.png"))[0] == 404
        assert (await http(ctl, "GET", "/jobs/77"))[0] == 404
        assert (await http(ctl, "GET", "/nope"))[0] == 404
        assert (await http(ctl, "POST", "/state"))[0] == 405
        assert (await http(ctl, "POST", "/fault", {"type": "nope"}))[0] == 400
        assert (await http(ctl, "POST", "/fault", {"type": "busy", "x": 1}))[0] == 400
        code, _ = await http(ctl, "POST", "/reset")
        assert code == 200 and not p.store.jobs

    run(with_control(tmp_path, go))


def test_busy_fault_counts_down_and_clears(tmp_path):
    async def go(p, ctl):
        _, out = await http(ctl, "POST", "/fault", {"type": "busy", "params": {"count": 2}})
        assert out["faults"] == {"busy": {"count": 2}}
        for _ in range(2):
            assert status(await call(p, "Print-Job", PWG, DOC)) == "server-error-busy"
        assert not p.store.jobs
        assert status(await call(p, "Print-Job", PWG, DOC)) == "successful-ok"
        # explicit off, and reads are never affected
        await http(ctl, "POST", "/fault", {"type": "busy"})
        assert status(await call(p, "Get-Printer-Attributes")) == "successful-ok"
        assert status(await call(p, "Create-Job")) == "server-error-busy"
        await http(ctl, "POST", "/fault", {"type": "busy", "enabled": False})
        assert (await http(ctl, "GET", "/state"))[1]["faults"] == {}

    run(with_control(tmp_path, go))


def test_startup_faults_survive_reset_runtime_ones_do_not(tmp_path):
    async def go(p, ctl):
        assert status(await call(p, "Create-Job")) == "server-error-busy"
        await http(ctl, "POST", "/fault", {"type": "media-jam"})
        await http(ctl, "POST", "/reset")
        faults_now = (await http(ctl, "GET", "/state"))[1]["faults"]
        assert faults_now == {"busy": {}}

    run(with_control(tmp_path, go, startup=[{"type": "busy"}]))


# ---------------------------------------------------------------- printer-state faults


def test_media_empty_holds_jobs_until_cleared(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "media-empty"})
        a = names(await call(p, "Get-Printer-Attributes"))
        assert a["printer-state"]["values"] == [5]
        assert a["printer-state-reasons"]["values"] == ["media-empty-error"]
        r = await call(p, "Print-Job", PWG, DOC)
        jid = names(r, "job-attributes")["job-id"]["values"][0]
        await asyncio.sleep(0.15)
        job = p.store.get(jid)
        assert job.state == jobs.PROCESSING_STOPPED and job.reasons == ["printer-stopped"]
        await http(ctl, "POST", "/fault", {"type": "media-empty", "enabled": False})
        await p.wait_idle()
        assert job.state == jobs.COMPLETED
        assert names(await call(p, "Get-Printer-Attributes"))["printer-state"]["values"] == [3]

    run(with_control(tmp_path, go))


def test_jam_abort_mode_and_cancel_of_held_job(tmp_path):
    async def go(p, ctl):
        await http(
            ctl,
            "POST",
            "/fault",
            {"type": "media-jam", "params": {"severity": "warning", "job_mode": "abort"}},
        )
        await call(p, "Print-Job", PWG, DOC)
        await p.wait_idle()
        assert p.store.get(1).state == jobs.ABORTED
        assert (await http(ctl, "GET", "/state"))[1]["state_reasons"] == ["media-jam-warning"]
        await http(ctl, "POST", "/fault", {"type": "media-jam", "enabled": False})
        await http(ctl, "POST", "/fault", {"type": "toner-empty"})
        await call(p, "Print-Job", PWG, DOC)
        await asyncio.sleep(0.1)
        assert p.store.get(2).state == jobs.PROCESSING_STOPPED
        await call(p, "Cancel-Job", [A("job-id", "integer", 2)])
        await p.wait_idle()
        assert p.store.get(2).state == jobs.CANCELED

    run(with_control(tmp_path, go))


def test_slow_completion_keeps_job_processing(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "slow-completion", "params": {"seconds": 0.4}})
        t0 = time.monotonic()
        await call(p, "Print-Job", PWG, DOC)
        await asyncio.sleep(0.1)
        assert p.store.get(1).state == jobs.PROCESSING
        assert (await http(ctl, "GET", "/state"))[1]["printer_state"] == "processing"
        await p.wait_idle()
        assert p.store.get(1).state == jobs.COMPLETED and time.monotonic() - t0 >= 0.4
        # count-less fault stays on: next job is slow too
        await call(p, "Print-Job", PWG, DOC)
        await asyncio.sleep(0.1)
        assert p.store.get(2).state == jobs.PROCESSING

    run(with_control(tmp_path, go))


# ---------------------------------------------------------------- transport faults


def raw_post(port, body, read=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    head = (
        f"POST /ipp/print HTTP/1.1\r\nHost: h\r\nContent-Type: application/ipp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    s.sendall(head + body)
    return s


def ipp_body(p, op="Print-Job", data=DOC):
    req = ipp.make_request(op, f"ipp://127.0.0.1:{p.port}{p.resource}")
    req["groups"][0]["attributes"].extend(PWG)
    return ipp.encode(req, data)


def test_unauthorized_returns_401_with_challenge(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "unauthorized", "params": {"count": 1}})

        def once():
            s = raw_post(p.port, ipp_body(p))
            return s.recv(4096)

        resp = await asyncio.to_thread(once)
        assert resp.startswith(b"HTTP/1.1 401") and b"WWW-Authenticate: Basic" in resp
        assert not p.store.jobs
        assert status(await call(p, "Get-Printer-Attributes")) == "successful-ok"

    run(with_control(tmp_path, go))


def test_reset_mid_upload_drops_connection_and_creates_no_job(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "reset-mid-upload", "params": {"count": 1}})
        big = make_pwg([blank_page(300, 300)])

        def once():
            s = raw_post(p.port, ipp_body(p, data=big))
            try:
                return s.recv(4096)
            except ConnectionResetError:
                return b""

        assert await asyncio.to_thread(once) == b""
        await p.wait_idle()
        assert not p.store.jobs
        assert status(await call(p, "Print-Job", PWG, DOC)) == "successful-ok"

    run(with_control(tmp_path, go))


def test_stall_body_stops_reading_and_stop_cleans_up(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "stall-body", "params": {"after_bytes": 200}})
        body = ipp_body(p, data=DOC)
        s = await asyncio.to_thread(raw_post, p.port, body)
        s.settimeout(0.5)
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(s.recv, 10)  # no answer, connection held open
        assert not p.store.jobs and len(p.connections) == 1
        s.close()

    run(with_control(tmp_path, go))  # finishing at all proves stop() cancels the held connection


def test_offline_hangs_requests_until_cleared(tmp_path):
    async def go(p, ctl):
        await http(ctl, "POST", "/fault", {"type": "offline"})
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(call(p, "Get-Printer-Attributes"), 0.5)
        await http(ctl, "POST", "/fault", {"type": "offline", "enabled": False})
        assert status(await call(p, "Get-Printer-Attributes")) == "successful-ok"

    run(with_control(tmp_path, go))
