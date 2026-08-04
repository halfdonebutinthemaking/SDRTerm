"""QPSK symbol slicer + Gray-differential decoder — Python 3 port of
extractor-python/demod.py.

Assumes cut_and_downmix has already:
  - centred the signal on DC
  - aligned to the sync-word phase
  - applied the RRC matched filter

Symbol timing is refined per-symbol by checking whether the current sample
sits at the peak of the RRC pulse (using neighbouring-sample sign trick).
"""
import cmath
import math

import numpy as np

from . import iridium
from .complex_sync_search import ComplexSyncSearch


UW_DOWNLINK = "022220002002"
UW_UPLINK   = "220002002022"


class Demod(object):
    def __init__(self, sample_rate, verbose=False, debug=False):
        self._sample_rate = sample_rate
        self._verbose = verbose
        self._debug = debug

        if self._sample_rate % iridium.SYMBOLS_PER_SECOND != 0:
            raise Exception("Non-int samples per symbol")

        self._samples_per_symbol = self._sample_rate // iridium.SYMBOLS_PER_SECOND

        # Beginning of burst is flaky — skip a few symbols before referring
        # to the signal level.
        self._skip = 5 * self._samples_per_symbol

        self._sync_search = ComplexSyncSearch(
            self._sample_rate, verbose=self._verbose)

    def qpsk(self, phase):
        """Map a phase angle (degrees) to one of the 4 QPSK symbols,
        also returning the offset from the nearest quadrant centre."""
        self._nsymbols += 1
        phase = phase % 360
        sym = int(phase) // 90
        off = 45 - (phase % 90)
        if abs(off) > 22:
            self._errors += 1
        return sym, off

    def _find_start(self, signal, direction):
        if direction is not None:
            start, _ = self._sync_search.estimate_sync_word_start(signal, direction)
        else:
            start_dl, confidence_dl = self._sync_search.estimate_sync_word_start(
                signal, iridium.DOWNLINK)
            start_ul, confidence_ul = self._sync_search.estimate_sync_word_start(
                signal, iridium.UPLINK)
            if confidence_dl > confidence_ul:
                start = start_dl
            else:
                start = start_ul
        return start

    def demod(self, signal, direction=None, return_final_offset=False):
        self._errors = 0
        self._nsymbols = 0

        level = abs(np.mean(signal[self._skip:self._skip + 16 * self._samples_per_symbol]))
        lmax  = abs(np.max(signal[self._skip:self._skip + 16 * self._samples_per_symbol]))

        i = self._find_start(signal, direction)
        symbols = []

        phase = 0            # cumulative phase offset (degrees)
        alpha = 2            # degrees before we shift phase
        delay = 0
        sdiff = 2            # timing check difference (in samples)
        if self._samples_per_symbol < 20:
            sdiff = 1

        while True:
            try:
                cur     = signal[i].real
                pre     = signal[i - self._samples_per_symbol].real
                post    = signal[i + self._samples_per_symbol].real
                curpre  = signal[i - sdiff].real
                curpost = signal[i + sdiff].real

                if pre < 0 and post < 0 and cur > 0:
                    if curpre > cur and cur > curpost:
                        i -= sdiff; delay -= sdiff
                    if curpre < cur and cur < curpost:
                        i += sdiff; delay -= sdiff
                elif pre > 0 and post > 0 and cur < 0:
                    if curpre > cur and cur > curpost:
                        i += sdiff; delay += sdiff
                    if curpre < cur and cur < curpost:
                        i -= sdiff; delay -= sdiff
                else:
                    cur     = signal[i].imag
                    pre     = signal[i - self._samples_per_symbol].imag
                    post    = signal[i + self._samples_per_symbol].imag
                    curpre  = signal[i - sdiff].imag
                    curpost = signal[i + sdiff].imag

                    if pre < 0 and post < 0 and cur > 0:
                        if curpre > cur and cur > curpost:
                            i -= sdiff; delay -= sdiff
                        if curpre < cur and cur < curpost:
                            i += sdiff; delay += sdiff
                    elif pre > 0 and post > 0 and cur < 0:
                        if curpre > cur and cur > curpost:
                            i += sdiff; delay += sdiff
                        if curpre < cur and cur < curpost:
                            i -= sdiff; delay -= sdiff
            except IndexError:
                pass

            ang = cmath.phase(signal[i]) / math.pi * 180
            symbol, offset = self.qpsk(ang + phase)
            if offset > alpha:
                phase += sdiff
            if offset < -alpha:
                phase -= sdiff

            symbols.append(symbol)
            i += self._samples_per_symbol
            if i >= len(signal):
                break
            if abs(signal[i]) < lmax / 8:
                break

        access = ""
        for s in symbols[:iridium.UW_LENGTH]:
            access += str(s)

        # Gray-code differential decode
        data = ""
        oldsym = 0
        dataarray = []
        for s in symbols:
            bits = (s - oldsym) % 4
            if bits == 0:
                bits = 0
            elif bits == 1:
                bits = 2
            elif bits == 2:
                bits = 3
            else:
                bits = 1
            oldsym = s
            data += str((bits & 2) // 2) + str(bits & 1)
            dataarray += [(bits & 2) // 2, bits & 1]

        access_ok = access in (UW_DOWNLINK, UW_UPLINK)

        lead_out = "100101111010110110110011001111"
        lead_out_ok = lead_out in data

        confidence = (1 - float(self._errors) / max(1, self._nsymbols)) * 100

        self._real_freq_offset = phase / 360.0 * iridium.SYMBOLS_PER_SECOND / max(1, self._nsymbols)

        if access_ok:
            data = "<" + data[:iridium.UW_LENGTH * 2] + "> " + data[iridium.UW_LENGTH * 2:]

        if lead_out_ok:
            idx = data.find(lead_out)
            data = (data[:idx] + "[" + data[idx:idx + len(lead_out)] + "]"
                    + data[idx + len(lead_out):])

        # Space every 32 bits for readability (matches iridium-toolkit format)
        import re
        data = re.sub(r'([01]{32})', r'\1 ', data)

        if return_final_offset:
            return (dataarray, data, access_ok, lead_out_ok, confidence,
                    level, self._nsymbols, self._real_freq_offset)
        return (dataarray, data, access_ok, lead_out_ok, confidence,
                level, self._nsymbols)
