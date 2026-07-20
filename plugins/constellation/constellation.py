import numpy as np
from math import gcd, log2, cos, sin, radians
from scipy.signal import resample_poly
from core import Decoder, AppState

_DEFAULT_SYMRATE  = 10_500   # sym/s
_SYMRATE_STEP_C   =    500   # coarse step (+/-)
_SYMRATE_STEP_F   =     50   # fine step ([/])
_SYMRATE_MIN      =    500
_SYMRATE_MAX      = 200_000
_TARGET_SPS      =      8   # samples per symbol after resampling
_RRC_ALPHA       =   0.35   # roll-off factor (standard)
_MAX_POINTS      =  4_000   # scatter buffer depth
_PLOT_RANGE      =    1.8   # normalised IQ units shown on each axis
_DENSITY         = ' .:-=+*#'

_M_OPTIONS       = [2, 4, 8, 16]
_REF_ANGLE_STEP  = 5.0        # degrees per keypress
_MOD_NAMES       = {2: 'BPSK', 4: 'QPSK', 8: '8PSK', 16: '16PSK'}


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
    key_help        = '+/-=rate(500)  [/]=rate(50)  m=M  ,/.=rotate  z=diff  r=clear'
    min_sample_rate = 10_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._symrate        = _DEFAULT_SYMRATE
        self._points         = []
        self._n_total        = 0
        self._rrc_cache      = {}   # (symrate, bw_hz) → (up, down, rrc_coeffs)
        self._carrier_phase  = 0.0  # accumulated carrier phase (rad) — stays continuous across chunks
        self._phase_ref      = None # cross-frame 4th-power phase reference
        self._m              = 4    # reference constellation size (QPSK default)
        self._ref_angle      = 0.0  # rotation of reference markers in degrees
        self._differential   = False

    def start(self, state: AppState) -> None:
        self._points     = []
        self._n_total    = 0
        self._carrier_phase = 0.0
        self._phase_ref  = None

    def stop(self) -> None:
        self._points     = []
        self._n_total    = 0
        self._carrier_phase = 0.0
        self._phase_ref  = None

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        peak    = (results or {}).get('peak_marker', {})
        peak_hz = peak.get('peak_hz', state.center_hz)

        # Mix to baseband — accumulate carrier phase so the phasor is continuous even
        # when offset_hz shifts between chunks (alpha-beta tracker moves peak_hz).
        offset_hz           = peak_hz - state.center_hz
        n                   = len(samples)
        t_local             = np.arange(n) / state.bw_hz
        baseband            = (samples * np.exp(
            -1j * (self._carrier_phase + 2 * np.pi * offset_hz * t_local)
        )).astype(np.complex128)
        self._carrier_phase = (
            self._carrier_phase + 2 * np.pi * offset_hz * n / state.bw_hz
        ) % (2 * np.pi)

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

        # Per-frame carrier phase correction — skipped in differential mode because
        # the carrier offset cancels in the symbol-to-symbol phase difference.
        if not self._differential:
            powered     = matched.astype(np.complex128) ** 4
            frame_phase = np.angle(np.mean(powered)) / 4.0
            if self._phase_ref is None:
                correction      = frame_phase
                self._phase_ref = frame_phase
            else:
                candidates = [frame_phase + k * np.pi / 2 for k in range(4)]
                diffs      = [(c - self._phase_ref + np.pi) % (2 * np.pi) - np.pi
                              for c in candidates]
                correction      = candidates[int(np.argmin(np.abs(diffs)))]
                self._phase_ref = np.angle(
                    0.85 * np.exp(1j * self._phase_ref) + 0.15 * np.exp(1j * correction)
                )
            matched = (matched * np.exp(-1j * correction)).astype(np.complex64)

        # Sample at symbol centres, accounting for RRC filter delay
        delay  = len(rrc_taps) // 2
        offset = delay % _TARGET_SPS + _TARGET_SPS // 2
        syms   = matched[offset::_TARGET_SPS]

        if len(syms) == 0:
            return {'symrate': self._symrate, 'n_points': len(self._points),
                    'differential': self._differential}

        # Differential mode: plot phase change between consecutive symbols
        if self._differential:
            if len(syms) < 2:
                return {'symrate': self._symrate, 'n_points': len(self._points),
                        'differential': True}
            pts = syms[1:] * np.conj(syms[:-1])
        else:
            pts = syms

        # Normalise to median magnitude ≈ 1
        med = float(np.median(np.abs(pts)))
        if med > 1e-6:
            pts = pts / med

        self._points.extend(pts.tolist())
        self._n_total += len(pts)
        if len(self._points) > _MAX_POINTS:
            self._points = self._points[-_MAX_POINTS:]

        return {
            'symrate':      self._symrate,
            'n_points':     len(self._points),
            'n_total':      self._n_total,
            'm':            self._m,
            'differential': self._differential,
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key in (ord('+'), ord('=')):
            self._symrate    = min(_SYMRATE_MAX, self._symrate + _SYMRATE_STEP_C)
            self._points     = []
            self._phase_ref  = None
            self._carrier_phase = 0.0
            return True
        if key == ord('-'):
            self._symrate    = max(_SYMRATE_MIN, self._symrate - _SYMRATE_STEP_C)
            self._points     = []
            self._phase_ref  = None
            self._carrier_phase = 0.0
            return True
        if key == ord(']'):
            self._symrate    = min(_SYMRATE_MAX, self._symrate + _SYMRATE_STEP_F)
            self._points     = []
            self._phase_ref  = None
            self._carrier_phase = 0.0
            return True
        if key == ord('['):
            self._symrate    = max(_SYMRATE_MIN, self._symrate - _SYMRATE_STEP_F)
            self._points     = []
            self._phase_ref  = None
            self._carrier_phase = 0.0
            return True
        if key == ord('r'):
            self._points     = []
            self._n_total    = 0
            self._phase_ref  = None
            self._carrier_phase = 0.0
            return True
        if key == ord('m'):
            idx        = _M_OPTIONS.index(self._m) if self._m in _M_OPTIONS else 0
            self._m    = _M_OPTIONS[(idx + 1) % len(_M_OPTIONS)]
            return True
        if key == ord(','):
            self._ref_angle = (self._ref_angle - _REF_ANGLE_STEP) % 360
            return True
        if key == ord('.'):
            self._ref_angle = (self._ref_angle + _REF_ANGLE_STEP) % 360
            return True
        if key == ord('z'):
            self._differential  = not self._differential
            self._points        = []
            self._phase_ref     = None
            self._carrier_phase = 0.0
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        symrate = result.get('symrate', _DEFAULT_SYMRATE)
        m       = result.get('m', self._m)
        return '[CONST {:.1f}k {} {:.0f}k] '.format(
            symrate / 1e3, _MOD_NAMES.get(m, '{}PSK'.format(m)),
            symrate * log2(m) / 1e3)

    def save_state(self) -> dict:
        return {'symrate': self._symrate, 'm': self._m, 'ref_angle': self._ref_angle,
                'differential': self._differential}

    def load_state(self, d: dict) -> None:
        self._symrate      = int(d.get('symrate', _DEFAULT_SYMRATE))
        self._m            = int(d.get('m', 4))
        self._ref_angle    = float(d.get('ref_angle', 0.0))
        self._differential = bool(d.get('differential', False))

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        symrate = result.get('symrate', _DEFAULT_SYMRATE)
        n_pts   = result.get('n_points', 0)
        m       = result.get('m', self._m)
        bitrate = int(symrate * log2(m))

        # ── Symbol decisions + EVM ─────────────────────────────────────────
        pts         = np.array(self._points, dtype=np.complex64) if self._points else None
        evm_pct     = None
        snr_db      = None
        assignments = None

        if pts is not None and m >= 2:
            angles      = np.radians(self._ref_angle + np.arange(m) * 360.0 / m)
            refs        = np.exp(1j * angles).astype(np.complex64)
            dists       = np.abs(pts[:, None] - refs[None, :])   # (N, M)
            assignments = np.argmin(dists, axis=1)                # (N,)
            errors      = pts - refs[assignments]
            evm_pct     = float(np.sqrt(np.mean(np.abs(errors) ** 2))) * 100
            snr_db      = -20.0 * np.log10(max(evm_pct / 100.0, 1e-6))

        # ── Header ─────────────────────────────────────────────────────────
        # EVM quality colour: green < 10%, yellow 10-25%, red > 25%
        evm_str  = '  EVM {:.1f}%  ~{:.0f}dB'.format(evm_pct, snr_db) \
                   if evm_pct is not None else ''
        try:
            curses.init_pair(13, curses.COLOR_YELLOW, -1)
            if evm_pct is None:
                evm_attr = curses.A_BOLD
            elif evm_pct < 10.0:
                evm_attr = curses.color_pair(3) | curses.A_BOLD   # green
            elif evm_pct < 25.0:
                evm_attr = curses.color_pair(13) | curses.A_BOLD  # yellow
            else:
                evm_attr = curses.color_pair(2) | curses.A_BOLD   # red
        except Exception:
            evm_attr = curses.A_BOLD

        diff_str = '  [DIFF]' if result.get('differential') else ''
        h_left  = 'Constellation  {:,} sym/s  {}  {:,} bit/s{}'.format(
            symrate, _MOD_NAMES.get(m, '{}PSK'.format(m)), bitrate, diff_str)
        h_right = '   {}/{} pts'.format(n_pts, _MAX_POINTS)
        h_total = h_left + evm_str + h_right
        hx      = max(0, (cols - len(h_total)) // 2)
        try:
            screen_obj.addstr(1, hx, h_left, curses.A_BOLD)
            if evm_str:
                screen_obj.addstr(1, hx + len(h_left), evm_str, evm_attr)
            screen_obj.addstr(1, hx + len(h_left) + len(evm_str), h_right, curses.A_BOLD)
        except curses.error:
            pass

        # ── Plot area ──────────────────────────────────────────────────────
        plot_h = rows - 5
        plot_w = min(cols - 4, plot_h * 2)
        plot_y = 2
        plot_x = (cols - plot_w) // 2

        if plot_h < 6 or plot_w < 12:
            return

        # ── Density grid + cluster assignment grid ─────────────────────────
        grid         = np.zeros((plot_h, plot_w), dtype=np.int32)
        cluster_grid = np.full((plot_h, plot_w), -1, dtype=np.int32)

        if pts is not None:
            col_f = (pts.real / _PLOT_RANGE + 1.0) / 2.0 * plot_w
            row_f = (1.0 - pts.imag / _PLOT_RANGE) / 2.0 * plot_h
            ci    = col_f.astype(np.int32)
            ri    = row_f.astype(np.int32)
            mask  = (ci >= 0) & (ci < plot_w) & (ri >= 0) & (ri < plot_h)
            for r, c in zip(ri[mask], ci[mask]):
                grid[r, c] += 1
            if assignments is not None:
                cluster_grid[ri[mask], ci[mask]] = assignments[mask]

        cx, cy = plot_w // 2, plot_h // 2

        # ── First pass: background + axes ─────────────────────────────────
        for r in range(plot_h):
            row = []
            for c in range(plot_w):
                d      = grid[r, c]
                on_hax = (r == cy)
                on_vax = (c == cx)
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

        # ── Second pass: recolour occupied cells by cluster ────────────────
        _CLUSTER_FG = [
            curses.COLOR_CYAN, curses.COLOR_YELLOW,
            curses.COLOR_MAGENTA, curses.COLOR_GREEN,
            curses.COLOR_WHITE, curses.COLOR_BLUE,
            curses.COLOR_CYAN, curses.COLOR_YELLOW,
        ]
        _BASE = 5
        for k in range(min(m, len(_CLUSTER_FG))):
            try:
                curses.init_pair(_BASE + k, _CLUSTER_FG[k], -1)
            except Exception:
                pass
        for r in range(plot_h):
            for c in range(plot_w):
                cl = cluster_grid[r, c]
                if grid[r, c] > 0 and cl >= 0:
                    ch   = _DENSITY[min(grid[r, c], len(_DENSITY) - 1)]
                    attr = curses.color_pair(_BASE + cl % len(_CLUSTER_FG)) | curses.A_BOLD
                    try:
                        screen_obj.addstr(plot_y + r, plot_x + c, ch, attr)
                    except curses.error:
                        pass

        # ── Reference markers ──────────────────────────────────────────────
        try:
            curses.init_pair(4, curses.COLOR_RED, -1)
            ref_attr = curses.color_pair(4) | curses.A_BOLD
        except Exception:
            ref_attr = curses.A_BOLD
        for k in range(self._m):
            angle = radians(self._ref_angle + k * 360.0 / self._m)
            rx    = cos(angle)
            ry    = sin(angle)
            rc    = int((rx / _PLOT_RANGE + 1.0) / 2.0 * plot_w)
            rr    = int((1.0 - ry / _PLOT_RANGE) / 2.0 * plot_h)
            if 0 <= rc < plot_w and 0 <= rr < plot_h:
                try:
                    screen_obj.addstr(plot_y + rr, plot_x + rc, 'o', ref_attr)
                except curses.error:
                    pass

        # ── Axis labels ────────────────────────────────────────────────────
        for label, y, x in [
            ('+Q', plot_y,              plot_x + cx - 1),
            ('-Q', plot_y + plot_h - 1, plot_x + cx - 1),
            ('-I', plot_y + cy,         plot_x),
            ('+I', plot_y + cy,         plot_x + plot_w - 2),
        ]:
            try:
                screen_obj.addstr(y, x, label)
            except curses.error:
                pass

        # ── Footer ─────────────────────────────────────────────────────────
        footer = ('+/- coarse ±{:,}   [/] fine ±{:,}   {:,} sym/s   '
                  'm={} ({})   ,/.=rotate {:.0f}°   z={}   r=clear').format(
                  _SYMRATE_STEP_C, _SYMRATE_STEP_F, symrate,
                  m, _MOD_NAMES.get(m, '{}PSK'.format(m)), self._ref_angle,
                  'DIFF' if result.get('differential') else 'abs')
        try:
            screen_obj.addstr(rows - 2, 2, footer[:cols - 4], curses.A_DIM)
        except curses.error:
            pass
