"""How each TV takes screen mirroring, once the probe (or a session) has found out.

Kept in mirroring.json next to settings.json and pairings.json, under the identifier the device
has in pairings.json, so that the next session asks for what worked straight away instead of
being refused first. A session still falls back once if the TV has changed its mind.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from ..config import app_data_dir
from ..text import Text
from .h264 import DEFAULT_TRAILER, TRAILERS
from .negotiation import CONTROL_FIRST, DEFAULT_LEAD_MS, NTP, ORDERINGS, Plan

_LOGGER = logging.getLogger(__name__)

FILE_NAME = "mirroring.json"
MIN_LEAD_MS, MAX_LEAD_MS = 20, 1000  # a lead outside these is not taken


@dataclass
class MirrorProfile:
    ordering: str = CONTROL_FIRST
    audio: bool = True  # send the silent type-96 audio stream
    timing: str = NTP
    trailer: str = DEFAULT_TRAILER  # how the avcC record ends (h264.TRAILERS)
    lead_ms: int = DEFAULT_LEAD_MS
    checked: str = ""  # when it last worked (local time), "" for the defaults

    def plan(self, fallback: bool = True) -> Plan:
        return Plan(ordering=self.ordering, audio=self.audio, timing=self.timing, lead_ms=self.lead_ms,
                    fallback=fallback)

    @classmethod
    def of(cls, plan: Plan, trailer: str) -> "MirrorProfile":
        return cls(plan.ordering, plan.audio, plan.timing, trailer, plan.lead_ms, time.strftime("%Y-%m-%d %H:%M"))

    def describe(self) -> Text:
        """What is asked for, as Plan.describe() says it, and how the video goes (a Text: Test this
        TV shows it in the user's language)."""
        audio = (Text("mirror_with_audio", "with the audio stream") if self.audio
                 else Text("mirror_without_audio", "without the audio stream"))
        return Text("mirror_profile", "{ordering}, {audio}, {timing} timing, avcC ending {trailer}, lead {lead} ms",
                    ordering=self.ordering, audio=audio, timing=self.timing.upper(), trailer=self.trailer,
                    lead=self.lead_ms)

    @classmethod
    def from_dict(cls, raw: dict) -> "MirrorProfile":
        known = {f.name for f in fields(cls)}
        profile = cls(**{k: v for k, v in raw.items() if k in known})
        defaults = cls()
        if profile.ordering not in ORDERINGS:
            profile.ordering = defaults.ordering
        if profile.trailer not in TRAILERS:
            profile.trailer = defaults.trailer
        profile.timing = NTP  # PTP is only ever probed (negotiation.py)
        if not isinstance(profile.lead_ms, int) or not MIN_LEAD_MS <= profile.lead_ms <= MAX_LEAD_MS:
            profile.lead_ms = defaults.lead_ms
        profile.audio = bool(profile.audio)
        return profile


def profiles_file() -> Path:
    return app_data_dir() / FILE_NAME


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        _LOGGER.warning("Ignoring unreadable %s: %s", path, ex)
        return {}
    return data if isinstance(data, dict) else {}


def load(identifier: str, path: Optional[Path] = None) -> Optional[MirrorProfile]:
    """The saved way of mirroring to this device, None if there is none."""
    if not identifier:
        return None
    entry = _read(path or profiles_file()).get(identifier)
    if not isinstance(entry, dict) or not isinstance(entry.get("profile"), dict):
        return None
    return MirrorProfile.from_dict(entry["profile"])


def save(identifier: str, profile: MirrorProfile, *, name: str = "", info: str = "",
         path: Optional[Path] = None) -> Path:
    """Remember `profile` for this device (and, for bug reports, what its /info said)."""
    path = path or profiles_file()
    data = _read(path)
    entry = {"name": name, "profile": asdict(profile)}
    if info:
        entry["info"] = info
    data[identifier] = entry
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    _LOGGER.info("Saved how to mirror to %s: %s", name or identifier, profile.describe())
    return path
