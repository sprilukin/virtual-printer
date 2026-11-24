"""`vp` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import capture as cap
from . import profile as prof
from . import scrub as scr


def _raw_dir(arg: str, root: Path) -> Path:
    p = Path(arg)
    return p if p.exists() and p.is_dir() else root / "captures" / "raw" / arg


def cmd_capture(a: argparse.Namespace, root: Path) -> int:
    name = a.name or a.uri.rstrip("/").split("/")[-2]
    raw = root / "captures" / "raw" / name
    meta = cap.capture(a.uri, raw, a.mdns_timeout)
    print(f"captured {meta['bytes']} bytes into {raw}")
    return 0


def cmd_convert(a: argparse.Namespace, root: Path) -> int:
    raw = _raw_dir(a.capture, root)
    c = cap.convert(raw)
    print(
        f"{len(c['attributes'])} attributes, TXT {'yes' if c['dnssd'] else 'no'} -> {raw / cap.CANONICAL}"
    )
    return 0


def cmd_scrub(a: argparse.Namespace, root: Path) -> int:
    raw = _raw_dir(a.capture, root)
    canonical = cap.convert(raw)
    meta = canonical.get("capture_meta", {})
    ids = scr.Identifiers(
        hosts=[*(a.host or []), *([meta["host"]] if meta.get("host") else [])],
        ips=[*(a.ip or []), *([meta["ip"]] if meta.get("ip") else [])],
        serials=a.serial or [],
        macs=a.mac or [],
    )
    name = a.name or raw.name
    try:
        profile = scr.scrub(canonical, name, ids)
        prof.validate(profile)
    except (scr.LeakError, prof.ProfileError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    out = root / "profiles" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "printer.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    print(f"scrubbed profile written to {out / 'printer.json'}; leak check passed")
    print("Read it once before committing.")
    return 0


def cmd_validate(a: argparse.Namespace, root: Path) -> int:
    rc = 0
    names = a.profiles or sorted(
        p.name
        for p in (root / "profiles").iterdir()
        if p.is_dir() and ((p / "printer.json").exists() or (p / "overlay.json").exists())
    )
    for n in names:
        try:
            p = prof.load(n, root / "profiles")
            print(f"ok   {n} ({len(p['attributes'])} attributes)")
        except prof.ProfileError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            rc = 1
    return rc


def cmd_ca(a: argparse.Namespace, root: Path) -> int:
    from . import tls

    cert, _ = tls.ensure_ca((a.tls_dir or root / "tls").resolve())
    print(f"local CA certificate (install on the test device): {cert}")
    return 0


def cmd_up(a: argparse.Namespace, root: Path) -> int:
    from .runner import run_up

    return run_up(a, root)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vp", description="Virtual printer tooling")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser(
        "capture", help="read-only capture of a printer (one Get-Printer-Attributes + mDNS)"
    )
    c.add_argument("uri", help="e.g. ipp://printer.local:631/ipp/print")
    c.add_argument("--name", help="capture directory name under captures/raw/")
    c.add_argument("--mdns-timeout", type=float, default=5.0)
    c.set_defaults(fn=cmd_capture)

    c = sub.add_parser("convert", help="raw capture to the canonical JSON tree")
    c.add_argument("capture")
    c.set_defaults(fn=cmd_convert)

    c = sub.add_parser(
        "scrub", help="convert, scrub, leak-check and write profiles/<name>/printer.json"
    )
    c.add_argument("capture")
    c.add_argument("--name", help="profile name (default: capture directory name)")
    c.add_argument("--ip", action="append")
    c.add_argument("--host", action="append")
    c.add_argument("--serial", action="append")
    c.add_argument("--mac", action="append")
    c.set_defaults(fn=cmd_scrub)

    c = sub.add_parser("validate", help="load and validate profiles (default: all)")
    c.add_argument("profiles", nargs="*")
    c.set_defaults(fn=cmd_validate)

    c = sub.add_parser("ca", help="create the local CA used by profiles with tls mode 'ca'")
    c.add_argument("--tls-dir", type=Path, help="certificate directory (default: <root>/tls)")
    c.set_defaults(fn=cmd_ca)

    c = sub.add_parser("up", help="start virtual printers for one or more profiles")
    c.add_argument("profiles", nargs="+")
    c.add_argument(
        "--bind",
        default="127.0.0.1",
        help="address to listen on; 0.0.0.0 for a phone on the same Wi-Fi (default: 127.0.0.1)",
    )
    c.add_argument(
        "--base-port", type=int, default=6310, help="first port; one per profile (default: 6310)"
    )
    c.add_argument("--spool", type=Path, help="job output directory (default: <root>/spool)")
    c.add_argument(
        "--control-base-port",
        type=int,
        default=7310,
        help="first control API port; one per profile (default: 7310)",
    )
    c.add_argument(
        "--control-bind",
        default="127.0.0.1",
        help="address of the control API, which has no authentication (default: 127.0.0.1)",
    )
    c.add_argument("--tls-dir", type=Path, help="certificate directory (default: <root>/tls)")
    c.add_argument(
        "--tls-mode",
        choices=["self-signed", "ca"],
        help="override the certificate mode of TLS profiles ('ca': signed by `vp ca`)",
    )
    c.add_argument(
        "--tls-san",
        action="append",
        help="extra DNS name or IP the server certificate must cover (repeatable)",
    )
    c.add_argument("--quiet", action="store_true", help="do not log each request")
    c.add_argument(
        "--verbose",
        action="store_true",
        help="also log request headers and the attributes of each IPP request and response",
    )
    c.add_argument("--no-control", action="store_true", help="do not start the control API")
    c.add_argument("--no-mdns", action="store_true", help="do not advertise over mDNS")
    c.add_argument("--lenient", action="store_true", help="accept jobs with unadvertised values")
    c.set_defaults(fn=cmd_up)

    a = ap.parse_args(argv)
    return int(a.fn(a, a.root))


if __name__ == "__main__":
    sys.exit(main())
