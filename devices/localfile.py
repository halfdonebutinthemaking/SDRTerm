import os
import re
import time
import threading
import numpy as np
from core import Device, AppState, BW_STEPS

# Match a centre frequency embedded in a filename, e.g.:
#   baseband_179965057Hz_17-48-03_18-07-2026 250k.wav   (SDR++)
#   recording_433920000Hz.iq
_FNAME_FREQ_RE = re.compile(r'[_\-\s](\d{6,12})Hz[_\-\s\.]', re.IGNORECASE)


class LocalFileDevice(Device):
    """Replay a raw complex64 IQ file as if it were live SDR hardware.
    The file loops continuously.  Playback is paced at the current sample rate
    (set by main.py via device.sample_rate = ...) so the spectrum updates at
    roughly the same cadence as real hardware.

    For SigMF files the recorded centre frequency (_file_center_hz) is read
    from the .sigmf-meta companion and exposed so the spectrum plugin can roll
    its output to compensate for display-centre changes (follow mode, manual
    frequency entry).  IQ samples are never modified.
    """

    name                 = 'localfile'
    key_help             = ''
    supported_bandwidths = BW_STEPS

    def __init__(self):
        self._path             = None
        self._data             = None   # ndarray complex64, set in open()
        self._pos              = 0
        self._file_sr          = 2_400_000  # recording's actual sample rate
        self._file_sr_explicit = False      # True if set_file_rate() was called
        self._sr               = 2_400_000  # reported to the app
        self._center_hz        = 0.0
        self._file_center_hz   = None       # set only for SigMF; None = no mixer
        self._gain             = 0.0
        self._stop_evt         = threading.Event()
        self._thread           = None

    def set_path(self, path: str) -> None:
        self._path             = path
        self._file_sr_explicit = False

    def set_file_rate(self, rate: int) -> None:
        self._file_sr          = rate
        self._file_sr_explicit = True

    def open(self) -> bool:
        if not self._path:
            return False
        try:
            p = self._path.lower()
            if p.endswith('.wav'):
                return self._open_wav()
            if p.endswith('.sigmf-data') or p.endswith('.sigmf'):
                return self._open_sigmf()
            return self._open_iq()
        except (OSError, ValueError, Exception):
            return False

    def _open_iq(self) -> bool:
        data = np.memmap(self._path, dtype=np.complex64, mode='r')
        if len(data) == 0:
            return False
        self._data = data
        self._pos  = 0
        m = _FNAME_FREQ_RE.search(os.path.basename(self._path))
        self._file_center_hz = float(m.group(1)) if m else None
        if self._file_center_hz is not None:
            self._center_hz = self._file_center_hz
        return True

    def _open_sigmf(self) -> bool:
        import json
        base = self._path
        for suffix in ('.sigmf-data', '.sigmf'):
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)]
                break
        data_path = base + '.sigmf-data'
        meta_path = base + '.sigmf-meta'

        data = np.memmap(data_path, dtype=np.complex64, mode='r')
        if len(data) == 0:
            return False
        self._data           = data
        self._pos            = 0
        self._file_center_hz = None   # will be overwritten below if meta has it

        if not self._file_sr_explicit and os.path.exists(meta_path):
            try:
                with open(meta_path) as mf:
                    meta = json.load(mf)
                sr = meta.get('global', {}).get('core:sample_rate')
                if sr:
                    self._file_sr = int(sr)
                    self._sr      = int(sr)
                captures = meta.get('captures', [])
                if captures:
                    freq = captures[0].get('core:frequency')
                    if freq:
                        self._center_hz      = float(freq)
                        self._file_center_hz = float(freq)  # mixer enabled
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        return True

    def _open_wav(self) -> bool:
        from scipy.io import wavfile
        rate, raw = wavfile.read(self._path)

        if raw.dtype == np.uint8:
            raw = (raw.astype(np.float32) - 128.0) / 128.0
        elif raw.dtype == np.int16:
            raw = raw.astype(np.float32) / 32768.0
        elif raw.dtype == np.int32:
            raw = raw.astype(np.float32) / 2147483648.0
        else:
            raw = raw.astype(np.float32)

        if raw.ndim == 2:
            iq = (raw[:, 0] + 1j * raw[:, 1]).astype(np.complex64)
        else:
            iq = raw.astype(np.complex64)

        if len(iq) == 0:
            return False

        self._data = iq
        self._pos  = 0
        m = _FNAME_FREQ_RE.search(os.path.basename(self._path))
        self._file_center_hz = float(m.group(1)) if m else None
        if self._file_center_hz is not None:
            self._center_hz = self._file_center_hz
        if not self._file_sr_explicit:
            self._file_sr = int(rate)
            self._sr      = int(rate)
        return True

    def close(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._data = None

    # ── hardware-property shims ───────────────────────────────────────────────
    @property
    def sample_rate(self):    return self._sr
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
        # Ensure any previous _run thread has stopped before clearing the
        # event; otherwise it may see the cleared flag and run past its cancel.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._stop_evt.clear()
        pace_sr  = self._file_sr
        interval = num_samples / pace_sr

        def _run():
            data     = self._data
            n        = len(data)
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
                    self._pos = end % n
                else:
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
        dur_s = len(self._data) / self._file_sr
        pos_s = self._pos / self._file_sr
        return '[FILE {} {:.0f}s/{:.0f}s] '.format(name, pos_s, dur_s)
