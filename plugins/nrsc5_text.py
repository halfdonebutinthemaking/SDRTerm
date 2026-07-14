"""NRSC-5 HD Radio text decoder.

Uses libnrsc5 (https://github.com/theori-io/nrsc5) via ctypes when the
library is installed ('brew install nrsc5' on macOS).  Falls back to a
signal-presence / SNR display when the library is absent.

OFDM parameters (FM Hybrid mode, NRSC-5-C §11):
  Native sample rate : 744 187.5 Hz
  FFT size           : 2048
  Cyclic prefix      : 112 samples
  Symbol period      : 2160 samples
  Subcarrier spacing : 363.4 Hz
  Primary sidebands  : SC ±356…±546  (191 subcarriers each)
  Secondary sidebands: SC ±547…±682  (136 subcarriers each)
"""
import ctypes as ct
import ctypes.util
import curses
import threading
import time
import numpy as np
from core import Decoder, AppState, LABEL_W

# ── OFDM constants ────────────────────────────────────────────────────────────
_SR   = 744_187          # processing rate (native: 744 187.5 Hz)
_FFT  = 2048
_CP   = 112
_SYM  = _FFT + _CP       # 2160 samples / symbol

_HI_A, _HI_B =  356,  546   # primary upper sideband SC range
_MIN_SR       = 1_024_000
_DECODE_SYMS  = 64
_DECODE_INTERVAL = 3.0

# ── libnrsc5 ctypes interface ─────────────────────────────────────────────────
_EVT_SYNC      = 2
_EVT_LOST_SYNC = 3
_EVT_MER       = 4
_EVT_ID3       = 8
_EVT_SIS       = 11


class _Mer(ct.Structure):
    _fields_ = [('lower', ct.c_float), ('upper', ct.c_float)]


class _Sis(ct.Structure):
    # Matches nrsc5.h on 64-bit little-endian (ARM64 / x86_64).
    # ctypes inserts 4 B of padding after fcc_facility_id to align the
    # following char* to an 8-byte boundary — matching the C ABI.
    _fields_ = [
        ('country_code',    ct.c_char_p),
        ('fcc_facility_id', ct.c_int),
        ('name',            ct.c_char_p),
        ('slogan',          ct.c_char_p),
        ('message',         ct.c_char_p),
        ('alert',           ct.c_char_p),
        ('latitude',        ct.c_float),
        ('longitude',       ct.c_float),
        ('altitude',        ct.c_int),
        ('num_services',    ct.c_uint),
        ('services',        ct.c_void_p),
    ]


class _ID3(ct.Structure):
    # ctypes inserts 4 B of padding after `program` to align the first char*.
    _fields_ = [
        ('program',    ct.c_uint),
        ('title',      ct.c_char_p),
        ('artist',     ct.c_char_p),
        ('album',      ct.c_char_p),
        ('genre',      ct.c_char_p),
        ('ufid_owner', ct.c_char_p),
        ('ufid_id',    ct.c_char_p),
        ('xhdr_mime',  ct.c_uint),
        ('xhdr_param', ct.c_int),
        ('xhdr_lot',   ct.c_int),
    ]


class _EvtUnion(ct.Union):
    _fields_ = [('mer', _Mer), ('id3', _ID3), ('sis', _Sis)]


class _Event(ct.Structure):
    # `type` is 4 B; the union has 8-B alignment (contains pointers),
    # so ctypes adds 4 B of padding before `u`.  This matches the C ABI.
    _fields_ = [('type', ct.c_uint), ('u', _EvtUnion)]


_CB_T = ct.CFUNCTYPE(None, ct.POINTER(_Event), ct.c_void_p)


def _find_libnrsc5():
    """Return a ctypes CDLL handle for libnrsc5 or None."""
    candidates = [
        'nrsc5',
        '/opt/homebrew/lib/libnrsc5.dylib',
        '/usr/local/lib/libnrsc5.dylib',
        '/usr/lib/libnrsc5.so',
        '/usr/lib/x86_64-linux-gnu/libnrsc5.so.0',
        '/usr/lib/aarch64-linux-gnu/libnrsc5.so.0',
    ]
    for name in candidates:
        path = ct.util.find_library(name) or name
        try:
            lib = ct.CDLL(path)
            # Verify required symbols exist and set signatures
            lib.nrsc5_open_pipe.restype        = ct.c_int
            lib.nrsc5_open_pipe.argtypes       = [ct.POINTER(ct.c_void_p)]
            lib.nrsc5_close.argtypes           = [ct.c_void_p]
            lib.nrsc5_start.argtypes           = [ct.c_void_p]
            lib.nrsc5_stop.argtypes            = [ct.c_void_p]
            lib.nrsc5_set_callback.argtypes    = [ct.c_void_p, _CB_T, ct.c_void_p]
            lib.nrsc5_pipe_samples_cs16.argtypes = [ct.c_void_p,
                                                     ct.POINTER(ct.c_int16),
                                                     ct.c_uint]
            return lib
        except (OSError, AttributeError):
            pass
    return None


# ── signal-analysis helpers (fallback path, no libnrsc5) ─────────────────────

def _cp_metric(iq: np.ndarray) -> np.ndarray:
    """CP correlation magnitude; peaks at OFDM symbol boundaries."""
    n = len(iq) - _FFT - _CP
    if n <= 0:
        return np.zeros(1, dtype=np.float32)
    prod = iq[:n] * np.conj(iq[_FFT: _FFT + n])
    cs   = np.concatenate([[0j], np.cumsum(prod)])
    return np.abs(cs[_CP: n + 1] - cs[: n + 1 - _CP]).astype(np.float32)


def _multi_peak_sync(iq: np.ndarray):
    """Return (offset, quality) by averaging the two strongest CP peaks.

    Pure noise gives avg quality ≈ 1.3; a real NRSC-5 signal gives ≥ 8.
    Using two peaks (one symbol apart) filters incidental random peaks.
    """
    metric = _cp_metric(iq)
    if len(metric) < _SYM:
        return 0, 0.0
    offset = int(np.argmax(metric[:_SYM]))
    mean   = float(metric.mean()) + 1e-9
    q1     = float(metric[offset]) / mean
    q2     = float(metric[min(offset + _SYM, len(metric) - 1)]) / mean
    return offset, (q1 + q2) / 2.0


def _sideband_snr_db(iq: np.ndarray, offset: int, sc_outer: int) -> float:
    """Compare power in digital sideband SC to adjacent noise-floor reference.

    Reference band: SC 200..355 (above FM content, below inner NRSC-5 edge).
    Positive values indicate energy above the noise floor at that location.
    """
    avail = (len(iq) - offset) // _SYM
    if avail < 2:
        return -99.0
    end      = offset + avail * _SYM
    payloads = iq[offset:end].reshape(avail, _SYM)[:, _CP:]
    ffts     = np.fft.fft(payloads, axis=1)

    hi      = ffts[:, _HI_A: sc_outer + 1]
    lo      = ffts[:, _FFT - sc_outer: _FFT - _HI_A + 1]
    sb_pwr  = (float(np.mean(np.abs(hi) ** 2)) +
               float(np.mean(np.abs(lo) ** 2))) * 0.5

    ref_hi   = ffts[:, 200: _HI_A]
    ref_lo   = ffts[:, _FFT - _HI_A: _FFT - 200]
    ref_pwr  = (float(np.mean(np.abs(ref_hi) ** 2)) +
                float(np.mean(np.abs(ref_lo) ** 2))) * 0.5 + 1e-20

    return float(10.0 * np.log10(sb_pwr / ref_pwr))


# ── Plugin ────────────────────────────────────────────────────────────────────

class NRSC5TextDecoder(Decoder):
    name            = 'nrsc5_text'
    key             = 'n'
    key_help        = '[/]=shoulder'
    min_sample_rate = _MIN_SR

    def __init__(self):
        self._lib      = None          # libnrsc5 CDLL or None
        self._st       = ct.c_void_p() # nrsc5_t*
        self._cb_ref   = None          # keep CFUNCTYPE alive

        self._buf_lock  = threading.Lock()
        self._iq_buf    = np.zeros(0, dtype=np.complex64)
        self._sr_cached = None
        self._sc_outer  = _HI_B

        self._info_lock = threading.Lock()
        self._info      = {
            'status': 'searching…',
            'name': '', 'slogan': '', 'psd': '',
            'snr': -99.0, 'sync_q': 0.0, 'lib': False,
        }
        self._active = False
        self._event  = threading.Event()
        self._thread = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._active = True
        with self._buf_lock:
            self._iq_buf    = np.zeros(0, dtype=np.complex64)
            self._sr_cached = None

        self._lib = _find_libnrsc5()
        if self._lib:
            ptr = ct.c_void_p()
            if self._lib.nrsc5_open_pipe(ct.byref(ptr)) == 0:
                self._st = ptr
                self._cb_ref = _CB_T(self._nrsc5_callback)
                self._lib.nrsc5_set_callback(self._st, self._cb_ref, None)
                self._thread = threading.Thread(
                    target=lambda: self._lib.nrsc5_start(self._st),
                    name='nrsc5-decode', daemon=True)
                self._thread.start()
                with self._info_lock:
                    self._info['lib']    = True
                    self._info['status'] = 'searching…'
                return
            # open_pipe failed — fall through to analysis path
            self._lib = None

        with self._info_lock:
            self._info['lib']    = False
            self._info['status'] = 'searching…'
        self._thread = threading.Thread(target=self._analysis_loop,
                                        name='nrsc5-analysis', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        if self._lib and self._st:
            self._lib.nrsc5_stop(self._st)
        self._event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._lib and self._st:
            self._lib.nrsc5_close(self._st)
        self._st     = ct.c_void_p()
        self._lib    = None
        self._cb_ref = None
        with self._buf_lock:
            self._iq_buf = np.zeros(0, dtype=np.complex64)

    # ── SDR callback (fast path) ──────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        from scipy.signal import resample as _sp_resample
        sr = int(state.bw_hz)
        if sr != self._sr_cached:
            self._sr_cached = sr
            with self._buf_lock:
                self._iq_buf = np.zeros(0, dtype=np.complex64)

        self._sc_outer = state.nrsc5_sc_outer

        out_len = max(1, round(len(samples) * _SR / sr))
        rs = _sp_resample(samples, out_len).astype(np.complex64)

        if self._lib and self._st:
            # Feed directly to libnrsc5 as interleaved int16 I/Q at ~744 kHz
            i16  = (np.clip(rs.real, -1.0, 1.0) * 32767).astype(np.int16)
            q16  = (np.clip(rs.imag, -1.0, 1.0) * 32767).astype(np.int16)
            cs16 = np.empty(len(rs) * 2, dtype=np.int16)
            cs16[0::2] = i16
            cs16[1::2] = q16
            self._lib.nrsc5_pipe_samples_cs16(
                self._st,
                cs16.ctypes.data_as(ct.POINTER(ct.c_int16)),
                ct.c_uint(len(cs16)))
        else:
            with self._buf_lock:
                self._iq_buf = np.concatenate([self._iq_buf, rs])
                _cap = (_DECODE_SYMS * _SYM + _CP) * 2
                if len(self._iq_buf) > _cap:
                    self._iq_buf = self._iq_buf[-_cap:]
                if len(self._iq_buf) >= _DECODE_SYMS * _SYM + _CP:
                    self._event.set()

        with self._info_lock:
            return dict(self._info)

    # ── libnrsc5 event callback ───────────────────────────────────────────────

    def _nrsc5_callback(self, evt_p, _opaque):
        if not self._active:
            return
        evt = evt_p.contents
        t   = evt.type
        upd = {}
        if t == _EVT_SYNC:
            upd['status'] = 'locked'
        elif t == _EVT_LOST_SYNC:
            upd['status'] = 'searching…'
        elif t == _EVT_MER:
            upd['snr'] = round(float(evt.u.mer.lower), 1)
        elif t == _EVT_SIS:
            raw_name   = evt.u.sis.name
            raw_slogan = evt.u.sis.slogan
            if raw_name:
                upd['name']   = raw_name.decode('utf-8', errors='replace').strip()
            if raw_slogan:
                upd['slogan'] = raw_slogan.decode('utf-8', errors='replace').strip()
        elif t == _EVT_ID3:
            parts = []
            for field in (evt.u.id3.title, evt.u.id3.artist):
                if field:
                    s = field.decode('utf-8', errors='replace').strip()
                    if s:
                        parts.append(s)
            if parts:
                upd['psd'] = ' — '.join(parts)
        if upd:
            with self._info_lock:
                self._info.update(upd)

    # ── signal-analysis fallback loop (no libnrsc5) ───────────────────────────

    def _analysis_loop(self) -> None:
        need     = _DECODE_SYMS * _SYM + _CP
        next_run = 0.0
        while self._active:
            self._event.wait(timeout=0.5)
            self._event.clear()
            if not self._active:
                break
            if time.monotonic() < next_run:
                continue
            with self._buf_lock:
                if len(self._iq_buf) < need:
                    continue
                iq           = self._iq_buf[:need].copy()
                self._iq_buf = self._iq_buf[need // 2:]

            sc_outer        = self._sc_outer
            offset, sync_q  = _multi_peak_sync(iq)
            snr_db          = _sideband_snr_db(iq, offset, sc_outer)

            if sync_q > 8.0 and snr_db > 2.0:
                status = 'signal present'
            elif sync_q > 4.0:
                status = 'syncing'
            else:
                status = 'searching…'

            with self._info_lock:
                self._info.update({
                    'status': status,
                    'sync_q': round(sync_q, 1),
                    'snr':    round(snr_db, 1),
                })
            next_run = time.monotonic() + _DECODE_INTERVAL

    # ── UI hooks ──────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        from core import NRSC5_SC_MIN, NRSC5_SC_MAX, NRSC5_SC_STEP
        if key == ord('['):
            state.nrsc5_sc_outer = max(NRSC5_SC_MIN,
                                       state.nrsc5_sc_outer - NRSC5_SC_STEP)
            return True
        if key == ord(']'):
            state.nrsc5_sc_outer = min(NRSC5_SC_MAX,
                                       state.nrsc5_sc_outer + NRSC5_SC_STEP)
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        sc_w    = _SR / _FFT
        sh_khz  = (state.nrsc5_sc_outer - _HI_A) * sc_w / 1000
        lib_tag = '' if result.get('lib') else '(no lib) '
        return '[HD:{} {:+.0f}dB {:.0f}kHz] {}'.format(
            result.get('status', '?'),
            result.get('snr', -99.0),
            sh_khz,
            lib_tag)

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        if height < 6:
            return

        # Cyan tint on digital sideband columns
        if curses.has_colors():
            cf       = state.center_hz
            sc_w     = _SR / _FFT
            inner_hz = _HI_A * sc_w          # ≈ 129 kHz (inner edge, fixed)
            outer_hz = state.nrsc5_sc_outer * sc_w
            for sign in (-1, 1):
                c_l = int(max(0, (cf + sign * inner_hz - freq_min)
                              / freq_range * plot_w))
                c_r = int(min(plot_w, (cf + sign * outer_hz - freq_min)
                              / freq_range * plot_w))
                if sign == -1:
                    c_l, c_r = c_r, c_l
                if c_r <= c_l:
                    continue
                attr = curses.color_pair(1)
                for r in range(height - 5):
                    try:
                        screen_obj.chgat(r + 1, LABEL_W + c_l, c_r - c_l, attr)
                    except curses.error:
                        pass

        # Bottom 5 body rows: decoded / detected text
        status  = result.get('status',  'searching…')
        snr     = result.get('snr',     -99.0)
        sync_q  = result.get('sync_q',  0.0)
        lib_ok  = result.get('lib',     False)
        station = result.get('name')   or result.get('id')     or '—'
        hint    = '' if lib_ok else '  [brew install nrsc5 to decode]'

        lines = [
            'HD Radio  {}  {:+.0f} dB  sync {:.1f}{}'.format(
                status, snr, sync_q, hint),
            'Station: {}'.format(station),
            'Slogan:  {}'.format(result.get('slogan') or '—'),
            'Now:     {}'.format(result.get('psd')    or '—'),
            '─' * min(plot_w, 60),
        ]

        base = height - 5
        for i, line in enumerate(lines):
            row = base + i
            if row < 0 or row >= height:
                continue
            try:
                screen_obj.addstr(
                    row + 1, LABEL_W,
                    line[:plot_w].ljust(plot_w)[:plot_w],
                    curses.A_BOLD if i < 4 else curses.A_DIM)
            except curses.error:
                pass
