#!/usr/bin/env python3
import os, time, curses, numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from rtlsdr import RtlSdr

CENTER_HZ  = 105.8e6   # 105.8 MHz (FM band)
FFT_BINS   = 4096      # larger FFT = narrower bins = lower mean noise floor

DB_MAX     = 0.0
DB_MIN     = -110.0
DB_RANGE   = DB_MAX - DB_MIN

LABEL_W    = 7          # dB axis width: "-110 | "

REFRESH_S  = 0.15       # seconds between frames (~7 fps)
N_AVG      = 8          # FFT frames averaged per update

GAIN_MIN   = 0.0
GAIN_MAX   = 49.6
GAIN_STEP  = 0.5
GAIN_DEF   = 0.0        # starting manual gain (dB)

# RTL-SDR stable sample rates in ascending order (Hz)
BW_STEPS = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]
BW_IDX   = len(BW_STEPS) - 1

WINDOW = np.hanning(FFT_BINS)


def fmt_freq(hz):
    """Format a frequency in the most readable unit without losing precision."""
    if abs(hz) >= 1e6:
        return "{:.3f} MHz".format(hz / 1e6)
    elif abs(hz) >= 1e3:
        return "{:.3f} kHz".format(hz / 1e3)
    else:
        return "{:.0f} Hz".format(hz)


def parse_freq(s):
    """Parse Hz / kHz (k) / MHz (M) string → float Hz, or None on error."""
    s = s.strip()
    if not s:
        return None
    try:
        if s[-1] in ('M', 'm'):
            return float(s[:-1]) * 1e6
        elif s[-1] in ('K', 'k'):
            return float(s[:-1]) * 1e3
        else:
            return float(s)
    except ValueError:
        return None


def correct_iq(samples):
    """Software IQ correction:
    1. DC offset removal  (kills the centre-frequency spike)
    2. Amplitude balance  (equalises I/Q power → reduces mirror images)
    3. Phase balance      (orthogonalises I/Q → further reduces mirrors)
    """
    samples = samples - np.mean(samples)
    i, q  = samples.real.copy(), samples.imag.copy()
    i_pwr = np.mean(i ** 2)
    q_pwr = np.mean(q ** 2)
    if q_pwr > 0:
        q *= np.sqrt(i_pwr / q_pwr)
    if i_pwr > 0:
        q -= (np.mean(i * q) / i_pwr) * i
    return i + 1j * q


def draw_fft_matrix(screen_obj, freqs, mags_db, bw_hz,
                    iq_corr=False,
                    gain_mode=False, gain_db=GAIN_DEF, gain_auto=True,
                    freq_input=None):
    """
    screen_obj : curses window
    freqs      : 1-D array of frequencies (Hz), length FFT_BINS
    mags_db    : 1-D array of magnitudes in dBFS, same length as freqs
    bw_hz      : current bandwidth in Hz
    iq_corr    : IQ correction toggle state
    gain_mode  : True while gain control is active (arrow on axis, bold header)
    gain_db    : current manual gain value in dB
    gain_auto  : True if SDR is in auto-gain mode
    freq_input : if not None, show frequency edit prompt in the footer
    """
    ROWS, COLS = screen_obj.getmaxyx()
    plot_w = COLS - LABEL_W
    height = ROWS - 2       # row 0 = header, rows 1..ROWS-2 = spectrum

    # ── build spectrum columns (peak per column) ──────────────────
    freq_min   = float(freqs[0])
    freq_range = float(freqs[-1] - freqs[0]) or 1.0
    col_idx    = np.round((freqs - freq_min) / freq_range * (plot_w - 1)).astype(int)

    col_db = np.full(plot_w, DB_MIN)
    for i, db in enumerate(mags_db):
        col = int(col_idx[i])
        if 0 <= col < plot_w:
            col_db[col] = max(col_db[col], float(db))

    out = [['·'] * plot_w for _ in range(height)]
    for col, db in enumerate(col_db):
        db_clamped = max(DB_MIN, min(DB_MAX, db))
        bar_height = int((db_clamped - DB_MIN) / DB_RANGE * height)
        for r in range(height - bar_height, height):
            out[r][col] = '█'

    screen_obj.erase()

    # ── header: [Gain: X] f_lo ·········· f_ctr ·········· f_hi ──
    g_str    = "[Gain: auto] " if gain_auto else "[Gain: {:.1f} dB] ".format(gain_db)
    f_lo     = fmt_freq(freqs[0])
    f_ctr    = fmt_freq((freqs[0] + freqs[-1]) / 2)
    f_hi     = fmt_freq(freqs[-1])

    ctr_col   = LABEL_W + (plot_w - len(f_ctr)) // 2
    right_col = COLS - len(f_hi)

    header = (g_str + f_lo).ljust(ctr_col) + f_ctr
    header = header.ljust(right_col) + f_hi

    g_attr = curses.A_BOLD if gain_mode else curses.A_NORMAL
    try:
        screen_obj.addstr(0, 0, header[:COLS], g_attr)
    except curses.error:
        pass

    # ── spectrum rows with dB ticks and optional gain arrow ───────
    db_ticks = set()
    for db_mark in range(int(DB_MAX), int(DB_MIN) - 1, -10):
        row = int((DB_MAX - db_mark) / DB_RANGE * height)
        if 0 <= row < height:
            db_ticks.add(row)

    # Gain arrow: top row = GAIN_MAX, bottom row = GAIN_MIN
    arrow_row = None
    if gain_mode and not gain_auto:
        arrow_row = height - 1 - round(
            (gain_db - GAIN_MIN) / (GAIN_MAX - GAIN_MIN) * (height - 1))
        arrow_row = max(0, min(height - 1, arrow_row))

    for r in range(height):
        db_at_row = DB_MAX - (r / height) * DB_RANGE
        label = "{:4.0f} | ".format(db_at_row) if r in db_ticks else "     | "
        if r == arrow_row:
            label = label[:4] + ">| "
        try:
            screen_obj.addstr(r + 1, 0, label + ''.join(out[r]))
        except curses.error:
            pass

    # ── bottom row ────────────────────────────────────────────────
    try:
        if freq_input is not None:
            prompt = "Freq: {}_".format(freq_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), "  RET=ok  ESC=cancel")
        else:
            iq_tag  = "[IQ:ON] " if iq_corr else "[IQ:off]"
            iq_attr = curses.A_BOLD if iq_corr else curses.A_DIM
            screen_obj.addstr(ROWS - 1, 0, iq_tag, iq_attr)

            rhs = "BW {}  A=auto  G=gain  I=IQ  F=freq  Q=quit".format(fmt_freq(bw_hz))
            screen_obj.addstr(ROWS - 1, COLS - len(rhs) - 1, rhs)
    except curses.error:
        pass

    screen_obj.refresh()


def _curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    center_hz  = CENTER_HZ
    bw_idx     = BW_IDX
    bw_hz      = BW_STEPS[bw_idx]
    iq_corr    = False
    gain_mode  = False
    gain_db    = GAIN_DEF   # manual gain value; applied when gain_mode entered
    gain_auto  = False      # start in manual gain mode
    freq_input = None
    last_mags  = None
    last_freqs = np.linspace(center_hz - bw_hz / 2,
                             center_hz + bw_hz / 2, FFT_BINS)

    sdr = RtlSdr()
    sdr.sample_rate = bw_hz
    sdr.center_freq = center_hz
    sdr.gain        = gain_db

    last_draw = 0.0

    def redraw():
        if last_mags is not None:
            draw_fft_matrix(stdscr, last_freqs, last_mags, bw_hz,
                            iq_corr, gain_mode, gain_db, gain_auto,
                            freq_input)

    try:
        while True:
            key = stdscr.getch()

            if freq_input is not None:
                # ── frequency edit mode ───────────────────────────
                if key == 27:
                    freq_input = None
                elif key in (10, 13, curses.KEY_ENTER):
                    parsed = parse_freq(freq_input)
                    if parsed is not None:
                        center_hz = parsed
                        sdr.center_freq = center_hz
                        last_freqs = np.linspace(center_hz - bw_hz / 2,
                                                 center_hz + bw_hz / 2,
                                                 FFT_BINS)
                    freq_input = None
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    freq_input = freq_input[:-1]
                elif 32 <= key <= 126 and chr(key) in '0123456789.kKmM':
                    freq_input += chr(key)
                redraw()

            elif gain_mode:
                # ── gain control mode — left/right adjust gain ────
                if key == ord('g') or key == ord('G'):
                    gain_mode = False
                    redraw()

                elif key == curses.KEY_UP:
                    gain_db = min(GAIN_MAX, gain_db + GAIN_STEP)
                    sdr.gain = gain_db
                    redraw()

                elif key == curses.KEY_DOWN:
                    gain_db = max(GAIN_MIN, gain_db - GAIN_STEP)
                    sdr.gain = gain_db
                    redraw()

            else:
                # ── normal mode ───────────────────────────────────
                if key == ord('q') or key == ord('Q'):
                    break

                elif key == ord('g') or key == ord('G'):
                    gain_mode = True
                    if gain_auto:
                        gain_auto = False
                        sdr.gain  = gain_db
                    redraw()

                elif key == ord('f') or key == ord('F'):
                    freq_input = ""
                    redraw()

                elif key == ord('a') or key == ord('A'):
                    gain_auto = not gain_auto
                    if gain_auto:
                        sdr.gain = 'auto'
                    else:
                        sdr.gain = gain_db
                    redraw()

                elif key == ord('i') or key == ord('I'):
                    iq_corr = not iq_corr

                elif key == curses.KEY_LEFT:
                    center_hz -= bw_hz / FFT_BINS
                    sdr.center_freq = center_hz

                elif key == curses.KEY_RIGHT:
                    center_hz += bw_hz / FFT_BINS
                    sdr.center_freq = center_hz

                elif key == curses.KEY_UP:
                    if bw_idx < len(BW_STEPS) - 1:
                        bw_idx += 1
                        bw_hz = BW_STEPS[bw_idx]
                        sdr.sample_rate = bw_hz

                elif key == curses.KEY_DOWN:
                    if bw_idx > 0:
                        bw_idx -= 1
                        bw_hz = BW_STEPS[bw_idx]
                        sdr.sample_rate = bw_hz

            # ── read + draw at target frame rate ──────────────────
            now = time.monotonic()
            if now - last_draw >= REFRESH_S:
                last_freqs = np.linspace(center_hz - bw_hz / 2,
                                         center_hz + bw_hz / 2, FFT_BINS)
                samples = sdr.read_samples(FFT_BINS * N_AVG)
                frames  = samples.reshape(N_AVG, FFT_BINS)
                power   = np.zeros(FFT_BINS)
                for frame in frames:
                    if iq_corr:
                        frame = correct_iq(frame)
                    fft_out = np.fft.fftshift(
                        np.fft.fft(frame * WINDOW, FFT_BINS))
                    power += np.abs(fft_out) ** 2
                last_mags = 10 * np.log10(
                    power / N_AVG / FFT_BINS ** 2 + 1e-20)
                draw_fft_matrix(stdscr, last_freqs, last_mags, bw_hz,
                                iq_corr, gain_mode, gain_db, gain_auto)
                last_draw = time.monotonic()

            if key == -1:
                time.sleep(0.005)

    finally:
        sdr.close()


def main():
    # librtlsdr prints "Exact sample rate is: X Hz" to C-level stderr on every
    # sample-rate change. Redirect fd 2 to /dev/null for the curses session so
    # those writes don't corrupt the terminal, then restore on exit.
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
