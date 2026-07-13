import os
import time
import threading
import numpy as np
from core import Device, AppState, BW_STEPS


class LocalFileDevice(Device):
    """Replay a raw complex64 IQ file as if it were live SDR hardware.
    The file loops continuously.  Playback is paced at the current sample rate
    (set by main.py via device.sample_rate = ...) so the spectrum updates at
    roughly the same cadence as real hardware.
    """

    name                 = 'localfile'
    key_help             = ''
    supported_bandwidths = BW_STEPS

    def __init__(self):
        self._path      = None
        self._data      = None   # np.memmap, complex64, set in open()
        self._pos       = 0
        self._file_sr   = 2_400_000  # recording's actual sample rate — set via set_file_rate()
        self._sr        = 2_400_000  # reported to the app; may differ from _file_sr
        self._center_hz = 0.0
        self._gain      = 0.0
        self._stop_evt  = threading.Event()
        self._thread    = None

    def set_path(self, path: str) -> None:
        self._path = path

    def set_file_rate(self, rate: int) -> None:
        self._file_sr = rate

    def open(self) -> bool:
        if not self._path:
            return False
        try:
            data = np.memmap(self._path, dtype=np.complex64, mode='r')
            if len(data) == 0:
                return False
            self._data = data
            self._pos  = 0
            return True
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._data = None

    # ── hardware-property shims (file device ignores writes) ──────────────────
    @property
    def sample_rate(self):   return self._sr
    @sample_rate.setter
    def sample_rate(self, v): self._sr = int(v)

    @property
    def center_freq(self):    return self._center_hz
    @center_freq.setter
    def center_freq(self, v): self._center_hz = float(v)

    @property
    def gain(self):           return self._gain
    @gain.setter
    def gain(self, v):        self._gain = 0.0 if v == 'auto' else float(v)

    # ── async reader ──────────────────────────────────────────────────────────
    def read_samples_async(self, callback, num_samples: int = 16_384) -> None:
        self._stop_evt.clear()
        # Capture file_sr now so that app BW changes (which rewrite self._sr)
        # cannot alter the playback pacing mid-stream.
        pace_sr  = self._file_sr
        interval = num_samples / pace_sr

        def _run():
            data     = self._data
            n        = len(data)
            # Sleep-first: wait until the scheduled deadline, then read and
            # deliver.  Callback/processing time never eats into the interval,
            # so chunks arrive at uniform t0 + k*interval regardless of how
            # long the downstream pipeline takes.
            deadline = time.monotonic()
            while not self._stop_evt.is_set():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if self._stop_evt.is_set():
                    break
                end = self._pos + num_samples
                if end <= n:
                    chunk     = np.array(data[self._pos:end])
                    self._pos = end % n   # 0 when end == n exactly
                else:
                    # straddles EOF: tail of file + head
                    tail      = np.array(data[self._pos:])
                    head      = np.array(data[:end - n])
                    chunk     = np.concatenate([tail, head])
                    self._pos = end - n
                callback(chunk, None)
                deadline += interval

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def cancel_read_async(self) -> None:
        self._stop_evt.set()

    # ── UI ────────────────────────────────────────────────────────────────────
    def status_text(self, state: 'AppState') -> str:
        if self._data is None:
            return ''
        name = os.path.basename(self._path or '')
        if len(name) > 20:
            name = name[:9] + '…' + name[-10:]
        pct      = self._pos / len(self._data) * 100 if len(self._data) else 0
        dur_s    = len(self._data) / self._file_sr
        pos_s    = self._pos / self._file_sr
        return '[FILE {} {:.0f}s/{:.0f}s] '.format(name, pos_s, dur_s)
