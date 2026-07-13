from core import Device


class RtlSdrDevice(Device):
    name = 'RTL-SDR'

    def __init__(self):
        self._sdr = None

    def open(self) -> bool:
        try:
            from rtlsdr import RtlSdr
            self._sdr = RtlSdr()
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
