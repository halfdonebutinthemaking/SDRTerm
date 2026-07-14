#!/usr/bin/env python3
import os, time, curses, threading
from collections import deque
import numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from core import (
    AppState, Device, fmt_freq, parse_freq,
    FFT_BINS, N_AVG, DB_MAX, DB_MIN, DB_RANGE,
    LABEL_W, REFRESH_S, GAIN_MIN, GAIN_MAX, GAIN_STEP, READ_MAX,
    _required_bw, _nearest_bw, toggle_decoder,
)
from devices import load_devices, open_first_device, open_device_by_name, open_file_device
from plugins import load_plugins


# ── plugin menu overlay ───────────────────────────────────────────────────────
def _draw_plugin_menu(screen_obj: curses.window, state: AppState,
                      all_plugins: list, ROWS: int, COLS: int) -> None:
    if not all_plugins:
        return
    hint1 = ' spc=toggle  ret=apply  esc=cancel '
    hint2 = ' </>=reorder pipeline '
    min_w = max(len(hint1) + 4, len(hint2) + 4,
                max(len(p.name) for p in all_plugins) + 26, 44)
    w  = min(COLS - 4, min_w)
    h  = len(all_plugins) + 5   # title + sep + plugins + 2 hint lines
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
            label   = '[{}] #{:d}  {:14}  key: {}'.format(
                tick, i + 1, plugin.name, plugin.key or '—')
            screen_obj.addstr(y0 + 2 + i, x0 + 2, label[:w - 4].ljust(w - 4), attr)
        screen_obj.addstr(y0 + h - 2, x0 + 2, hint1[:w - 4], curses.A_DIM)
        screen_obj.addstr(y0 + h - 1, x0 + 2, hint2[:w - 4], curses.A_DIM)
    except curses.error:
        pass


# ── renderer ──────────────────────────────────────────────────────────────────
def draw(screen_obj: curses.window, state: AppState, results: dict,
         registry: dict, tab_plugins: list, all_plugins: list,
         sdr: Device) -> None:
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

        elif state.path_input is not None:
            prompt = 'Path: {}_'.format(state.path_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')

        elif state.tab_idx == 0:
            # core tab — left side
            screen_obj.addstr(ROWS - 1, 0, '[core]', curses.A_BOLD)
            col = 7
            iq_tag = '[IQ:ON]' if state.iq_corr else '[IQ:off]'
            screen_obj.addstr(ROWS - 1, col, iq_tag,
                              curses.A_BOLD if state.iq_corr else curses.A_DIM)
            col += len(iq_tag) + 1
            dev_status = sdr.status_text(state)
            if dev_status:
                screen_obj.addstr(ROWS - 1, col, dev_status)
                col += len(dev_status) + 1
            # core tab — right side
            rhs_parts = ['a=auto', 'g=gain', 'i=iq', 'p=plugins']
            if sdr.key_help:
                rhs_parts.append(sdr.key_help)
            if tab_plugins:
                rhs_parts.append('tab=plugin settings')
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
            draw(stdscr, state, results, registry, cur_tabs, all_plugins, sdr)

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
        elif key == ord('<'):                     # move earlier in pipeline
            i = state.menu_cursor
            if i > 0:
                all_plugins[i], all_plugins[i - 1] = all_plugins[i - 1], all_plugins[i]
                state.menu_cursor = i - 1
        elif key == ord('>'):                     # move later in pipeline
            i = state.menu_cursor
            if i < len(all_plugins) - 1:
                all_plugins[i], all_plugins[i + 1] = all_plugins[i + 1], all_plugins[i]
                state.menu_cursor = i + 1
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

    # ── path input modal ─────────────────────────────────────────────────────
    if state.path_input is not None:
        if key == 27:
            state.path_input = state.path_input_target = None
        elif key in (10, 13, curses.KEY_ENTER):
            target = registry.get(state.path_input_target)
            if target and hasattr(target, 'set_path'):
                target.set_path(state.path_input or None)
            state.path_input = state.path_input_target = None
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            state.path_input = state.path_input[:-1]
        elif 32 <= key <= 126:
            state.path_input += chr(key)
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
            higher = [b for b in sdr.supported_bandwidths if b > state.bw_hz]
            if higher:
                state.bw_hz     = min(higher)
                sdr.sample_rate = state.bw_hz
        elif key == curses.KEY_DOWN:
            min_bw = _required_bw(state.active_decoders, registry)
            lower  = [b for b in sdr.supported_bandwidths
                      if b < state.bw_hz and b >= min_bw]
            if lower:
                state.bw_hz     = max(lower)
                sdr.sample_rate = state.bw_hz
        else:
            sdr.handle_key(key, state)
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
def _curses_main(stdscr: curses.window, sdr: Device, state: AppState) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)

    registry = load_plugins()
    registry['spectrum'].start(state)

    # stable ordered list of all toggleable plugins (used for the menu)
    all_plugins = [p for p in registry.values() if p.key]

    # Clamp bw_hz to the nearest value the device actually supports.
    # Matters when --bw was given but doesn't exactly match a supported step.
    if state.bw_hz not in sdr.supported_bandwidths:
        state.bw_hz = min(sdr.supported_bandwidths,
                          key=lambda b: abs(b - state.bw_hz))

    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = 'auto' if state.gain_auto else state.gain_db

    SPEC_NEED = FFT_BINS * N_AVG
    iq_deque  = deque(maxlen=64)
    results: dict = {}
    stop_evt  = threading.Event()

    def _sdr_cb(samples, _ctx):
        if stop_evt.is_set():
            return
        # Process plugins in pipeline order.  _prev_plugin lets the record
        # plugin find its immediate predecessor without scanning all_plugins.
        frame_results = {}
        prev_plugin   = None
        for plugin in all_plugins:
            if plugin.name in state.active_decoders:
                frame_results['_prev_plugin'] = prev_plugin
                r = plugin.process(samples, state, frame_results, sdr)
                if r is not None:
                    frame_results[plugin.name] = r
                prev_plugin = plugin
        results.update(frame_results)
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

            # Hardware changes queued by active RTL-TCP plugin.
            # Applied here (main loop) not in process() to avoid calling
            # sdr.* from inside the librtlsdr async-read callback, which
            # deadlocks libusb's internal event-loop locks.
            if state.pending_freq is not None:
                state.center_hz   = state.pending_freq
                sdr.center_freq   = state.pending_freq
                state.pending_freq = None

            if state.pending_gain is not None:
                if state.pending_gain < 0:
                    state.gain_auto = True
                    sdr.gain        = 'auto'
                else:
                    state.gain_auto = False
                    state.gain_db   = state.pending_gain
                    sdr.gain        = state.pending_gain
                state.pending_gain = None

            if state.pending_sr is not None:
                state.bw_hz     = _nearest_bw(state.pending_sr, sdr.supported_bandwidths)
                sdr.sample_rate = state.bw_hz
                state.pending_sr = None
                # bw_hz changed → the check below restarts the reader

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
                draw(stdscr, state, results, registry, tab_plugins, all_plugins, sdr)
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
    import argparse
    parser = argparse.ArgumentParser(
        prog='sdrterm',
        description='SDRTerm — terminal SDR spectrum analyser',
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--d', metavar='NAME',
                        help='device to open, e.g. RTL-SDR-V3\n'
                             'omit to use the first available device')
    parser.add_argument('--file', metavar='PATH',
                        help='replay a raw complex64 .iq file (selects the localfile device)')
    parser.add_argument('--bw', metavar='BW',
                        help='capture bandwidth / sample rate, e.g. 2.4M, 1024k, 250000\n'
                             'for real hardware: sets the initial sample rate\n'
                             'for --file: must match the rate used when recording')
    parser.add_argument('--f', metavar='FREQ',
                        help='centre frequency, e.g. 105.8M, 1420000000')
    parser.add_argument('--g', metavar='GAIN',
                        help='gain in dB (e.g. 30.0) or "auto"')
    parser.add_argument('--i', metavar='on|off', choices=['on', 'off'],
                        help='enable IQ correction at startup')
    args = parser.parse_args()

    # build initial AppState from CLI overrides
    state = AppState()
    if args.f:
        hz = parse_freq(args.f)
        if hz is None:
            parser.error('invalid frequency: {}'.format(args.f))
        state.center_hz = hz
    bw_hz = None
    if args.bw:
        bw_hz = parse_freq(args.bw)
        if bw_hz is None:
            parser.error('invalid --bw value: {}'.format(args.bw))
        bw_hz = int(bw_hz)
        state.bw_hz = bw_hz   # clamped to device's supported list in _curses_main
    if args.g:
        if args.g.lower() == 'auto':
            state.gain_auto = True
        else:
            try:
                state.gain_db = float(args.g)
            except ValueError:
                parser.error('invalid gain value: {}'.format(args.g))
    if args.i:
        state.iq_corr = (args.i == 'on')

    # suppress librtlsdr/libusb noise on stderr for the entire session
    devnull  = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    error_msg = None
    try:
        if args.file:
            sdr = open_file_device(args.file, bw_hz)
            if sdr is None:
                error_msg = 'cannot open IQ file: {}'.format(args.file)
        elif args.d:
            sdr = open_device_by_name(args.d)
            if sdr is None:
                known = ', '.join(d.name for d in load_devices()) or 'none'
                error_msg = 'device not found: {}  (known drivers: {})'.format(
                    args.d, known)
        else:
            sdr = open_first_device()
            if sdr is None:
                error_msg = 'no device found'
        if error_msg is None:
            curses.wrapper(lambda stdscr: _curses_main(stdscr, sdr, state))
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
    if error_msg:
        print(error_msg)


if __name__ == '__main__':
    main()
