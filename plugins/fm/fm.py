import curses
import wave
import threading
from math import gcd
import numpy as np
from core import Decoder, AppState, AUDIO_RATE, LABEL_W


class FMDecoder(Decoder):
    name            = 'fm'
    key             = 'm'
    key_help        = '[/]=band'
    min_sample_rate = 250_000
    priority        = 10   # run before RDS and record so audio is never delayed

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

        # DC-blocker — first-order IIR high-pass at ~30 Hz corner.  Kept
        # per-instance so state carries across chunks (unlike a naive
        # per-chunk mean-subtract, which introduces a step at every
        # chunk boundary as the DC estimate drifts, showing up as
        # audible crackle at ~chunk-rate Hz).  Rebuilt when sample rate
        # changes so the corner stays constant regardless of bw_hz.
        self._dc_b    = None
        self._dc_a    = None
        self._zi_dc_i = None
        self._zi_dc_q = None

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
        if not self._active:
            outdata[:] = 0.0
            return
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

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        from scipy.signal import resample_poly
        lf = self._lfilter
        sr = int(state.bw_hz)

        # DC-blocker — first-order IIR high-pass at ~30 Hz corner,
        # applied to I and Q separately with state preserved across
        # chunks.  On direct-conversion radios (HackRF) LO leakage
        # puts a big spike at DC that dominates the arctangent phase
        # demod; superhet radios (RTL-SDR) don't have the spike but
        # a low-Hz HP is a harmless no-op for them either way (FM is
        # phase-encoded, blind to DC).
        #
        # Why not just samples - samples.mean(): the per-chunk mean
        # drifts a little between chunks as the HackRF's actual DC
        # offset shifts with gain-settling / thermal effects, so
        # mean-subtract leaves a small step at every chunk boundary —
        # audible as ~8 Hz crackle at 2 MSPS with 262 k chunks.  A
        # stateful HP is continuous across chunks with no boundary
        # artefact.
        if self._dc_b is None or self._sr != sr:
            _fc = 30.0                                          # Hz
            _R  = float(np.exp(-2 * np.pi * _fc / sr))          # → ~0.9999 at 2 MSPS
            self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
            self._dc_a = np.array([1.0, -_R],  dtype=np.float64)
            self._zi_dc_i = None
            self._zi_dc_q = None
        i_raw = samples.real.astype(np.float64)
        q_raw = samples.imag.astype(np.float64)
        if self._zi_dc_i is None:
            # Seed at the first sample's value so the very first chunk
            # doesn't start with a huge transient from 0.
            self._zi_dc_i = self._lfilter_zi(self._dc_b, self._dc_a) * i_raw[0]
            self._zi_dc_q = self._lfilter_zi(self._dc_b, self._dc_a) * q_raw[0]
        i_dc, self._zi_dc_i = lf(self._dc_b, self._dc_a, i_raw, zi=self._zi_dc_i)
        q_dc, self._zi_dc_q = lf(self._dc_b, self._dc_a, q_raw, zi=self._zi_dc_q)
        samples = i_dc + 1j * q_dc

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

        return {'rms': float(np.sqrt(np.mean(audio ** 2))), 'audio': audio}

    def stop(self) -> None:
        self._active = False   # callback sees this and returns without the lock
        if self._stream:
            self._stream.abort()   # don't wait for buffer drain — avoids main-thread freeze
            self._stream.close()
            self._stream = None
        self._if_bw   = None
        self._sr      = None
        self._zi_if_i = None
        self._zi_if_q = None
        self._zi_dc_i = None
        self._zi_dc_q = None
        self._dc_b    = None
        self._dc_a    = None
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

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        if not curses.has_colors():
            return
        col_l = int(max(0, (state.center_hz - state.fm_bw_hz - freq_min)
                        / freq_range * plot_w))
        col_r = int(min(plot_w, (state.center_hz + state.fm_bw_hz - freq_min)
                        / freq_range * plot_w))
        if col_r <= col_l:
            return
        attr = curses.color_pair(1)
        n    = col_r - col_l
        for r in range(height):
            try:
                screen_obj.chgat(r + 1, LABEL_W + col_l, n, attr)
            except curses.error:
                pass

    # ── recording hooks ───────────────────────────────────────────────────────
    record_ext = 'wav'

    def record_open(self, path: str):
        wf = wave.open(path, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)    # int16
        wf.setframerate(AUDIO_RATE)
        return wf

    def record_write(self, handle, result: dict) -> int:
        audio = result.get('audio')
        if audio is None:
            return 0
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        handle.writeframes(pcm.tobytes())
        return pcm.nbytes

    def record_close(self, handle) -> None:
        handle.close()
