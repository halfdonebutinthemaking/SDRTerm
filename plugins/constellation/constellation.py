import numpy as np
from math import gcd
from scipy.signal import resample_poly
from core import Decoder, AppState

_DEFAULT_SYMRATE = 10_500   # sym/s
_SYMRATE_STEP    =    500   # per keypress
_SYMRATE_MIN     =    500
_SYMRATE_MAX     = 200_000
_TARGET_SPS      =      8   # samples per symbol after resampling
_RRC_ALPHA       =   0.35   # roll-off factor (standard)
_MAX_POINTS      =  4_000   # scatter buffer depth
_PLOT_RANGE      =    1.8   # normalised IQ units shown on each axis
_DENSITY         = ' .:-=+*#'


def _rrc(n_taps: int, alpha: float, sps: int) -> np.ndarray:
    """Root-raised cosine FIR coefficients."""
    t = (np.arange(n_taps) - n_taps // 2) / sps
    h = np.zeros(n_taps)
    for i, ti in enumerate(t):
        if ti == 0:
            h[i] = 1.0 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * ti) - 1.0) < 1e-6:
            h[i] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha))
            )
        else:
            h[i] = (
                np.sin(np.pi * ti * (1 - alpha))
                + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha))
            ) / (np.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float32)


class ConstellationDecoder(Decoder):
    name            = 'constellation'
    key             = 'c'
    key_help        = '+/-=sym-rate  r=clear'
    min_sample_rate = 10_000
    realtime        = False
    bg_queue_depth  = 2

    def __init__(self):
        self._symrate    = _DEFAULT_SYMRATE
        self._points     = []
        self._n_total    = 0
        self._rrc_cache  = {}   # (symrate, bw_hz) → (up, down, rrc_coeffs)

    def start(self, state: AppState) -> None:
        self._points  = []
        self._n_total = 0

    def stop(self) -> None:
        self._points  = []
        self._n_total = 0

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        peak    = (results or {}).get('peak_marker', {})
        peak_hz = peak.get('peak_hz')
        if peak_hz is None:
            return {'symrate': self._symrate, 'n_points': len(self._points)}

        # Mix to baseband
        offset_hz = peak_hz - state.center_hz
        t         = np.arange(len(samples)) / state.bw_hz
        baseband  = (samples * np.exp(-2j * np.pi * offset_hz * t)).astype(np.complex128)

        # Resample to TARGET_SPS × symrate — cache up/down/filter per (symrate, bw_hz)
        cache_key = (self._symrate, int(state.bw_hz))
        if cache_key not in self._rrc_cache:
            target_sr = self._symrate * _TARGET_SPS
            src_sr    = int(state.bw_hz)
            g         = gcd(target_sr, src_sr)
            up        = target_sr // g
            down      = src_sr    // g
            # Guard against absurd up/down — cap and accept slight frequency error
            while up > 500 or down > 500:
                up = max(1, up // 2)
                down = max(1, down // 2)
            rrc_taps  = _rrc(8 * _TARGET_SPS + 1, _RRC_ALPHA, _TARGET_SPS)
            self._rrc_cache[cache_key] = (up, down, rrc_taps)
            if len(self._rrc_cache) > 32:
                self._rrc_cache.pop(next(iter(self._rrc_cache)))

        up, down, rrc_taps = self._rrc_cache[cache_key]

        try:
            resampled = resample_poly(baseband, up, down)
        except Exception:
            return {'symrate': self._symrate, 'n_points': len(self._points)}

        if len(resampled) < _TARGET_SPS * 4:
            return {'symrate': self._symrate, 'n_points': len(self._points)}

        # Matched RRC filter
        matched = np.convolve(resampled, rrc_taps, mode='same').astype(np.complex64)

        # Batch carrier-phase correction via 4th-power estimate (works for QPSK)
        powered = matched.astype(np.complex128) ** 4
        phase   = np.angle(np.mean(powered)) / 4.0
        matched = (matched * np.exp(-1j * phase)).astype(np.complex64)

        # Sample at symbol centres, accounting for RRC filter delay
        delay  = len(rrc_taps) // 2
        offset = delay % _TARGET_SPS + _TARGET_SPS // 2
        syms   = matched[offset::_TARGET_SPS]

        if len(syms) == 0:
            return {'symrate': self._symrate, 'n_points': len(self._points)}

        # Normalise to median magnitude ≈ 1
        med = float(np.median(np.abs(syms)))
        if med > 1e-6:
            syms = syms / med

        self._points.extend(syms.tolist())
        self._n_total += len(syms)
        if len(self._points) > _MAX_POINTS:
            self._points = self._points[-_MAX_POINTS:]

        return {
            'symrate':  self._symrate,
            'n_points': len(self._points),
            'n_total':  self._n_total,
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key in (ord('+'), ord('=')):
            self._symrate = min(_SYMRATE_MAX, self._symrate + _SYMRATE_STEP)
            self._points  = []
            return True
        if key == ord('-'):
            self._symrate = max(_SYMRATE_MIN, self._symrate - _SYMRATE_STEP)
            self._points  = []
            return True
        if key == ord('r'):
            self._points  = []
            self._n_total = 0
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        return '[CONST {:.1f}k] '.format(result.get('symrate', _DEFAULT_SYMRATE) / 1e3)

    def save_state(self) -> dict:
        return {'symrate': self._symrate}

    def load_state(self, d: dict) -> None:
        self._symrate = int(d.get('symrate', _DEFAULT_SYMRATE))

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        symrate = result.get('symrate', _DEFAULT_SYMRATE)
        n_pts   = result.get('n_points', 0)

        header = 'Constellation  {:,} sym/s   {} / {} pts'.format(
            symrate, n_pts, _MAX_POINTS)
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header, curses.A_BOLD)
        except curses.error:
            pass

        # Plot area: square-ish, centred, 2:1 char aspect ratio
        plot_h = rows - 5
        plot_w = min(cols - 4, plot_h * 2)
        plot_y = 2
        plot_x = (cols - plot_w) // 2

        if plot_h < 6 or plot_w < 12:
            return

        # Build density grid
        grid = np.zeros((plot_h, plot_w), dtype=np.int32)
        if self._points:
            pts   = np.array(self._points, dtype=np.complex64)
            col_f = (pts.real / _PLOT_RANGE + 1.0) / 2.0 * plot_w
            row_f = (1.0 - pts.imag / _PLOT_RANGE) / 2.0 * plot_h
            ci    = col_f.astype(np.int32)
            ri    = row_f.astype(np.int32)
            mask  = (ci >= 0) & (ci < plot_w) & (ri >= 0) & (ri < plot_h)
            for r, c in zip(ri[mask], ci[mask]):
                grid[r, c] += 1

        cx, cy = plot_w // 2, plot_h // 2

        for r in range(plot_h):
            row = []
            for c in range(plot_w):
                d       = grid[r, c]
                on_hax  = (r == cy)
                on_vax  = (c == cx)
                if d > 0:
                    row.append(_DENSITY[min(d, len(_DENSITY) - 1)])
                elif on_hax and on_vax:
                    row.append('+')
                elif on_hax:
                    row.append('─')
                elif on_vax:
                    row.append('│')
                else:
                    row.append(' ')
            try:
                screen_obj.addstr(plot_y + r, plot_x, ''.join(row))
            except curses.error:
                pass

        # Axis labels
        for label, y, x in [
            ('+Q', plot_y,          plot_x + cx - 1),
            ('-Q', plot_y + plot_h - 1, plot_x + cx - 1),
            ('-I', plot_y + cy,     plot_x),
            ('+I', plot_y + cy,     plot_x + plot_w - 2),
        ]:
            try:
                screen_obj.addstr(y, x, label)
            except curses.error:
                pass

        footer = ('+/- sym rate ({:,} sym/s, step {:,})   '
                  'r=clear   RRC α={:.2f}').format(
                  symrate, _SYMRATE_STEP, _RRC_ALPHA)
        try:
            screen_obj.addstr(rows - 2, 2, footer[:cols - 4], curses.A_DIM)
        except curses.error:
            pass
