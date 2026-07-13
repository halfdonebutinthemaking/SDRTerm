#!/usr/bin/env python3
import os, time, curses, threading
from collections import deque
import numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from core import (
    AppState, Device, fmt_freq, parse_freq,
    BW_STEPS, FFT_BINS, N_AVG, DB_MAX, DB_MIN, DB_RANGE,
    LABEL_W, REFRESH_S, GAIN_MIN, GAIN_MAX, GAIN_STEP, READ_MAX,
    _required_bw, _nearest_bw, toggle_decoder,
)
from devices import open_first_device
from plugins import load_plugins


# ── plugin menu overlay ───────────────────────────────────────────────────────
def _draw_plugin_menu(screen_obj: curses.window, state: AppState,
                      all_plugins: list, ROWS: int, COLS: int) -> None:
    if not all_plugins:
        return
    w  = min(COLS - 4, max(36, max(len(p.name) for p in all_plugins) + 20))
    h  = len(all_plugins) + 4
    y0 = max(0, (ROWS - h) // 2)
    x0 = max(0, (COLS - w) // 2)
    try:
        for r in range(h):
            screen_obj.addstr(y0 + r, x0, ' ' * w)
        screen_obj.addstr(y0,     x0 + 2, ' Plugins ', curses.A_BOLD)
        screen_obj.addstr(y0 + 1, x0 + 2, '─' * (w - 4))
        for i, plugin in enumerate(all_plugins):
            enabled = plugin.name in state.menu_active
            tick    = 'x' if enabled else ' '
            attr    = curses.A_REVERSE if i == state.menu_cursor else curses.A_NORMAL
            label   = '[{}]  {:14}  key: {}'.format(
                tick, plugin.name, plugin.key or '—')
            screen_obj.addstr(y0 + 2 + i, x0 + 2, label[:w - 4].ljust(w - 4), attr)
        hint = ' spc=toggle   ret=apply   esc=cancel '
        screen_obj.addstr(y0 + h - 1, x0 + 2, hint[:w - 4], curses.A_DIM)
    except curses.error:
        pass


# ── renderer ──────────────────────────────────────────────────────────────────
def draw(screen_obj: curses.window, state: AppState, results: dict,
         registry: dict, tab_plugins: list, all_plugins: list) -> None:
    sp = results.get('spectrum')
    if sp is None:
        return

    freqs, mags_db = sp['freqs'], sp['mags_db']
    ROWS, COLS = screen_obj.getmaxyx()
    plot_w = COLS - LABEL_W
    height = ROWS - 2

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

    # spectrum rows
    db_ticks = {int((DB_MAX - m) / DB_RANGE * height)
                for m in range(int(DB_MAX), int(DB_MIN) - 1, -10)}
    arrow_row = None
    if state.gain_mode and not state.gain_auto:
        arrow_row = height - 1 - round(
            (state.gain_db - GAIN_MIN) / (GAIN_MAX - GAIN_MIN) * (height - 1))
        arrow_row = max(0, min(height - 1, arrow_row))

    # ask active plugins for a band highlight (first match wins)
    band_l = band_r = None
    if curses.has_colors():
        for name in state.active_decoders:
            plugin = registry.get(name)
            if plugin is None:
                continue
            cols = plugin.band_columns(state, freq_min, freq_range, plot_w)
            if cols:
                band_l, band_r = cols
                break

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
    try:
        if state.freq_input is not None:
            prompt = 'Freq: {}_'.format(state.freq_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')

        elif state.tab_idx == 0:
            # core tab
            screen_obj.addstr(ROWS - 1, 0, '[core]', curses.A_BOLD)
            col = 7
            iq_tag = '[IQ:ON]' if state.iq_corr else '[IQ:off]'
            screen_obj.addstr(ROWS - 1, col, iq_tag,
                              curses.A_BOLD if state.iq_corr else curses.A_DIM)
            col += len(iq_tag) + 1
            plugin_toggles = '  '.join(
                '{}={}'.format(p.key, p.name) for p in all_plugins if p.key)
            rhs_parts = ['a=auto', 'g=gain', 'i=iq', 'p=plugins']
            if plugin_toggles:
                rhs_parts.append(plugin_toggles)
            rhs_parts += ['f=freq', 'q=quit']
            rhs = '  '.join(rhs_parts)
            screen_obj.addstr(ROWS - 1, COLS - len(rhs) - 1, rhs)

        else:
            # plugin tab
            plugin = tab_plugins[state.tab_idx - 1]
            ctx = '[{}]'.format(plugin.name)
            screen_obj.addstr(ROWS - 1, 0, ctx, curses.A_BOLD)
            col = len(ctx) + 1
            result = results.get(plugin.name)
            if result:
                text = plugin.status_text(state, result)
                if text:
                    screen_obj.addstr(ROWS - 1, col, text, curses.A_BOLD)
                    col += len(text)
            parts = []
            if plugin.key:
                parts.append('{}=toggle'.format(plugin.key))
            if plugin.key_help:
                parts.append(plugin.key_help)
            parts += ['f=freq', 'q=quit']
            rhs = '  '.join(parts)
            screen_obj.addstr(ROWS - 1, COLS - len(rhs) - 1, rhs)

    except curses.error:
        pass

    # plugin menu drawn last so it overlays everything
    if state.menu_active is not None:
        _draw_plugin_menu(screen_obj, state, all_plugins, ROWS, COLS)

    screen_obj.refresh()


# ── key handler ───────────────────────────────────────────────────────────────
def handle_keys(key: int, stdscr, state: AppState, registry: dict,
                tab_plugins: list, all_plugins: list,
                sdr: Device, results: dict) -> None:

    def redraw():
        if results:
            cur_tabs = [p for p in all_plugins if p.name in state.active_decoders]
            state.tab_idx = min(state.tab_idx, len(cur_tabs))
            draw(stdscr, state, results, registry, cur_tabs, all_plugins)

    # ── plugin menu modal ─────────────────────────────────────────────────────
    if state.menu_active is not None:
        if key == 27:                             # esc — cancel
            state.menu_active = None
        elif key in (10, 13, curses.KEY_ENTER):   # return — apply
            for p in all_plugins:
                currently = p.name in state.active_decoders
                want      = p.name in state.menu_active
                if currently != want:
                    toggle_decoder(p.name, registry, state, sdr)
            state.menu_active = None
        elif key == ord(' '):
            if 0 <= state.menu_cursor < len(all_plugins):
                name = all_plugins[state.menu_cursor].name
                state.menu_active ^= {name}       # toggle membership
        elif key == curses.KEY_UP:
            state.menu_cursor = max(0, state.menu_cursor - 1)
        elif key == curses.KEY_DOWN:
            state.menu_cursor = min(len(all_plugins) - 1, state.menu_cursor + 1)
        redraw()
        return

    # ── freq input modal ──────────────────────────────────────────────────────
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
        return

    # ── always-reserved ───────────────────────────────────────────────────────
    if key in (ord('q'), ord('Q')):
        state.quit = True
        return

    if key in (ord('f'), ord('F')):
        state.freq_input = ''
        redraw()
        return

    if key == 9:                                  # tab — cycle context
        n_tabs = 1 + len(tab_plugins)
        state.tab_idx = (state.tab_idx + 1) % n_tabs
        redraw()
        return

    # ── gain mode (sub-mode, only reachable from core tab) ───────────────────
    if state.gain_mode:
        if key in (ord('g'), ord('G')):
            state.gain_mode = False
        elif key == curses.KEY_UP:
            state.gain_db = min(GAIN_MAX, state.gain_db + GAIN_STEP)
            sdr.gain = state.gain_db
        elif key == curses.KEY_DOWN:
            state.gain_db = max(GAIN_MIN, state.gain_db - GAIN_STEP)
            sdr.gain = state.gain_db
        redraw()
        return

    # ── core tab ──────────────────────────────────────────────────────────────
    if state.tab_idx == 0:
        if key in (ord('a'), ord('A')):
            state.gain_auto = not state.gain_auto
            sdr.gain = 'auto' if state.gain_auto else state.gain_db
        elif key in (ord('g'), ord('G')):
            state.gain_mode = True
            if state.gain_auto:
                state.gain_auto = False
                sdr.gain = state.gain_db
        elif key in (ord('i'), ord('I')):
            state.iq_corr = not state.iq_corr
        elif key in (ord('p'), ord('P')):
            state.menu_active = state.active_decoders - {'spectrum'}
            state.menu_cursor = 0
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
            min_bw = _required_bw(state.active_decoders, registry)
            if state.bw_idx > 0 and BW_STEPS[state.bw_idx - 1] >= min_bw:
                state.bw_idx   -= 1
                sdr.sample_rate = state.bw_hz
        else:
            # per-plugin quick-toggle keys
            for name, plugin in registry.items():
                if plugin.key and key == ord(plugin.key):
                    toggle_decoder(name, registry, state, sdr)
                    break
        redraw()
        return

    # ── plugin tab ────────────────────────────────────────────────────────────
    plugin = tab_plugins[state.tab_idx - 1]
    if plugin.key and key == ord(plugin.key):     # toggle key → disable & back to core
        toggle_decoder(plugin.name, registry, state, sdr)
    else:
        plugin.handle_key(key, state, sdr)
    redraw()


# ── main curses loop ──────────────────────────────────────────────────────────
def _curses_main(stdscr: curses.window, sdr: Device) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)

    state    = AppState()
    registry = load_plugins()
    registry['spectrum'].start(state)

    # stable ordered list of all toggleable plugins (used for the menu)
    all_plugins = [p for p in registry.values() if p.key]

    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = state.gain_db

    SPEC_NEED = FFT_BINS * N_AVG
    iq_deque  = deque(maxlen=64)
    results: dict = {}
    stop_evt  = threading.Event()

    def _sdr_cb(samples, _ctx):
        if stop_evt.is_set():
            return
        for name in list(state.active_decoders):
            if name != 'spectrum':
                results[name] = registry[name].process(samples, state)
        iq_deque.append(samples)

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
            # derive tab order from currently enabled plugins
            tab_plugins = [p for p in all_plugins
                           if p.name in state.active_decoders]
            state.tab_idx = min(state.tab_idx, len(tab_plugins))

            key = stdscr.getch()
            handle_keys(key, stdscr, state, registry,
                        tab_plugins, all_plugins, sdr, results)

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
                draw(stdscr, state, results, registry, tab_plugins, all_plugins)
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
    # Suppress librtlsdr/libusb noise on stderr for the entire session.
    devnull  = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    error_msg = None
    try:
        sdr = open_first_device()
        if sdr is None:
            error_msg = 'no device found'
        else:
            curses.wrapper(lambda stdscr: _curses_main(stdscr, sdr))
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
    if error_msg:
        print(error_msg)


if __name__ == '__main__':
    main()
