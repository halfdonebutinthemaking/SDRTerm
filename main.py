#!/usr/bin/env python3
import os, time, curses, numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from rtlsdr import RtlSdr

CENTER_HZ = 105.8e6   # 105.8 MHz (FM band)
FFT_BINS  = 4096      # larger FFT = narrower bins = lower mean noise floor

DB_MAX    = 0.0
DB_MIN    = -110.0
DB_RANGE  = DB_MAX - DB_MIN

LABEL_W   = 7          # left dB axis width: "-110 | "

REFRESH_S = 0.15       # seconds between frames (~7 fps)
N_AVG     = 8          # frames averaged per update (smooths variance)

# RTL-SDR stable sample rates in ascending order (Hz)
BW_STEPS = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]
BW_IDX   = len(BW_STEPS) - 1   # start at maximum (2.4 MHz)

WINDOW = np.hanning(FFT_BINS)


def draw_fft_matrix(screen_obj, freqs, mags_db, bw_hz):
    """
    screen_obj : curses window
    freqs      : 1-D array of frequencies (Hz), length FFT_BINS
    mags_db    : 1-D array of magnitudes in dBFS, same length as freqs
    bw_hz      : current bandwidth in Hz (shown in header)
    """
    ROWS, COLS = screen_obj.getmaxyx()
    plot_w = COLS - LABEL_W
    height = ROWS - 2           # row 0 = header, rows 1..ROWS-2 = spectrum

    # Map FFT bins → display columns, keeping the peak when multiple bins
    # fall on the same column (common when FFT_BINS >> plot_w)
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

    # ── bottom-right: BW + hint ───────────────────────────────────
    bw_label = "BW {:.3f} MHz  ESC to quit".format(bw_hz / 1e6)
    try:
        screen_obj.addstr(ROWS - 1, COLS - len(bw_label) - 1, bw_label)
    except curses.error:
        pass

    screen_obj.refresh()


def _curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    center_hz = CENTER_HZ
    bw_idx    = BW_IDX
    bw_hz     = BW_STEPS[bw_idx]

    sdr = RtlSdr()
    sdr.sample_rate = bw_hz
    sdr.center_freq = center_hz
    sdr.gain        = 'auto'

    last_draw = 0.0

    try:
        while True:
            key = stdscr.getch()

            if key == 27:                       # ESC
                break

            elif key == curses.KEY_LEFT:
                bucket_hz = bw_hz / FFT_BINS
                center_hz -= bucket_hz
                sdr.center_freq = center_hz

            elif key == curses.KEY_RIGHT:
                bucket_hz = bw_hz / FFT_BINS
                center_hz += bucket_hz
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

            # read samples and draw at the target frame rate
            now = time.monotonic()
            if now - last_draw >= REFRESH_S:
                freqs = np.linspace(center_hz - bw_hz / 2,
                                    center_hz + bw_hz / 2,
                                    FFT_BINS)

                # accumulate power over N_AVG frames then convert to dB
                samples = sdr.read_samples(FFT_BINS * N_AVG)
                frames  = samples.reshape(N_AVG, FFT_BINS)
                power   = np.zeros(FFT_BINS)
                for frame in frames:
                    fft_out = np.fft.fftshift(np.fft.fft(frame * WINDOW, FFT_BINS))
                    power  += np.abs(fft_out) ** 2
                mags_db = 10 * np.log10(power / N_AVG / FFT_BINS ** 2 + 1e-20)

                draw_fft_matrix(stdscr, freqs, mags_db, bw_hz)
                last_draw = time.monotonic()

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
