"""mDNS advertisement: replays the profile's stored TXT record verbatim."""

from __future__ import annotations

import socket

from zeroconf import ServiceInfo, get_all_addresses
from zeroconf.asyncio import AsyncZeroconf


def lan_addresses(bind: str) -> list[str]:
    """IPv4 addresses to advertise for a listener bound to `bind`."""
    if bind not in ("0.0.0.0", ""):
        return [bind]
    return [a for a in get_all_addresses() if not a.startswith(("127.", "169.254."))]


def service_info(profile: dict, port: int, addresses: list[str], hostname: str) -> ServiceInfo:
    dns = profile["dnssd"]
    type_ = dns["service_type"] + ".local."
    return ServiceInfo(
        type_,
        f"{dns['service_name']}.{type_}",
        port=port,
        addresses=[socket.inet_aton(a) for a in addresses],
        properties=dict(dns["txt"]),
        server=f"{hostname}.local.",
    )


class Advertiser:
    """Registers services on the running event loop (zeroconf's sync API cannot)."""

    def __init__(self) -> None:
        self.azc: AsyncZeroconf | None = None
        self.infos: list[ServiceInfo] = []

    async def register(self, profile: dict, port: int, addresses: list[str]) -> ServiceInfo:
        if self.azc is None:
            self.azc = AsyncZeroconf()
        info = service_info(profile, port, addresses, f"vp-{profile['name']}")
        await self.azc.async_register_service(info, allow_name_change=False)
        self.infos.append(info)
        return info

    async def close(self) -> None:
        if self.azc is not None:
            for info in self.infos:
                await self.azc.async_unregister_service(info)
            await self.azc.async_close()
            self.azc = None
