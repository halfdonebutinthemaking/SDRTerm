"""HackRF One driver — direct ctypes binding to libhackrf.

Loads libhackrf natively (libhackrf.dylib on macOS from Homebrew,
libhackrf.so.0 on Linux) with no intermediate Python wrapper.  This avoids
the pyhackrf / pyhackrf2 problem where the wrapper hardcodes the Linux
library filename (`libhackrf.so.0`) and fails to load on macOS.

Requires the native library:
  macOS:  brew install hackrf
  Linux:  apt install libhackrf0     (Debian / Ubuntu)
          dnf install hackrf-devel   (Fedora)
"""
import ctypes
import ctypes.util
import platform
import sys
import threading
import time

import numpy as np
from core import Device, AppState


# ── libhackrf loader ─────────────────────────────────────────────────────────

def _load_libhackrf() -> ctypes.CDLL:
    """Locate and open libhackrf across platforms.  Raises OSError if missing."""
    candidates = []
    if platform.system() == "Darwin":
        candidates += [
            "/opt/homebrew/lib/libhackrf.dylib",   # Apple Silicon Homebrew
            "/usr/local/lib/libhackrf.dylib",      # Intel Homebrew
            "libhackrf.dylib",                     # DYLD_LIBRARY_PATH fallback
        ]
    else:
        candidates += [
            "libhackrf.so.0",
            "libhackrf.so",
        ]
    found = ctypes.util.find_library("hackrf")
    if found:
        candidates.append(found)
    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    raise OSError(
        "libhackrf not found. Install with:\n"
        "  macOS:  brew install hackrf\n"
        "  Linux:  apt install libhackrf0  or  dnf install hackrf-devel"
    )


# ── libhackrf struct + function bindings ─────────────────────────────────────

class _HackRFTransfer(ctypes.Structure):
    """libhackrf hackrf_transfer struct passed to the RX callback."""
    _fields_ = [
        ("device",         ctypes.c_void_p),
        ("buffer",         ctypes.POINTER(ctypes.c_uint8)),
        ("buffer_length",  ctypes.c_int),
        ("valid_length",   ctypes.c_int),
        ("rx_ctx",         ctypes.c_void_p),
        ("tx_ctx",         ctypes.c_void_p),
    ]


_SAMPLE_BLOCK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_HackRFTransfer))


def _bind(lib: ctypes.CDLL) -> None:
    """Attach restype / argtypes to libhackrf's C functions."""
    lib.hackrf_init.restype = ctypes.c_int
    lib.hackrf_init.argtypes = []
    lib.hackrf_exit.restype = ctypes.c_int
    lib.hackrf_exit.argtypes = []
    lib.hackrf_open.restype = ctypes.c_int
    lib.hackrf_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.hackrf_close.restype = ctypes.c_int
    lib.hackrf_close.argtypes = [ctypes.c_void_p]
    lib.hackrf_set_freq.restype = ctypes.c_int
    lib.hackrf_set_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.hackrf_set_sample_rate.restype = ctypes.c_int
    lib.hackrf_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_double]
    lib.hackrf_set_lna_gain.restype = ctypes.c_int
    lib.hackrf_set_lna_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.hackrf_set_vga_gain.restype = ctypes.c_int
    lib.hackrf_set_vga_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.hackrf_set_amp_enable.restype = ctypes.c_int
    lib.hackrf_set_amp_enable.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    # hackrf_set_antenna_enable = antenna-port 3.3V DC (bias-tee), for powering
    # active antennas / external LNAs.  Separate from the ~14 dB internal amp.
    lib.hackrf_set_antenna_enable.restype = ctypes.c_int
    lib.hackrf_set_antenna_enable.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    lib.hackrf_set_baseband_filter_bandwidth.restype = ctypes.c_int
    lib.hackrf_set_baseband_filter_bandwidth.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.hackrf_start_rx.restype = ctypes.c_int
    lib.hackrf_start_rx.argtypes = [ctypes.c_void_p, _SAMPLE_BLOCK_CB, ctypes.c_void_p]
    lib.hackrf_stop_rx.restype = ctypes.c_int
    lib.hackrf_stop_rx.argtypes = [ctypes.c_void_p]


# Module-level singleton — libhackrf's hackrf_init() must be called once per
# process, and repeated loads waste time / can leak state.
_lib: ctypes.CDLL = None    # populated on first successful _get_lib()


def _get_lib() -> ctypes.CDLL:
    """Return the loaded + initialised libhackrf, or raise a descriptive error."""
    global _lib
    if _lib is None:
        loaded = _load_libhackrf()
        _bind(loaded)
        rc = loaded.hackrf_init()
        if rc != 0:
            raise RuntimeError("hackrf_init() failed with code {}".format(rc))
        _lib = loaded
    return _lib


# ── driver ───────────────────────────────────────────────────────────────────

_HRF_BW = [
    2_000_000, 4_000_000, 6_000_000, 8_000_000,
    10_000_000, 12_500_000, 16_000_000, 20_000_000,
]

# libhackrf accepts filter bandwidths of 1.75, 2.5, 3.5, 5, 5.5, 6, 7, 8, 9,
# 10, 12, 14, 15, 20, 24, 28 MHz.  Map each supported sample rate to the
# nearest lower-or-equal filter bandwidth.
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
    key_help             = 'b=bias-tee  B=amp'
    supported_bandwidths = _HRF_BW
    freq_min             = 1_000_000.0
    freq_max             = 6_000_000_000.0

    def __init__(self):
        self._dev          = None                    # hackrf_device* (opaque)
        self._amp          = False                   # ~14 dB RF pre-amp
        self._bias_tee     = False                   # 3.3 V antenna port power
        self._lna_gain     = 16                      # 0–40, 8 dB steps
        self._vga_gain     = 20                      # 0–62, 2 dB steps
        self._sample_rate  = _HRF_BW[-1]
        self._center_freq  = 100_000_000.0
        self._gain         = 0.0
        self._cb_c         = None                    # keep CFUNCTYPE ref alive
        self._stop_evt     = threading.Event()
        self._cb_running   = threading.Event()       # set while callback is inside

    def open(self) -> bool:
        try:
            lib = _get_lib()
        except (OSError, RuntimeError) as e:
            print("HackRF: {}".format(e), file=sys.stderr)
            return False
        handle = ctypes.c_void_p()
        rc = lib.hackrf_open(ctypes.byref(handle))
        if rc != 0:
            print("HackRF: hackrf_open failed with code {} "
                  "(hardware present? another process using it?)".format(rc),
                  file=sys.stderr)
            return False
        self._dev = handle
        lib.hackrf_set_sample_rate(handle, ctypes.c_double(float(self._sample_rate)))
        lib.hackrf_set_freq(handle, ctypes.c_uint64(int(self._center_freq)))
        lib.hackrf_set_lna_gain(handle, self._lna_gain)
        lib.hackrf_set_vga_gain(handle, self._vga_gain)
        lib.hackrf_set_amp_enable(handle, 1 if self._amp else 0)
        try:
            lib.hackrf_set_antenna_enable(handle, 1 if self._bias_tee else 0)
        except OSError:
            # Very old libhackrf builds may lack this symbol; downgrade
            # gracefully (bias-tee just won't work on that build).
            self._bias_tee = False
        bw = _FILTER_MAP.get(
            self._sample_rate,
            min(_FILTER_MAP.values(), key=lambda x: abs(x - self._sample_rate)))
        try:
            lib.hackrf_set_baseband_filter_bandwidth(handle, bw)
        except OSError:
            pass
        return True

    def close(self) -> None:
        if self._dev is not None:
            self._stop_evt.set()
            try:
                _lib.hackrf_stop_rx(self._dev)
            except Exception:
                pass
            # hackrf_stop_rx returns before the libusb thread's in-flight
            # callback finishes.  Wait briefly for the callback to observe
            # _stop_evt and return, so hackrf_close() doesn't race with it.
            for _ in range(20):        # up to ~200 ms
                if not self._cb_running.is_set():
                    break
                time.sleep(0.01)
            try:
                _lib.hackrf_close(self._dev)
            except Exception:
                pass
            self._dev = None
            self._cb_c = None

    def reopen(self) -> None:
        """Close and reopen — required for sample-rate changes.

        Matches the rtlsdr driver's pattern (main.py calls sdr.reopen()
        every time state.bw_hz changes).  All device state is stored as
        instance attributes, so a fresh open() re-applies center_freq,
        sample_rate, gains, and amp from the current values.
        """
        self.close()
        self.open()

    # ── hardware properties ───────────────────────────────────────────────────
    @property
    def sample_rate(self):
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, v):
        self._sample_rate = int(v)
        if self._dev is not None:
            _lib.hackrf_set_sample_rate(self._dev,
                                        ctypes.c_double(float(self._sample_rate)))
            bw = _FILTER_MAP.get(
                self._sample_rate,
                min(_FILTER_MAP.values(), key=lambda x: abs(x - self._sample_rate)))
            try:
                _lib.hackrf_set_baseband_filter_bandwidth(self._dev, bw)
            except OSError:
                pass

    @property
    def center_freq(self):
        return self._center_freq

    @center_freq.setter
    def center_freq(self, v):
        self._center_freq = float(v)
        if self._dev is not None:
            _lib.hackrf_set_freq(self._dev, ctypes.c_uint64(int(v)))

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, v):
        if v == 'auto':
            # HackRF has no hardware AGC; treat 'auto' as a mid-range preset.
            self._gain     = 0.0
            self._lna_gain = 24
            self._vga_gain = 30
        else:
            self._gain = float(v)
            # Map a single dB value onto LNA + VGA (LNA 0–40 in 8 dB steps,
            # VGA 0–62 in 2 dB steps for the remainder).
            lna = min(40, max(0, round(float(v) / 8) * 8))
            vga = min(62, max(0, round((float(v) - lna) / 2) * 2))
            self._lna_gain = lna
            self._vga_gain = vga
        if self._dev is not None:
            try:
                _lib.hackrf_set_lna_gain(self._dev, self._lna_gain)
                _lib.hackrf_set_vga_gain(self._dev, self._vga_gain)
            except OSError:
                pass

    # ── async reader ──────────────────────────────────────────────────────────

    def read_samples_async(self, callback, num_samples: int) -> None:
        if self._dev is None:
            return

        buf: list = []
        py_cb = callback

        def _rx_cb(transfer_ptr):
            # Called from a libusb thread inside libhackrf.
            if self._stop_evt.is_set():
                return 0
            self._cb_running.set()
            try:
                transfer = transfer_ptr.contents
                n_bytes = transfer.valid_length
                if n_bytes <= 0:
                    return 0
                # HackRF delivers interleaved signed 8-bit I/Q as uint8_t*.
                # string_at copies the buffer safely (libusb reuses the source).
                raw = np.frombuffer(
                    ctypes.string_at(transfer.buffer, n_bytes),
                    dtype=np.int8,
                )
                samples = raw.astype(np.float32) / 128.0
                iq = (samples[0::2] + 1j * samples[1::2]).astype(np.complex64)
                buf.append(iq)
                total = sum(len(c) for c in buf)
                while total >= num_samples:
                    chunk = np.concatenate(buf)
                    py_cb(chunk[:num_samples], None)
                    remaining = chunk[num_samples:]
                    buf.clear()
                    if len(remaining):
                        buf.append(remaining)
                    total = len(remaining)
                return 0
            finally:
                self._cb_running.clear()

        self._cb_c = _SAMPLE_BLOCK_CB(_rx_cb)   # must outlive start_rx call
        self._stop_evt.clear()
        self._cb_running.clear()
        rc = _lib.hackrf_start_rx(self._dev, self._cb_c, None)
        if rc != 0:
            print("HackRF: hackrf_start_rx failed with code {}".format(rc),
                  file=sys.stderr)
            return
        # Block until cancel_read_async() sets _stop_evt.  librtlsdr's
        # read_samples_async() blocks in its own libusb event loop, so
        # main.py's reader thread stays alive while RX is running; it uses
        # `reader.is_alive()` as the signal that the device is streaming.
        # hackrf_start_rx returns immediately, so without this wait the
        # reader thread would exit right after start_rx, main.py would
        # think no cleanup is needed on the next bandwidth change, and
        # would try to reconfigure a still-streaming device — hang.
        self._stop_evt.wait()

    def cancel_read_async(self) -> None:
        if self._dev is not None:
            self._stop_evt.set()
            try:
                _lib.hackrf_stop_rx(self._dev)
            except Exception:
                pass

    # ── device UI hooks ───────────────────────────────────────────────────────

    def handle_key(self, key: int, state: 'AppState') -> bool:
        if key == ord('b'):
            self._bias_tee = not self._bias_tee
            if self._dev is not None:
                try:
                    _lib.hackrf_set_antenna_enable(
                        self._dev, 1 if self._bias_tee else 0)
                except OSError:
                    self._bias_tee = False
            return True
        if key == ord('B'):
            self._amp = not self._amp
            if self._dev is not None:
                try:
                    _lib.hackrf_set_amp_enable(self._dev, 1 if self._amp else 0)
                except OSError:
                    self._amp = False
            return True
        return False

    def status_text(self, state: 'AppState') -> str:
        return ('[bias-tee:{}] [amp:{}] '.format(
            'on' if self._bias_tee else 'off',
            'on' if self._amp      else 'off'))
