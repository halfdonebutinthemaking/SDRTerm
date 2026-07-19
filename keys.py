import curses
import os
import time

from core import (
    AppState, Device, parse_freq, toggle_decoder,
    FFT_BINS, LABEL_W, GAIN_MIN, GAIN_MAX, GAIN_STEP,
    _required_bw,
)
from presets import _apply_preset, _find_presets, _preset_default_name, _save_preset_to
from render import draw


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

    # ── path input modal ──────────────────────────────────────────────────────
    if state.path_input is not None:
        if key == 27:
            state.path_input       = None
            state.path_input_cb    = None
            state.path_input_label = 'Path'
        elif key in (10, 13, curses.KEY_ENTER):
            if state.path_input_cb:
                state.path_input_cb(state.path_input)
            state.path_input       = None
            state.path_input_cb    = None
            state.path_input_label = 'Path'
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

    # ── plugin-specific keys (checked before global keys so plugins can
    #    override globals like f/F when their tab is active) ───────────────────
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

    if key in (ord('f'), ord('F')):
        state.freq_input = ''
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
            state.bw_hz = min(higher)
            # sdr.sample_rate applied by _start_reader() after async read stops
    elif key == curses.KEY_DOWN:
        min_bw = _required_bw(state.active_decoders, registry)
        lower  = [b for b in sdr.supported_bandwidths
                  if b < state.bw_hz and b >= min_bw]
        if lower:
            state.bw_hz = max(lower)
            # sdr.sample_rate applied by _start_reader() after async read stops
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
                state.path_input    = ''
                def _preset_load_cb(val, _st=state, _reg=registry, _sdr=sdr, _pl=all_plugins):
                    if val:
                        ok = _apply_preset(val, _st, _reg, _sdr, _pl)
                        _st.flash_msg   = ('loaded: ' + os.path.basename(val)) if ok else 'cannot load: ' + val
                        _st.flash_until = time.monotonic() + 2.0
                state.path_input_cb = _preset_load_cb
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
