#!/usr/bin/env python3
import os, time, curses, threading
from collections import deque
import numpy as np
from dataclasses import dataclass, field
from typing import Any, Optional

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from rtlsdr import RtlSdr

# ── constants ─────────────────────────────────────────────────────────────────
CENTER_HZ  = 105.8e6
FFT_BINS   = 4096
DB_MAX     = 0.0
DB_MIN     = -110.0
DB_RANGE   = DB_MAX - DB_MIN
LABEL_W    = 7
REFRESH_S  = 0.15
N_AVG      = 8
GAIN_MIN   = 0.0
GAIN_MAX   = 49.6
GAIN_STEP  = 0.5
GAIN_DEF   = 0.0
BW_STEPS   = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]
AUDIO_RATE = 48_000
# rtlsdr_read_sync triggers LIBUSB_ERROR_OVERFLOW above a hardware-dependent
# limit.  16 384 samples (32 768 bytes) matches one librtlsdr async-callback
# frame and is reliably safe across all sample rates on macOS.
READ_MAX   = 16_384    # samples per rtlsdr_read_sync call

WINDOW     = np.hanning(FFT_BINS)
FM_BW_MIN  = 30_000
FM_BW_MAX  = 200_000
FM_BW_STEP = 10_000


# ── helpers ───────────────────────────────────────────────────────────────────
def fmt_freq(hz):
    if abs(hz) >= 1e6:   return '{:.3f} MHz'.format(hz / 1e6)
    elif abs(hz) >= 1e3: return '{:.3f} kHz'.format(hz / 1e3)
    else:                return '{:.0f} Hz'.format(hz)


def parse_freq(s):
    s = s.strip()
    if not s:
        return None
    try:
        if   s[-1] in ('M', 'm'): return float(s[:-1]) * 1e6
        elif s[-1] in ('K', 'k'): return float(s[:-1]) * 1e3
        else:                     return float(s)
    except ValueError:
        return None


def correct_iq(samples):
    samples = samples - np.mean(samples)
    i, q  = samples.real.copy(), samples.imag.copy()
    i_pwr = np.mean(i ** 2)
    q_pwr = np.mean(q ** 2)
    if q_pwr > 0: q *= np.sqrt(i_pwr / q_pwr)
    if i_pwr > 0: q -= (np.mean(i * q) / i_pwr) * i
    return i + 1j * q



# ── AppState ──────────────────────────────────────────────────────────────────
@dataclass
class AppState:
    center_hz:       float         = CENTER_HZ
    bw_idx:          int           = len(BW_STEPS) - 1
    gain_db:         float         = GAIN_DEF
    gain_auto:       bool          = False
    iq_corr:         bool          = False
    gain_mode:       bool          = False
    freq_input:      Optional[str] = None
    quit:            bool          = False
    active_decoders: set           = field(default_factory=lambda: {'spectrum'})
    fm_bw_hz:        int           = 100_000

    @property
    def bw_hz(self) -> int:
        return BW_STEPS[self.bw_idx]



# ── Decoder base ──────────────────────────────────────────────────────────────
class Decoder:
    name:            str = ''
    min_sample_rate: int = 250_000

    def start(self, state: AppState) -> None:  pass
    def process(self, samples: np.ndarray, state: AppState) -> Any: return None
    def stop(self) -> None: pass


# ── SpectrumDecoder ───────────────────────────────────────────────────────────
class SpectrumDecoder(Decoder):
    name            = 'spectrum'
    min_sample_rate = 250_000

    def process(self, samples: np.ndarray, state: AppState) -> dict:
        s      = samples[:FFT_BINS * N_AVG]   # use only what the FFT needs
        frames = s.reshape(N_AVG, FFT_BINS)
        power  = np.zeros(FFT_BINS)
        for frame in frames:
            if state.iq_corr:
                frame = correct_iq(frame)
            fft_out = np.fft.fftshift(np.fft.fft(frame * WINDOW, FFT_BINS))
            power  += np.abs(fft_out) ** 2
        mags_db = 10 * np.log10(power / N_AVG / FFT_BINS ** 2 + 1e-20)
        freqs   = np.linspace(state.center_hz - state.bw_hz / 2,
                              state.center_hz + state.bw_hz / 2, FFT_BINS)
        return {'freqs': freqs, 'mags_db': mags_db}


# ── FMDecoder ─────────────────────────────────────────────────────────────────
class FMDecoder(Decoder):
    name            = 'fm'
    min_sample_rate = 2_400_000

    def __init__(self):
        from scipy.signal import cheby1, lfilter, lfilter_zi, firwin
        import sounddevice as _sd
        self._sd         = _sd
        self._lfilter    = lfilter
        self._lfilter_zi = lfilter_zi

        # Anti-alias IIR filters for two-stage decimation (×10 then ×5).
        # Built manually (vs scipy.signal.decimate) so we can carry zi between
        # chunks — decimate() resets to zero initial conditions every call,
        # causing an audible transient at each chunk boundary.
        b1, a1 = cheby1(8, 0.05, 0.8 / 10)
        b2, a2 = cheby1(8, 0.05, 0.8 / 5)
        self._b1 = b1.astype(np.float64);  self._a1 = a1.astype(np.float64)
        self._b2 = b2.astype(np.float64);  self._a2 = a2.astype(np.float64)

        # Audio LPF at 15 kHz (applied at AUDIO_RATE after decimation)
        self._lpf_b = firwin(64, 15_000 / (AUDIO_RATE / 2)).astype(np.float32)

        # 50 µs de-emphasis IIR (EU; use 75e-6 for North America)
        tau = 50e-6;  dt = 1.0 / AUDIO_RATE;  a = dt / (tau + dt)
        self._de_b = np.array([a],             dtype=np.float32)
        self._de_a = np.array([1., -(1. - a)], dtype=np.float32)

        # IF (channel-select) filter — real LPF applied to I and Q separately.
        # Rebuilt in process() whenever state.fm_bw_hz changes.
        self._if_bw   = None
        self._b_if    = None
        self._a_if    = None
        self._zi_if_i = None
        self._zi_if_q = None

        # Filter states — None triggers (re)initialisation on first process()
        self._zi1    = None
        self._zi2    = None
        self._zi_lpf = np.zeros(len(self._lpf_b) - 1, dtype=np.float32)
        self._zi_de  = np.zeros(1,                    dtype=np.float32)

        # Soft AGC
        self._peak = 0.1

        # Shared audio buffer: process() appends, PortAudio callback drains.
        # Protected by a plain threading.Lock — the callback holds it only for
        # a short slice/concatenate, so contention with the main thread is brief.
        self._buf_lock  = threading.Lock()
        self._audio_buf = np.zeros(0, dtype=np.float32)

        self._stream = None
        self._active = False

    # ── PortAudio callback (runs on audio-hw thread) ───────────────────────────
    def _audio_callback(self, outdata: np.ndarray, frames: int,
                        time_info, status) -> None:
        with self._buf_lock:
            have = len(self._audio_buf)
            take = min(have, frames)
            outdata[:take, 0] = self._audio_buf[:take]
            outdata[take:, 0] = 0.0           # silence on underrun
            if take:
                self._audio_buf = self._audio_buf[take:]

    def start(self, state: AppState) -> None:
        self._active = True
        # Pre-fill with one chunk of silence so the callback doesn't underrun
        # during the first blocking read_samples() call (~157 ms at 2.4 MHz).
        with self._buf_lock:
            self._audio_buf = np.zeros(int(AUDIO_RATE * 0.20), dtype=np.float32)
        self._stream = self._sd.OutputStream(
            samplerate=AUDIO_RATE, channels=1, dtype='float32',
            latency=0.05, callback=self._audio_callback, blocksize=2048,
        )
        self._stream.start()

    def process(self, samples: np.ndarray, state: AppState) -> dict:
        lf = self._lfilter

        # Rebuild IF filter when fm_bw_hz changes
        if state.fm_bw_hz != self._if_bw:
            from scipy.signal import cheby1
            self._if_bw = state.fm_bw_hz
            wn = min(state.fm_bw_hz / (state.bw_hz / 2), 0.95)
            b, a = cheby1(6, 0.1, wn)
            self._b_if = b.astype(np.float64)
            self._a_if = a.astype(np.float64)
            self._zi_if_i = self._zi_if_q = None

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
        audio = (np.angle(diff) / np.pi).astype(np.float64)

        # Decimate ×10 with persistent IIR state
        if self._zi1 is None:
            self._zi1 = self._lfilter_zi(self._b1, self._a1) * audio[0]
        audio, self._zi1 = lf(self._b1, self._a1, audio, zi=self._zi1)
        audio = audio.astype(np.float32)[::10]

        # Decimate ×5 with persistent IIR state
        a64 = audio.astype(np.float64)
        if self._zi2 is None:
            self._zi2 = self._lfilter_zi(self._b2, self._a2) * a64[0]
        a64, self._zi2 = lf(self._b2, self._a2, a64, zi=self._zi2)
        audio = a64.astype(np.float32)[::5]

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
            # Cap to 2 s to prevent unbounded growth if playback ever stalls
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
        # Reset filter states so a restart starts clean
        self._zi_if_i = None
        self._zi_if_q = None
        self._zi1    = None
        self._zi2    = None
        self._zi_lpf = np.zeros(len(self._lpf_b) - 1, dtype=np.float32)
        self._zi_de  = np.zeros(1,                    dtype=np.float32)
        with self._buf_lock:
            self._audio_buf = np.zeros(0, dtype=np.float32)


# ── decoder registry helpers ──────────────────────────────────────────────────
def _required_bw(names: set, registry: dict) -> int:
    if not names:
        return BW_STEPS[0]
    return max(registry[n].min_sample_rate for n in names)


def _nearest_bw(rate: int) -> int:
    for step in BW_STEPS:
        if step >= rate:
            return step
    return BW_STEPS[-1]


def toggle_decoder(name: str, registry: dict,
                   state: AppState, sdr: RtlSdr) -> None:
    if name in state.active_decoders:
        registry[name].stop()
        state.active_decoders.discard(name)
    else:
        needed = _required_bw(state.active_decoders | {name}, registry)
        new_bw = _nearest_bw(needed)
        if new_bw != state.bw_hz:
            state.bw_idx    = BW_STEPS.index(new_bw)
            sdr.sample_rate = new_bw
        registry[name].start(state)
        state.active_decoders.add(name)


# ── renderer ──────────────────────────────────────────────────────────────────
def draw(screen_obj: curses.window, state: AppState, results: dict) -> None:
    sp = results.get('spectrum')
    if sp is None:
        return

    freqs, mags_db = sp['freqs'], sp['mags_db']
    ROWS, COLS = screen_obj.getmaxyx()
    plot_w = COLS - LABEL_W
    height = ROWS - 2

    # build bar chart columns (peak per column when FFT_BINS >> display width)
    freq_min   = float(freqs[0])
    freq_range = float(freqs[-1] - freqs[0]) or 1.0
    col_idx    = np.round((freqs - freq_min) / freq_range * (plot_w - 1)).astype(int)
    col_db     = np.full(plot_w, DB_MIN)
    for i, db in enumerate(mags_db):
        c = int(col_idx[i])
        if 0 <= c < plot_w:
            col_db[c] = max(col_db[c], float(db))

    out = [['·'] * plot_w for _ in range(height)]
    for col, db in enumerate(col_db):
        bar = int((max(DB_MIN, min(DB_MAX, db)) - DB_MIN) / DB_RANGE * height)
        for r in range(height - bar, height):
            out[r][col] = '█'

    screen_obj.erase()

    # header
    g_str     = '[Gain: auto] ' if state.gain_auto \
                else '[Gain: {:.1f} dB] '.format(state.gain_db)
    f_lo      = fmt_freq(freqs[0])
    f_ctr     = fmt_freq((freqs[0] + freqs[-1]) / 2)
    f_hi      = fmt_freq(freqs[-1])
    ctr_col   = LABEL_W + (plot_w - len(f_ctr)) // 2
    right_col = COLS - len(f_hi)
    header    = (g_str + f_lo).ljust(ctr_col) + f_ctr
    header    = header.ljust(right_col) + f_hi
    try:
        screen_obj.addstr(0, 0, header[:COLS],
                          curses.A_BOLD if state.gain_mode else curses.A_NORMAL)
    except curses.error:
        pass

    # spectrum rows with dB tick marks and optional gain arrow
    db_ticks = {int((DB_MAX - m) / DB_RANGE * height)
                for m in range(int(DB_MAX), int(DB_MIN) - 1, -10)}
    arrow_row = None
    if state.gain_mode and not state.gain_auto:
        arrow_row = height - 1 - round(
            (state.gain_db - GAIN_MIN) / (GAIN_MAX - GAIN_MIN) * (height - 1))
        arrow_row = max(0, min(height - 1, arrow_row))

    # FM band column span for cyan highlight
    band_l = band_r = None
    if 'fm' in state.active_decoders and curses.has_colors():
        band_l = int(max(0, (state.center_hz - state.fm_bw_hz - freq_min)
                         / freq_range * plot_w))
        band_r = int(min(plot_w, (state.center_hz + state.fm_bw_hz - freq_min)
                         / freq_range * plot_w))

    for r in range(height):
        db_at = DB_MAX - (r / height) * DB_RANGE
        label = '{:4.0f} | '.format(db_at) if r in db_ticks else '     | '
        if r == arrow_row:
            label = label[:4] + '>| '
        row_str = ''.join(out[r])
        try:
            screen_obj.addstr(r + 1, 0, label)
            if band_l is not None and band_r > band_l:
                if band_l > 0:
                    screen_obj.addstr(r + 1, LABEL_W,          row_str[:band_l])
                screen_obj.addstr(r + 1, LABEL_W + band_l,
                                  row_str[band_l:band_r], curses.color_pair(1))
                if band_r < plot_w:
                    screen_obj.addstr(r + 1, LABEL_W + band_r, row_str[band_r:])
            else:
                screen_obj.addstr(r + 1, LABEL_W, row_str)
        except curses.error:
            pass

    # footer
    fm = results.get('fm')
    try:
        if state.freq_input is not None:
            prompt = 'Freq: {}_'.format(state.freq_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), '  RET=ok  ESC=cancel')
        else:
            iq_tag = '[IQ:ON] ' if state.iq_corr else '[IQ:off]'
            screen_obj.addstr(ROWS - 1, 0, iq_tag,
                              curses.A_BOLD if state.iq_corr else curses.A_DIM)
            col = 9
            if fm is not None:
                fm_tag = '[FM {:.0f}kHz {:3d}%] '.format(
                    state.fm_bw_hz / 1000, int(fm['rms'] * 100))
                screen_obj.addstr(ROWS - 1, col, fm_tag, curses.A_BOLD)
                col += len(fm_tag)

            rhs = 'BW {}  A=auto  G=gain  I=IQ  F=freq  M=FM  [/]=band  Q=quit'.format(
                fmt_freq(state.bw_hz))
            screen_obj.addstr(ROWS - 1, COLS - len(rhs) - 1, rhs)
    except curses.error:
        pass

    screen_obj.refresh()


# ── key handler ───────────────────────────────────────────────────────────────
def handle_keys(key: int, stdscr, state: AppState,
                registry: dict, sdr: RtlSdr, results: dict) -> None:

    def redraw():
        if results:
            draw(stdscr, state, results)

    if state.freq_input is not None:
        if key == 27:
            state.freq_input = None
        elif key in (10, 13, curses.KEY_ENTER):
            parsed = parse_freq(state.freq_input)
            if parsed is not None:
                state.center_hz = parsed
                sdr.center_freq = state.center_hz
            state.freq_input = None
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            state.freq_input = state.freq_input[:-1]
        elif 32 <= key <= 126 and chr(key) in '0123456789.kKmM':
            state.freq_input += chr(key)
        redraw()

    elif state.gain_mode:
        if key in (ord('g'), ord('G')):
            state.gain_mode = False
            redraw()
        elif key == curses.KEY_UP:
            state.gain_db = min(GAIN_MAX, state.gain_db + GAIN_STEP)
            sdr.gain = state.gain_db
            redraw()
        elif key == curses.KEY_DOWN:
            state.gain_db = max(GAIN_MIN, state.gain_db - GAIN_STEP)
            sdr.gain = state.gain_db
            redraw()

    else:
        if key in (ord('q'), ord('Q')):
            state.quit = True

        elif key in (ord('g'), ord('G')):
            state.gain_mode = True
            if state.gain_auto:
                state.gain_auto = False
                sdr.gain = state.gain_db
            redraw()

        elif key in (ord('a'), ord('A')):
            state.gain_auto = not state.gain_auto
            sdr.gain = 'auto' if state.gain_auto else state.gain_db
            redraw()

        elif key in (ord('f'), ord('F')):
            state.freq_input = ''
            redraw()

        elif key in (ord('i'), ord('I')):
            state.iq_corr = not state.iq_corr

        elif key in (ord('m'), ord('M')):
            toggle_decoder('fm', registry, state, sdr)
            redraw()

        elif key == ord('['):
            state.fm_bw_hz = max(FM_BW_MIN, state.fm_bw_hz - FM_BW_STEP)
            redraw()

        elif key == ord(']'):
            state.fm_bw_hz = min(FM_BW_MAX, state.fm_bw_hz + FM_BW_STEP)
            redraw()

        elif key == curses.KEY_LEFT:
            state.center_hz -= state.bw_hz / FFT_BINS
            sdr.center_freq  = state.center_hz

        elif key == curses.KEY_RIGHT:
            state.center_hz += state.bw_hz / FFT_BINS
            sdr.center_freq  = state.center_hz

        elif key == curses.KEY_UP:
            if state.bw_idx < len(BW_STEPS) - 1:
                state.bw_idx   += 1
                sdr.sample_rate = state.bw_hz

        elif key == curses.KEY_DOWN:
            # refuse if reducing would drop below what active decoders need
            min_bw = _required_bw(state.active_decoders, registry)
            if state.bw_idx > 0 and BW_STEPS[state.bw_idx - 1] >= min_bw:
                state.bw_idx   -= 1
                sdr.sample_rate = state.bw_hz


# ── main curses loop ──────────────────────────────────────────────────────────
def _curses_main(stdscr: curses.window, sdr: RtlSdr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)

    state    = AppState()
    registry = {
        'spectrum': SpectrumDecoder(),
        'fm':       FMDecoder(),
    }
    registry['spectrum'].start(state)

    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = state.gain_db

    SPEC_NEED = FFT_BINS * N_AVG
    iq_deque  = deque(maxlen=64)
    results: dict = {}
    stop_evt  = threading.Event()

    # Async callback — called by librtlsdr's USB thread with no gaps between
    # transfers (multiple overlapping USB buffers keep the stream continuous).
    def _sdr_cb(samples, _ctx):
        if stop_evt.is_set():
            return
        if 'fm' in state.active_decoders:
            results['fm'] = registry['fm'].process(samples, state)
        iq_deque.append(samples)

    # Each call to read_samples_async blocks until cancel_read_async() is called,
    # so it runs in its own thread.  Restart on sample-rate changes.
    reader: list = [None]

    def _start_reader():
        if reader[0] and reader[0].is_alive():
            sdr.cancel_read_async()
            reader[0].join(timeout=1.0)
        def _run():
            sdr.read_samples_async(_sdr_cb, num_samples=READ_MAX)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        reader[0] = t

    _start_reader()

    spec_chunks: list = []
    spec_count  = 0
    last_draw   = 0.0
    last_bw     = state.bw_hz

    try:
        while not state.quit:
            key = stdscr.getch()
            handle_keys(key, stdscr, state, registry, sdr, results)

            # BW changed (FM toggle, arrow keys) → restart async stream
            if state.bw_hz != last_bw:
                last_bw = state.bw_hz
                spec_chunks.clear()
                spec_count = 0
                _start_reader()

            while iq_deque:
                try:
                    c = iq_deque.popleft()
                    spec_chunks.append(c)
                    spec_count += len(c)
                except IndexError:
                    break

            now = time.monotonic()
            if now - last_draw >= REFRESH_S and spec_count >= SPEC_NEED:
                samples = np.concatenate(spec_chunks)
                results['spectrum'] = registry['spectrum'].process(
                    samples[:SPEC_NEED], state)
                spec_chunks.clear()
                spec_count = 0
                draw(stdscr, state, results)
                last_draw = now
            elif key == -1:
                time.sleep(0.002)

    finally:
        stop_evt.set()
        sdr.cancel_read_async()
        if reader[0]:
            reader[0].join(timeout=2.0)
        for name in list(state.active_decoders):
            registry[name].stop()
        sdr.close()


def main() -> None:
    # Open the SDR before entering curses so a device conflict gives a readable
    # error in the normal terminal rather than a silent blank screen.
    sdr = RtlSdr()

    devnull  = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        curses.wrapper(lambda stdscr: _curses_main(stdscr, sdr))
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)


if __name__ == '__main__':
    main()
