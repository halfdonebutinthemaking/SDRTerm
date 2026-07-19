"""Tests for device drivers — LocalFileDevice fully exercised; others tested
at the property/interface level without requiring physical hardware."""
import os
import tempfile

import numpy as np
import pytest

from core import AppState, BW_STEPS, Device


# ── LocalFileDevice ───────────────────────────────────────────────────────────

class TestLocalFileDevice:
    @pytest.fixture
    def device(self):
        from devices.localfile import LocalFileDevice
        d = LocalFileDevice()
        yield d
        d.close()

    @pytest.fixture
    def iq_path(self):
        n = 65_536
        rng = np.random.default_rng(7)
        data = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        with tempfile.NamedTemporaryFile(suffix='.iq', delete=False) as f:
            data.tofile(f)
            path = f.name
        yield path, data
        os.unlink(path)

    def test_open_returns_true(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        assert device.open() is True

    def test_data_length_matches_file(self, device, iq_path):
        path, data = iq_path
        device.set_path(path)
        device.open()
        assert len(device._data) == len(data)

    def test_sample_rate_property_roundtrip(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        device.sample_rate = 1_024_000
        assert device.sample_rate == 1_024_000

    def test_center_freq_property_roundtrip(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        device.center_freq = 433.92e6
        assert device.center_freq == pytest.approx(433.92e6)

    def test_gain_numeric(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        device.gain = 20.0
        assert device.gain == pytest.approx(20.0)

    def test_gain_auto_accepted(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        device.gain = 'auto'   # must not raise
        assert device.gain == 0.0

    def test_set_file_rate_overrides_default(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.set_file_rate(500_000)
        device.open()
        assert device._file_sr == 500_000

    def test_filename_freq_parsed(self, device):
        n = 4_096
        data = np.zeros(n, dtype=np.complex64)
        with tempfile.NamedTemporaryFile(
                suffix='_433920000Hz_test.iq', delete=False) as f:
            data.tofile(f)
            path = f.name
        try:
            device.set_path(path)
            device.open()
            assert device._file_center_hz == pytest.approx(433_920_000.0)
            assert device.center_freq == pytest.approx(433_920_000.0)
        finally:
            os.unlink(path)

    def test_no_freq_in_plain_filename(self, device, iq_path):
        path, _ = iq_path   # suffix is just '.iq', no Hz pattern
        device.set_path(path)
        device.open()
        assert device._file_center_hz is None

    def test_nonexistent_path_returns_false(self, device):
        device.set_path('/nonexistent/signal.iq')
        assert device.open() is False

    def test_empty_file_returns_false(self, device):
        with tempfile.NamedTemporaryFile(suffix='.iq', delete=False) as f:
            path = f.name
        try:
            device.set_path(path)
            assert device.open() is False
        finally:
            os.unlink(path)

    def test_close_clears_data(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        device.close()
        assert device._data is None

    def test_status_text_non_empty_when_open(self, device, iq_path):
        path, _ = iq_path
        device.set_path(path)
        device.open()
        text = device.status_text(AppState())
        assert isinstance(text, str) and len(text) > 0

    def test_status_text_empty_before_open(self, device):
        text = device.status_text(AppState())
        assert text == ''

    def test_wav_file_opens(self, device):
        """Stereo int16 WAV is loaded as complex IQ."""
        from scipy.io import wavfile
        n = 4_096
        rng = np.random.default_rng(99)
        stereo = (rng.integers(-32768, 32767, size=(n, 2), dtype=np.int16))
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wavfile.write(f.name, 44100, stereo)
            path = f.name
        try:
            device.set_path(path)
            assert device.open() is True
            assert len(device._data) == n
            assert device._file_sr == 44_100
        finally:
            os.unlink(path)


# ── Device base class interface ───────────────────────────────────────────────

class TestDeviceBaseInterface:
    """The base Device class must satisfy the minimum interface contract."""

    def test_open_returns_false(self):
        assert Device().open() is False

    def test_close_does_not_raise(self):
        Device().close()

    def test_handle_key_returns_false(self):
        assert Device().handle_key(ord('b'), AppState()) is False

    def test_status_text_returns_str(self):
        assert isinstance(Device().status_text(AppState()), str)

    def test_supported_bandwidths_not_empty(self):
        assert len(Device().supported_bandwidths) > 0

    def test_supported_bandwidths_sorted(self):
        bws = Device().supported_bandwidths
        assert bws == sorted(bws)


# ── RTL-SDR V3 driver (import-only — no hardware required) ───────────────────

class TestRtlSdrV3Driver:
    def test_import_succeeds(self):
        from devices.rtlsdr_v3 import RtlSdrDevice
        assert RtlSdrDevice is not None

    def test_name(self):
        from devices.rtlsdr_v3 import RtlSdrDevice
        assert RtlSdrDevice.name == 'RTL-SDR-V3'

    def test_freq_range(self):
        from devices.rtlsdr_v3 import RtlSdrDevice
        assert RtlSdrDevice.freq_min == pytest.approx(25e6)
        assert RtlSdrDevice.freq_max == pytest.approx(1766e6)

    def test_supported_bandwidths_all_positive(self):
        from devices.rtlsdr_v3 import RtlSdrDevice
        assert all(b > 0 for b in RtlSdrDevice.supported_bandwidths)

    def test_open_returns_false_without_hardware(self):
        from devices.rtlsdr_v3 import RtlSdrDevice
        d = RtlSdrDevice()
        result = d.open()
        if result:
            d.close()
            pytest.skip('RTL-SDR hardware is attached')
        assert result is False


# ── HackRF driver (import-only — no hardware required) ───────────────────────

class TestHackRFDriver:
    def test_import_succeeds(self):
        from devices.hackrf import HackRFDevice
        assert HackRFDevice is not None

    def test_name(self):
        from devices.hackrf import HackRFDevice
        assert HackRFDevice.name == 'HackRF'

    def test_freq_range(self):
        from devices.hackrf import HackRFDevice
        assert HackRFDevice.freq_min == pytest.approx(1e6)
        assert HackRFDevice.freq_max == pytest.approx(6e9)

    def test_supported_bandwidths_all_positive(self):
        from devices.hackrf import HackRFDevice
        assert all(b > 0 for b in HackRFDevice.supported_bandwidths)

    def test_open_returns_false_without_hardware(self):
        from devices.hackrf import HackRFDevice
        assert HackRFDevice().open() is False
