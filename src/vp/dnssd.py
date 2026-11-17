"""DNS-SD TXT record handling: zeroconf capture and `dns-sd -L` text parsing."""

from __future__ import annotations

import re
import time

SERVICE_TYPES = ("_ipp._tcp.local.", "_ipps._tcp.local.")


def _unescape(s: str) -> str:
    """Undo dns-sd's backslash escapes: `\\ `, `\\(`, and decimal `\\032`."""
    s = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\\(.)", r"\1", s)


def _split_unescaped(line: str) -> list[str]:
    tokens, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i : i + 2])
            i += 2
            continue
        if c == " ":
            if cur:
                tokens.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        tokens.append("".join(cur))
    return tokens


def parse_dnssd_text(text: str) -> dict:
    """Parse the output of `dns-sd -L "<name>" _ipp._tcp local`."""
    service_name = host = None
    port = None
    txt: list[list[str]] = []
    for line in text.splitlines():
        m = re.search(r"(\S+?)\._ipp(s?)\._tcp\.local\. can be reached at (\S+?):(\d+)", line)
        if m:
            service_name = _unescape(m.group(1))
            host, port = m.group(3), int(m.group(4))
        elif re.match(r"^\s*txtvers=", line):
            for tok in _split_unescaped(line.strip()):
                key, _, value = tok.partition("=")
                txt.append([key, _unescape(value)])
    if not txt:
        raise ValueError("no TXT record found in dns-sd output")
    return {
        "service_type": "_ipp._tcp",
        "service_name": service_name,
        "host": host,
        "port": port,
        "txt": txt,
    }


def browse_txt(host: str, ip: str | None, timeout: float = 5.0) -> dict | None:
    """Find the _ipp._tcp service for `host` via mDNS and return its TXT record."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: list[tuple[str, str]] = []

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            found.append((type_, name))

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        browser = ServiceBrowser(zc, list(SERVICE_TYPES), Listener())
        time.sleep(timeout)
        browser.cancel()
        want = host.lower().rstrip(".")
        for type_, name in sorted(found, key=lambda tn: SERVICE_TYPES.index(tn[0])):
            info = zc.get_service_info(type_, name, timeout=3000)
            if info is None:
                continue
            server = (info.server or "").lower().rstrip(".")
            addrs = info.parsed_addresses()
            if server == want or server.split(".")[0] == want.split(".")[0] or (ip and ip in addrs):
                txt = [
                    [
                        k.decode("utf-8", "replace"),
                        "" if v is None else v.decode("utf-8", "replace"),
                    ]
                    for k, v in info.properties.items()
                ]
                label = name[: -len(type_) - 1] if name.endswith(type_) else name
                return {
                    "service_type": type_.replace(".local.", ""),
                    "service_name": label,
                    "host": info.server,
                    "port": info.port,
                    "txt": txt,
                }
    finally:
        zc.close()
    return None
