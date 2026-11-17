import asyncio
import base64
import http.client
import ssl
from pathlib import Path

import pytest

from vp import ipp, profile, tls
from vp.server import Printer

PROFILES = Path(__file__).parent.parent / "profiles"
GOOD = "Basic " + base64.b64encode(b"office:print-me").decode()


def _request_bytes() -> bytes:
    uri = "ipps://127.0.0.1/ipp/print"
    req = ipp.make_request("Get-Printer-Attributes", uri, [])
    return ipp.encode(req)


def _post(port: int, ctx: ssl.SSLContext | None, auth: str | None = None):
    headers = {"Content-Type": "application/ipp"}
    if auth:
        headers["Authorization"] = auth
    conn = (
        http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
        if ctx
        else http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    )
    try:
        conn.request("POST", "/ipp/print", body=_request_bytes(), headers=headers)
        r = conn.getresponse()
        return r.status, r.getheader("WWW-Authenticate"), r.read()
    finally:
        conn.close()


def _client_trusting(cafile: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cafile))


async def _with_office(tmp_path, mode, fn):
    prof = profile.load("ipps-auth-office", PROFILES)
    prof["options"]["tls"] = {"mode": mode}
    cert, key = tls.issue(tmp_path / "tls", prof["name"], mode)
    p = Printer(prof, tmp_path / "spool", ssl_context=tls.server_context(cert, key))
    await p.start()
    try:
        return await fn(p, cert)
    finally:
        await p.stop()


def test_profile_loads_with_tls_and_auth():
    p = profile.load("ipps-auth-office", PROFILES)
    assert p["options"]["tls"]["mode"] == "self-signed"
    assert p["options"]["auth"]["users"] == {"office": "print-me"}
    assert p["dnssd"]["service_type"] == "_ipps._tcp"
    assert p["name"].startswith("ipps-auth") and p["attributes"]


def test_self_signed_trusted_by_pinning_and_basic_auth(tmp_path):
    async def go(p, cert):
        ctx = _client_trusting(cert)
        port = p.port
        # no credentials: challenged
        status, challenge, _ = await asyncio.to_thread(_post, port, ctx)
        assert status == 401 and challenge == 'Basic realm="Office printer"'
        # wrong password
        bad = "Basic " + base64.b64encode(b"office:nope").decode()
        assert (await asyncio.to_thread(_post, port, ctx, bad))[0] == 401
        # right credentials: IPP answer with ipps URI and tls/basic advertised
        status, _, body = await asyncio.to_thread(_post, port, ctx, GOOD)
        assert status == 200
        attrs = {a["name"]: a for g in ipp.decode(body)["groups"] for a in g["attributes"]}
        assert attrs["printer-uri-supported"]["values"][0].startswith("ipps://")
        assert attrs["uri-security-supported"]["values"] == ["tls"]
        assert attrs["uri-authentication-supported"]["values"] == ["basic"]
        assert p.auth_failures == 2

    asyncio.run(_with_office(tmp_path, "self-signed", go))


def test_ca_signed_cert_trusted_via_ca_only(tmp_path):
    async def go(p, cert):
        ca_cert, _ = tls.ensure_ca(tmp_path / "tls")
        assert (await asyncio.to_thread(_post, p.port, _client_trusting(ca_cert), GOOD))[0] == 200
        # the leaf itself is not a CA: pinning it directly still works, but an unrelated
        # trust store does not
        with pytest.raises(ssl.SSLError):
            await asyncio.to_thread(_post, p.port, ssl.create_default_context(), GOOD)

    asyncio.run(_with_office(tmp_path, "ca", go))


def test_plain_http_client_is_not_served_over_tls(tmp_path):
    async def go(p, cert):
        with pytest.raises((http.client.HTTPException, OSError)):
            await asyncio.to_thread(_post, p.port, None, GOOD)

    asyncio.run(_with_office(tmp_path, "self-signed", go))


def test_unauthenticated_get_page_is_open(tmp_path):
    async def go(p, cert):
        def get():
            c = http.client.HTTPSConnection("127.0.0.1", p.port, context=_client_trusting(cert))
            c.request("GET", "/ipp/print")
            return c.getresponse().status

        assert await asyncio.to_thread(get) == 200

    asyncio.run(_with_office(tmp_path, "self-signed", go))


def test_cert_is_reused_until_sans_change(tmp_path):
    c1, _ = tls.issue(tmp_path, "x", "self-signed")
    first = c1.read_bytes()
    assert tls.issue(tmp_path, "x", "self-signed")[0].read_bytes() == first
    assert tls.issue(tmp_path, "x", "self-signed", ["192.168.1.50"])[0].read_bytes() != first


def test_ca_is_stable_and_leaf_chains_to_it(tmp_path):
    ca1, _ = tls.ensure_ca(tmp_path)
    data = ca1.read_bytes()
    leaf, _ = tls.issue(tmp_path, "y", "ca")
    assert tls.ensure_ca(tmp_path)[0].read_bytes() == data
    assert leaf.read_bytes() != data


@pytest.mark.parametrize(
    "opts",
    [
        {"tls": {"mode": "bogus"}},
        {"tls": {"mode": "files"}},
        {"auth": {"type": "digest", "users": {"a": "b"}}},
        {"auth": {"type": "basic", "users": {}}},
    ],
)
def test_invalid_security_options_rejected(opts):
    p = profile.load("brother-dcp-l1630w", PROFILES)
    p["options"] = {**p.get("options", {}), **opts}
    with pytest.raises(profile.ProfileError):
        profile.validate(p)


def test_control_state_reports_security(tmp_path):
    from vp.control import ControlServer

    prof = profile.load("ipps-auth-office", PROFILES)
    cert, key = tls.issue(tmp_path / "tls", prof["name"], "self-signed")
    p = Printer(prof, tmp_path / "spool", ssl_context=tls.server_context(cert, key))
    st = ControlServer(p).state()
    assert st["tls"] is True and st["auth"] == {"type": "basic", "users": ["office"]}
    assert st["uri"].startswith("ipps://")
