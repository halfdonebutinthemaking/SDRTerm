"""Sync-word correlator with fine frequency search — Python 3 port of
extractor-python/complex_sync_search.py.

Pre-computes RRC-shaped, frequency-shifted preamble+UW templates for each
integer Hz offset in ±F_SEARCH; correlates against a signal to find both
the sync-word start and the residual carrier frequency offset.
"""
import numpy as np
import scipy.optimize
import scipy.signal

from . import filters
from . import iridium

F_SEARCH = 100


class ComplexSyncSearch(object):
    def __init__(self, sample_rate, verbose=False):
        self._sample_rate = sample_rate
        self._samples_per_symbol = self._sample_rate // iridium.SYMBOLS_PER_SECOND

        self._sync_words = [{}, {}]
        self._sync_words[iridium.DOWNLINK][0]  = self.generate_padded_sync_words(-F_SEARCH, F_SEARCH,  0, iridium.DOWNLINK)
        self._sync_words[iridium.DOWNLINK][16] = self.generate_padded_sync_words(-F_SEARCH, F_SEARCH, 16, iridium.DOWNLINK)
        self._sync_words[iridium.DOWNLINK][64] = self.generate_padded_sync_words(-F_SEARCH, F_SEARCH, 64, iridium.DOWNLINK)

        self._sync_words[iridium.UPLINK][16] = self.generate_padded_sync_words(-F_SEARCH, F_SEARCH, 16, iridium.UPLINK)

        self._verbose = verbose

    def generate_padded_sync_words(self, f_min, f_max, preamble_length, direction):
        s1 = -1 - 1j
        s0 = -s1

        if direction == iridium.DOWNLINK:
            sync_word = [s0] * preamble_length + [s0, s1, s1, s1, s1, s0, s0, s0, s1, s0, s0, s1]
        elif direction == iridium.UPLINK:
            sync_word = [s1, s0] * (preamble_length // 2) + [s1, s1, s0, s0, s0, s1, s0, s0, s1, s0, s1, s1]

        sync_word_padded = []
        for bit in sync_word:
            sync_word_padded += [bit]
            sync_word_padded += [0] * (self._samples_per_symbol - 1)

        rrc = filters.rrcosfilter(161, 0.4, 1.0 / iridium.SYMBOLS_PER_SECOND, self._sample_rate)[1]
        sync_word_padded_filtered = np.convolve(sync_word_padded, rrc, 'full')

        sync_words_shifted = {}
        for offset in range(f_min, f_max):
            shift_signal = np.exp(
                complex(0, -1) * np.arange(len(sync_word_padded_filtered)) *
                2 * np.pi * offset / float(self._sample_rate))
            shifted = sync_word_padded_filtered * shift_signal
            sync_words_shifted[offset] = np.conjugate(shifted[::-1])
        return sync_words_shifted

    def estimate_sync_word_start(self, signal, direction):
        sync_middle, confidence, _ = self.estimate_sync_word(signal, self._sync_words[direction][16][0])
        # Compensate for the 16 symbols of preamble
        sync_start = sync_middle + 2 * self._samples_per_symbol
        return sync_start, confidence

    def estimate_sync_word(self, signal, preamble):
        c = scipy.signal.fftconvolve(signal, preamble, 'same')
        sync_middle = int(np.argmax(np.abs(c)))
        return sync_middle, np.abs(c[sync_middle]), np.angle(c[sync_middle])

    def estimate_sync_word_freq(self, signal, preamble_length, direction):
        if preamble_length not in self._sync_words[direction]:
            return None, None, None

        sync_words = self._sync_words[direction][preamble_length]

        def f_est(freq, preambles):
            c = scipy.signal.fftconvolve(signal, preambles[int(freq + 0.5)], 'same')
            return -float(np.max(np.abs(c)))

        freq = int(scipy.optimize.fminbound(
            f_est, -(F_SEARCH - 1), (F_SEARCH - 1),
            args=(sync_words,), xtol=1) + 0.5)

        if abs(freq) == F_SEARCH - 1:
            return None, None, None

        _, confidence, phase = self.estimate_sync_word(signal, sync_words[freq])
        return freq, phase, confidence
