import curses
import os
import time

import numpy as np

from core import (
    AppState, Device, fmt_freq,
    DB_MAX, DB_MIN, DB_RANGE, LABEL_W, GAIN_MIN, GAIN_MAX,
)

_WF_CHARS = ' ░▒▓█'
_WF_N     = len(_WF_CHARS)


def _draw_plugin_menu(screen_obj: curses.window, state: AppState,
                      all_plugins: list, ROWS: int, COLS: int) -> None:
    if not all_plugins:
        return
    hint1 = ' spc=toggle  ret=apply  esc=cancel '
    hint2 = ' </>=reorder pipeline '
    min_w = max(len(hint1) + 4, len(hint2) + 4,
                max(len(p.name) for p in all_plugins) + 12, 36)
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
            label   = '[{}] #{:d}  {}'.format(tick, i + 1, plugin.name)
            screen_obj.addstr(y0 + 2 + i, x0 + 2, label[:w - 4].ljust(w - 4), attr)
        screen_obj.addstr(y0 + h - 2, x0 + 2, hint1[:w - 4], curses.A_DIM)
        screen_obj.addstr(y0 + h - 1, x0 + 2, hint2[:w - 4], curses.A_DIM)
    except curses.error:
        pass


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


def draw(screen_obj: curses.window, state: AppState, results: dict,
         registry: dict, tab_plugins: list, all_plugins: list,
         sdr: Device, wf_rows) -> None:
    if state.debug_console is not None and state.debug_console in registry:
        _draw_debug_console(screen_obj, state, registry[state.debug_console])
        return

    # Full-view plugin: bypass spectrum rendering, hand all content to the plugin
    if state.tab_idx > 0 and state.tab_idx <= len(tab_plugins):
        _fv = tab_plugins[state.tab_idx - 1]
        if _fv.full_view:
            ROWS, COLS = screen_obj.getmaxyx()
            screen_obj.erase()
            _fv.draw_full(screen_obj, state, results.get(_fv.name) or {}, ROWS, COLS)
            _fv_res = results.get(_fv.name) or {}
            try:
                if state.freq_input is not None:
                    prompt = 'Freq: {}_'.format(state.freq_input)
                    screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
                    screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')
                elif state.path_input is not None:
                    prompt = '{}: {}_'.format(state.path_input_label, state.path_input)
                    screen_obj.addstr(ROWS - 1, 0, prompt, curses.A_BOLD)
                    screen_obj.addstr(ROWS - 1, len(prompt), '  ret=ok  esc=cancel')
                else:
                    ctx = '[{}]'.format(_fv.name)
                    screen_obj.addstr(ROWS - 1, 0, ctx, curses.A_BOLD)
                    _fv_col = len(ctx) + 1
                    _fv_text = _fv.status_text(state, _fv_res)
                    if _fv_text:
                        screen_obj.addstr(ROWS - 1, _fv_col, _fv_text, curses.A_BOLD)
                        _fv_col += len(_fv_text)
                    _fv_parts = ['x=discard', 'd=debug']
                    if _fv.key_help:
                        _fv_parts.append(_fv.key_help)
                    _fv_parts += ['f=freq', 'q=quit']
                    _fv_rhs = '  '.join(_fv_parts)
                    screen_obj.addstr(ROWS - 1, COLS - len(_fv_rhs) - 1, _fv_rhs)
            except curses.error:
                pass
            if state.menu_active is not None:
                _draw_plugin_menu(screen_obj, state, all_plugins, ROWS, COLS)
            if state.preset_menu is not None:
                _draw_preset_menu(screen_obj, state, ROWS, COLS)
            if time.monotonic() < state.flash_until and state.flash_msg:
                _fv_msg = '  {}  '.format(state.flash_msg)
                _fv_x   = max(0, (COLS - len(_fv_msg)) // 2)
                try:
                    screen_obj.addstr(ROWS - 1, _fv_x, _fv_msg[:COLS - _fv_x],
                                      curses.A_REVERSE | curses.A_BOLD)
                except curses.error:
                    pass
            screen_obj.refresh()
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
            prompt = '{}: {}_'.format(state.path_input_label, state.path_input)
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
