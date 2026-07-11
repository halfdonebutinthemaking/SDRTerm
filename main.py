#!/usr/bin/env python3
import os, time, curses, numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from rtlsdr import RtlSdr

CENTER_HZ = 105.8e6   # 105.8 MHz (FM band)
BW_HZ     = 2.4e6     # 2.4 MHz sample rate / bandwidth
FFT_BINS  = 512

DB_MAX    = 0.0
DB_MIN    = -110.0
DB_RANGE  = DB_MAX - DB_MIN

LABEL_W   = 7          # left dB axis width: "-110 | "

WINDOW    = np.hanning(FFT_BINS)   # Hann window to reduce spectral leakage


def draw_fft_matrix(screen_obj, freqs, mags_db):
    """
    screen_obj : curses window
    freqs      : 1-D array of frequencies (Hz)
    mags_db    : 1-D array of magnitudes in dBFS, same length as freqs
    """
    ROWS, COLS = screen_obj.getmaxyx()
    plot_w = COLS - LABEL_W
    height = ROWS - 2           # row 0 = header, rows 1..ROWS-2 = spectrum

    out = [['·'] * plot_w for _ in range(height)]

    freq_min   = float(freqs[0])
    freq_range = float(freqs[-1] - freqs[0]) or 1.0
    col_idx    = np.round((freqs - freq_min) / freq_range * (plot_w - 1)).astype(int)

    for i, db in enumerate(mags_db):
        col = int(col_idx[i])
        if not (0 <= col < plot_w):
            continue
        db_clamped = max(DB_MIN, min(DB_MAX, float(db)))
        bar_height = int((db_clamped - DB_MIN) / DB_RANGE * height)
        for r in range(height - bar_height, height):
            out[r][col] = '█'

    screen_obj.clear()

    # ── header: lo left | center middle | hi right ───────────────
    f_lo  = "{:.3f} MHz".format(freqs[0] / 1e6)
    f_ctr = "{:.3f} MHz".format((freqs[0] + freqs[-1]) / 2e6)
    f_hi  = "{:.3f} MHz".format(freqs[-1] / 1e6)

    ctr_col   = LABEL_W + (plot_w - len(f_ctr)) // 2
    right_col = COLS - len(f_hi)

    header = (" " * LABEL_W + f_lo).ljust(ctr_col) + f_ctr
    header = header.ljust(right_col) + f_hi

    try:
        screen_obj.addstr(0, 0, header[:COLS], curses.A_BOLD)
    except curses.error:
        pass

    # ── spectrum rows with dB tick marks every 10 dB ─────────────
    db_ticks = set()
    for db_mark in range(int(DB_MAX), int(DB_MIN) - 1, -10):
        row = int((DB_MAX - db_mark) / DB_RANGE * height)
        if 0 <= row < height:
            db_ticks.add(row)

    for r in range(height):
        db_at_row = DB_MAX - (r / height) * DB_RANGE
        label = "{:4.0f} | ".format(db_at_row) if r in db_ticks else "     | "
        try:
            screen_obj.addstr(r + 1, 0, label + ''.join(out[r]))
        except curses.error:
            pass

    # ── bottom-right hint ─────────────────────────────────────────
    hint = "ESC to quit"
    try:
        screen_obj.addstr(ROWS - 1, COLS - len(hint) - 1, hint)
    except curses.error:
        pass

    screen_obj.refresh()


def _curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    center_hz = CENTER_HZ
    bucket_hz = BW_HZ / FFT_BINS

    sdr = RtlSdr()
    sdr.sample_rate = BW_HZ
    sdr.center_freq = center_hz
    sdr.gain        = 'auto'

    try:
        while True:
            key = stdscr.getch()
            if key == 27:                   # ESC
                break
            elif key == curses.KEY_LEFT:
                center_hz -= bucket_hz
                sdr.center_freq = center_hz
            elif key == curses.KEY_RIGHT:
                center_hz += bucket_hz
                sdr.center_freq = center_hz

            freqs = np.linspace(center_hz - BW_HZ / 2,
                                center_hz + BW_HZ / 2,
                                FFT_BINS)

            samples = sdr.read_samples(FFT_BINS)
            fft_out = np.fft.fftshift(np.fft.fft(samples * WINDOW, FFT_BINS))
            mags_db = 20 * np.log10(np.abs(fft_out) / FFT_BINS + 1e-12)

            draw_fft_matrix(stdscr, freqs, mags_db)
    finally:
        sdr.close()


def main():
    curses.wrapper(_curses_main)


if __name__ == '__main__':
    main()
