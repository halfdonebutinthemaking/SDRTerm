import curses
import time
import numpy as np
from core import Decoder, AppState, FFT_BINS, LABEL_W, fmt_freq

_HOLD_DEFAULT = 2.0    # seconds between forced marker updates
_HOLD_MIN     = 0.5
_HOLD_MAX     = 10.0
_HOLD_STEP    = 0.5
_SNAP_DB      = 6.0    # immediately snap to a peak this much stronger than held


class PeakMarker(Decoder):
    name            = 'peak_marker'
    key             = 'k'
    key_help        = '-/+=hold  c=center  t=follow'
    min_sample_rate = 250_000

    def __init__(self):
        self._held_hz     = None
        self._held_db     = -999.0
        self._hold_until  = 0.0
        self._hold_s      = _HOLD_DEFAULT
        self._follow      = False
        self._color_ready = False

    def start(self, state: AppState) -> None:
        self._held_hz    = None
        self._held_db    = -999.0
        self._hold_until = 0.0

    def stop(self) -> None:
        self._held_hz = None
        self._held_db = -999.0

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        n = FFT_BINS
        if len(samples) < n:
            return {'peak_hz': self._held_hz, 'peak_db': self._held_db,
                    'hold_s': self._hold_s, 'follow': self._follow}

        # FFT on the most recent FFT_BINS IQ samples.
        # Working from IQ (not from the pre-computed spectrum) keeps the
        # frequency estimate independent of the display pipeline and lets
        # future extensions (carrier tracking, PLL) operate on the raw signal.
        block = samples[-n:]
        spec  = np.fft.fftshift(np.abs(np.fft.fft(block)) ** 2)
        db    = 10.0 * np.log10(spec / (n * n) + 1e-20)

        max_idx = int(np.argmax(db))
        max_db  = float(db[max_idx])

        # Quadratic (parabolic) interpolation gives sub-bin frequency accuracy
        # without a longer FFT.  Uses the three bins around the peak.
        if 0 < max_idx < n - 1:
            y0, y1, y2 = db[max_idx - 1], db[max_idx], db[max_idx + 1]
            denom = y0 - 2.0 * y1 + y2
            delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-10 else 0.0
            frac  = (max_idx + delta) / n   # 0 … 1 across fftshifted spectrum
        else:
            frac = max_idx / n

        # fftshifted: frac=0 → center_hz − bw/2,  frac=1 → center_hz + bw/2
        new_hz = state.center_hz + (frac - 0.5) * state.bw_hz

        now = time.monotonic()
        if (self._held_hz is None
                or now >= self._hold_until
                or max_db >= self._held_db + _SNAP_DB):
            self._held_hz    = new_hz
            self._held_db    = max_db
            self._hold_until = now + self._hold_s
            if self._follow:
                dead_hz = max(1_000.0, state.bw_hz * 0.025)
                if abs(new_hz - state.center_hz) > dead_hz:
                    state.pending_freq = new_hz

        return {'peak_hz': self._held_hz, 'peak_db': self._held_db,
                'hold_s':  self._hold_s, 'follow': self._follow}

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
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        hz     = result.get('peak_hz')
        db     = result.get('peak_db', -999.0)
        hs     = result.get('hold_s', self._hold_s)
        follow = result.get('follow', False)
        suffix = '  FOLLOW' if follow else ''
        if hz is None:
            return '[peak:—  hold:{:.1f}s{}] '.format(hs, suffix)
        return '[peak:{}  {:.0f}dBFS  hold:{:.1f}s{}] '.format(
            fmt_freq(hz), db, hs, suffix)

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        hz = result.get('peak_hz')
        if hz is None or freq_range == 0.0:
            return

        if not self._color_ready:
            try:
                curses.init_pair(2, curses.COLOR_RED, -1)
                self._color_ready = True
            except Exception:
                pass

        col = int((hz - freq_min) / freq_range * plot_w)
        if not (0 <= col < plot_w):
            return

        attr = curses.color_pair(2) | curses.A_BOLD

        # Downward-pointing triangle at the very top of the peak column
        try:
            screen_obj.addstr(1, LABEL_W + col, '▼', attr)
        except curses.error:
            pass

        # Frequency label centred on the marker, clamped to plot area
        label = fmt_freq(hz)
        lx    = LABEL_W + col - len(label) // 2
        lx    = max(LABEL_W, min(LABEL_W + plot_w - len(label), lx))
        try:
            screen_obj.addstr(2, lx, label, attr)
        except curses.error:
            pass
