"""Finding Cast receivers: _googlecast._tcp on mDNS, through pyatv's own mDNS client.

(The rest of the app finds AirPlay receivers with pyatv; a second zeroconf instance next to
it is what broke discovery on Windows before, see devices.py.) A TV that mDNS does not reach
(another subnet, a router that drops multicast) can be named by its IP address: its Cast
port answers whether or not it was found.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Optional

from .channel import PORT

_LOGGER = logging.getLogger(__name__)

SERVICE = "_googlecast._tcp.local"
# ca= bits (the receiver's capabilities): video out, the only kind that can show a screen
CAPABILITY_VIDEO_OUT = 1 << 0


@dataclass
class CastDevice:
    name: str
    host: str
    port: int = PORT
    identifier: str = ""
    model: str = ""
    properties: dict = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        """Whether the receiver says it has a screen (a speaker or speaker group has none)."""
        try:
            return bool(int(self.properties.get("ca", "1")) & CAPABILITY_VIDEO_OUT)
        except ValueError:
            return True

    def describe(self) -> str:
        return f"{self.name} ({self.model or 'Cast receiver'}, {self.host}:{self.port})"


def _device(service) -> Optional[CastDevice]:
    if service.address is None or not service.port:
        return None
    props = {str(k).lower(): str(v) for k, v in dict(service.properties).items()}
    name = props.get("fn") or service.name
    return CastDevice(name=name, host=str(service.address), port=service.port, identifier=props.get("id", ""),
                      model=props.get("md", ""), properties=props)


async def scan(timeout: float = 4, hosts: Optional[list[str]] = None) -> list[CastDevice]:
    """Cast receivers that answer within `timeout` seconds. `hosts` are asked directly (unicast
    mDNS), and listed by IP even if they do not answer it."""
    from pyatv.core import mdns  # pylint: disable=import-outside-toplevel

    loop = asyncio.get_running_loop()
    found: dict[str, CastDevice] = {}
    responses = []
    try:
        if hosts:
            for host in hosts:
                try:
                    responses.append(await mdns.unicast(loop, host, [SERVICE], timeout=int(max(1, timeout))))
                except (OSError, asyncio.TimeoutError) as ex:
                    _LOGGER.debug("No mDNS answer from %s: %s", host, ex)
        else:
            responses = await mdns.multicast(loop, [SERVICE], timeout=int(max(1, timeout)))
    except OSError as ex:
        _LOGGER.warning("Cast discovery failed: %s", ex)
    for response in responses:
        for service in response.services:
            if not service.type.startswith("_googlecast._tcp"):
                continue
            device = _device(service)
            if device is not None:
                found[device.identifier or device.host] = device
    for host in hosts or []:
        if not any(device.host == host for device in found.values()):
            found[host] = CastDevice(name=host, host=host)
    return sorted(found.values(), key=lambda d: d.name.lower())


def is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


async def find(wanted: str, timeout: float = 4) -> Optional[CastDevice]:
    """The receiver named (or with the id or IP) `wanted`; an IP address needs no scan."""
    if is_address(wanted):
        devices = await scan(min(timeout, 2), hosts=[wanted])
        return devices[0] if devices else CastDevice(name=wanted, host=wanted)
    devices = await scan(timeout)
    lowered = wanted.lower()
    for device in devices:
        if lowered in (device.name.lower(), device.identifier.lower(), device.host):
            return device
    return next((d for d in devices if lowered in d.name.lower()), None)
