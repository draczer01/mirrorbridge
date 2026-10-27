"""Pair + cast against pyatv's own fake Apple TV, with real ffmpeg and real HTTP.

Needs the pyatv source tree (its tests/fake_device is not shipped in the wheel):
    PYATV_SRC=/path/to/pyatv-0.18.0 pytest tests/test_end_to_end.py
"""

import asyncio
import os
import shutil
import sys

import pytest

PYATV_SRC = os.environ.get("PYATV_SRC")
pytestmark = pytest.mark.skipif(
    not PYATV_SRC or shutil.which("ffmpeg") is None, reason="needs PYATV_SRC and ffmpeg"
)


@pytest.fixture
async def fake_atv():
    sys.path.insert(0, PYATV_SRC)
    from pyatv.const import Protocol
    from tests.fake_device import FakeAppleTV  # pyatv's test package

    atv = FakeAppleTV(asyncio.get_running_loop(), test_mode=False)
    atv.add_service(Protocol.AirPlay)
    await atv.start()
    yield atv
    await atv.stop()


def _device(fake_atv, properties=None):
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol

    from aircast.devices import Device

    conf = AppleTV("127.0.0.1", "Living Room")
    conf.add_service(
        ManualService("airplay_id", Protocol.AirPlay, fake_atv.get_port(Protocol.AirPlay), properties or {"features": "0x1,0x0"})
    )
    return Device(conf)


@pytest.fixture
def recorded_keys():
    """The fake device replays a recorded session, so pin the client's random keys."""
    import binascii
    from unittest.mock import patch

    from tests.fake_device.airplay import DEVICE_AUTH_KEY, DEVICE_IDENTIFIER

    def predetermined(num):
        return binascii.unhexlify(DEVICE_IDENTIFIER if num == 8 else DEVICE_AUTH_KEY)

    with patch("pyatv.protocols.airplay.srp.urandom", side_effect=predetermined):
        yield


@pytest.mark.parametrize("model, playlist", [
    ({"model": "AppleTV3,2"}, "stream.m3u8"),  # Apple TVs: the same stream as always
    ({"model": "55QM7L", "manufacturer": "TCL"}, "master.m3u8"),
])
async def test_pair_with_pin_then_cast_screen(fake_atv, recorded_keys, tmp_path, model, playlist):
    from pyatv.const import Protocol
    from pyatv.storage.memory_storage import MemoryStorage
    from tests.fake_device.airplay import DEVICE_PIN

    from aircast.config import Settings
    from aircast.devices import DeviceManager
    from aircast.session import CastSession, State

    usecase = fake_atv.get_usecase(Protocol.AirPlay)
    state = fake_atv.get_state(Protocol.AirPlay)
    usecase.airplay_require_authentication()

    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    device = _device(fake_atv, {"features": "0x1,0x0", **model})

    asked = []

    async def pin_cb(device_provides_pin, _ours):
        asked.append(device_provides_pin)
        return str(DEVICE_PIN)

    await manager.pair(device, pin_cb)
    assert asked == [True]
    assert device.is_paired

    # Receiver "plays" for a few polls, then the user presses stop on the remote.
    for _ in range(4):
        usecase.airplay_playback_playing()

    states = []
    settings = Settings(capture_backend="test", encoder="libx264", latency_mode="low",
                        output_height=360, bind_address="127.0.0.1", http_port=0, capture_audio=False)
    session = CastSession(manager, device, settings, lambda s, m: states.append(s), work_dir=tmp_path)
    await asyncio.wait_for(session.run(), timeout=60)

    assert state.last_airplay_url == session.stream_url
    assert session.stream_url.endswith("/" + playlist)
    assert state.last_airplay_content.startswith(b"#EXTM3U")  # the receiver fetched our playlist
    if playlist == "master.m3u8":
        assert b"CODECS=" in state.last_airplay_content
    assert State.CASTING in states and states[-1] == State.STOPPED
    assert session._proc is None and session.encoder_returncode is not None  # encoder shut down


async def test_hap_pairing_used_by_modern_apple_tv(fake_atv, recorded_keys):
    """AirPlay 2 receivers (tvOS) advertise HAP pairing via the features flag."""
    from pyatv.const import Protocol
    from pyatv.storage.memory_storage import MemoryStorage
    from tests.fake_device.airplay import DEVICE_PIN

    from aircast.devices import DeviceManager

    fake_atv.get_usecase(Protocol.AirPlay).airplay_require_authentication()
    storage = MemoryStorage()
    manager = DeviceManager(asyncio.get_running_loop(), storage=storage)
    device = _device(fake_atv, {"features": "0x00000000,0x00010000"})

    async def pin_cb(*_):
        return str(DEVICE_PIN)

    await manager.pair(device, pin_cb)
    assert device.is_paired
    assert storage.settings[0].protocols.airplay.credentials == device.airplay.credentials


async def test_wrong_pin_fails(fake_atv):
    from pyatv import exceptions
    from pyatv.const import Protocol
    from pyatv.storage.memory_storage import MemoryStorage

    from aircast.devices import DeviceManager

    fake_atv.get_usecase(Protocol.AirPlay).airplay_require_authentication()
    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    device = _device(fake_atv)

    async def pin_cb(*_):
        return "9999"

    with pytest.raises(exceptions.PairingError):
        await manager.pair(device, pin_cb)
    assert not device.is_paired


async def test_stop_from_app(fake_atv, tmp_path):
    from pyatv.const import Protocol
    from pyatv.storage.memory_storage import MemoryStorage

    from aircast.config import Settings
    from aircast.devices import DeviceManager
    from aircast.session import CastSession, State

    usecase = fake_atv.get_usecase(Protocol.AirPlay)
    for _ in range(200):
        usecase.airplay_playback_playing()
    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    settings = Settings(capture_backend="test", encoder="libx264", output_height=360,
                        bind_address="127.0.0.1", http_port=0, capture_audio=False)
    states = []
    session = CastSession(manager, _device(fake_atv), settings, lambda s, m: states.append(s), work_dir=tmp_path)
    task = asyncio.ensure_future(session.run())
    while State.CASTING not in states:
        assert not task.done(), states
        await asyncio.sleep(0.2)
    await session.stop()
    await asyncio.wait_for(task, 20)
    assert states[-2:] == [State.STOPPING, State.STOPPED]


async def test_falls_back_when_first_capture_method_dies(fake_atv, tmp_path, monkeypatch):
    """If ddagrab cannot start (driver/GPU problem), the cast continues with gdigrab."""
    from pyatv.const import Protocol
    from pyatv.storage.memory_storage import MemoryStorage

    from aircast import ffmpeg as ff
    from aircast.config import Settings
    from aircast.devices import DeviceManager
    from aircast.session import CastSession, State

    real_input = ff._video_input

    def fake_input(backend, settings, monitor, region=None):
        if backend == "ddagrab":  # simulate a capture source that fails immediately
            return ["-f", "lavfi", "-i", "nullsrc=size=0x0:rate=30"], []
        return real_input("test", settings, monitor, region)

    monkeypatch.setattr(ff, "_video_input", fake_input)
    monkeypatch.setattr(ff, "pick_capture_backend", lambda *_a, **_k: "ddagrab")
    for _ in range(3):
        fake_atv.get_usecase(Protocol.AirPlay).airplay_playback_playing()

    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    settings = Settings(encoder="libx264", output_height=360, bind_address="127.0.0.1", http_port=0,
                        capture_audio=False)
    states = []
    session = CastSession(manager, _device(fake_atv), settings, lambda s, m: states.append(s), work_dir=tmp_path)
    await asyncio.wait_for(session.run(), timeout=90)
    assert session.backend == "gdigrab"
    assert State.CASTING in states and states[-1] == State.STOPPED


async def test_stop_while_starting_ends_at_once_and_stops_ffmpeg(fake_atv, tmp_path, monkeypatch):
    """Stop (or Quit) pressed while the encoder is still buffering must not be ignored."""
    from pyatv.storage.memory_storage import MemoryStorage

    from aircast.config import Settings
    from aircast.devices import DeviceManager
    from aircast.session import CastSession, State

    async def never_ready(self, *_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(CastSession, "_wait_for_segments", never_ready)
    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    settings = Settings(capture_backend="test", encoder="libx264", output_height=360,
                        bind_address="127.0.0.1", http_port=0, capture_audio=False)
    states = []
    session = CastSession(manager, _device(fake_atv), settings, lambda s, m: states.append(s), work_dir=tmp_path)
    task = asyncio.ensure_future(session.run())
    while State.BUFFERING not in states:
        await asyncio.sleep(0.05)
    proc = session._proc
    await session.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert session.finished.is_set()
    assert proc.poll() is not None  # FFmpeg is gone, not left encoding in the background
    assert states[-1] == State.STOPPED
