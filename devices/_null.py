"""Placeholder Device for offline / inspection-only sessions.

Used when SDRTerm is launched without --d or --file and no SDR auto-detects.
The UI still opens so the user can access the web server to inspect data
captured in previous sessions (ADS-B CSV logs, Meteor pass cache, etc.).

The underscore prefix on this filename keeps load_devices() from
auto-discovering it; main.py instantiates it explicitly.
"""

import threading

from core import Device, BW_STEPS


class NullDevice(Device):
    name                 = 'offline'
    key_help             = ''
    supported_bandwidths = BW_STEPS
    is_offline           = True

    def __init__(self):
        self._sr        = BW_STEPS[-1]
        self._center_hz = 0.0
        self._gain      = 0.0
        self._stop_evt  = threading.Event()

    def open(self) -> bool:             return True
    def close(self) -> None:            self._stop_evt.set()
    def reopen(self) -> None:           self._stop_evt.clear()
    def read_samples_async(self, callback, num_samples: int) -> None:
        # Block until cancel_read_async fires so main.py's reader thread
        # stays alive without consuming CPU and exits cleanly on shutdown.
        self._stop_evt.wait()
    def cancel_read_async(self) -> None: self._stop_evt.set()

    @property
    def sample_rate(self) -> int:       return self._sr
    @sample_rate.setter
    def sample_rate(self, v: int) -> None: self._sr = int(v)

    @property
    def center_freq(self) -> float:     return self._center_hz
    @center_freq.setter
    def center_freq(self, v: float) -> None: self._center_hz = float(v)

    @property
    def gain(self):                     return self._gain
    @gain.setter
    def gain(self, v) -> None:          self._gain = v

    def status_text(self, state) -> str:
        return '[OFFLINE] '
