"""
Iridium L-band burst detector (Stage 1: detect-only, no demodulation).

Iridium is a LEO satellite constellation whose 66 satellites send bursts on
the L-band downlink 1616.0 - 1626.5 MHz.  The band is divided into 252
channels spaced 41.667 kHz apart.  Any given ~20 ms burst may appear on any
channel as a satellite hops.

This plugin does NOT demodulate anything.  It runs a sliding FFT, learns the
noise floor per bin, and reports every channel whose power crosses a
threshold above the noise floor.  The purpose is a live diagnostic:

  "Does my antenna hear Iridium at all, and if so, on which channels?"

Because the RTL-SDR maxes out around 2.4 - 3 MHz of stable IQ bandwidth, it
can only see ~1/4 of the Iridium band at once.  Tune the SDR centre inside
the band (e.g. 1621.25 MHz) with a sample rate ≥ 2 MHz to get the widest
practical coverage.

Antenna warning: Iridium signals are ~-120 dBm at ground level.  A patch or
helical antenna aimed at the sky works.  A random wire antenna in a room may
show zero bursts even during an overhead pass.
"""

import time
from collections import deque

import numpy as np

from core import Decoder, AppState, fmt_freq

# ── Iridium band plan ────────────────────────────────────────────────────────
IRIDIUM_BAND_LOW_HZ  = 1_616_000_000
IRIDIUM_BAND_HIGH_HZ = 1_626_500_000
IRIDIUM_CHAN_COUNT   = 252
IRIDIUM_CHAN_SPACING = (IRIDIUM_BAND_HIGH_HZ - IRIDIUM_BAND_LOW_HZ) / IRIDIUM_CHAN_COUNT  # ≈ 41667 Hz


def _channel_freq(chan_id: int) -> float:
    """Centre frequency of Iridium channel `chan_id` (0..251)."""
    return IRIDIUM_BAND_LOW_HZ + (chan_id + 0.5) * IRIDIUM_CHAN_SPACING


# ── burst-capture constants ──────────────────────────────────────────────────
_BURST_WIN_MS      = 50          # ± ~25 ms around burst peak → 50 ms slice
_IQ_RING_MS        = 120         # rolling raw-IQ history buffer, must be > _BURST_WIN_MS
# Bursts land in `iridium_bursts/` next to this plugin (gitignored) — see
# decode_bursts.sh for the shell watcher that consumes them.
import os as _os
_BURST_DIR         = _os.path.join(_os.path.dirname(__file__), 'iridium_bursts')

# ── detector tuning constants ────────────────────────────────────────────────
_FFT_SIZE          = 2048        # 2 MSPS / 2048 ≈ 977 Hz/bin, ~1 ms per frame
_NOISE_ALPHA       = 0.02        # EMA coefficient — ~50-frame time constant
_DEFAULT_THRESH_DB = 12.0        # detection threshold in dB above local noise floor.
                                 # 12 dB (16× power) filters out most birdies/spurs while still
                                 # catching real Iridium bursts (typically 15–30 dB above noise).
                                 # Tunable at runtime with +/- in the plugin tab.
_MIN_THRESH_DB     = 3.0         # 2× power
_MAX_THRESH_DB     = 30.0        # 1000× power
_THRESH_STEP_DB    = 3.0         # +3 dB = 2× ratio per step
_STATS_INTERVAL_S  = 1.0
_RECENT_WIN_S      = 10.0        # rolling window for per-channel activity counts
_MAX_RECENT_LIST   = 15          # rolling display list length


class IridiumDetector(Decoder):
    name            = 'iridium'
    key             = 'i'
    key_help        = '+/-=threshold  c=capture  r=clear'
    min_sample_rate = 2_000_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._window        = np.hanning(_FFT_SIZE).astype(np.float32)
        self._noise_floor   = None
        self._chan_map      = {}   # {chan_id: (bin_low, bin_high)} for current tuning
        self._chan_bursts   = {}   # {chan_id: deque(monotonic_ts)}
        self._recent_bursts = deque(maxlen=_MAX_RECENT_LIST)
        self._total_bursts  = 0
        self._burst_rate    = 0.0
        self._accum_bursts  = 0
        self._last_stats    = 0.0
        self._last_center   = 0.0
        self._last_bw       = 0
        self._threshold_db  = _DEFAULT_THRESH_DB    # tunable at runtime via +/-
        # Burst capture (Stage 2a) — off by default; each burst is ~800 KB
        # of wide IQ at 2 MSPS so 100 bursts fills ~80 MB.  Toggle with 'c'.
        self._capture_on    = False
        self._iq_ring       = np.empty(0, dtype=np.complex64)
        self._captured      = 0    # counter shown in the header

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        # threshold_db + capture_on intentionally NOT reset — persist across
        # start/stop so users don't lose tuned settings when toggling the
        # plugin.
        self._noise_floor   = None
        self._chan_map      = {}
        self._chan_bursts   = {}
        self._recent_bursts.clear()
        self._total_bursts  = 0
        self._burst_rate    = 0.0
        self._accum_bursts  = 0
        self._last_stats    = time.monotonic()
        self._last_center   = 0.0
        self._last_bw       = 0
        self._iq_ring       = np.empty(0, dtype=np.complex64)
        self._captured      = 0

    def stop(self) -> None:
        self._noise_floor   = None
        self._chan_map      = {}
        self._chan_bursts   = {}
        self._recent_bursts.clear()
        self._total_bursts  = 0
        self._burst_rate    = 0.0
        self._accum_bursts  = 0
        self._iq_ring       = np.empty(0, dtype=np.complex64)

    # ── channel map ─────────────────────────────────────────────────────────

    def _rebuild_chan_map(self, state: AppState) -> None:
        """Precompute FFT bin ranges for each Iridium channel visible in the current tuning.

        Also resets the per-bin noise floor: after a retune, each FFT bin is
        looking at a completely different frequency, so the stored EMA value
        is stale. Leaving it in place would cause spurious detections or
        missed bursts for the first ~50 frames while the EMA re-converged.
        Setting to None triggers a fresh initialisation on the next process()
        call, using the first chunk after retune as the noise-floor seed.
        """
        freqs = np.linspace(-state.bw_hz / 2, state.bw_hz / 2, _FFT_SIZE) + state.center_hz
        self._chan_map = {}
        half = IRIDIUM_CHAN_SPACING / 2
        for chan_id in range(IRIDIUM_CHAN_COUNT):
            cf = _channel_freq(chan_id)
            low, high = cf - half, cf + half
            if low > freqs[-1] or high < freqs[0]:
                continue
            bl = int(np.searchsorted(freqs, low))
            bh = int(np.searchsorted(freqs, high))
            if bh > bl:
                self._chan_map[chan_id] = (bl, bh)
        self._noise_floor = None
        self._last_center = state.center_hz
        self._last_bw     = state.bw_hz

    # ── process ─────────────────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if state.center_hz != self._last_center or state.bw_hz != self._last_bw:
            self._rebuild_chan_map(state)

        # Rolling raw-IQ history — sized by wall time so the capture window
        # size is independent of chunk size / SDR driver settings.
        if self._capture_on:
            ring_max = max(len(samples) + 1, int(state.bw_hz * _IQ_RING_MS / 1000))
            self._iq_ring = np.concatenate([self._iq_ring, samples])
            if len(self._iq_ring) > ring_max:
                self._iq_ring = self._iq_ring[-ring_max:]

        n_frames = len(samples) // _FFT_SIZE
        if n_frames < 1 or not self._chan_map:
            return self._make_result(state)

        # Vectorised FFT: reshape → window → FFT → shift → magnitude²
        chunk    = samples[:n_frames * _FFT_SIZE].reshape(n_frames, _FFT_SIZE)
        windowed = chunk * self._window
        specs    = np.fft.fftshift(np.fft.fft(windowed, axis=1), axes=1)
        powers   = specs.real ** 2 + specs.imag ** 2

        # Update per-bin noise floor as an EMA of the chunk-*median* spectrum.
        # Median across frames rejects burst outliers — Iridium bursts are
        # short (~1-8 frames at 1 ms/frame) and sparse, so within any chunk
        # most frames per bin are noise and the median lands on the true
        # noise level.  Using mean() here would let real bursts inflate the
        # floor, which then compresses the reported burst SNR down toward
        # the detection threshold (an "everything at threshold" symptom).
        avg = np.median(powers, axis=0)
        if self._noise_floor is None or len(self._noise_floor) != _FFT_SIZE:
            self._noise_floor = avg.copy()
        else:
            self._noise_floor = ((1.0 - _NOISE_ALPHA) * self._noise_floor
                                 + _NOISE_ALPHA * avg)

        # Per-channel power vs its own noise floor; one detection per channel per chunk
        now = time.monotonic()
        thresh_ratio = 10.0 ** (self._threshold_db / 10.0)
        for chan_id, (bl, bh) in self._chan_map.items():
            chan_pow = powers[:, bl:bh].mean(axis=1)
            chan_nf  = float(self._noise_floor[bl:bh].mean())
            if chan_nf < 1e-30:
                continue
            hot = chan_pow > (chan_nf * thresh_ratio)
            if not hot.any():
                continue
            peak_idx = int(np.argmax(chan_pow))
            peak_db  = 10.0 * float(np.log10(chan_pow[peak_idx] / chan_nf))
            self._register_burst(chan_id, peak_db, now)
            if self._capture_on:
                # peak_idx is a frame index within the current chunk.  Locate
                # the frame's centre sample in the freshly-updated ring, then
                # cut a ±(_BURST_WIN_MS/2) window around it.
                peak_offset_in_chunk = peak_idx * _FFT_SIZE + _FFT_SIZE // 2
                peak_pos_in_ring     = len(self._iq_ring) - len(samples) + peak_offset_in_chunk
                half                 = int(state.bw_hz * _BURST_WIN_MS / 1000 / 2)
                start                = max(0, peak_pos_in_ring - half)
                end                  = min(len(self._iq_ring), peak_pos_in_ring + half)
                if end - start > 0:
                    self._save_burst(self._iq_ring[start:end], state, chan_id, peak_db)

        # Update burst rate stat once per second
        dt = now - self._last_stats
        if dt >= _STATS_INTERVAL_S:
            self._burst_rate  = self._accum_bursts / dt
            self._accum_bursts = 0
            self._last_stats   = now

        return self._make_result(state)

    def _save_burst(self, iq: np.ndarray, state: AppState,
                    chan_id: int, snr_db: float) -> None:
        """Write a wide-IQ .cf32 + JSON sidecar for an external decoder to consume.

        Cheap synchronous I/O is fine here: bursts are ~800 KB at 2 MSPS × 50 ms
        and the plugin runs in a background thread — the main UI never blocks.
        """
        import json
        try:
            _os.makedirs(_BURST_DIR, exist_ok=True)
            ts        = time.strftime('%Y-%m-%dT%H-%M-%S') + '.{:03d}'.format(
                int((time.time() * 1000) % 1000))
            chan_freq = _channel_freq(chan_id)
            base      = '{}_ch{:03d}_{:+.1f}dB'.format(ts, chan_id, snr_db)
            data_path = _os.path.join(_BURST_DIR, base + '.cf32')
            meta_path = _os.path.join(_BURST_DIR, base + '.json')
            iq.astype(np.complex64).tofile(data_path)
            meta = {
                'sample_rate':     int(state.bw_hz),
                'tuned_center_hz': float(state.center_hz),
                'chan_id':         int(chan_id),
                'chan_freq_hz':    float(chan_freq),
                'chan_offset_hz':  float(chan_freq - state.center_hz),
                'snr_db':          float(snr_db),
                'timestamp':       ts,
                'n_samples':       int(len(iq)),
                'format':          'complex64_le',
            }
            with open(meta_path, 'w') as f:
                json.dump(meta, f, indent=2)
            self._captured += 1
        except OSError:
            # Disk full / permission denied — silently skip; capture is a
            # diagnostic, not critical to the detector's operation.
            pass

    def _register_burst(self, chan_id: int, power_db: float, now: float) -> None:
        self._total_bursts  += 1
        self._accum_bursts  += 1
        d = self._chan_bursts.setdefault(chan_id, deque())
        d.append(now)
        cutoff = now - _RECENT_WIN_S
        while d and d[0] < cutoff:
            d.popleft()
        self._recent_bursts.appendleft({
            'wall_ts':  time.strftime('%H:%M:%S'),
            'chan_id':  chan_id,
            'freq_hz':  _channel_freq(chan_id),
            'power_db': power_db,
        })

    def _make_result(self, state: AppState) -> dict:
        return {
            'total_bursts':    self._total_bursts,
            'burst_rate':      self._burst_rate,
            'chan_counts':     {c: len(d) for c, d in self._chan_bursts.items()},
            'recent_bursts':   list(self._recent_bursts),
            'n_visible_chans': len(self._chan_map),
            'in_band':         self._is_in_band(state),
            'threshold_db':    self._threshold_db,
            'capture_on':      self._capture_on,
            'captured':        self._captured,
        }

    def _is_in_band(self, state: AppState) -> bool:
        low  = state.center_hz - state.bw_hz / 2
        high = state.center_hz + state.bw_hz / 2
        return high >= IRIDIUM_BAND_LOW_HZ and low <= IRIDIUM_BAND_HIGH_HZ

    # ── key / status / view ────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self.start(state)
            return True
        if key in (ord('+'), ord('=')):
            self._threshold_db = min(_MAX_THRESH_DB, self._threshold_db + _THRESH_STEP_DB)
            return True
        if key == ord('-'):
            self._threshold_db = max(_MIN_THRESH_DB, self._threshold_db - _THRESH_STEP_DB)
            return True
        if key == ord('c'):
            self._capture_on = not self._capture_on
            if not self._capture_on:
                # Freeing the ring buffer when capture stops keeps memory low
                # for long sessions where capture was only used briefly.
                self._iq_ring = np.empty(0, dtype=np.complex64)
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        if not result.get('in_band'):
            return '[IR band?] '
        return '[IR {}b {:.1f}/s] '.format(
            result.get('total_bursts', 0),
            result.get('burst_rate', 0.0))

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        header = 'Iridium L-band Activity Detector  (1616.0 – 1626.5 MHz, 252 channels)'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        y = 3
        if not result.get('in_band'):
            msg = 'NOT TUNED TO IRIDIUM BAND — set centre near 1621.25 MHz with ≥ 2 MHz bandwidth.'
            try:
                screen_obj.addstr(y, 2, msg[:cols - 4], curses.A_BOLD)
            except curses.error:
                pass
            return

        n_vis     = result.get('n_visible_chans', 0)
        coverage  = 100.0 * n_vis / IRIDIUM_CHAN_COUNT
        thresh_db = result.get('threshold_db', _DEFAULT_THRESH_DB)
        if result.get('capture_on'):
            cap_str = '   |   Capture: ON ({} saved)'.format(result.get('captured', 0))
        else:
            cap_str = '   |   Capture: off'
        stats = ('Coverage: {}/{} channels ({:.0f}%)   |   '
                 'Total bursts: {}   |   Rate: {:.1f}/s   |   '
                 'Threshold: {:.0f} dB{}').format(
                 n_vis, IRIDIUM_CHAN_COUNT, coverage,
                 result.get('total_bursts', 0),
                 result.get('burst_rate', 0.0),
                 thresh_db, cap_str)
        try:
            screen_obj.addstr(y, 2, stats[:cols - 4])
        except curses.error:
            pass
        y += 2

        # Active-channel list, sorted by count within the last _RECENT_WIN_S seconds
        chan_counts = result.get('chan_counts', {})
        active = sorted(((c, n) for c, n in chan_counts.items() if n > 0),
                        key=lambda kv: -kv[1])[:20]

        if active:
            hdr = 'Active channels (last {}s):'.format(int(_RECENT_WIN_S))
            try:
                screen_obj.addstr(y, 2, hdr, curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            bar_w  = max(4, min(30, cols - 40))
            max_ct = max(c for _, c in active) or 1
            for chan_id, count in active:
                if y >= rows - 8:
                    break
                filled = int(bar_w * count / max_ct)
                bar    = '█' * filled + '░' * (bar_w - filled)
                line = '  Ch {:3d}  {:>11s}  {} {:4d}'.format(
                    chan_id, fmt_freq(_channel_freq(chan_id)), bar, count)
                try:
                    screen_obj.addstr(y, 2, line[:cols - 4])
                except curses.error:
                    pass
                y += 1
            y += 1
        else:
            try:
                screen_obj.addstr(y, 2, 'No bursts detected yet — waiting for satellite pass…',
                                  curses.A_DIM)
            except curses.error:
                pass
            y += 2

        # Recent bursts list
        recent = result.get('recent_bursts', [])
        if recent and y < rows - 3:
            try:
                screen_obj.addstr(y, 2, 'Recent bursts:', curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            for b in recent:
                if y >= rows - 2:
                    break
                line = '  [{}]  Ch {:3d}  {:>11s}  {:+.1f} dB'.format(
                    b['wall_ts'], b['chan_id'], fmt_freq(b['freq_hz']), b['power_db'])
                try:
                    screen_obj.addstr(y, 2, line[:cols - 4])
                except curses.error:
                    pass
                y += 1

    # ── persistence ────────────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {'threshold_db': self._threshold_db,
                'capture_on':   self._capture_on}

    def load_state(self, d: dict) -> None:
        v = float(d.get('threshold_db', _DEFAULT_THRESH_DB))
        self._threshold_db = max(_MIN_THRESH_DB, min(_MAX_THRESH_DB, v))
        self._capture_on   = bool(d.get('capture_on', False))
