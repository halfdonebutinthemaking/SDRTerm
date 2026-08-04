"""Python 3 port of gr-iridium/lib/burst_downmix_impl.cc.

Per-burst DSP:
  1. Rough CFO shift (relative_frequency from detector)
  2. Input FIR low-pass + decimation to output_sample_rate (500 kHz)
  3. Envelope-based start detection (threshold = 0.28 * max over search_depth)
  4. Squared-signal FFT for fine CFO (16× over-sampled + quadratic peak interp)
  5. Fine CFO shift
  6. RRC matched filter
  7. FFT-based correlation with DL and UL sync templates → pick higher-conf
  8. Phase rotation to align with correlation peak
  9. Cut so unique-word start = sample 0

Emits a PDU dict per successful frame:
    {
        'samples':      np.ndarray[complex64],
        'sample_rate':  float,
        'center_frequency': float,      # post fine-CFO correction
        'direction':    iridium.DOWNLINK or UPLINK,
        'uw_start':     float,          # sub-sample correlation correction
        'timestamp':    int,            # ns since epoch (offset by burst start)
        'id':           int,
        'noise':        float,          # dBFS/Hz
        'magnitude':    float,          # dB above noise
    }
"""
import math
import numpy as np
import scipy.signal as sig

from . import iridium


def _firdes_low_pass_2(gain: float, sample_rate: float, cutoff_freq: float,
                       transition_width: float, attenuation_dB: float
                       ) -> np.ndarray:
    """Port of gnuradio's firdes::low_pass_2 — Parks-McClellan / Remez
    equiripple FIR design.

    Follows gnuradio's ntaps calculation (Kaiser formula):
        N = (attenuation - 7.95) / (14.36 * transition/sample_rate) + 1
    forced to be odd.

    Uses scipy.signal.remez with two bands (passband + stopband).
    """
    # Kaiser tap-count estimate — gr uses this same formula in
    # firdes::compute_ntaps_atten via a Kaiser-window intermediate.
    n = int((attenuation_dB - 7.95) /
            (14.36 * transition_width / sample_rate)) + 1
    if n % 2 == 0:
        n += 1
    if n < 5:
        n = 5

    # Passband edge = cutoff, stopband edge = cutoff + transition
    passband_edge = cutoff_freq
    stopband_edge = cutoff_freq + transition_width
    # Weight the stopband more heavily to hit attenuation target
    delta_p = 0.01
    delta_s = 10 ** (-attenuation_dB / 20)
    weight_p = 1.0 / delta_p
    weight_s = 1.0 / delta_s

    taps = sig.remez(
        n,
        [0, passband_edge, stopband_edge, sample_rate / 2],
        [gain, 0.0],
        weight=[weight_p, weight_s],
        fs=sample_rate,
    )
    return taps.astype(np.float32)


class BurstDownmix:
    def __init__(self, output_sample_rate: int, search_depth: int = None,
                 handle_multiple_frames_per_burst: bool = True,
                 input_taps: np.ndarray = None,
                 start_finder_taps: np.ndarray = None):
        self.output_sample_rate = int(output_sample_rate)
        self.output_sps = self.output_sample_rate // iridium.SYMBOLS_PER_SECOND
        self.handle_multi = handle_multiple_frames_per_burst

        # Default search depth = fft_size in gr-iridium's flowgraph;
        # for typical 2 MHz sample rate that's 2048.  Here it's expressed
        # in OUTPUT-rate samples so we need to scale.  For 500 kHz output
        # rate, 512 samples = 1024 µs ≈ 25 symbols of scan.
        self.search_depth = int(search_depth) if search_depth else 512

        # 0.1 ms of pre-start padding (same as C++ constant)
        self.pre_start_samples = int(0.1e-3 * self.output_sample_rate)

        # CFO estimation FFT size: power-of-2 covering preamble+10 symbols
        preamble_plus_10 = self.output_sps * (
            iridium.PREAMBLE_LENGTH_SHORT + 10)
        self.cfo_fft_size = 2 ** int(math.log2(preamble_plus_10))
        self.cfo_fft_over_size = 16       # zero-pad factor for finer freq resolution
        self.cfo_window = np.blackman(self.cfo_fft_size).astype(np.float32)

        # Sync search window: covers long preamble + UW + margin
        self.sync_search_len = ((iridium.PREAMBLE_LENGTH_LONG +
                                  iridium.UW_LENGTH + 8) * self.output_sps)

        # Input FIR: exact port of gr's firdes.low_pass_2(gain=1,
        # sr=input_sr, cutoff=burst_width/2=20 kHz, transition=burst_width=40 kHz,
        # attenuation=40 dB).  gr uses Parks-McClellan (Remez) — much shorter
        # (~113 taps at 2 MHz input) than our previous Hamming firwin(401).
        # An over-designed filter cuts too tight and removes signal edges.
        if input_taps is None:
            # Assume 2 MHz input; caller can override for other sample rates
            input_taps = _firdes_low_pass_2(
                gain=1.0, sample_rate=2_000_000,
                cutoff_freq=40_000 / 2, transition_width=40_000,
                attenuation_dB=40.0)
        self.input_taps = np.asarray(input_taps, dtype=np.float32)

        # Start-finder FIR: matches gr's low_pass_2(1, burst_sr, 5e3/2,
        # 10e3/2, 60) — narrow low-pass on |signal|² for envelope detection
        if start_finder_taps is None:
            start_finder_taps = _firdes_low_pass_2(
                gain=1.0, sample_rate=self.output_sample_rate,
                cutoff_freq=5e3 / 2, transition_width=10e3 / 2,
                attenuation_dB=60.0)
        self.start_finder_taps = np.asarray(start_finder_taps, dtype=np.float32)

        # Root-raised-cosine matched filter (51 taps, α=0.4)
        self.rrc_taps = self._rrc_taps(1.0, self.output_sample_rate,
                                        iridium.SYMBOLS_PER_SECOND, 0.4, 51)

        # Raised-cosine (for sync-word template shaping — different from RRC!)
        self.rc_taps = self._rc_taps(51, 0.4,
                                      1.0 / iridium.SYMBOLS_PER_SECOND,
                                      self.output_sample_rate)

        # Pre-compute FFT-domain sync word templates
        (self.dl_sync_template_fft,
         self.ul_sync_template_fft,
         self.corr_fft_size,
         self.sync_word_len) = self._precompute_sync_templates()

    # ── Filter construction (helpers) ────────────────────────────────────
    @staticmethod
    def _rrc_taps(gain: float, fs: float, symbol_rate: float,
                  alpha: float, ntaps: int) -> np.ndarray:
        """Exact port of gr::filter::firdes::root_raised_cosine.

        See gnuradio/gr-filter/lib/firdes.cc:641.  Uses gr's specific
        rational-form calculation (not the standard t-based RRC) and
        DC-gain normalisation (not L2).  This exact formula matters —
        subtle differences in tap magnitude directly affect symbol
        constellation quality after matched filtering.
        """
        if ntaps % 2 == 0:
            ntaps |= 1
        spb = fs / symbol_rate  # samples per bit (=SPS)
        taps = np.zeros(ntaps, dtype=np.float64)
        scale = 0.0
        for i in range(ntaps):
            xindx = i - ntaps // 2
            x1 = math.pi * xindx / spb
            x2 = 4 * alpha * xindx / spb
            x3 = x2 * x2 - 1
            if abs(x3) >= 1e-6:
                if i != ntaps // 2:
                    num = (math.cos((1 + alpha) * x1) +
                           math.sin((1 - alpha) * x1) / (4 * alpha * xindx / spb))
                else:
                    num = math.cos((1 + alpha) * x1) + (1 - alpha) * math.pi / (4 * alpha)
                den = x3 * math.pi
            else:
                if alpha == 1:
                    taps[i] = -1.0
                    scale += taps[i]
                    continue
                x3 = (1 - alpha) * x1
                x2 = (1 + alpha) * x1
                num = (math.sin(x2) * (1 + alpha) * math.pi -
                       math.cos(x3) * ((1 - alpha) * math.pi * spb) / (4 * alpha * xindx) +
                       math.sin(x3) * spb * spb / (4 * alpha * xindx * xindx))
                den = -32 * math.pi * alpha * alpha * xindx / spb
            taps[i] = 4 * alpha * num / den
            scale += taps[i]
        # DC-gain normalisation (matches gr's convention)
        taps *= gain / scale
        return taps.astype(np.float32)

    @staticmethod
    def _rc_taps(ntaps: int, alpha: float, Ts: float, Fs: float) -> np.ndarray:
        """Raised-cosine filter matching the C++ rcosfilter function."""
        taps = np.zeros(ntaps, dtype=np.float64)
        for i in range(-ntaps // 2 + 1, ntaps // 2 + 1):
            t = i / Fs
            if abs(abs(t) - Ts / (2 * alpha)) < 1e-12:
                h = np.pi / (4 * Ts) * np.sinc(1 / (2 * alpha))
            else:
                if t == 0:
                    sinc_val = 1.0
                else:
                    sinc_val = np.sin(np.pi * t / Ts) / (np.pi * t / Ts)
                cos_val = np.cos(np.pi * alpha * t / Ts)
                denom = 1 - (2 * alpha * t / Ts) ** 2
                h = (1.0 / Ts) * sinc_val * cos_val / denom
            taps[i + (ntaps + 1) // 2 - 1] = h * Ts
        return taps.astype(np.float32)

    def _generate_sync_word(self, direction: int) -> np.ndarray:
        """Generate the (RC-shaped, upsampled) sync-word template used
        for correlation.  Matches C++ generate_sync_word() with the
        #if 1 blocks enabled."""
        s1 = -1 - 1j
        s0 = -s1
        uw_dl = [s0, s1, s1, s1, s1, s0, s0, s0, s1, s0, s0, s1]
        uw_ul = [s1, s1, s0, s0, s0, s1, s0, s0, s1, s0, s1, s1]

        if direction == iridium.DOWNLINK:
            sync = [s0] * iridium.PREAMBLE_LENGTH_SHORT + uw_dl
        else:
            sync = [s1 if i % 2 == 0 else s0
                    for i in range(iridium.PREAMBLE_LENGTH_SHORT)] + uw_ul

        # Upsample with sps zeros between each symbol, drop trailing zeros
        sps = self.output_sps
        upsampled = np.zeros(len(sync) * sps, dtype=np.complex64)
        upsampled[::sps] = sync
        # Match C++ behaviour: remove padding after the LAST symbol so the
        # sync word ends exactly on that symbol.
        upsampled = upsampled[:len(upsampled) - (sps - 1)] if sps > 1 else upsampled

        # Apply raised-cosine shaping (same as C++ d_rc_fir.filterN)
        # C++ pads with half_rc_size zeros on each side then filters
        half_rc = (len(self.rc_taps) - 1) // 2
        padded = np.concatenate([np.zeros(half_rc, dtype=np.complex64),
                                  upsampled,
                                  np.zeros(half_rc, dtype=np.complex64)])
        filtered = np.convolve(padded, self.rc_taps, mode='full')
        # C++ filters back into a same-sized buffer; equivalent to `same`-mode
        # with just the input's length worth of samples
        start = half_rc + (len(self.rc_taps) - 1) // 2
        template = filtered[start:start + len(upsampled)].astype(np.complex64)

        # Reverse and conjugate (matched-filter form)
        return np.conjugate(template[::-1])

    def _precompute_sync_templates(self):
        """FFT-domain templates for DL and UL sync-word correlation."""
        dl_template = self._generate_sync_word(iridium.DOWNLINK)
        ul_template = self._generate_sync_word(iridium.UPLINK)
        sync_word_len = len(dl_template)
        assert len(ul_template) == sync_word_len

        # FFT size must be power of 2 covering sync_search_len + sync_word_len - 1
        target = self.sync_search_len + sync_word_len - 1
        corr_fft_size = 2 ** int(math.ceil(math.log2(target)))

        # Zero-pad templates to corr_fft_size and FFT
        dl_padded = np.zeros(corr_fft_size, dtype=np.complex64)
        dl_padded[:sync_word_len] = dl_template
        ul_padded = np.zeros(corr_fft_size, dtype=np.complex64)
        ul_padded[:sync_word_len] = ul_template

        dl_fft = np.fft.fft(dl_padded)
        ul_fft = np.fft.fft(ul_padded)
        return dl_fft, ul_fft, corr_fft_size, sync_word_len

    # ── Per-burst processing ────────────────────────────────────────────
    def process(self, burst_samples: np.ndarray,
                relative_frequency: float,
                center_frequency: float,
                input_sample_rate: float,
                timestamp: int = 0,
                burst_id: int = 0,
                noise: float = 0.0,
                magnitude: float = 0.0) -> list:
        """Process one burst, return list of frame PDUs.

        `relative_frequency` is a fraction of `input_sample_rate` in
        [-0.5, 0.5), matching fft_burst_tagger's convention
        `(center_bin - fft_size/2) / fft_size`.
        """
        # Rough CFO shift: multiply by exp(-2πj * rel_freq * n)
        n = len(burst_samples)
        rot = np.exp(
            -2j * np.pi * relative_frequency * np.arange(n)
        ).astype(np.complex64)
        shifted = (burst_samples * rot).astype(np.complex64)
        # Frequency correction in absolute Hz
        center_frequency = center_frequency + relative_frequency * input_sample_rate

        # Low-pass + decimate
        decimation = int(round(input_sample_rate / self.output_sample_rate))
        # C++ uses filterNdec which is FIR filter + decimate in one pass
        filtered = np.convolve(shifted, self.input_taps, mode='valid')
        decimated = filtered[::decimation].astype(np.complex64)

        sample_rate = input_sample_rate / decimation
        burst_size = len(decimated)

        # Start finder: |signal|² → low-pass → threshold
        fir_size = len(self.start_finder_taps)
        half_fir = (fir_size - 1) // 2
        N = min(self.search_depth, burst_size - (fir_size - 1))
        if N <= 0:
            return []
        mag2 = (decimated.real ** 2 + decimated.imag ** 2).astype(np.float32)
        mag2_filt = np.convolve(mag2[:N + fir_size - 1],
                                self.start_finder_taps, mode='valid')
        max_val = float(mag2_filt.max())
        threshold = max_val * 0.28
        # First index where filtered magnitude exceeds threshold
        above = np.where(mag2_filt >= threshold)[0]
        if len(above) == 0 or above[0] == 0:
            start = 0
        else:
            start = int(max(above[0] + half_fir - self.pre_start_samples, 0))

        frames = []
        if self.handle_multi:
            sub_id = burst_id
            while True:
                consumed = self._process_next_frame(
                    decimated, sample_rate, center_frequency,
                    timestamp, sub_id, start, noise, magnitude, frames)
                if consumed <= 0:
                    break
                start += consumed
                sub_id += 1
        else:
            self._process_next_frame(
                decimated, sample_rate, center_frequency,
                timestamp, burst_id, start, noise, magnitude, frames)
        return frames

    def _process_next_frame(self, decimated, sample_rate, center_frequency,
                             timestamp, sub_id, start, noise, magnitude,
                             out_frames):
        """Extract one frame from the burst starting at `start`.  Returns
        the number of samples consumed (so the caller can advance start
        for the next frame).  Returns 0 if no more frames."""
        burst_size = len(decimated)

        # Simplex vs normal frame length limits
        if center_frequency > iridium.SIMPLEX_FREQUENCY_MIN:
            max_frame_length = iridium.MAX_FRAME_LENGTH_SIMPLEX * self.output_sps
            min_frame_length = iridium.MIN_FRAME_LENGTH_SIMPLEX * self.output_sps
        else:
            max_frame_length = iridium.MAX_FRAME_LENGTH_NORMAL * self.output_sps
            min_frame_length = iridium.MIN_FRAME_LENGTH_NORMAL * self.output_sps

        if burst_size - start < min_frame_length:
            return 0
        if burst_size - start < self.cfo_fft_size:
            return 0

        # Fine CFO: FFT of squared signal
        frame_slice = decimated[start:start + self.cfo_fft_size]
        squared = frame_slice ** 2
        windowed = squared * self.cfo_window
        fft_input = np.zeros(self.cfo_fft_size * self.cfo_fft_over_size,
                             dtype=np.complex64)
        fft_input[:self.cfo_fft_size] = windowed
        fft_out = np.fft.fft(fft_input)
        mag2 = np.abs(fft_out) ** 2

        max_index_shifted = int(np.argmax(mag2))
        # Convert index to signed [-N/2, N/2)
        N_fft = self.cfo_fft_size * self.cfo_fft_over_size
        max_index = (max_index_shifted if max_index_shifted < N_fft // 2
                     else max_index_shifted - N_fft)

        # Quadratic interpolation using neighbours (indices in unshifted space)
        def shift_idx(k):
            k = max(-N_fft // 2, min(N_fft // 2 - 1, k))
            return k if k >= 0 else k + N_fft

        alpha = mag2[shift_idx(max_index - 1)]
        beta  = mag2[shift_idx(max_index)]
        gamma = mag2[shift_idx(max_index + 1)]
        denom = alpha - 2 * beta + gamma
        correction = 0.5 * (alpha - gamma) / denom if abs(denom) > 1e-30 else 0.0
        interpolated_index = max_index + correction

        # Divide by N_fft (bin size) and by 2 (undo the squaring)
        center_offset = interpolated_index / N_fft / 2

        # Shift by fine CFO
        rot = np.exp(
            -2j * np.pi * center_offset * np.arange(burst_size - start)
        ).astype(np.complex64)
        shifted = decimated[start:] * rot
        center_frequency = center_frequency + center_offset * sample_rate

        # Pad + RRC matched filter
        half_rrc = (len(self.rrc_taps) - 1) // 2
        padded = np.concatenate([
            np.zeros(half_rrc, dtype=np.complex64),
            shifted,
            np.zeros(half_rrc, dtype=np.complex64)
        ])
        filtered_full = np.convolve(padded, self.rrc_taps, mode='full')
        # C++ filterN gives output of the same size as input; take the
        # center portion
        offset = half_rrc + (len(self.rrc_taps) - 1) // 2
        rrc_out = filtered_full[offset:offset + len(shifted)].astype(np.complex64)

        # FFT-based correlation with DL and UL sync templates
        search_len = min(self.sync_search_len, len(rrc_out))
        corr_input = np.zeros(self.corr_fft_size, dtype=np.complex64)
        corr_input[:search_len] = rrc_out[:search_len]
        corr_fft = np.fft.fft(corr_input)

        dl_ifft = np.fft.ifft(corr_fft * self.dl_sync_template_fft)
        ul_ifft = np.fft.ifft(corr_fft * self.ul_sync_template_fft)
        dl_mag2 = np.abs(dl_ifft) ** 2
        ul_mag2 = np.abs(ul_ifft) ** 2

        max_dl_idx = int(np.argmax(dl_mag2))
        max_ul_idx = int(np.argmax(ul_mag2))
        max_dl = float(dl_mag2[max_dl_idx])
        max_ul = float(ul_mag2[max_ul_idx])

        def _corr_interp(mag2_arr, idx):
            if 0 < idx < len(mag2_arr) - 1:
                a, b, g = mag2_arr[idx - 1], mag2_arr[idx], mag2_arr[idx + 1]
                d = a - 2 * b + g
                return 0.5 * (a - g) / d if abs(d) > 1e-30 else 0.0
            return 0.0

        if max_dl > max_ul:
            direction = iridium.DOWNLINK
            corr_offset = max_dl_idx
            correction_sub = _corr_interp(dl_mag2, max_dl_idx)
            corr_result = dl_ifft[corr_offset]
        else:
            direction = iridium.UPLINK
            corr_offset = max_ul_idx
            correction_sub = _corr_interp(ul_mag2, max_ul_idx)
            corr_result = ul_ifft[corr_offset]

        # C++: preamble_offset = corr_offset - sync_word_len + 1
        preamble_offset = corr_offset - self.sync_word_len + 1
        uw_start = preamble_offset + iridium.PREAMBLE_LENGTH_SHORT * self.output_sps

        if uw_start < 0:
            return 0

        frame_size = min(len(rrc_out), uw_start + max_frame_length)
        consumed_samples = frame_size

        # Rotate to align phase with correlation output
        phase_correction = np.conj(corr_result / abs(corr_result))
        rotated = (rrc_out[:frame_size] * phase_correction).astype(np.complex64)

        # Cut so UW starts at sample 0
        frame_size_final = max(0, frame_size - uw_start)
        frame_data = rotated[uw_start:uw_start + frame_size_final]

        # Timestamp offset (ns)
        timestamp_out = timestamp + int(start * 1e9 / int(sample_rate))

        out_frames.append({
            'samples':          frame_data,
            'sample_rate':      float(sample_rate),
            'center_frequency': float(center_frequency),
            'direction':        direction,
            'uw_start':         float(correction_sub),
            'timestamp':        int(timestamp_out),
            'id':               int(sub_id),
            'noise':            float(noise),
            'magnitude':        float(magnitude),
        })

        return consumed_samples
