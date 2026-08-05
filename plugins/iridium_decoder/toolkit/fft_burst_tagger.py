"""Python 3 port of gr-iridium's C++ fft_burst_tagger.

Line-by-line translation of lib/fft_burst_tagger_impl.cc from
https://github.com/muccc/gr-iridium (GPLv3).

The algorithm:
  - Per-FFT-frame magnitude² spectrum
  - Divide by moving-average baseline (last 512 frames' sum)
  - Existing bursts extended if their bin (±1) still exceeds threshold
  - Bins near existing bursts masked out
  - New bursts created for unmasked peaks above threshold
  - Noise-floor moving average only updated when NO active bursts
  - Emits (start_sample, stop_sample, center_bin) tuples matching
    what gr-iridium tags on its output stream

Sample rate must be divisible by 100000 (same restriction as the C++
version) so that timestamps can be computed without integer overflow.
"""
import math
import numpy as np

# Optional pyFFTW backend — 2-3× faster than numpy for the ~977 FFTs/s
# tagger workload at 2 MHz.  Falls back to numpy.fft silently if pyfftw
# isn't installed, so the plugin still works out of the box.
try:
    import pyfftw
    import pyfftw.interfaces.numpy_fft as _fft_backend
    # Cache plans so repeated FFTs of the same size hit the fast path
    pyfftw.interfaces.cache.enable()
    _HAS_PYFFTW = True
except ImportError:
    _fft_backend = np.fft
    _HAS_PYFFTW = False


class Burst:
    __slots__ = ('id', 'start', 'stop', 'last_active', 'center_bin',
                 'magnitude', 'noise')

    def __init__(self):
        self.id = 0
        self.start = 0
        self.stop = 0
        self.last_active = 0
        self.center_bin = 0
        self.magnitude = 0.0
        self.noise = 0.0


class FftBurstTagger:
    def __init__(self,
                 sample_rate: int,
                 fft_size: int = None,
                 threshold_db: float = 8.0,
                 burst_pre_len: int = None,
                 burst_post_len: int = None,
                 burst_width: int = 40_000,
                 history_size: int = 512,
                 max_burst_len: int = None,
                 max_bursts: int = 0,
                 center_frequency: float = 0.0):
        self.sample_rate = int(sample_rate)
        self.center_frequency = float(center_frequency)

        # Defaults matching iridium_extractor_flowgraph.py
        if fft_size is None:
            fft_size = 2 ** round(math.log2(sample_rate / 1000))
        if burst_pre_len is None:
            burst_pre_len = 2 * fft_size
        if burst_post_len is None:
            burst_post_len = int(sample_rate * 16e-3)
        if max_burst_len is None:
            max_burst_len = int(sample_rate * 0.09)

        self.fft_size = int(fft_size)
        self.burst_pre_len = int(burst_pre_len)
        self.burst_post_len = int(burst_post_len)
        self.burst_width_bins = int(burst_width / (sample_rate / fft_size))
        self.history_size = int(history_size)
        self.max_burst_len = int(max_burst_len)

        # Blackman window scaled by 1/0.42 (Blackman coherent gain) so that
        # magnitude values are the actual signal amplitude, not scaled down
        # by the window.
        window = np.blackman(self.fft_size).astype(np.float32) / 0.42
        self.window = window

        # Blackman window's Equivalent Noise Bandwidth
        self.window_enbw = 1.72

        # Threshold conversion: from dB above noise to linear ratio.
        # The comparison is done on `magnitude² / sum(history)`, i.e.
        # signal_power / (history_size * mean_noise_power).  So to test
        # "signal is X dB above noise" we compare against:
        #   10^(X/10) / history_size / ENBW
        # The ENBW correction accounts for the fact that our per-bin
        # magnitude includes energy from neighboring frequencies via
        # window leakage.
        self.threshold_db = float(threshold_db)
        self.threshold = self._compute_threshold(self.threshold_db)

        if max_bursts:
            self.max_bursts = int(max_bursts)
        else:
            # Consider the signal to be invalid if more than 80 % of all
            # channels are in use — same heuristic as the C++ version.
            self.max_bursts = int((sample_rate / burst_width) * 0.8)

        # Per-frame state
        self.baseline_history = np.zeros(
            (self.history_size, self.fft_size), dtype=np.float32)
        self.baseline_sum = np.zeros(self.fft_size, dtype=np.float32)
        self.burst_mask = np.ones(self.fft_size, dtype=np.float32)
        self.history_index = 0
        self.history_primed = False
        self.squelch_count = 0

        # Global sample index counter (position in the input stream)
        self.d_index = 0
        self.burst_id = 0

        # Active + finished bursts
        self.bursts: list = []
        self.new_bursts: list = []
        self.gone_bursts: list = []

    def _compute_threshold(self, threshold_db: float) -> float:
        return ((10 ** (threshold_db / 10))
                / self.history_size / self.window_enbw)

    def set_threshold_db(self, threshold_db: float):
        """Adjust the detection threshold at runtime.  Higher = fewer
        marginal detections, lower CPU load per second, but may miss
        weak bursts.  Safe to call while process_frame is running."""
        self.threshold_db = float(threshold_db)
        self.threshold = self._compute_threshold(self.threshold_db)

    # ── FFT + magnitude² ─────────────────────────────────────────────────
    def _magnitude_squared_shifted(self, samples: np.ndarray) -> np.ndarray:
        """FFT of `samples * window`, magnitude squared, shifted so DC is
        in the middle (bin fft_size/2)."""
        spec = _fft_backend.fft(samples * self.window)
        mag2 = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        # C++ does the shift manually via two half-copies:
        #   [d_fft_size/2 : d_fft_size] ← FFT bins [d_fft_size/2 : end]
        #   [0 : d_fft_size/2]          ← FFT bins [0 : d_fft_size/2]
        # np.fft.fftshift does the same thing.
        return np.fft.fftshift(mag2)

    # ── Baseline (moving-average) maintenance ────────────────────────────
    def _update_filters_pre(self, mag2_shifted: np.ndarray) -> bool:
        """Divide current mag² by baseline sum → relative_magnitude.
        Returns False (skip peak detection) if history not primed yet."""
        if not self.history_primed:
            return False, None
        # Safe division: baseline_sum should be > 0 when history_primed
        rel_mag = mag2_shifted / (self.baseline_sum + 1e-30)
        return True, rel_mag

    def _update_filters_post(self, mag2_shifted: np.ndarray, force: bool):
        """Roll the current mag² into the moving-average history — but
        ONLY when no burst is active, so we don't contaminate the noise
        floor with burst energy."""
        if not self.bursts or force:
            self.baseline_sum = (self.baseline_sum
                                 - self.baseline_history[self.history_index]
                                 + mag2_shifted)
            self.baseline_history[self.history_index] = mag2_shifted
            self.history_index += 1
            if self.history_index == self.history_size:
                self.history_primed = True
                self.history_index = 0

    # ── Burst tracking ───────────────────────────────────────────────────
    def _update_bursts(self, rel_mag: np.ndarray):
        for b in self.bursts:
            # C++ checks bins center-1, center, center+1
            if (rel_mag[b.center_bin - 1] > self.threshold or
                rel_mag[b.center_bin]     > self.threshold or
                rel_mag[b.center_bin + 1] > self.threshold):
                b.last_active = self.d_index

    def _delete_gone_bursts(self):
        update_noise_floor = False
        keep = []
        for b in self.bursts:
            long_burst = (self.max_burst_len > 0 and
                          (b.last_active - b.start) > self.max_burst_len)
            if long_burst:
                update_noise_floor = True
            if (b.last_active + self.burst_post_len) <= self.d_index or long_burst:
                b.stop = self.d_index
                self.gone_bursts.append(b)
            else:
                keep.append(b)
        self.bursts = keep
        return update_noise_floor

    def _mask_burst(self, center_bin: int):
        lo = max(center_bin - self.burst_width_bins // 2, 0)
        hi = min(center_bin + self.burst_width_bins // 2, self.fft_size - 1)
        self.burst_mask[lo:hi + 1] = 0.0

    def _update_burst_mask(self):
        self.burst_mask.fill(1.0)
        for b in self.bursts:
            self._mask_burst(b.center_bin)

    def _remove_peaks_around_bursts(self, rel_mag: np.ndarray) -> np.ndarray:
        return rel_mag * self.burst_mask

    def _extract_peaks(self, rel_mag: np.ndarray) -> list:
        # C++ iterates from burst_width/2 to fft_size - burst_width/2
        lo = self.burst_width_bins // 2
        hi = self.fft_size - self.burst_width_bins // 2
        bins = np.arange(lo, hi)
        vals = rel_mag[lo:hi]
        above = vals > self.threshold
        if not np.any(above):
            return []
        peak_bins = bins[above]
        peak_vals = vals[above]
        # Sort descending by magnitude
        order = np.argsort(-peak_vals)
        return list(zip(peak_bins[order].tolist(), peak_vals[order].tolist()))

    def _create_new_bursts(self, peaks: list):
        for center_bin, rel_magnitude in peaks:
            if self.burst_mask[center_bin] <= 0:
                continue
            b = Burst()
            b.id = self.burst_id
            b.center_bin = center_bin
            # Allow downstream to sub-id (C++ increments by 10)
            self.burst_id += 10
            # Undo the ENBW/history_size normalisation applied in threshold
            b.magnitude = 10 * math.log10(
                rel_magnitude * self.history_size * self.window_enbw)
            # Burst may have started one FFT earlier (pre-len samples back)
            b.start = self.d_index - self.burst_pre_len
            b.last_active = b.start
            # Noise floor at that bin (dBFS/Hz)
            b.noise = 10 * math.log10(
                self.baseline_sum[b.center_bin] / self.history_size /
                (self.fft_size * self.fft_size) / self.window_enbw /
                (self.sample_rate / self.fft_size))
            self.bursts.append(b)
            self.new_bursts.append(b)
            self._mask_burst(b.center_bin)

        # C++ burst-squelch: too many simultaneous bursts = give up all
        # and reset the noise estimate.
        if self.max_bursts > 0 and len(self.bursts) > self.max_bursts:
            self.new_bursts.clear()
            for b in self.bursts:
                if b.start != self.d_index - self.burst_pre_len:
                    b.stop = self.d_index
                    self.gone_bursts.append(b)
            self.bursts.clear()
            self._update_burst_mask()
            self.squelch_count += 3
            if self.squelch_count >= 10:
                self.history_index = 0
                self.history_primed = False
                self.baseline_history.fill(0.0)
                self.baseline_sum.fill(0.0)
                self.squelch_count = 0
        else:
            if self.squelch_count:
                self.squelch_count -= 1

    # ── Top-level: process one FFT frame ────────────────────────────────
    def process_frame(self, samples: np.ndarray):
        """Consume one FFT-frame worth of complex samples.
        Emits burst events by appending to self.new_bursts / self.gone_bursts;
        caller should drain those lists after each call."""
        assert len(samples) == self.fft_size, \
            "expected %d samples, got %d" % (self.fft_size, len(samples))

        mag2 = self._magnitude_squared_shifted(samples)
        primed, rel_mag = self._update_filters_pre(mag2)
        if primed:
            self._update_bursts(rel_mag)
            rel_mag = self._remove_peaks_around_bursts(rel_mag)
            peaks = self._extract_peaks(rel_mag)
            force_update = self._delete_gone_bursts()
            self._update_burst_mask()
            self._create_new_bursts(peaks)
            # If a long-burst was force-expired, roll the noise floor
            # forward using this frame anyway.
            if force_update:
                self._update_filters_post(mag2, force=True)
        self._update_filters_post(mag2, force=False)
        self.d_index += self.fft_size

    def drain(self):
        """Return (new_bursts, gone_bursts) since the last call, and
        clear the internal lists."""
        n, g = self.new_bursts, self.gone_bursts
        self.new_bursts = []
        self.gone_bursts = []
        return n, g

    def relative_frequency(self, center_bin: int) -> float:
        """Convert center bin to frequency offset from tuner center in Hz."""
        # center_bin is in fftshifted space (0..fft_size-1, DC at fft_size/2)
        rel = (center_bin - self.fft_size / 2) / float(self.fft_size)
        return rel * self.sample_rate

    def alternate_center_bin(self, center_bin: int) -> int:
        """Mirror the bin across N/2.  Verified empirically: gr-iridium's
        fft_burst_tagger emits bins mirrored relative to numpy's FFT
        output for the same input samples (start_index matches exactly,
        but center_bin = fft_size - our_bin).  Root cause is still under
        investigation — possibly a subtle FFT-plan or IQ-ordering
        difference.  Meanwhile: for real decodes, running the downstream
        burst_downmix with BOTH bin conventions per burst catches
        significantly more valid frames than either alone."""
        return self.fft_size - int(center_bin)
