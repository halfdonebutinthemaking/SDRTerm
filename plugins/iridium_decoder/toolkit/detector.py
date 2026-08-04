"""FFT-based burst detector — Python 3 port of extractor-python/detector.py.

Sliding FFT over the raw IQ; each bin's magnitude divided by a moving-
average noise floor yields a per-bin SNR.  Peaks above the threshold are
tracked across frames; when they end, the collected time-domain signal
slice is handed to a callback for demodulation.
"""
import math
import numpy as np


class Detector(object):
    def __init__(self, sample_rate, fft_peak=7.0, sample_format=None,
                 search_size=1, verbose=False, signal_width=40e3, burst_size=6):
        self._sample_rate = sample_rate
        # FFT approximately 1 ms long
        self._fft_size = int(math.pow(2, 1 + int(math.log(self._sample_rate / 1000, 2))))
        self._bin_size = float(self._fft_size) / self._sample_rate * 1000
        self._verbose = verbose
        self._search_size = search_size
        self._fft_peak = fft_peak
        self._burst_size = burst_size

        if sample_format == "rtl" or sample_format == "cu8":
            self._struct_elem = np.uint8
            self._struct_len = np.dtype(self._struct_elem).itemsize * self._fft_size * 2
        elif sample_format == "hackrf" or sample_format == "ci8":
            self._struct_elem = np.int8
            self._struct_len = np.dtype(self._struct_elem).itemsize * self._fft_size * 2
        elif sample_format == "sc16" or sample_format == "ci16_le":
            self._struct_elem = np.int16
            self._struct_len = np.dtype(self._struct_elem).itemsize * self._fft_size * 2
        elif sample_format in ("float", "cf32_le", "fc32", "cfile"):
            self._struct_elem = np.complex64
            self._struct_len = np.dtype(self._struct_elem).itemsize * self._fft_size
        else:
            raise Exception("No sample format given")

        self._window = np.blackman(self._fft_size)
        self._fft_histlen = 500                          # ~500 * 1 ms moving average
        self._data_histlen = self._search_size
        self._data_postlen = 8
        self._signal_maxlen = 1 + int(30 / self._bin_size)  # ~30 ms
        self._fft_freq = np.fft.fftshift(np.fft.fftfreq(self._fft_size))
        # Area to ignore around an already found signal in FFT bins
        self._signal_width = signal_width / (self._sample_rate / self._fft_size)

    def _bytes_to_complex(self, data):
        slice_ = np.frombuffer(data, dtype=self._struct_elem)
        if self._struct_elem is np.uint8:
            slice_ = slice_.astype(np.float32)
            slice_ = (slice_ - 127.4) / 128.0
            slice_ = slice_.view(np.complex64)
        elif self._struct_elem is np.int8:
            slice_ = slice_.astype(np.float32) / 128.0
            slice_ = slice_.view(np.complex64)
        elif self._struct_elem is np.int16:
            slice_ = slice_.astype(np.float32) / 32768.0
            slice_ = slice_.view(np.complex64)
        return slice_

    def process_file(self, file_name, data_collector):
        data_hist = []
        fft_avg = np.zeros(self._fft_size)
        fft_hist = []

        index = -1
        signals = 0
        peaks = []       # each: [peakidx, postlen_countdown, start_index, info_tuple, slice]

        def remove_signal(peakl, idx):
            w = int(self._signal_width - 1) // 2
            p0 = max(0, idx - w)
            p1 = min(self._fft_size - 1, idx + w)
            peakl[p0:p1 + 1] = 0

        with open(file_name, "rb") as f:
            burst_signals = 0
            burst_mute = 0
            while True:
                data = f.read(self._struct_len)
                if burst_signals > 0:
                    burst_signals -= 1
                if burst_mute > 0:
                    burst_mute -= 1
                if not data:
                    break
                if len(data) != self._struct_len:
                    break

                index += 1
                if index % self._search_size == 0:
                    slice_ = self._bytes_to_complex(data)
                    fft_result = np.abs(np.fft.fftshift(np.fft.fft(slice_ * self._window)))

                    if len(fft_hist) > 25:
                        # grace period after start of file
                        peakl = (fft_result / fft_avg) * len(fft_hist)

                        # Advance any in-progress peaks; extend their slices
                        for p in peaks:
                            pi = p[0]
                            if peakl[pi] > self._fft_peak:
                                p[1] = self._search_size + self._data_postlen
                            p[1] -= 1
                            p[4] = np.append(p[4], slice_)
                            if (index - p[2]) < self._signal_maxlen:
                                remove_signal(peakl, pi)

                        peakidx = int(np.argmax(peakl))
                        peak = peakl[peakidx]
                        while peak > self._fft_peak and burst_mute == 0:
                            signals += 1
                            burst_signals += 1
                            if burst_signals == self._burst_size:
                                break

                            time_stamp = index * self._bin_size
                            signal_strength = 10 * math.log(peak, 10)
                            bin_index = peakidx
                            freq = self._fft_freq[peakidx] * self._sample_rate
                            info = (time_stamp, signal_strength, bin_index, freq)
                            signal = np.append(np.concatenate(data_hist), slice_) \
                                if data_hist else slice_.copy()

                            writepost = self._search_size + self._data_postlen
                            peaks.append([peakidx, writepost, index, info, signal])

                            remove_signal(peakl, peakidx)
                            peakidx = int(np.argmax(peakl))
                            peak = peakl[peakidx]

                    if burst_signals == self._burst_size:
                        burst_mute = 10
                        burst_signals = 0

                    # Collect finished peaks
                    peaks_to_collect = [p for p in peaks if p[1] <= 0]
                    for peak in peaks_to_collect:
                        data_collector(peak[3][0], peak[3][1], peak[3][2],
                                       peak[3][3], peak[4])
                    peaks = [p for p in peaks if p[1] > 0]

                    # Update noise floor moving average only when there's
                    # no burst in progress
                    if len(peaks) == 0:
                        fft_hist.append(fft_result)
                        fft_avg = fft_avg + fft_result
                        if len(fft_hist) > self._fft_histlen:
                            fft_avg = fft_avg - fft_hist[0]
                            fft_hist.pop(0)

                data_hist.append(slice_ if index % self._search_size == 0
                                 else self._bytes_to_complex(data))
                if len(data_hist) > self._data_histlen:
                    data_hist.pop(0)

        # Flush any remaining peaks
        for peak in peaks:
            data_collector(peak[3][0], peak[3][1], peak[3][2], peak[3][3], peak[4])

        return signals
