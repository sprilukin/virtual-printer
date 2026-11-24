"""`vp up`: run several printers until interrupted, then shut down cleanly."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from . import mdns, tls
from . import profile as prof
from .control import ControlServer
from .server import Printer


async def serve(
    printers: list[Printer],
    advertise: bool,
    bind: str,
    stop: asyncio.Event,
    controls: list[ControlServer] | None = None,
) -> None:
    adv = mdns.Advertiser() if advertise else None
    controls = controls or []
    try:
        for p, ctl in zip(printers, controls + [None] * len(printers), strict=False):
            await p.start()
            if ctl:
                await ctl.start()
            if adv:
                addrs = mdns.lan_addresses(bind)
                if addrs:
                    await adv.register(p.profile, p.port, addrs)
            where = p.host if p.host != "0.0.0.0" else "<this-machine>"
            print(
                f"{p.name:30} {p.scheme}://{where}:{p.port}{p.resource}  spool: {p.store.spool}",
                flush=True,
            )
            if p.auth:
                users = ", ".join(sorted(p.auth["users"]))
                print(f"{'':30} basic auth, users: {users}", flush=True)
            if ctl:
                print(f"{'':30} control http://{ctl.host}:{ctl.port}/state", flush=True)
        await stop.wait()
    finally:
        print("shutting down", flush=True)
        if adv:
            await adv.close()
        for ctl in controls:
            await ctl.stop()
        for p in printers:
            await p.stop()


def _tls_sans(a: argparse.Namespace) -> list[str]:
    """Names and addresses the server certificate must cover."""
    sans = list(a.tls_san or [])
    if a.bind not in ("0.0.0.0", "", "::"):
        sans.append(a.bind)
    else:
        sans += mdns.lan_addresses(a.bind)
    return sans


def run_up(a: argparse.Namespace, root: Path) -> int:
    spool = (a.spool or root / "spool").resolve()
    tls_dir = (a.tls_dir or root / "tls").resolve()
    printers = []
    for i, name in enumerate(a.profiles):
        try:
            profile = prof.load(name, root / "profiles")
        except prof.ProfileError as exc:
            print(f"FAILED: {exc}")
            return 1
        if a.lenient:
            profile.setdefault("options", {})["validation"] = "lenient"
        if a.tls_mode and "tls" in profile.get("options", {}):
            profile["options"]["tls"] = {"mode": a.tls_mode}
        try:
            ctx = tls.context_for(profile, tls_dir, root, _tls_sans(a))
        except (OSError, ValueError) as exc:
            print(f"FAILED: TLS setup for {name}: {exc}")
            return 1
        printers.append(
            Printer(
                profile,
                spool / name,
                a.bind,
                a.base_port + i,
                ctx,
                None if a.quiet else lambda line: print(line, flush=True),
            )
        )
    controls = (
        []
        if a.no_control
        else [
            ControlServer(p, a.control_bind, a.control_base_port + i)
            for i, p in enumerate(printers)
        ]
    )
    if len({p.name for p in printers}) != len(printers):
        print("FAILED: a profile was listed twice")
        return 1
    advertise = not a.no_mdns and a.bind not in ("127.0.0.1", "::1", "localhost")
    if not a.no_mdns and not advertise:
        print("mDNS off: bound to loopback. Use --bind 0.0.0.0 to be discoverable from a phone.")

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await serve(printers, advertise, a.bind, stop, controls)

    try:
        asyncio.run(main())
    except OSError as exc:
        busy = next((p for p in printers if not p.listening), None)
        host, port, flag = (busy.host, busy.port, "--base-port") if busy else (a.bind, None, "")
        if busy is None:
            ctl = next(c for c in controls if not c.listening)
            host, port, flag = ctl.host, ctl.port, "--control-base-port"
        print(
            f"FAILED: cannot listen on {host}:{port}: {exc.strerror or exc}\n"
            f"Is another `vp up` still running? Find it with `lsof -nP -iTCP:{port} -sTCP:LISTEN`, "
            f"or pick other ports with {flag}.",
            file=sys.stderr,
        )
        return 1
    return 0
