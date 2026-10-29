"""Discovery, PIN pairing and connection, all through pyatv.

pyatv runs its own mDNS (zeroconf) browser. Running a second zeroconf instance next to
it on Windows is what broke discovery in the old script, so everything goes through
pyatv here, with one shared credential store.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import pyatv
from pyatv import exceptions
from pyatv.const import DeviceModel, PairingRequirement, Protocol
from pyatv.interface import AppleTV, BaseConfig, Storage
from pyatv.protocols.airplay.utils import AirPlayFlags
from pyatv.protocols.airplay.utils import parse_features as pyatv_parse_features
from pyatv.storage.file_storage import FileStorage

from .config import credentials_file
from .text import Text

_LOGGER = logging.getLogger(__name__)

# Called during pairing. device_provides_pin=True: return the PIN shown on the TV.
# False: the receiver wants *us* to provide a PIN; the callback gets it to display.
PinCallback = Callable[[bool, Optional[str]], Awaitable[Optional[str]]]

# Apple's own AirPlay receivers, by the "model" they advertise (AppleTV11,1, Mac14,3...).
_APPLE_MODEL = re.compile(r"^(AppleTV|AudioAccessory|AirPort|Mac|iMac)")

# TXT values worth having in a log when a receiver misbehaves.
_DIAGNOSTIC_KEYS = ("model", "manufacturer", "srcvers", "osvers", "fv", "sdk", "protovers",
                    "flags", "acl", "act", "pw")

# The feature bits that decide how a video URL can be sent.
_VIDEO_BITS = {"VideoV1": 0, "PlayQueue": 33, "VideoV2": 49}


def parse_features(text: str) -> int:
    """AirPlay feature bits from a TXT value such as "0x5A7FFFF7,0x1E" (low word first).

    pyatv joins the two hex strings as text, which gives wrong bits when a receiver does
    not zero-pad the low word (Samsung: "0x7F8AD0,0x38BCF46").
    """
    low, _, high = text.strip().partition(",")
    return (int(high, 16) << 32 if high else 0) | int(low, 16)


def feature_bits(value: int) -> list[int]:
    return [bit for bit in range(64) if value >> bit & 1]


def _has_video(features: int) -> bool:
    return bool(features & (AirPlayFlags.SupportsAirPlayVideoV1 | AirPlayFlags.SupportsAirPlayVideoV2))


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
    def features(self) -> int | None:
        """The AirPlay feature bits the receiver advertises; None if it gave none."""
        svc = self.airplay
        text = svc and (svc.properties.get("features") or svc.properties.get("ft"))
        if not text:
            return None  # manual/unicast entries may lack it
        try:
            return parse_features(text)
        except ValueError:
            return None

    def _pyatv_features(self) -> int | None:
        """The feature bits as pyatv reads them where it decides if play_url may run."""
        svc = self.airplay
        text = svc and svc.properties.get("features")
        if not text:
            return None
        try:
            return int(pyatv_parse_features(text))
        except ValueError:
            return None

    @property
    def can_cast_video(self) -> bool:
        """True if the receiver advertises AirPlay video (needed for URL/HLS playback).

        Other brands' receivers are judged by the bits they really advertise, and pyatv
        is handed those bits before it connects (fix_features). Apple's receivers, and
        those that do not say what they are, keep pyatv's own reading: it is the one
        that decides whether pyatv lets play_url run at all.
        """
        svc = self.airplay
        if svc is None or svc.pairing in (PairingRequirement.Unsupported, PairingRequirement.Disabled):
            return False
        features = self.features if self.is_apple is False else self._pyatv_features()
        if features is None:
            return True  # let the device decide
        return _has_video(features)

    def fix_features(self) -> None:
        """Make pyatv read the AirPlay video bits that a receiver of another brand advertises.

        pyatv joins "low,high" as text, so a receiver that does not zero-pad the low word
        looks to it like one with other bits. Where that changes whether AirPlay video is
        there, pyatv would refuse play_url ("not supported") after MirrorBridge showed the TV
        as ready, so the value is rewritten zero-padded, which pyatv reads correctly.
        Apple's receivers are left alone.
        """
        svc = self.airplay
        features = self.features
        if svc is None or features is None or self.is_apple is not False:
            return
        theirs = self._pyatv_features()
        if theirs is not None and _has_video(theirs) == _has_video(features):
            return
        text = f"0x{features & 0xFFFFFFFF:08X},0x{features >> 32:08X}"
        _LOGGER.info("pyatv misreads the features of %s (%s); handing it %s", self.name,
                     svc.properties.get("features") or svc.properties.get("ft"), text)
        for key in ("features", "ft"):
            if key == "features" or key in svc.properties:
                svc.properties[key] = text

    @property
    def is_apple(self) -> bool | None:
        """True for an Apple TV (or another Apple receiver), False for another brand's,
        None when the receiver does not say what it is (manual entries, some scans)."""
        info = self.config.device_info
        if info and info.model != DeviceModel.Unknown:
            return True  # pyatv only knows Apple's models
        svc = self.airplay
        props = svc.properties if svc else {}
        model = props.get("model") or (info.raw_model if info else None) or ""
        maker = props.get("manufacturer", "")
        if _APPLE_MODEL.match(model) or maker.lower().startswith("apple"):
            return True
        if model or maker:
            return False
        return None

    def details(self) -> str:
        """What the receiver advertises over mDNS, for the log and `scan -v`."""
        svc = self.airplay
        if svc is None:
            return "no AirPlay service"
        props = svc.properties
        parts = [f"{key}={props[key]}" for key in _DIAGNOSTIC_KEYS if props.get(key)]
        raw = props.get("features") or props.get("ft")
        features = self.features
        if features is None:
            parts.append(f"features={raw or 'none'}")
        else:
            bits = feature_bits(features)
            video = ", ".join(f"{name} {'yes' if bit in bits else 'no'}" for name, bit in _VIDEO_BITS.items())
            parts.append(f"features={raw} (bits {','.join(map(str, bits))}; {video})")
            try:  # pyatv picks AirPlay 1 or 2 from its own reading
                theirs = ",".join(map(str, feature_bits(int(pyatv_parse_features(raw)))))
            except ValueError:
                theirs = "nothing"
            if theirs != ",".join(map(str, bits)):
                parts.append(f"pyatv reads the features as bits {theirs}")
        maker = {True: "Apple", False: "not Apple", None: "unknown"}[self.is_apple]
        return f"{'; '.join(parts)}; maker: {maker}"

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
    def requires_password(self) -> bool:
        """The receiver advertises an AirPlay password (TXT "pw", or flags bit 0x80)."""
        svc = self.airplay
        return bool(svc and svc.requires_password)

    @property
    def needs_password(self) -> bool:
        svc = self.airplay
        return bool(self.requires_password and not svc.password)

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


class ConnectFailed(RuntimeError):
    """pyatv could not connect to the receiver; the message names it and says why."""


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
        device.fix_features()
        handler = await pyatv.pair(device.config, Protocol.AirPlay, self.loop, storage=self.storage)
        try:
            await handler.begin()
            if handler.device_provides_pin:
                pin = await pin_callback(True, None)
                if not pin:
                    raise exceptions.PairingError(Text("pair_cancelled", "Pairing cancelled"))
                handler.pin(int(pin.strip()))
            else:
                our_pin = f"{random.randint(0, 9999):04d}"
                handler.pin(int(our_pin))
                await pin_callback(False, our_pin)
            await handler.finish()
            if not handler.has_paired:
                raise exceptions.PairingError(Text("pin_refused", "The device did not accept the PIN"))
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
        _LOGGER.info("Receiver %s (%s): %s", device.name, device.address, device.details())
        device.fix_features()
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
        except Exception as ex:  # pylint: disable=broad-except
            raise ConnectFailed(Text("connect_failed", "Could not connect to {device}: {error}",
                                     device=device.name, error=str(ex) or type(ex).__name__)) from ex
