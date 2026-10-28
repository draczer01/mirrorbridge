import asyncio
import logging

import pytest
from pyatv.const import Protocol

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


def _device(properties, device_info=None):
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import PairingRequirement, Protocol

    conf = AppleTV("192.168.1.40", "Sala familiar", device_info=device_info)
    conf.add_service(ManualService("id", Protocol.AirPlay, 7000, properties,
                                   pairing_requirement=PairingRequirement.Mandatory))
    return devices.Device(conf)


SAMSUNG_FEATURES = "0x7F8AD0,0x38BCF46"  # QN90D: the low word is not zero-padded


def test_features_are_read_with_the_low_word_first_even_without_zero_padding():
    bits = devices.feature_bits(devices.parse_features(SAMSUNG_FEATURES))
    assert bits == [4, 6, 7, 9, 11, *range(15, 23), 33, 34, 38, *range(40, 44), *range(46, 50),
                    51, 55, 56, 57]
    apple = "0x4A7FDFD5,0xBC157FDE"  # zero-padded: pyatv reads it the same way
    from pyatv.protocols.airplay.utils import parse_features as pyatv_parse

    assert devices.parse_features(apple) == int(pyatv_parse(apple))
    assert devices.parse_features("0x1") == 1


def test_maker_comes_from_the_advertised_model():
    from pyatv.const import DeviceModel
    from pyatv.interface import DeviceInfo

    assert _device({"model": "AppleTV11,1"}).is_apple is True
    assert _device({"model": "Mac14,3"}).is_apple is True
    assert _device({}, DeviceInfo({DeviceInfo.MODEL: DeviceModel.Gen4K})).is_apple is True
    assert _device({"model": "55QM7L", "manufacturer": "TCL"}).is_apple is False
    assert _device({"manufacturer": "Samsung"}).is_apple is False
    assert _device({"features": "0x1,0x0"}).is_apple is None  # manual entry: unknown


def test_details_show_raw_values_and_decoded_video_bits():
    dev = _device({"features": SAMSUNG_FEATURES, "model": "QN90D", "manufacturer": "Samsung",
                   "srcvers": "377.40.00", "pk": "secret-looking-key"})
    text = dev.details()
    assert "model=QN90D" in text and "manufacturer=Samsung" in text and "srcvers=377.40.00" in text
    assert f"features={SAMSUNG_FEATURES} (bits 4,6,7," in text
    assert "VideoV1 no, PlayQueue yes, VideoV2 yes" in text
    assert "pyatv reads the features as bits" in text  # its decoding differs for this TV
    assert "maker: not Apple" in text and "pk=" not in text
    assert dev.can_cast_video


def _pyatv_allows_play_url(device) -> bool:
    """pyatv's own gate, which FacadeStream.play_url checks before anything else."""
    from pyatv.const import FeatureName, FeatureState
    from pyatv.protocols.airplay import AirPlayFeatures
    from pyatv.protocols.airplay.utils import parse_features as pyatv_parse

    features = AirPlayFeatures(pyatv_parse(device.airplay.properties.get("features", "0x0")))
    return features.get_feature(FeatureName.PlayUrl).state == FeatureState.Available


@pytest.mark.parametrize("features", ["0x7F8AD0,0x18BCF46", "0x7F8AD0,0x20000"])
def test_other_brands_video_bits_are_handed_to_pyatv_zero_padded(features, caplog):
    """VideoV2 (bit 49) is set, but pyatv's reading of the unpadded value says no video:
    without the fix AirCast would show the TV as ready and pyatv would refuse play_url."""
    dev = _device({"features": features, "manufacturer": "TCL"})
    assert dev.can_cast_video and not _pyatv_allows_play_url(dev)
    bits = devices.feature_bits(dev.features)
    with caplog.at_level(logging.INFO, logger="aircast.devices"):
        dev.fix_features()
    assert _pyatv_allows_play_url(dev)
    assert devices.feature_bits(dev.features) == bits  # the same bits, now read alike
    assert f"misreads the features of Sala familiar ({features})" in caplog.text


def test_features_that_pyatv_reads_well_enough_are_left_alone():
    samsung = _device({"features": SAMSUNG_FEATURES, "manufacturer": "Samsung"})
    samsung.fix_features()  # both readings have AirPlay video
    assert samsung.airplay.properties["features"] == SAMSUNG_FEATURES
    # Apple's receivers, and those that do not say what they are, keep pyatv's reading:
    # the device list then agrees with what pyatv will allow.
    for props in ({"features": "0x7F8AD0,0x20000", "model": "AppleTV14,1"}, {"features": "0x7F8AD0,0x20000"}):
        dev = _device(props)
        dev.fix_features()
        assert dev.airplay.properties["features"] == "0x7F8AD0,0x20000"
        assert dev.can_cast_video is _pyatv_allows_play_url(dev) is False


async def test_connect_logs_what_the_receiver_advertises(monkeypatch, caplog):
    connected = []

    async def fake_connect(config, loop, storage=None):
        connected.append(config.get_service(Protocol.AirPlay).properties["features"])
        return "atv"

    monkeypatch.setattr(devices.pyatv, "connect", fake_connect)
    from pyatv.storage.memory_storage import MemoryStorage

    manager = devices.DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())
    dev = _device({"features": "0x7F8AD0,0x20000", "model": "55QM7L", "manufacturer": "TCL"})
    with caplog.at_level(logging.INFO, logger="aircast.devices"):
        assert await manager.connect(dev) == "atv"
    assert ("Receiver Sala familiar (192.168.1.40): model=55QM7L; manufacturer=TCL; "
            "features=0x7F8AD0,0x20000 (bits 4,6,7,9,11,15,16,17,18,19,20,21,22,49; "
            "VideoV1 no, PlayQueue no, VideoV2 yes)") in caplog.text
    assert connected == ["0x007F8AD0,0x00020000"]  # pyatv is handed the corrected value


def test_scan_verbose_prints_what_each_receiver_advertises(capsys):
    from aircast import cli

    dev = _device({"features": SAMSUNG_FEATURES, "model": "QN90D", "manufacturer": "Samsung"})
    cli._print_devices([dev])
    assert "model=" not in capsys.readouterr().out
    cli._print_devices([dev], verbose=True)
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:3] == ["1.", "Sala", "familiar"]
    assert out[1] == f"    {dev.details()}" and "VideoV2 yes" in out[1]
