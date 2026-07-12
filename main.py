#!/usr/bin/env python3
import os, time, curses, queue, threading
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

WINDOW = np.hanning(FFT_BINS)


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

    @property
    def bw_hz(self) -> int:
        return BW_STEPS[self.bw_idx]

    @property
    def chunk_size(self) -> int:
        # When FM is active we need enough samples to fill a full refresh period
        # so the audio stream gets continuous output (2.4 MHz × 0.15 s ≈ 360 k).
        # SpectrumDecoder always truncates to FFT_BINS * N_AVG before processing.
        base = FFT_BINS * N_AVG
        if 'fm' in self.active_decoders:
            return max(base, int(self.bw_hz * REFRESH_S) + FFT_BINS)
        return base


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
        from scipy.signal import firwin, decimate as _dec, lfilter as _lf
        self._dec   = _dec
        self._lf    = _lf
        # audio LPF: keep < 15 kHz (mono FM), applied at AUDIO_RATE after decimation
        self._lpf_b = firwin(64, 15_000 / (AUDIO_RATE / 2)).astype(np.float32)
        # 50 µs de-emphasis IIR (European standard; use 75e-6 for North America)
        tau         = 50e-6
        dt          = 1.0 / AUDIO_RATE
        a           = dt / (tau + dt)
        self._de_b  = np.array([a],           dtype=np.float32)
        self._de_a  = np.array([1., -(1. - a)], dtype=np.float32)
        # running peak for soft AGC (slow decay avoids gain-pumping on silence)
        self._peak  = 0.1
        self._q     = queue.Queue(maxsize=8)
        self._stream  = None
        self._thread  = None
        self._active  = False

    def start(self, state: AppState) -> None:
        import sounddevice as sd
        self._active = True
        self._stream = sd.OutputStream(
            samplerate=AUDIO_RATE, channels=1, dtype='float32', latency='low')
        self._stream.start()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def process(self, samples: np.ndarray, state: AppState) -> dict:
        # instantaneous frequency via conjugate multiplication → FM demod
        diff  = samples[1:] * np.conj(samples[:-1])
        audio = (np.angle(diff) / np.pi).astype(np.float32)

        # decimate 2.4 MHz → 48 kHz in two stages (×10 then ×5 = ×50)
        audio = self._dec(audio, 10, zero_phase=False).astype(np.float32)
        audio = self._dec(audio,  5, zero_phase=False).astype(np.float32)

        # audio LPF then de-emphasis
        audio = self._lf(self._lpf_b, 1.0,       audio).astype(np.float32)
        audio = self._lf(self._de_b,  self._de_a, audio).astype(np.float32)

        # soft AGC: track running peak with slow decay
        peak       = float(np.max(np.abs(audio)))
        self._peak = max(peak, self._peak * 0.999)
        if self._peak > 1e-6:
            audio = (audio / self._peak * 0.9)

        try:
            self._q.put_nowait(audio)
        except queue.Full:
            pass   # drop chunk; main loop must not block on audio

        return {'rms': float(np.sqrt(np.mean(audio ** 2)))}

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _worker(self) -> None:
        while self._active or not self._q.empty():
            try:
                chunk = self._q.get(timeout=0.05)
                if self._stream and self._stream.active:
                    self._stream.write(chunk)
            except queue.Empty:
                pass


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

    for r in range(height):
        db_at = DB_MAX - (r / height) * DB_RANGE
        label = '{:4.0f} | '.format(db_at) if r in db_ticks else '     | '
        if r == arrow_row:
            label = label[:4] + '>| '
        try:
            screen_obj.addstr(r + 1, 0, label + ''.join(out[r]))
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
                fm_tag = '[FM:{:3d}%] '.format(int(fm['rms'] * 100))
                screen_obj.addstr(ROWS - 1, col, fm_tag, curses.A_BOLD)
                col += len(fm_tag)

            rhs = 'BW {}  A=auto  G=gain  I=IQ  F=freq  M=FM  Q=quit'.format(
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
def _curses_main(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    state    = AppState()
    registry = {
        'spectrum': SpectrumDecoder(),
        'fm':       FMDecoder(),
    }
    registry['spectrum'].start(state)

    sdr             = RtlSdr()
    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = state.gain_db

    last_draw = 0.0
    results: dict = {}

    try:
        while not state.quit:
            key = stdscr.getch()
            handle_keys(key, stdscr, state, registry, sdr, results)

            now = time.monotonic()
            if now - last_draw >= REFRESH_S:
                samples = sdr.read_samples(state.chunk_size)
                for name in list(state.active_decoders):
                    results[name] = registry[name].process(samples, state)
                draw(stdscr, state, results)
                last_draw = time.monotonic()

            if key == -1:
                time.sleep(0.005)
    finally:
        for name in list(state.active_decoders):
            registry[name].stop()
        sdr.close()


def main() -> None:
    devnull  = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        curses.wrapper(_curses_main)
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)


if __name__ == '__main__':
    main()
