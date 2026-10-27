import asyncio

from aircast import config, devices


async def test_corrupt_pairings_file_does_not_break_everything(tmp_path, monkeypatch):
    """A half-written pairings.json (crash or power loss) must not block scan/pair/cast."""
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(devices, "credentials_file", lambda: tmp_path / "pairings.json")
    (tmp_path / "pairings.json").write_text("")
    manager = devices.DeviceManager(asyncio.get_running_loop())
    await manager._ensure_loaded()  # pylint: disable=protected-access
    assert manager._loaded  # pylint: disable=protected-access
    assert (tmp_path / "pairings.json.bad").exists()
