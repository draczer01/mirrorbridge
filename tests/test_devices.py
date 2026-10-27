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
