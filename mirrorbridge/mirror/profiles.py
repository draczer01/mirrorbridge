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
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Optional

from ..config import app_data_dir
from ..text import Text
from .h264 import DEFAULT_TRAILER, TRAILERS
from .negotiation import CONTROL_FIRST, DEFAULT_LEAD_MS, NTP, ORDERINGS, Plan

_LOGGER = logging.getLogger(__name__)

FILE_NAME = "mirroring.json"
MIN_LEAD_MS, MAX_LEAD_MS = 20, 1000  # a lead outside these is not taken
# What is known of the TV and the PC's sound (MirrorProfile.sound): nothing yet, it closed the
# session when the sound started (no sound unless asked for), or someone heard it there.
SOUND_UNKNOWN, SOUND_OFF, SOUND_HEARD = "", "off", "heard"
SOUND_VERDICTS = (SOUND_UNKNOWN, SOUND_OFF, SOUND_HEARD)


@dataclass
class MirrorProfile:
    ordering: str = CONTROL_FIRST
    audio: bool = True  # set up the type-96 audio stream (silent unless the sound is on)
    timing: str = NTP
    trailer: str = DEFAULT_TRAILER  # how the avcC record ends (h264.TRAILERS)
    lead_ms: int = DEFAULT_LEAD_MS
    checked: str = ""  # when it last worked (local time), "" for the defaults
    sound: str = SOUND_UNKNOWN  # SOUND_VERDICTS: what is known of the PC's sound on this TV
    sound_note: str = ""  # when and why, for "off"

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
        if profile.sound not in SOUND_VERDICTS:
            profile.sound = defaults.sound
        if not isinstance(profile.sound_note, str):
            profile.sound_note = defaults.sound_note
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
    _write(path, data)
    _LOGGER.info("Saved how to mirror to %s: %s%s", name or identifier, profile.describe(),
                 f"; sound {profile.sound}" if profile.sound else "")
    return path


def set_sound(identifier: str, value: str, note: str = "", path: Optional[Path] = None, *,
              profile: Optional[MirrorProfile] = None, name: str = "") -> Optional[Path]:
    """Keep what is known of the PC's sound on this device (SOUND_VERDICTS) in its saved entry,
    the rest of it as it was. With nothing saved for it yet, `profile` (what the session that
    found it out used) is saved with it; without one, nothing is, and None is returned: the
    defaults are not to pass for what worked."""
    if value not in SOUND_VERDICTS:
        raise ValueError(f"not a sound verdict: {value!r}")
    path = path or profiles_file()
    data = _read(path)
    entry = data.get(identifier)
    if not identifier or not isinstance(entry, dict) or not isinstance(entry.get("profile"), dict):
        if identifier and profile is not None:
            return save(identifier, replace(profile, sound=value, sound_note=note), name=name, path=path)
        _LOGGER.info("Nothing saved for %s yet: its sound (%s) is not kept", identifier or "this TV",
                     value or "unknown")
        return None
    entry["profile"] = {**entry["profile"], "sound": value, "sound_note": note}
    _write(path, data)
    _LOGGER.info("Saved the sound of %s: %s%s", entry.get("name") or identifier, value or "unknown",
                 f" ({note})" if note else "")
    return path


def _write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)
