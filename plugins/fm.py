import threading
from math import gcd
import numpy as np
from core import Decoder, AppState, AUDIO_RATE


class FMDecoder(Decoder):
    name            = 'fm'
    key             = 'm'
    key_help        = '[/]=band'
    min_sample_rate = 250_000

    def __init__(self):
        from scipy.signal import lfilter, lfilter_zi, firwin
        import sounddevice as _sd
        self._sd         = _sd
        self._lfilter    = lfilter
        self._lfilter_zi = lfilter_zi

        # Audio LPF at 15 kHz (applied at AUDIO_RATE after resampling)
        self._lpf_b = firwin(64, 15_000 / (AUDIO_RATE / 2)).astype(np.float32)

        # 50 µs de-emphasis IIR (EU; use 75e-6 for North America)
        tau = 50e-6;  dt = 1.0 / AUDIO_RATE;  a = dt / (tau + dt)
        self._de_b = np.array([a],             dtype=np.float32)
        self._de_a = np.array([1., -(1. - a)], dtype=np.float32)

        # IF (channel-select) filter — rebuilt when fm_bw_hz or sample rate changes
        self._if_bw   = None
        self._sr      = None
        self._b_if    = None
        self._a_if    = None
        self._zi_if_i = None
        self._zi_if_q = None

        # Rational resample ratio reduced by gcd(sample_rate, AUDIO_RATE)
        self._resamp_up = 1
        self._resamp_dn = 1

        # Filter states
        self._zi_lpf = np.zeros(len(self._lpf_b) - 1, dtype=np.float32)
        self._zi_de  = np.zeros(1,                    dtype=np.float32)

        # Soft AGC
        self._peak = 0.1

        # Shared audio buffer: process() appends, PortAudio callback drains.
        self._buf_lock  = threading.Lock()
        self._audio_buf = np.zeros(0, dtype=np.float32)

        self._stream = None
        self._active = False

    def _audio_callback(self, outdata: np.ndarray, frames: int,
                        time_info, status) -> None:
        with self._buf_lock:
            have = len(self._audio_buf)
            take = min(have, frames)
            outdata[:take, 0] = self._audio_buf[:take]
            outdata[take:, 0] = 0.0
            if take:
                self._audio_buf = self._audio_buf[take:]

    def start(self, state: AppState) -> None:
        self._active = True
        with self._buf_lock:
            self._audio_buf = np.zeros(int(AUDIO_RATE * 0.20), dtype=np.float32)
        self._stream = self._sd.OutputStream(
            samplerate=AUDIO_RATE, channels=1, dtype='float32',
            latency=0.05, callback=self._audio_callback, blocksize=2048,
        )
        self._stream.start()

    def process(self, samples: np.ndarray, state: AppState) -> dict:
        from scipy.signal import resample_poly
        lf = self._lfilter
        sr = int(state.bw_hz)

        # Rebuild IF filter and resample ratio when fm_bw_hz or sample rate changes
        if state.fm_bw_hz != self._if_bw or sr != self._sr:
            from scipy.signal import cheby1
            self._if_bw = state.fm_bw_hz
            self._sr    = sr
            wn = min(state.fm_bw_hz / (sr / 2), 0.95)
            b, a = cheby1(6, 0.1, wn)
            self._b_if = b.astype(np.float64)
            self._a_if = a.astype(np.float64)
            self._zi_if_i = self._zi_if_q = None
            g = gcd(sr, AUDIO_RATE)
            self._resamp_up = AUDIO_RATE // g
            self._resamp_dn = sr // g

        # IF filter: same real LPF on I and Q → selects ±fm_bw_hz around centre
        i_in = samples.real.astype(np.float64)
        q_in = samples.imag.astype(np.float64)
        if self._zi_if_i is None:
            self._zi_if_i = self._lfilter_zi(self._b_if, self._a_if) * i_in[0]
            self._zi_if_q = self._lfilter_zi(self._b_if, self._a_if) * q_in[0]
        i_filt, self._zi_if_i = lf(self._b_if, self._a_if, i_in, zi=self._zi_if_i)
        q_filt, self._zi_if_q = lf(self._b_if, self._a_if, q_in, zi=self._zi_if_q)
        samples = i_filt + 1j * q_filt

        # FM demod: instantaneous frequency via conjugate product
        diff  = samples[1:] * np.conj(samples[:-1])
        audio = (np.angle(diff) / np.pi).astype(np.float32)

        # Resample to AUDIO_RATE — works for any rational ratio
        if self._resamp_up != self._resamp_dn:
            audio = resample_poly(audio, self._resamp_up, self._resamp_dn).astype(np.float32)

        # Audio LPF (FIR) and de-emphasis (IIR) with state
        audio, self._zi_lpf = lf(self._lpf_b, 1.0,       audio, zi=self._zi_lpf)
        audio = audio.astype(np.float32)
        audio, self._zi_de  = lf(self._de_b,  self._de_a, audio, zi=self._zi_de)
        audio = audio.astype(np.float32)

        # Soft AGC
        peak       = float(np.max(np.abs(audio)))
        self._peak = max(peak, self._peak * 0.999)
        if self._peak > 1e-6:
            audio = (audio / self._peak * 0.9).astype(np.float32)

        with self._buf_lock:
            self._audio_buf = np.concatenate([self._audio_buf, audio])
            cap = int(AUDIO_RATE * 2.0)
            if len(self._audio_buf) > cap:
                self._audio_buf = self._audio_buf[-cap:]

        return {'rms': float(np.sqrt(np.mean(audio ** 2)))}

    def stop(self) -> None:
        self._active = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._if_bw   = None
        self._sr      = None
        self._zi_if_i = None
        self._zi_if_q = None
        self._zi_lpf  = np.zeros(len(self._lpf_b) - 1, dtype=np.float32)
        self._zi_de   = np.zeros(1,                    dtype=np.float32)
        with self._buf_lock:
            self._audio_buf = np.zeros(0, dtype=np.float32)

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        from core import FM_BW_MIN, FM_BW_MAX, FM_BW_STEP
        if key == ord('['):
            state.fm_bw_hz = max(FM_BW_MIN, state.fm_bw_hz - FM_BW_STEP)
            return True
        if key == ord(']'):
            state.fm_bw_hz = min(FM_BW_MAX, state.fm_bw_hz + FM_BW_STEP)
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        return '[FM {:.0f}kHz {:3d}%] '.format(
            state.fm_bw_hz / 1000, int(result['rms'] * 100))

    def band_columns(self, state: AppState, freq_min: float,
                     freq_range: float, plot_w: int):
        l = int(max(0, (state.center_hz - state.fm_bw_hz - freq_min)
                    / freq_range * plot_w))
        r = int(min(plot_w, (state.center_hz + state.fm_bw_hz - freq_min)
                    / freq_range * plot_w))
        return (l, r) if r > l else None
