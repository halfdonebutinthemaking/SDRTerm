"""Per-burst cut, downmix, and pre-demod alignment — Python 3 port of
extractor-python/cut_and_downmix.py.

Signal chain per detected burst:
  1. shift signal by detected offset frequency
  2. low-pass filter (401-tap FIR, ~50 kHz cutoff)
  3. decimate from input rate to 500 kHz
  4. envelope-threshold to find precise burst start
  5. FFT of signal² over preamble+UW → fine frequency estimate
     (squaring cancels the BPSK modulation of Iridium's preamble, leaving
     a clean tone at 2× carrier offset)
  6. quadratic peak interpolation for sub-bin frequency resolution
  7. shift by fine offset, then sync-word template match for phase
  8. RRC matched filter
"""
import cmath
import math

import numpy as np
import scipy.signal

from . import filters
from . import iridium
from .complex_sync_search import ComplexSyncSearch


class DownmixError(Exception):
    pass


class CutAndDownmix(object):
    def __init__(self, center, input_sample_rate, search_depth=7e-3,
                 search_window=50e3, symbols_per_second=25000, verbose=False):
        self._center = center
        self._input_sample_rate = int(input_sample_rate)
        self._output_sample_rate = 500000

        if self._input_sample_rate % self._output_sample_rate:
            raise RuntimeError(
                "Input sample rate must be a multiple of %d" %
                self._output_sample_rate)

        self._decimation = self._input_sample_rate // self._output_sample_rate

        self._search_depth = search_depth
        self._symbols_per_second = symbols_per_second
        self._output_samples_per_symbol = self._output_sample_rate // self._symbols_per_second
        self._verbose = verbose

        self._input_low_pass = scipy.signal.firwin(
            401, float(search_window) / self._input_sample_rate)
        self._low_pass2 = scipy.signal.firwin(
            401, 10e3 / self._output_sample_rate)
        self._rrc = filters.rrcosfilter(
            51, 0.4, 1.0 / self._symbols_per_second, self._output_sample_rate)[1]

        self._sync_search = ComplexSyncSearch(
            self._output_sample_rate, verbose=self._verbose)

        self._pre_start_samples = int(0.1e-3 * self._output_sample_rate)

    @property
    def output_sample_rate(self):
        return self._output_sample_rate

    def _fft(self, slice_, fft_len=None):
        if fft_len:
            fft_result = np.fft.fft(slice_, fft_len)
        else:
            fft_result = np.fft.fft(slice_)
        fft_freq = np.fft.fftfreq(len(fft_result))
        fft_result = np.fft.fftshift(fft_result)
        fft_freq = np.fft.fftshift(fft_freq)
        return fft_result, fft_freq

    def _signal_start(self, signal):
        signal_mag = np.abs(signal)
        signal_mag_lp = scipy.signal.fftconvolve(signal_mag, self._low_pass2, mode='same')
        threshold = np.max(signal_mag_lp) * 0.5
        indices = np.where(signal_mag_lp > threshold)[0]
        if len(indices) == 0:
            return 0
        start = max(int(indices[0]) - self._pre_start_samples, 0)
        return start

    def cut_and_downmix(self, signal, search_offset=None, direction=None,
                        frequency_offset=0, phase_offset=0):
        # Coarse shift to search_offset
        shift_signal = np.exp(
            complex(0, -1) * np.arange(len(signal)) *
            2 * np.pi * search_offset / float(self._input_sample_rate))
        signal = signal * shift_signal
        signal = scipy.signal.fftconvolve(signal, self._input_low_pass, mode='same')
        signal_center = self._center + search_offset
        signal = signal[::self._decimation]

        # Ring Alert and Pager Channels have a 64 symbol preamble
        if signal_center > 1626000000:
            preamble_length = 64
            direction = iridium.DOWNLINK
        else:
            preamble_length = 16

        # FFT over preamble + 10 symbols
        fft_length = 2 ** int(math.log(
            self._output_samples_per_symbol * (preamble_length + 10), 2))

        begin = self._signal_start(
            signal[:int(self._search_depth * self._output_sample_rate)])
        signal = signal[begin:]

        if len(signal) < fft_length:
            raise DownmixError("Signal too short after start-of-burst trim")

        # Square the preamble — Iridium preamble is BPSK-modulated
        # (all symbols ±1); squaring cancels the modulation and leaves a
        # complex exponential at 2× frequency offset that we can find by FFT.
        signal_preamble = signal[:fft_length] ** 2
        signal_preamble = signal_preamble * np.blackman(len(signal_preamble))
        fft_result, fft_freq = self._fft(signal_preamble, len(signal_preamble) * 16)
        fft_bin_size = fft_freq[101] - fft_freq[100]

        mag = np.abs(fft_result)
        max_index = int(np.argmax(mag))

        # Sub-bin refinement via quadratic peak interpolation
        # See http://www.dsprelated.com/dspbooks/sasp/Quadratic_Interpolation_Spectral_Peaks.html
        alpha = abs(fft_result[max_index - 1])
        beta  = abs(fft_result[max_index])
        gamma = abs(fft_result[max_index + 1])
        if (alpha - 2 * beta + gamma) == 0:
            correction = 0.0
        else:
            correction = 0.5 * (alpha - gamma) / (alpha - 2 * beta + gamma)
        real_index = max_index + correction

        a = int(math.floor(real_index))
        corrected_index = fft_freq[a] + (real_index - a) * fft_bin_size
        # Divide by 2 to undo the squaring done above.
        offset_freq = corrected_index * self._output_sample_rate / 2.0

        # Shift by fine offset
        shift_signal = np.exp(
            complex(0, -1) * np.arange(len(signal)) *
            2 * np.pi * offset_freq / float(self._output_sample_rate))
        signal = signal * shift_signal

        # Sync-word search for direction (if not specified) + phase alignment
        preamble_uw = signal[:(preamble_length + 16) * self._output_samples_per_symbol]

        if direction is not None:
            offset, phase, _ = self._sync_search.estimate_sync_word_freq(
                preamble_uw, preamble_length, direction)
        else:
            offset_dl, phase_dl, confidence_dl = self._sync_search.estimate_sync_word_freq(
                preamble_uw, preamble_length, iridium.DOWNLINK)
            offset_ul, phase_ul, confidence_ul = self._sync_search.estimate_sync_word_freq(
                preamble_uw, preamble_length, iridium.UPLINK)

            if confidence_dl is None and confidence_ul is None:
                raise DownmixError("No sync word found in either direction")
            if confidence_ul is None or (confidence_dl is not None and
                                          confidence_dl > confidence_ul):
                direction = iridium.DOWNLINK
                offset, phase = offset_dl, phase_dl
            else:
                direction = iridium.UPLINK
                offset, phase = offset_ul, phase_ul

        if offset is None:
            raise DownmixError("No valid freq offset for sync word found")

        offset = -offset
        phase += phase_offset
        offset += frequency_offset

        shift_signal = np.exp(
            complex(0, -1) * np.arange(len(signal)) *
            2 * np.pi * offset / float(self._output_sample_rate))
        signal = signal * shift_signal
        offset_freq += offset

        signal = signal * cmath.rect(1, -phase)
        signal = scipy.signal.fftconvolve(signal, self._rrc, 'same')

        return (signal, signal_center + offset_freq, direction)
