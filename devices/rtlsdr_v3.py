from core import Device

# RTL-SDR V3 reliable sample rates (Hz).  Values outside this set cause
# librtlsdr to silently round or produce spurious tones on some hardware.
_RTL_BW = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]


class RtlSdrDevice(Device):
    name                 = 'RTL-SDR-V3'
    key_help             = 'b=bias-tee'
    supported_bandwidths = _RTL_BW
    freq_min             = 25_000_000.0      # 25 MHz
    freq_max             = 1_766_000_000.0   # 1766 MHz

    def __init__(self):
        self._sdr      = None
        self._bias_tee = False
        self._has_bias_tee = False   # set True in open() if hardware supports it

    def open(self) -> bool:
        try:
            from rtlsdr import RtlSdr
            self._sdr = RtlSdr()
            self._has_bias_tee = hasattr(self._sdr, 'set_bias_tee')
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._sdr:
            self._sdr.close()

    @property
    def sample_rate(self):
        return self._sdr.sample_rate

    @sample_rate.setter
    def sample_rate(self, v):
        self._sdr.sample_rate = v

    @property
    def center_freq(self):
        return self._sdr.center_freq

    @center_freq.setter
    def center_freq(self, v):
        self._sdr.center_freq = v

    @property
    def gain(self):
        return self._sdr.gain

    @gain.setter
    def gain(self, v):
        self._sdr.gain = v

    def read_samples_async(self, callback, num_samples: int) -> None:
        self._sdr.read_samples_async(callback, num_samples=num_samples)

    def cancel_read_async(self) -> None:
        self._sdr.cancel_read_async()

    # ── device UI hooks ───────────────────────────────────────────────────────
    def handle_key(self, key: int, state) -> bool:
        if key == ord('b') and self._has_bias_tee:
            self._bias_tee = not self._bias_tee
            try:
                self._sdr.set_bias_tee(self._bias_tee)
            except Exception:
                self._bias_tee = False
            return True
        return False

    def status_text(self, state) -> str:
        if self._has_bias_tee:
            return ('[bias-tee:on]' if self._bias_tee else '[bias-tee:off]') + ' '
        return ''
