#!/usr/bin/env python3
import os, time, curses, threading, json, glob, datetime, queue
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

_WF_CHARS = ' ░▒▓█'
_WF_N     = len(_WF_CHARS)


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


# ── preset helpers ────────────────────────────────────────────────────────────
_PRESET_FIELDS = (
    'center_hz', 'bw_hz', 'gain_db', 'gain_auto', 'iq_corr',
    'fm_bw_hz', 'nrsc5_sc_outer', 'waterfall_active', 'active_decoders',
)


def _preset_default_name() -> str:
    return 'preset_{}.sdrterm'.format(
        datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S'))


def _save_preset_to(path: str, state: AppState, all_plugins: list) -> None:
    data = {}
    for f in _PRESET_FIELDS:
        v = getattr(state, f)
        data[f] = list(v) if isinstance(v, set) else v
    data['plugin_order'] = [p.name for p in all_plugins]
    with open(path, 'w') as fh:
        json.dump(data, fh, indent=2)


def _load_preset(path: str, state: AppState) -> bool:
    try:
        with open(path) as fh:
            data = json.load(fh)
        for f in _PRESET_FIELDS:
            if f not in data:
                continue
            v = data[f]
            if f == 'active_decoders':
                v = set(v) | {'spectrum'}
            setattr(state, f, v)
        if 'plugin_order' in data:
            state.plugin_order = data['plugin_order']
        return True
    except Exception:
        return False


def _find_presets() -> list:
    return sorted(glob.glob('*.sdrterm'))


def _apply_preset(path: str, state: AppState, registry: dict, sdr,
                  all_plugins: list = None) -> bool:
    """Load a preset at runtime: start/stop decoders and apply hardware settings."""
    old_active = set(state.active_decoders)
    if not _load_preset(path, state):
        return False
    if all_plugins is not None and state.plugin_order:
        _om = {name: i for i, name in enumerate(state.plugin_order)}
        all_plugins.sort(key=lambda p: _om.get(p.name, len(state.plugin_order)))
    new_active = state.active_decoders
    for name in old_active - new_active:
        if name in registry:
            registry[name].stop()
    needed = _required_bw(new_active, registry)
    clamped = _nearest_bw(needed, sdr.supported_bandwidths)
    if state.bw_hz not in sdr.supported_bandwidths:
        state.bw_hz = min(sdr.supported_bandwidths,
                          key=lambda b: abs(b - state.bw_hz))
    if state.bw_hz < clamped:
        state.bw_hz = clamped
    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = 'auto' if state.gain_auto else state.gain_db
    for name in new_active - old_active:
        if name in registry:
            registry[name].start(state)
    return True


def _draw_preset_menu(screen_obj: curses.window, state: AppState,
                      ROWS: int, COLS: int) -> None:
    paths = state.preset_menu
    if not paths:
        return
    hint  = ' ret=load  esc=cancel '
    min_w = max(len(hint) + 4, max(len(os.path.basename(p)) for p in paths) + 6, 36)
    w  = min(COLS - 4, min_w)
    h  = len(paths) + 4
    y0 = max(0, (ROWS - h) // 2)
    x0 = max(0, (COLS - w) // 2)
    try:
        for r in range(h):
            screen_obj.addstr(y0 + r, x0, ' ' * w)
        screen_obj.addstr(y0,     x0 + 2, ' Load Preset ', curses.A_BOLD)
        screen_obj.addstr(y0 + 1, x0 + 2, '─' * (w - 4))
        for i, p in enumerate(paths):
            attr  = curses.A_REVERSE if i == state.preset_cursor else curses.A_NORMAL
            label = os.path.basename(p)[:w - 4].ljust(w - 4)
            screen_obj.addstr(y0 + 2 + i, x0 + 2, label, attr)
        screen_obj.addstr(y0 + h - 1, x0 + 2, hint[:w - 4], curses.A_DIM)
    except curses.error:
        pass


# ── debug console overlay ─────────────────────────────────────────────────────
def _draw_debug_console(screen_obj: curses.window,
                        state: AppState, plugin) -> None:
    ROWS, COLS = screen_obj.getmaxyx()
    screen_obj.erase()

    title = '[{} debug]  ↑↓=line  PgUp/PgDn=page  esc=close'.format(plugin.name)
    try:
        screen_obj.addstr(0, 0, title[:COLS - 1], curses.A_BOLD)
    except curses.error:
        pass

    body_h  = ROWS - 2
    lines   = list(plugin._debug_lines) if plugin._debug_lines else []
    n       = len(lines)

    max_scroll         = max(0, n - body_h)
    state.debug_scroll = min(state.debug_scroll, max_scroll)
    scroll             = state.debug_scroll

    start   = max(0, n - body_h - scroll)
    end     = n - scroll
    visible = lines[start:end]

    for i, line in enumerate(visible):
        try:
            screen_obj.addstr(i + 1, 0, str(line)[:COLS - 1])
        except curses.error:
            pass

    if n:
        info = ('↑{}  {}/{}'.format(scroll, end, n)
                if scroll else 'tail  {}'.format(n))
    else:
        info = 'no output yet'
    try:
        screen_obj.addstr(ROWS - 1, 0, info[:COLS - 1], curses.A_DIM)
    except curses.error:
        pass

    screen_obj.refresh()


# ── renderer ──────────────────────────────────────────────────────────────────
def draw(screen_obj: curses.window, state: AppState, results: dict,
         registry: dict, tab_plugins: list, all_plugins: list,
         sdr: Device, wf_rows) -> None:
    if state.debug_console is not None and state.debug_console in registry:
        _draw_debug_console(screen_obj, state, registry[state.debug_console])
        return

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
    valid_mask = (col_idx >= 0) & (col_idx < plot_w)
    ci_v       = col_idx[valid_mask]

    # Current-frame column dBFS (used by spectrum mode and band highlight)
    col_db = np.full(plot_w, DB_MIN)
    np.maximum.at(col_db, ci_v, mags_db[valid_mask])

    screen_obj.erase()

    # ── header (shared) ───────────────────────────────────────────────────────
    f_lo      = fmt_freq(state.center_hz - state.bw_hz / 2)
    f_ctr     = fmt_freq(state.center_hz)
    f_hi      = fmt_freq(state.center_hz + state.bw_hz / 2)
    ctr_col   = LABEL_W + (plot_w - len(f_ctr)) // 2
    right_col = COLS - len(f_hi)
    header    = f_lo.ljust(ctr_col) + f_ctr
    header    = header.ljust(right_col) + f_hi
    try:
        screen_obj.addstr(0, 0, header[:COLS],
                          curses.A_BOLD if state.gain_mode else curses.A_NORMAL)
    except curses.error:
        pass

    # ── body ─────────────────────────────────────────────────────────────────
    if state.waterfall_active:
        # Waterfall: newest row at top, scrolls down.  Each row is one FFT frame.
        n_wf = len(wf_rows)
        for r in range(height):
            wf_r = n_wf - 1 - r   # index into deque; -1 = newest
            if 0 <= wf_r < n_wf:
                row_mags = wf_rows[wf_r]
                if len(row_mags) == len(col_idx):
                    row_col_db = np.full(plot_w, DB_MIN)
                    np.maximum.at(row_col_db, ci_v, row_mags[valid_mask])
                    row_str = ''.join(
                        _WF_CHARS[max(0, min(_WF_N - 1,
                            int((float(db) - DB_MIN) / DB_RANGE * _WF_N)))]
                        for db in row_col_db)
                else:
                    row_str = ' ' * plot_w
            else:
                row_str = ' ' * plot_w
            try:
                screen_obj.addstr(r + 1, 0, '     | ')
                screen_obj.addstr(r + 1, LABEL_W, row_str)
            except curses.error:
                pass

    else:
        # Spectrum bar chart
        out = [['·'] * plot_w for _ in range(height)]
        for col, db in enumerate(col_db):
            bar = int((max(DB_MIN, min(DB_MAX, db)) - DB_MIN) / DB_RANGE * height)
            for r in range(height - bar, height):
                out[r][col] = '█'

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
                screen_obj.addstr(r + 1, 0, label)
                screen_obj.addstr(r + 1, LABEL_W, ''.join(out[r]))
            except curses.error:
                pass

    # ── plugin overlays ───────────────────────────────────────────────────────
    if state.tab_idx == 0:
        # Core tab: show all active plugin overlays at once
        for _p in tab_plugins:
            _p.draw_overlay(screen_obj, state, results.get(_p.name) or {},
                            freq_min, freq_range, plot_w, height)
    elif 0 < state.tab_idx <= len(tab_plugins):
        # Plugin tab: only that plugin's own overlay
        _p = tab_plugins[state.tab_idx - 1]
        _p.draw_overlay(screen_obj, state, results.get(_p.name) or {},
                        freq_min, freq_range, plot_w, height)

    # ── footer (shared) ───────────────────────────────────────────────────────
    try:
        if state.freq_input is not None:
            prompt = 'Freq: {}_'.format(state.freq_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')

        elif state.path_input is not None:
            prompt = 'Path: {}_'.format(state.path_input)
            screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
            screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')

        elif state.save_input is not None:
            if state.save_input.startswith('?:'):
                fname  = os.path.basename(state.save_input[2:])
                prompt = ' {} exists — overwrite? [y/n]  esc=cancel '.format(fname)
                screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD | curses.A_REVERSE)
            else:
                prompt = 'Save as: {}_'.format(state.save_input)
                screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
                screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')

        elif state.tab_idx == 0:
            # core tab — left side
            # When gain has focus every item except [gain] is dimmed so the
            # user can see at a glance which parameter is being edited.
            gm  = state.gain_mode
            dim = curses.A_DIM

            screen_obj.addstr(ROWS - 1, 0, '[core]',
                              dim if gm else curses.A_BOLD)
            col = 7

            iq_tag = '[IQ:ON]' if state.iq_corr else '[IQ:off]'
            screen_obj.addstr(ROWS - 1, col, iq_tag,
                              dim if gm else (curses.A_BOLD if state.iq_corr else dim))
            col += len(iq_tag) + 1

            gain_tag = '[gain:auto]' if state.gain_auto \
                       else '[gain:{:.1f}dB]'.format(state.gain_db)
            screen_obj.addstr(ROWS - 1, col, gain_tag,
                              curses.A_BOLD if gm else curses.A_NORMAL)
            col += len(gain_tag) + 1

            bw_tag = '[bw:{}]'.format(fmt_freq(state.bw_hz))
            screen_obj.addstr(ROWS - 1, col, bw_tag,
                              dim if gm else curses.A_NORMAL)
            col += len(bw_tag) + 1

            dev_status = sdr.status_text(state)
            if dev_status:
                screen_obj.addstr(ROWS - 1, col, dev_status,
                                  dim if gm else curses.A_NORMAL)
                col += len(dev_status) + 1

            # core tab — right side
            rhs_parts = ['a=auto', 'g=gain', 'i=iq', 'p=plugins', 's=save', 'l=load']
            if sdr.key_help:
                rhs_parts.append(sdr.key_help)
            rhs_parts.append('v=spectrum' if state.waterfall_active else 'v=waterfall')
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
            parts = ['x=discard', 'd=debug']
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

    if state.preset_menu is not None:
        _draw_preset_menu(screen_obj, state, ROWS, COLS)

    if time.monotonic() < state.flash_until and state.flash_msg:
        msg = '  {}  '.format(state.flash_msg)
        x   = max(0, (COLS - len(msg)) // 2)
        try:
            screen_obj.addstr(ROWS - 1, x, msg[:COLS - x],
                              curses.A_REVERSE | curses.A_BOLD)
        except curses.error:
            pass

    screen_obj.refresh()


# ── key handler ───────────────────────────────────────────────────────────────
def handle_keys(key: int, stdscr, state: AppState, registry: dict,
                tab_plugins: list, all_plugins: list,
                sdr: Device, results: dict, wf_rows) -> None:

    def redraw():
        if results:
            cur_tabs = [p for p in all_plugins if p.name in state.active_decoders]
            state.tab_idx = min(state.tab_idx, len(cur_tabs))
            draw(stdscr, state, results, registry, cur_tabs, all_plugins, sdr, wf_rows)

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

    # ── preset menu modal ─────────────────────────────────────────────────────
    if state.preset_menu is not None:
        if key == 27:
            state.preset_menu = None
        elif key in (10, 13, curses.KEY_ENTER):
            if 0 <= state.preset_cursor < len(state.preset_menu):
                path = state.preset_menu[state.preset_cursor]
                ok   = _apply_preset(path, state, registry, sdr, all_plugins)
                state.flash_msg   = ('loaded: ' + os.path.basename(path)) if ok \
                                     else 'error loading preset'
                state.flash_until = time.monotonic() + 2.0
            state.preset_menu = None
        elif key == curses.KEY_UP:
            state.preset_cursor = max(0, state.preset_cursor - 1)
        elif key == curses.KEY_DOWN:
            state.preset_cursor = min(len(state.preset_menu) - 1,
                                      state.preset_cursor + 1)
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
            if state.path_input_target == '__preset__':
                if state.path_input:
                    ok = _apply_preset(state.path_input, state, registry, sdr, all_plugins)
                    state.flash_msg   = ('loaded: ' + os.path.basename(state.path_input)) \
                                         if ok else 'cannot load: ' + state.path_input
                    state.flash_until = time.monotonic() + 2.0
            else:
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

    # ── save-as input modal ───────────────────────────────────────────────────
    if state.save_input is not None:
        if state.save_input.startswith('?:'):
            # Overwrite-confirm phase: waiting for y / n / esc
            full_path = state.save_input[2:]
            if key in (27, ord('n'), ord('N')):
                state.save_input = None
            elif key in (ord('y'), ord('Y')):
                try:
                    _save_preset_to(full_path, state, all_plugins)
                    state.flash_msg = 'saved: ' + os.path.basename(full_path)
                except Exception as e:
                    state.flash_msg = 'save error: ' + str(e)
                state.flash_until = time.monotonic() + 2.0
                state.save_input  = None
        else:
            # Filename-editing phase
            if key == 27:
                state.save_input = None
            elif key in (10, 13, curses.KEY_ENTER):
                name = state.save_input
                if name:
                    if not name.endswith('.sdrterm'):
                        name += '.sdrterm'
                    if os.path.exists(name):
                        state.save_input = '?:' + name   # → overwrite confirm
                    else:
                        try:
                            _save_preset_to(name, state, all_plugins)
                            state.flash_msg = 'saved: ' + name
                        except Exception as e:
                            state.flash_msg = 'save error: ' + str(e)
                        state.flash_until = time.monotonic() + 2.0
                        state.save_input  = None
                else:
                    state.save_input = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                state.save_input = state.save_input[:-1]
            elif 32 <= key <= 126 and chr(key) not in '\'"\\':
                state.save_input += chr(key)
        redraw()
        return

    # ── debug console modal ───────────────────────────────────────────────────
    if state.debug_console is not None:
        ROWS, _ = stdscr.getmaxyx()
        page = max(1, ROWS - 4)
        if key == 27:
            state.debug_console = None
            state.debug_scroll  = 0
        elif key == curses.KEY_UP:
            state.debug_scroll += 1
        elif key == curses.KEY_DOWN:
            state.debug_scroll = max(0, state.debug_scroll - 1)
        elif key == curses.KEY_PPAGE:
            state.debug_scroll += page
        elif key == curses.KEY_NPAGE:
            state.debug_scroll = max(0, state.debug_scroll - page)
        elif key in (ord('q'), ord('Q')):
            state.quit = True
            return
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

    # ── gain sub-mode: up/down step gain, g exits ─────────────────────────────
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

    # ── plugin-specific keys (plugin tab only, checked before global keys) ────
    if state.tab_idx > 0:
        plugin = tab_plugins[state.tab_idx - 1]
        if key == ord('x'):
            toggle_decoder(plugin.name, registry, state, sdr)
            state.tab_idx = 0
            redraw()
            return
        if key == ord('d'):
            state.debug_console = plugin.name
            state.debug_scroll  = 0
            redraw()
            return
        if plugin.handle_key(key, state, sdr):
            redraw()
            return

    # ── global parameter keys — active on every tab ───────────────────────────
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
    elif key == curses.KEY_LEFT:
        _, COLS = stdscr.getmaxyx()
        state.center_hz -= state.bw_hz / max(1, COLS - LABEL_W)
        sdr.center_freq  = state.center_hz
    elif key == curses.KEY_RIGHT:
        _, COLS = stdscr.getmaxyx()
        state.center_hz += state.bw_hz / max(1, COLS - LABEL_W)
        sdr.center_freq  = state.center_hz
    elif key == ord(','):
        state.center_hz -= state.bw_hz / FFT_BINS
        sdr.center_freq  = state.center_hz
    elif key == ord('.'):
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
    # ── core-only keys ────────────────────────────────────────────────────────
    elif state.tab_idx == 0:
        if key in (ord('v'), ord('V')):
            state.waterfall_active = not state.waterfall_active
        elif key in (ord('p'), ord('P')):
            state.menu_active = state.active_decoders - {'spectrum'}
            state.menu_cursor = 0
        elif key in (ord('s'), ord('S')):
            state.save_input = _preset_default_name()
        elif key in (ord('l'), ord('L')):
            paths = _find_presets()
            if not paths:
                state.path_input        = ''
                state.path_input_target = '__preset__'
            elif len(paths) == 1:
                ok = _apply_preset(paths[0], state, registry, sdr, all_plugins)
                state.flash_msg   = ('loaded: ' + paths[0]) if ok \
                                     else 'error loading preset'
                state.flash_until = time.monotonic() + 2.0
            else:
                state.preset_menu   = paths
                state.preset_cursor = 0
        else:
            sdr.handle_key(key, state)
    else:
        # unknown key on plugin tab: let the device handle it (e.g. b=bias-tee)
        sdr.handle_key(key, state)
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
    # Discard any preset-loaded decoder names that don't exist in this registry
    state.active_decoders = (state.active_decoders & set(registry.keys())) | {'spectrum'}
    registry['spectrum'].start(state)
    # Start any additional decoders loaded from a preset
    for _name in list(state.active_decoders):
        if _name != 'spectrum' and _name in registry:
            registry[_name].start(state)

    # stable ordered list of all toggleable plugins (used for the menu)
    all_plugins = [p for p in registry.values() if p.key]
    if state.plugin_order:
        _om = {name: i for i, name in enumerate(state.plugin_order)}
        all_plugins.sort(key=lambda p: _om.get(p.name, len(state.plugin_order)))

    # Clamp bw_hz to the nearest value the device actually supports.
    # Matters when --bw was given or a preset had a different BW.
    if state.bw_hz not in sdr.supported_bandwidths:
        state.bw_hz = min(sdr.supported_bandwidths,
                          key=lambda b: abs(b - state.bw_hz))
    # Ensure BW is sufficient for all active decoders
    _min_needed = _required_bw(state.active_decoders, registry)
    if state.bw_hz < _min_needed:
        state.bw_hz = _nearest_bw(_min_needed, sdr.supported_bandwidths)

    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = 'auto' if state.gain_auto else state.gain_db

    SPEC_NEED = FFT_BINS * N_AVG
    iq_deque  = deque(maxlen=64)
    wf_rows   = deque(maxlen=256)   # one mags_db array per rendered spectrum frame
    results: dict = {}
    stop_evt  = threading.Event()

    # ── tiered plugin execution ───────────────────────────────────────────────
    # realtime=True  → process() called inline in the SDR callback (same thread,
    #                  zero latency; must be fast — FM, RDS, record).
    # realtime=False → samples pushed to a per-plugin bounded queue; a dedicated
    #                  daemon thread drains it at whatever pace it can sustain.
    #                  If the queue fills the chunk is silently dropped (display
    #                  plugins tolerate this; audio plugins never land here).
    bg_queues = {
        p.name: queue.Queue(maxsize=p.bg_queue_depth)
        for p in all_plugins if not p.realtime
    }

    def _bg_worker(plugin, q):
        while not stop_evt.is_set():
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:       # shutdown sentinel
                break
            r = plugin.process(chunk, state, results, sdr)
            if r is not None:
                results[plugin.name] = r

    bg_workers = [
        threading.Thread(target=_bg_worker,
                         args=(p, bg_queues[p.name]),
                         name='bg-' + p.name, daemon=True)
        for p in all_plugins if not p.realtime
    ]
    for t in bg_workers:
        t.start()

    def _sdr_cb(samples, _ctx):
        if stop_evt.is_set():
            return
        # Real-time pass: run audio/record plugins sorted by descending priority
        # so high-priority plugins (FM=10, RDS=5) always run before record (0),
        # regardless of the user's pipeline order.
        frame_results = {}
        prev_plugin   = None
        rt_active = sorted(
            (p for p in all_plugins
             if p.realtime and p.name in state.active_decoders),
            key=lambda p: -p.priority,
        )
        for plugin in rt_active:
            frame_results['_prev_plugin'] = prev_plugin
            r = plugin.process(samples, state, frame_results, sdr)
            if r is not None:
                frame_results[plugin.name] = r
            prev_plugin = plugin
        results.update(frame_results)

        # Background pass: push samples to each worker queue (non-blocking).
        for plugin in all_plugins:
            if not plugin.realtime and plugin.name in state.active_decoders:
                try:
                    bg_queues[plugin.name].put_nowait(samples)
                except queue.Full:
                    pass   # worker is behind; drop this chunk

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
                        tab_plugins, all_plugins, sdr, results, wf_rows)

            # Hardware changes queued by active RTL-TCP plugin.
            # Applied here (main loop) not in process() to avoid calling
            # sdr.* from inside the librtlsdr async-read callback, which
            # deadlocks libusb's internal event-loop locks.
            if state.pending_freq is not None:
                state.center_hz    = state.pending_freq
                sdr.center_freq    = state.pending_freq
                state.pending_freq = None
                iq_deque.clear()
                spec_chunks.clear()
                spec_count = 0

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
                wf_rows.clear()
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
                    samples[:SPEC_NEED], state, results, sdr)
                spec_chunks.clear()
                spec_count = 0
                wf_rows.append(results['spectrum']['mags_db'].copy())
                draw(stdscr, state, results, registry, tab_plugins, all_plugins, sdr, wf_rows)
                last_draw = now
            elif key == -1:
                time.sleep(0.002)

    finally:
        stop_evt.set()
        sdr.cancel_read_async()
        if reader[0]:
            reader[0].join(timeout=2.0)
        # Send shutdown sentinels so workers blocked on q.get() exit immediately.
        for q in bg_queues.values():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass   # stop_evt is set; worker exits on its next Empty timeout
        for t in bg_workers:
            t.join(timeout=1.0)
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
                        help='replay a .iq (raw complex64) or stereo .wav IQ file '
                             '(selects the localfile device; WAV sample rate is read from the file header)')
    parser.add_argument('--bw', metavar='BW',
                        help='capture bandwidth / sample rate, e.g. 2.4M, 1024k, 250000\n'
                             'for real hardware: sets the initial sample rate\n'
                             'for --file .iq: must match the rate used when recording\n'
                             'for --file .wav: overrides the rate from the file header')
    parser.add_argument('--f', metavar='FREQ',
                        help='centre frequency, e.g. 105.8M, 1420000000')
    parser.add_argument('--g', metavar='GAIN',
                        help='gain in dB (e.g. 30.0) or "auto"')
    parser.add_argument('--i', metavar='on|off', choices=['on', 'off'],
                        help='enable IQ correction at startup')
    parser.add_argument('--preset', metavar='FILE',
                        help='load a .sdrterm preset file (overrides all other settings)')
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
    # --preset overrides everything set above
    if args.preset:
        if not _load_preset(args.preset, state):
            parser.error('cannot load preset: {}'.format(args.preset))

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
                error_msg = 'cannot open file: {}'.format(args.file)
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
