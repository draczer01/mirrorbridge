"""For every test: no MIRRORBRIDGE_MIRROR_SOUND switch from the environment of the machine that
runs them, and no mirroring session that looks for this PC's sound output or mutes its real
speakers (a session finds no output to record, and mutes a RecordingSpeakers). A test marked
real_sound keeps the real ones."""

import os

import pytest

SOUND_SWITCHES = "MIRRORBRIDGE_MIRROR_SOUND"  # and every MIRRORBRIDGE_MIRROR_SOUND_* after it


def pytest_configure(config):
    config.addinivalue_line("markers", "real_sound: the mirroring sessions look for and mute this PC's real speakers")


class RecordingSpeakers:
    """speakers.MutedSpeakers as a recorder: what a session asked of it, with nothing muted."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.muted = False
        self.error = ""

    def mute(self) -> bool:
        self.calls.append("mute")
        if self.muted:
            return False
        self.muted = True
        return True

    def restore(self) -> None:
        self.calls.append("restore")
        self.muted = False


@pytest.fixture(autouse=True)
def no_real_sound(request, monkeypatch):
    for name in [name for name in os.environ if name.startswith(SOUND_SWITCHES)]:
        monkeypatch.delenv(name)
    if request.node.get_closest_marker("real_sound") is None:
        from mirrorbridge.mirror import session as mirror_session

        monkeypatch.setattr(mirror_session.system_audio, "loopback_available", lambda: (False, "tests"))
        monkeypatch.setattr(mirror_session, "MutedSpeakers", RecordingSpeakers)
