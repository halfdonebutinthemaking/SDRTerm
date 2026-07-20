import curses
import time
import numpy as np
from core import Decoder, AppState, FFT_BINS, LABEL_W, fmt_freq

_HOLD_DEFAULT = 2.0    # seconds between forced marker updates
_HOLD_MIN     = 0.5
_HOLD_MAX     = 10.0
_HOLD_STEP    = 0.5
_SNAP_DB      = 6.0    # immediately snap to a peak this much stronger than held

_TRACK_ALPHA  = 0.3        # frequency correction gain
_TRACK_BETA   = 0.01       # rate correction gain (Hz/s per Hz error per second)
_TRACK_WINDOW = 10_000.0   # Hz — search radius around alpha-beta prediction


class PeakMarker(Decoder):
    name            = 'peak_marker'
    key             = 'k'
    key_help        = '-/+=hold  c=center  t=follow  r=track'
    min_sample_rate = 250_000
    realtime        = False

    def __init__(self):
        self._held_hz     = None
        self._held_db     = -999.0
        self._hold_until  = 0.0
        self._hold_s      = _HOLD_DEFAULT
        self._follow      = False
        self._tracking    = False
        self._freq_est    = None   # alpha-beta frequency estimate (Hz)
        self._rate_est    = 0.0    # Hz/s — estimated drift rate
        self._last_t      = 0.0
        self._color_ready = False

    def start(self, state: AppState) -> None:
        self._held_hz    = None
        self._held_db    = -999.0
        self._hold_until = 0.0
        self._freq_est   = None
        self._rate_est   = 0.0
        self._last_t     = time.monotonic()

    def stop(self) -> None:
        self._held_hz  = None
        self._held_db  = -999.0
        self._freq_est = None
        self._rate_est = 0.0

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        n    = FFT_BINS
        base = {'peak_hz':   self._held_hz,  'peak_db':   self._held_db,
                'hold_s':    self._hold_s,    'follow':    self._follow,
                'tracking':  self._tracking,  'rate_hz_s': self._rate_est}
        if len(samples) < n:
            return base

        block = samples[-n:]
        spec  = np.fft.fftshift(np.abs(np.fft.fft(block)) ** 2)
        db    = 10.0 * np.log10(spec / (n * n) + 1e-20)

        # For file replay use the recorded centre as the FFT frequency reference.
        ref_hz = getattr(sdr, '_file_center_hz', None) or state.center_hz

        now = time.monotonic()
        dt  = max(now - self._last_t, 1e-3)

        if self._tracking and self._freq_est is not None:
            # ── alpha-beta tracking path ──────────────────────────────────────
            pred_hz = self._freq_est + self._rate_est * dt

            # Build frequency axis; restrict search to a window around prediction
            freqs = np.linspace(ref_hz - state.bw_hz / 2,
                                ref_hz + state.bw_hz / 2, n)
            mask  = np.abs(freqs - pred_hz) < _TRACK_WINDOW
            if not mask.any():
                mask = np.ones(n, dtype=bool)   # signal left BW — fall back to full band

            search_db = np.where(mask, db, -999.0)
            max_idx   = int(np.argmax(search_db))
            max_db    = float(db[max_idx])

            # Parabolic interpolation for sub-bin accuracy
            if 0 < max_idx < n - 1:
                y0, y1, y2 = db[max_idx - 1], db[max_idx], db[max_idx + 1]
                denom = y0 - 2.0 * y1 + y2
                delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-10 else 0.0
                frac  = (max_idx + delta) / n
            else:
                frac = max_idx / n
            measured = ref_hz + (frac - 0.5) * state.bw_hz

            # Alpha-beta update
            err             = measured - pred_hz
            self._freq_est  = pred_hz + _TRACK_ALPHA * err
            self._rate_est += _TRACK_BETA * err / dt
            self._last_t    = now

            self._held_hz = self._freq_est
            self._held_db = max_db

            if self._follow and abs(self._freq_est - state.center_hz) > 500.0:
                state.pending_freq = self._freq_est

        else:
            # ── original hold-off path ────────────────────────────────────────
            max_idx = int(np.argmax(db))
            max_db  = float(db[max_idx])

            if 0 < max_idx < n - 1:
                y0, y1, y2 = db[max_idx - 1], db[max_idx], db[max_idx + 1]
                denom = y0 - 2.0 * y1 + y2
                delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-10 else 0.0
                frac  = (max_idx + delta) / n
            else:
                frac = max_idx / n
            new_hz = ref_hz + (frac - 0.5) * state.bw_hz

            if (self._held_hz is None
                    or now >= self._hold_until
                    or max_db >= self._held_db + _SNAP_DB):
                self._held_hz    = new_hz
                self._held_db    = max_db
                self._hold_until = now + self._hold_s
                if self._follow and abs(new_hz - state.center_hz) > 1_000.0:
                    state.pending_freq = new_hz

        return {'peak_hz':   self._held_hz,
                'peak_db':   self._held_db,
                'hold_s':    self._hold_s,
                'follow':    self._follow,
                'tracking':  self._tracking,
                'rate_hz_s': self._rate_est}

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('-'):
            self._hold_s = max(_HOLD_MIN, round(self._hold_s - _HOLD_STEP, 1))
            return True
        if key in (ord('+'), ord('=')):
            self._hold_s = min(_HOLD_MAX, round(self._hold_s + _HOLD_STEP, 1))
            return True
        if key == ord('c') and self._held_hz is not None:
            state.pending_freq = self._held_hz
            return True
        if key == ord('t'):
            self._follow = not self._follow
            return True
        if key == ord('r'):
            self._tracking = not self._tracking
            if self._tracking:
                # Seed from last known peak; assume stationary until proven otherwise
                self._freq_est = self._held_hz
                self._rate_est = 0.0
                self._last_t   = time.monotonic()
            else:
                self._freq_est = None
                self._rate_est = 0.0
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        hz       = result.get('peak_hz')
        db       = result.get('peak_db', -999.0)
        hs       = result.get('hold_s', self._hold_s)
        follow   = result.get('follow', False)
        tracking = result.get('tracking', False)
        rate     = result.get('rate_hz_s', 0.0)

        flags = ''
        if follow:   flags += '  FOLLOW'
        if tracking: flags += '  TRACK {:+.0f}Hz/s'.format(rate)

        if hz is None:
            return '[peak:—  hold:{:.1f}s{}] '.format(hs, flags)
        if tracking:
            return '[peak:{}  {:.0f}dBFS{}] '.format(fmt_freq(hz), db, flags)
        return '[peak:{}  {:.0f}dBFS  hold:{:.1f}s{}] '.format(
            fmt_freq(hz), db, hs, flags)

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        hz = result.get('peak_hz')
        if hz is None or freq_range == 0.0:
            return

        if not self._color_ready:
            try:
                curses.init_pair(2, curses.COLOR_RED,   -1)
                curses.init_pair(3, curses.COLOR_GREEN, -1)
                self._color_ready = True
            except Exception:
                pass

        col = int((hz - freq_min) / freq_range * plot_w)
        if not (0 <= col < plot_w):
            return

        # Green marker when tracking, red when using plain hold-off
        pair = 3 if result.get('tracking') else 2
        attr = curses.color_pair(pair) | curses.A_BOLD

        try:
            screen_obj.addstr(1, LABEL_W + col, '▼', attr)
        except curses.error:
            pass

        label = fmt_freq(hz)
        lx    = LABEL_W + col - len(label) // 2
        lx    = max(LABEL_W, min(LABEL_W + plot_w - len(label), lx))
        try:
            screen_obj.addstr(2, lx, label, attr)
        except curses.error:
            pass
