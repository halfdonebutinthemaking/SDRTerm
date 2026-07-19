import time
import numpy as np
import curses
from core import Decoder, AppState, fmt_freq, parse_freq

_SETTLE_S      = 0.15   # seconds to discard after each retune
_DWELL_DEFAULT = 0.5    # seconds to accumulate FFT per step
_DWELL_MIN     = 0.2
_DWELL_MAX     = 5.0
_DWELL_STEP    = 0.1
_SNR_DEFAULT   = 10.0
_SNR_MIN       = 3.0
_SNR_MAX       = 30.0
_SNR_STEP      = 0.5
_STEP_OVERLAP  = 0.85   # step = BW * this (15 % overlap kept in each direction)
_DECAY_SWEEPS  = 2      # sweeps before a signal is removed if not re-detected
_DECAY_TIME    = 10.0   # minimum age before a signal is eligible for decay (s)
_AGE_DIM_S     = 30.0   # signals older than this are shown dimmed


class RangeScan(Decoder):
    name            = 'range-scan'
    key             = 'e'
    key_help        = '[/]=dwell  -/+=snr  m=min  n=max  ret=tune  s=sort'
    min_sample_rate = 250_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._scanning      = False
        self._min_freq      = 0.0
        self._max_freq      = 0.0
        self._step_size     = 0
        self._step_total    = 0
        self._step_idx      = 0
        self._step_hz       = 0.0    # centre of the current scan step
        self._step_issued_t = 0.0    # monotonic time when pending_freq was queued
        self._fft_acc       = None   # accumulated power spectrum (linear)
        self._fft_count     = 0
        self._dwell_start   = 0.0
        self._signals       = {}     # quantised_hz → dict
        self._sweep_count   = 0      # completed sweeps since last detection
        self._cursor        = 0
        self._sort_snr      = False  # False=sort by freq, True=sort by SNR
        self._dwell_s       = _DWELL_DEFAULT
        self._min_snr       = _SNR_DEFAULT
        self._saved_center  = 0.0

    def start(self, state: AppState) -> None:
        self._dwell_s  = _DWELL_DEFAULT
        self._min_snr  = _SNR_DEFAULT
        self._scanning = False
        self._signals  = {}
        self._cursor   = 0

    def stop(self) -> None:
        self._scanning = False

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None):
        now = time.monotonic()
        result = {
            'scanning':    self._scanning,
            'step_idx':    self._step_idx,
            'step_total':  self._step_total,
            'step_hz':     self._step_hz,
            'signals':     dict(self._signals),
            'cursor':      self._cursor,
            'sort_snr':    self._sort_snr,
            'dwell_s':     self._dwell_s,
            'min_snr':     self._min_snr,
            'min_freq':    self._min_freq,
            'max_freq':    self._max_freq,
        }
        if not self._scanning:
            return result

        # Discard samples during settle window after a retune
        if now - self._step_issued_t < _SETTLE_S:
            return result

        # Accumulate FFT power
        n = len(samples)
        if n >= 256:
            window = np.hanning(n)
            fft    = np.fft.fftshift(np.fft.fft(samples * window, n))
            power  = np.abs(fft) ** 2
            if self._fft_acc is None or len(self._fft_acc) != n:
                self._fft_acc   = power
                self._fft_count = 1
            else:
                self._fft_acc  += power
                self._fft_count += 1

        # Check if dwell window has elapsed
        if (self._fft_count > 0 and
                now - self._dwell_start >= self._dwell_s):
            self._extract_peaks(sdr)
            self._advance_step(state, sdr)

        return result

    def _extract_peaks(self, sdr) -> None:
        if self._fft_acc is None or self._fft_count == 0:
            return

        avg_pwr  = self._fft_acc / self._fft_count
        avg_db   = 10.0 * np.log10(np.maximum(avg_pwr, 1e-20))
        n_bins   = len(avg_db)

        # Noise floor: median of the lower 70 % of bins by power level
        sorted_db  = np.sort(avg_db)
        noise_floor = float(np.median(sorted_db[:max(1, int(n_bins * 0.70))]))

        snr_db = avg_db - noise_floor

        # Frequency axis for this step
        if sdr is not None:
            sr = sdr.sample_rate
        else:
            sr = 2_400_000
        step_half = self._step_size / 2.0
        freqs = self._step_hz + np.linspace(-sr / 2, sr / 2, n_bins, endpoint=False)

        # Only count peaks in the non-overlap zone (centre ± step_size/2)
        freq_mask = np.abs(freqs - self._step_hz) <= step_half

        # Find connected groups of bins above threshold within zone
        above = (snr_db >= self._min_snr) & freq_mask
        in_group = False
        group_snr   = []
        group_freqs = []
        now = time.monotonic()

        for i, flag in enumerate(above):
            if flag:
                group_snr.append(float(snr_db[i]))
                group_freqs.append(float(freqs[i]))
                in_group = True
            elif in_group:
                # Emit peak at bin with max SNR in this group
                peak_idx  = int(np.argmax(group_snr))
                peak_freq = group_freqs[peak_idx]
                peak_snr  = group_snr[peak_idx]
                bw_est    = (freqs[1] - freqs[0]) * len(group_freqs)
                key       = int(round(peak_freq / 1000.0)) * 1000
                if key in self._signals:
                    self._signals[key]['snr']       = peak_snr
                    self._signals[key]['bw_hz']     = bw_est
                    self._signals[key]['last_seen']  = now
                    self._signals[key]['sweep_seen'] = self._sweep_count
                else:
                    self._signals[key] = {
                        'freq':       peak_freq,
                        'snr':        peak_snr,
                        'bw_hz':      bw_est,
                        'last_seen':  now,
                        'sweep_seen': self._sweep_count,
                        'first_seen': now,
                    }
                group_snr   = []
                group_freqs = []
                in_group    = False

        # Handle group open at end of array
        if in_group and group_freqs:
            peak_idx  = int(np.argmax(group_snr))
            peak_freq = group_freqs[peak_idx]
            peak_snr  = group_snr[peak_idx]
            bw_est    = (freqs[1] - freqs[0]) * len(group_freqs)
            key       = int(round(peak_freq / 1000.0)) * 1000
            if key in self._signals:
                self._signals[key]['snr']       = peak_snr
                self._signals[key]['bw_hz']     = bw_est
                self._signals[key]['last_seen']  = now
                self._signals[key]['sweep_seen'] = self._sweep_count
            else:
                self._signals[key] = {
                    'freq':       peak_freq,
                    'snr':        peak_snr,
                    'bw_hz':      bw_est,
                    'last_seen':  now,
                    'sweep_seen': self._sweep_count,
                    'first_seen': now,
                }

        # Decay signals not seen in recent sweeps
        stale = [k for k, v in self._signals.items()
                 if (now - v['first_seen'] > _DECAY_TIME and
                     self._sweep_count - v['sweep_seen'] > _DECAY_SWEEPS)]
        for k in stale:
            del self._signals[k]

        self._fft_acc   = None
        self._fft_count = 0

    def _advance_step(self, state: AppState, sdr) -> None:
        self._step_idx += 1
        if self._step_idx >= self._step_total:
            self._step_idx  = 0
            self._sweep_count += 1

        self._step_hz       = self._min_freq + self._step_idx * self._step_size + self._step_size / 2.0
        state.pending_freq  = self._step_hz
        self._step_issued_t = time.monotonic()
        self._dwell_start   = self._step_issued_t + _SETTLE_S
        self._fft_acc       = None
        self._fft_count     = 0

    def _start_scan(self, state: AppState, sdr) -> None:
        bw = state.bw_hz

        # Derive scan range
        if state.scan_freq_min > 0:
            f_min = state.scan_freq_min
        elif hasattr(sdr, 'freq_min') and sdr.freq_min > 0:
            f_min = sdr.freq_min
        else:
            f_min = state.center_hz - 20 * bw

        if state.scan_freq_max > 0:
            f_max = state.scan_freq_max
        elif hasattr(sdr, 'freq_max') and sdr.freq_max > 0:
            f_max = sdr.freq_max
        else:
            f_max = state.center_hz + 20 * bw

        if f_max <= f_min:
            f_max = f_min + bw

        self._min_freq   = f_min
        self._max_freq   = f_max
        self._step_size  = int(bw * _STEP_OVERLAP)
        self._step_total = max(1, int(np.ceil((f_max - f_min) / self._step_size)))
        self._step_idx   = 0
        self._sweep_count = 0
        self._saved_center = state.center_hz

        now = time.monotonic()
        self._step_hz       = f_min + self._step_size / 2.0
        state.pending_freq  = self._step_hz
        self._step_issued_t = now
        self._dwell_start   = now + _SETTLE_S
        self._fft_acc       = None
        self._fft_count     = 0
        self._scanning      = True

    def _stop_scan(self, state: AppState) -> None:
        self._scanning     = False
        state.pending_freq = self._saved_center

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('e'):
            if self._scanning:
                self._stop_scan(state)
            else:
                self._signals = {}
                self._cursor  = 0
                self._start_scan(state, sdr)
            return True

        if key in (curses.KEY_UP, ord('k')):
            self._cursor = max(0, self._cursor - 1)
            return True

        if key in (curses.KEY_DOWN, ord('j')):
            sig_count = len(self._signals)
            self._cursor = min(max(0, sig_count - 1), self._cursor + 1)
            return True

        if key in (10, 13, curses.KEY_ENTER):
            sigs = self._sorted_signals()
            if 0 <= self._cursor < len(sigs):
                freq = sigs[self._cursor]['freq']
                self._stop_scan(state)
                state.pending_freq = freq
            return True

        if key == ord('['):
            self._dwell_s = max(_DWELL_MIN, self._dwell_s - _DWELL_STEP)
            return True

        if key == ord(']'):
            self._dwell_s = min(_DWELL_MAX, self._dwell_s + _DWELL_STEP)
            return True

        if key in (ord('-'), ord('_')):
            self._min_snr = max(_SNR_MIN, self._min_snr - _SNR_STEP)
            return True

        if key in (ord('+'), ord('=')):
            self._min_snr = min(_SNR_MAX, self._min_snr + _SNR_STEP)
            return True

        if key == ord('s'):
            self._sort_snr = not self._sort_snr
            return True

        if key == ord('m'):
            state.path_input        = ''
            state.path_input_target = 'range-scan-min'
            state.path_input_label  = 'Scan min freq'
            return True

        if key == ord('n'):
            state.path_input        = ''
            state.path_input_target = 'range-scan-max'
            state.path_input_label  = 'Scan max freq'
            return True

        return False

    def status_text(self, state: AppState, result: dict):
        if result.get('scanning'):
            return '[scanning {}/{}] '.format(
                result.get('step_idx', 0) + 1,
                result.get('step_total', 0))
        return '[idle] '

    def _sorted_signals(self):
        sigs = list(self._signals.values())
        if self._sort_snr:
            sigs.sort(key=lambda s: -s['snr'])
        else:
            sigs.sort(key=lambda s: s['freq'])
        return sigs

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        scanning   = result.get('scanning', False)
        step_idx   = result.get('step_idx', 0)
        step_total = result.get('step_total', 0)
        step_hz    = result.get('step_hz', 0.0)
        min_freq   = result.get('min_freq', 0.0)
        max_freq   = result.get('max_freq', 0.0)
        dwell_s    = result.get('dwell_s', self._dwell_s)
        min_snr    = result.get('min_snr', self._min_snr)
        sort_snr   = result.get('sort_snr', self._sort_snr)
        signals    = result.get('signals', {})
        cursor     = self._cursor   # authoritative; updated by handle_key on main thread

        now = time.monotonic()

        # Row 0 — title / settings
        if min_freq > 0 and max_freq > 0:
            range_str = '{} – {}'.format(fmt_freq(min_freq), fmt_freq(max_freq))
        else:
            range_str = 'range: not set (m/n to configure)'
        title = ' Range Scan  {}  dwell:{:.1f}s  SNR≥{:.1f}dB  sort:{} '.format(
            range_str, dwell_s, min_snr, 'SNR' if sort_snr else 'freq')
        try:
            screen_obj.addstr(0, 0, title[:cols - 1], curses.A_BOLD)
        except curses.error:
            pass

        # Row 1 — progress bar or idle hint
        if scanning and step_total > 0:
            bar_w    = min(40, cols - 30)
            filled   = int(bar_w * step_idx / step_total)
            bar      = '█' * filled + '░' * (bar_w - filled)
            progress = '[{}] step {}/{}  {}'.format(
                bar, step_idx + 1, step_total, fmt_freq(step_hz))
        else:
            progress = '  press e to start scan'
        try:
            screen_obj.addstr(1, 0, progress[:cols - 1])
        except curses.error:
            pass

        # Row 2 — column headers
        hdr = '  {:>14}  {:>10}  {:>6}  {:>5}  {}'.format(
            'Frequency', 'BW', 'SNR', 'Age', '')
        try:
            screen_obj.addstr(2, 0, hdr[:cols - 1], curses.A_UNDERLINE)
        except curses.error:
            pass

        # Rows 3+ — signal list
        body_rows = max(0, rows - 4)   # row 0=title, 1=progress, 2=hdr, last=footer
        sigs = list(signals.values())
        if sort_snr:
            sigs.sort(key=lambda s: -s['snr'])
        else:
            sigs.sort(key=lambda s: s['freq'])

        # Clamp cursor
        if sigs:
            cursor = max(0, min(len(sigs) - 1, cursor))
        else:
            cursor = 0

        # Scroll so cursor is visible
        scroll = max(0, cursor - body_rows + 1) if body_rows > 0 else 0

        for i, sig in enumerate(sigs[scroll: scroll + body_rows]):
            abs_i  = i + scroll
            age    = now - sig.get('last_seen', now)
            age_s  = '{:.0f}s'.format(age)
            marker = '>' if abs_i == cursor else ' '
            line   = '{}  {:>14}  {:>10}  {:>5.1f}dB  {:>5}'.format(
                marker,
                fmt_freq(sig['freq']),
                fmt_freq(sig['bw_hz']),
                sig['snr'],
                age_s,
            )
            attr = curses.A_REVERSE if abs_i == cursor else curses.A_NORMAL
            if age > _AGE_DIM_S and abs_i != cursor:
                attr = curses.A_DIM
            try:
                screen_obj.addstr(3 + i, 0, line[:cols - 1], attr)
            except curses.error:
                pass

        if not sigs:
            try:
                screen_obj.addstr(3, 2, 'no signals detected yet', curses.A_DIM)
            except curses.error:
                pass
