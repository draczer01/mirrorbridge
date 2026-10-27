"""Discovery, PIN pairing and connection, all through pyatv.

pyatv runs its own mDNS (zeroconf) browser. Running a second zeroconf instance next to
it on Windows is what broke discovery in the old script, so everything goes through
pyatv here, with one shared credential store.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import pyatv
from pyatv import exceptions
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import AppleTV, BaseConfig, Storage
from pyatv.protocols.airplay.utils import AirPlayFlags, parse_features
from pyatv.storage.file_storage import FileStorage

from .config import credentials_file

_LOGGER = logging.getLogger(__name__)

# Called during pairing. device_provides_pin=True: return the PIN shown on the TV.
# False: the receiver wants *us* to provide a PIN; the callback gets it to display.
PinCallback = Callable[[bool, Optional[str]], Awaitable[Optional[str]]]


@dataclass
class Device:
    config: BaseConfig

    @property
    def identifier(self) -> str:
        return self.config.identifier or str(self.config.address)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def address(self) -> str:
        return str(self.config.address)

    @property
    def model(self) -> str:
        info = self.config.device_info
        return info.model_str if info else "AirPlay device"

    @property
    def airplay(self):
        return self.config.get_service(Protocol.AirPlay)

    @property
    def can_cast_video(self) -> bool:
        """True if the receiver advertises AirPlay video (needed for URL/HLS playback)."""
        svc = self.airplay
        if svc is None or svc.pairing in (PairingRequirement.Unsupported, PairingRequirement.Disabled):
            return False
        features = svc.properties.get("features")
        if not features:
            return True  # manual/unicast entries may lack it; let the device decide
        try:
            flags = parse_features(features)
        except Exception:  # pylint: disable=broad-except
            return True
        return bool(flags & (AirPlayFlags.SupportsAirPlayVideoV1 | AirPlayFlags.SupportsAirPlayVideoV2))

    @property
    def is_paired(self) -> bool:
        svc = self.airplay
        return bool(svc and svc.credentials)

    @property
    def needs_pairing(self) -> bool:
        svc = self.airplay
        if svc is None or svc.credentials:
            return False
        return svc.pairing == PairingRequirement.Mandatory

    @property
    def needs_password(self) -> bool:
        svc = self.airplay
        return bool(svc and svc.requires_password and not svc.password)

    @property
    def status_key(self) -> str:
        """Machine-readable status: paired, ready, needs_pin, audio_only, restricted."""
        svc = self.airplay
        if svc is not None and svc.pairing == PairingRequirement.Disabled:
            return "restricted"
        if svc is None or not self.can_cast_video:
            return "audio_only"
        if self.is_paired:
            return "paired"
        if self.needs_pairing:
            return "needs_pin"
        return "ready"

    @property
    def status(self) -> str:
        return {
            "restricted": "AirPlay restricted on device",
            "audio_only": "Audio only (no AirPlay video)",
            "paired": "Paired",
            "needs_pin": "Needs PIN pairing",
            "ready": "Ready",
        }[self.status_key]


class DeviceManager:
    """Owns the pyatv credential storage and wraps scan / pair / connect."""

    def __init__(self, loop: asyncio.AbstractEventLoop, storage: Storage | None = None) -> None:
        self.loop = loop
        self.storage = storage or FileStorage(str(credentials_file()), loop)
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if isinstance(self.storage, FileStorage):
            try:
                await self.storage.load()
            except (ValueError, OSError) as ex:  # empty or half-written file (crash, power loss)
                path = credentials_file()
                _LOGGER.warning("Pairings file %s is unreadable (%s); starting without saved pairings", path, ex)
                try:
                    path.replace(path.with_suffix(".json.bad"))
                except OSError:
                    pass
        self._loaded = True

    async def scan(self, timeout: float = 5, hosts: list[str] | None = None) -> list[Device]:
        """Find AirPlay receivers. `hosts` does a unicast scan (works when mDNS is blocked)."""
        await self._ensure_loaded()
        configs = await pyatv.scan(
            self.loop, timeout=timeout, hosts=hosts or None, storage=self.storage
        )
        devices = [Device(c) for c in configs if c.get_service(Protocol.AirPlay) or c.get_service(Protocol.RAOP)]
        devices.sort(key=lambda d: (not d.can_cast_video, d.name.lower()))
        return devices

    async def pair(self, device: Device, pin_callback: PinCallback) -> None:
        """Run AirPlay pair-setup. The TV shows a 4-digit PIN which the user types in."""
        await self._ensure_loaded()
        handler = await pyatv.pair(device.config, Protocol.AirPlay, self.loop, storage=self.storage)
        try:
            await handler.begin()
            if handler.device_provides_pin:
                pin = await pin_callback(True, None)
                if not pin:
                    raise exceptions.PairingError("Pairing cancelled")
                handler.pin(int(pin.strip()))
            else:
                our_pin = f"{random.randint(0, 9999):04d}"
                handler.pin(int(our_pin))
                await pin_callback(False, our_pin)
            await handler.finish()
            if not handler.has_paired:
                raise exceptions.PairingError("The device did not accept the PIN")
        finally:
            await handler.close()
        if isinstance(self.storage, FileStorage):
            await self.storage.save()
        _LOGGER.info("Paired with %s", device.name)

    async def forget(self, device: Device) -> None:
        await self._ensure_loaded()
        await self.storage.remove_settings(await self.storage.get_settings(device.config))
        if isinstance(self.storage, FileStorage):
            await self.storage.save()
        svc = device.airplay
        if svc:
            svc.credentials = None

    async def set_password(self, device: Device, password: str) -> None:
        await self._ensure_loaded()
        settings = await self.storage.get_settings(device.config)
        settings.protocols.airplay.password = password
        settings.protocols.raop.password = password
        if device.airplay:
            device.airplay.password = password
        if isinstance(self.storage, FileStorage):
            await self.storage.save()

    async def connect(self, device: Device) -> AppleTV:
        await self._ensure_loaded()
        # pyatv only cleans up a half-made connection on errors, not on cancellation
        # (Stop pressed while connecting), so let it finish and close the result.
        task = asyncio.ensure_future(pyatv.connect(device.config, self.loop, storage=self.storage))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            def close_when_done(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception() is None:
                    t.result().close()

            task.add_done_callback(close_when_done)
            raise
