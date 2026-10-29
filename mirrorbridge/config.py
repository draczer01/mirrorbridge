"""User settings, persisted as JSON in the per-user app data folder."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import APP_NAME, migration

_LOGGER = logging.getLogger(__name__)


def app_data_dir() -> Path:
    """Return (and create) the folder where MirrorBridge keeps its files.

    Everything that reads settings or pairings gets here first, so this is where the settings
    and pairings of the app's old name are brought over (see migration.bring_over).
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    migration.bring_over(path)
    return path


def credentials_file() -> Path:
    """pyatv storage file holding pairing credentials for every paired device."""
    return app_data_dir() / "pairings.json"


# What Settings.load checks: a value of another type (a settings.json edited by hand, a null) or
# not among the setting's CHOICES gets the default. These are Mirror screen's newer settings; the
# older ones are checked where they are used.
CHOICES: dict[str, tuple] = {
    "mirror_latency": ("low", "normal", "smooth"),
    "mirror_framerate": (0, 30, 60),
    "mirror_keyframes": (0, 10, 2),  # in the Debug menu's order (ffmpeg.MIRROR_KEYFRAME_CHOICES)
    "mirror_scaler": ("auto", "cpu"),
}
CHECKED = set(CHOICES) | {"mirror_encoder_extra"}


@dataclass
class Settings:
    """Everything the user can tune. Defaults are chosen to "just work"."""

    # Capture
    capture_backend: str = "auto"  # auto | ddagrab | gdigrab | test
    monitor_index: int = 0  # 0-based index into the list of monitors
    window_title: str = ""  # non-empty -> capture this window instead of a monitor
    # Extend screen: cast a virtual monitor Windows extends the desktop onto, so the TV is a
    # second screen. Overrides monitor_index and window_title.
    extend_screen: bool = False
    extend_mode: str = "1080p"  # 1080p | 720p | 4k (size of the virtual monitor)
    framerate: int = 30
    output_height: int = 1080  # scaled keeping aspect ratio; 0 = native
    show_cursor: bool = True

    # Encoding
    encoder: str = "auto"  # auto | h264_nvenc | h264_qsv | h264_amf | h264_mf | libx264
    video_bitrate_kbps: int = 8000
    latency_mode: str = "low"  # low | balanced | smooth

    # Audio
    capture_audio: bool = True
    audio_bitrate_kbps: int = 160
    mute_pc_audio: bool = True  # Cast mirroring: mute this PC's speakers while the TV plays the sound (no echo)

    # Networking
    http_port: int = 7878  # fixed so a single firewall rule covers it
    bind_address: str = ""  # empty -> auto-detect the interface reaching the device

    # Mirror screen settings (framerate above is for casts only)
    mirror_latency: str = "normal"  # low | normal | smooth: how long the TV waits to show a frame
    mirror_framerate: int = 0  # 0 = automatic (60 when this PC can), 30 or 60

    # Mirror screen's test switches (More options > Debug), for the sessions after they are set;
    # Test this TV and mirror --probe leave them out
    mirror_lead_ms: int = 0  # how long the TV waits to show a frame; 0 = what is saved for the TV
    mirror_keyframes: int = 0  # seconds between keyframes: 0 = only when needed, 10 or 2
    mirror_scaler: str = "auto"  # auto (on the graphics card when it can) | cpu (as before)
    mirror_on_arrival: bool = False  # ask the TV to show each frame as it comes, not at its time
    mirror_encoder_extra: str = ""  # settings.json only: FFmpeg options after the encoder's, for experiments

    # App
    language: str = "en"  # a gui.i18n.LANGUAGES code; any other ("" once meant "as Windows") is English
    tray_hint_shown: bool = False
    hide_hint_shown: bool = False
    extend_tip_shown: bool = False
    old_autostart_checked: bool = False  # the old name's Start with Windows entry was moved over

    # Misc
    ffmpeg_path: str = ""
    last_device_id: str = ""
    manual_hosts: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_data_dir() / "settings.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            _LOGGER.warning("Ignoring unreadable settings file %s: %s", path, ex)
            return cls()
        defaults = {f.name: f.default for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in defaults}
        for name in CHECKED.intersection(values):
            value = values[name]
            if type(value) is not type(defaults[name]) or value not in CHOICES.get(name, (value,)):
                _LOGGER.info("Ignoring %s=%r in %s: using %r", name, value, path, defaults[name])
                del values[name]
        return cls(**values)

    def save(self, path: Path | None = None) -> None:
        path = path or app_data_dir() / "settings.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
