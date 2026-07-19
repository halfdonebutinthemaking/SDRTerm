import numpy as np
from core import Device, AppState

# HackRF One tunable range and supported sample rates.
# pyhackrf / libhackrf expose sample rates from 2 to 20 MSPS; rates below
# 4 MSPS require the hardware's baseband filter to be set explicitly.
_HRF_BW = [
    2_000_000, 4_000_000, 6_000_000, 8_000_000,
    10_000_000, 12_500_000, 16_000_000, 20_000_000,
]

# Map from sample rate to the nearest libhackrf baseband filter bandwidth.
# libhackrf accepts: 1.75, 2.5, 3.5, 5, 5.5, 6, 7, 8, 9, 10, 12, 14, 15,
# 20, 24, 28 MHz.  Values are in Hz.
_FILTER_MAP = {
    2_000_000:  1_750_000,
    4_000_000:  3_500_000,
    6_000_000:  6_000_000,
    8_000_000:  7_000_000,
    10_000_000: 9_000_000,
    12_500_000: 12_000_000,
    16_000_000: 15_000_000,
    20_000_000: 20_000_000,
}


class HackRFDevice(Device):
    name                 = 'HackRF'
    key_help             = 'b=amp'
    supported_bandwidths = _HRF_BW
    freq_min             = 1_000_000.0       # 1 MHz
    freq_max             = 6_000_000_000.0   # 6 GHz

    def __init__(self):
        self._dev          = None
        self._amp          = False
        self._lna_gain     = 16    # dB, 0–40 in 8 dB steps
        self._vga_gain     = 20    # dB, 0–62 in 2 dB steps
        self._sample_rate  = _HRF_BW[-1]
        self._center_freq  = 100_000_000.0
        self._gain         = 0.0

    def open(self) -> bool:
        try:
            import hackrf
            self._dev = hackrf.HackRF()
            self._dev.sample_rate = self._sample_rate
            self._dev.center_freq = int(self._center_freq)
            self._dev.lna_gain    = self._lna_gain
            self._dev.vga_gain    = self._vga_gain
            self._dev.amplifier_on = self._amp
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    # ── hardware properties ───────────────────────────────────────────────────
    @property
    def sample_rate(self):
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, v):
        self._sample_rate = int(v)
        if self._dev:
            self._dev.sample_rate = self._sample_rate
            bw = _FILTER_MAP.get(self._sample_rate,
                                 min(_FILTER_MAP.values(),
                                     key=lambda x: abs(x - self._sample_rate)))
            try:
                self._dev.baseband_filter_bandwidth = bw
            except Exception:
                pass

    @property
    def center_freq(self):
        return self._center_freq

    @center_freq.setter
    def center_freq(self, v):
        self._center_freq = float(v)
        if self._dev:
            self._dev.center_freq = int(v)

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, v):
        if v == 'auto':
            # HackRF has no hardware AGC; treat 'auto' as a mid-range preset
            self._gain    = 0.0
            self._lna_gain = 24
            self._vga_gain = 30
        else:
            self._gain = float(v)
            # Map a single dB value onto LNA + VGA.
            # LNA covers 0–40 dB (8 dB steps), VGA covers the remainder.
            lna = min(40, max(0, round(float(v) / 8) * 8))
            vga = min(62, max(0, round((float(v) - lna) / 2) * 2))
            self._lna_gain = lna
            self._vga_gain = vga
        if self._dev:
            try:
                self._dev.lna_gain = self._lna_gain
                self._dev.vga_gain = self._vga_gain
            except Exception:
                pass

    # ── async reader ──────────────────────────────────────────────────────────
    def read_samples_async(self, callback, num_samples: int) -> None:
        if not self._dev:
            return

        buf: list = []

        def _rx_cb(data, _meta):
            # libhackrf delivers interleaved signed 8-bit I/Q pairs
            samples = data.astype(np.float32) / 128.0
            iq = (samples[0::2] + 1j * samples[1::2]).astype(np.complex64)
            buf.append(iq)
            total = sum(len(c) for c in buf)
            while total >= num_samples:
                chunk = np.concatenate(buf)
                callback(chunk[:num_samples], None)
                remaining = chunk[num_samples:]
                buf.clear()
                if len(remaining):
                    buf.append(remaining)
                total = len(remaining)

        try:
            self._dev.start_rx(_rx_cb)
        except Exception:
            pass

    def cancel_read_async(self) -> None:
        if self._dev:
            try:
                self._dev.stop_rx()
            except Exception:
                pass

    # ── device UI hooks ───────────────────────────────────────────────────────
    def handle_key(self, key: int, state: 'AppState') -> bool:
        if key == ord('b'):
            self._amp = not self._amp
            if self._dev:
                try:
                    self._dev.amplifier_on = self._amp
                except Exception:
                    self._amp = False
            return True
        return False

    def status_text(self, state: 'AppState') -> str:
        return ('[amp:on]' if self._amp else '[amp:off]') + ' '
