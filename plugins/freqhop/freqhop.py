import time
import curses
import numpy as np
from core import Decoder, AppState, fmt_freq, parse_freq

_SETTLE_S      = 0.20   # seconds to flush IQ buffer after each retune
_DWELL_DEFAULT = 3.0    # seconds per slot
_DWELL_MIN     = 0.5
_DWELL_MAX     = 60.0
_DWELL_STEP    = 0.5


class FreqHop(Decoder):
    name            = 'freqhop'
    key             = 'h'
    key_help        = 'h=hop  a=add  A=add freq  r=remove  [/]=dwell  ↑↓=select  ret=tune'
    min_sample_rate = 250_000
    realtime        = True
    full_view       = True

    def __init__(self):
        self._slots        = []    # list of {'freq_hz': float, 'dwell_s': float}
        self._cursor       = 0    # UI cursor for slot selection
        self._active       = False
        self._slot_idx     = 0    # which slot is currently being listened to
        self._hop_start    = 0.0  # monotonic time when we tuned to current slot
        self._saved_center = 0.0  # restored when hopping stops

    def start(self, state: AppState) -> None:
        self._active    = False
        self._slot_idx  = 0
        self._hop_start = 0.0

    def stop(self) -> None:
        self._active = False

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        remaining = None
        if self._active and self._slots:
            now     = time.monotonic()
            slot    = self._slots[self._slot_idx]
            elapsed = now - self._hop_start
            total   = _SETTLE_S + slot['dwell_s']
            remaining = max(0.0, total - elapsed)
            if elapsed >= total:
                self._slot_idx  = (self._slot_idx + 1) % len(self._slots)
                self._hop_start = now
                state.pending_freq = self._slots[self._slot_idx]['freq_hz']
                remaining = self._slots[self._slot_idx]['dwell_s']

        return {
            'active':    self._active,
            'slot_idx':  self._slot_idx,
            'slots':     list(self._slots),
            'remaining': remaining,
        }

    def _start_hop(self, state: AppState) -> None:
        if not self._slots:
            return
        self._saved_center = state.center_hz
        self._slot_idx     = 0
        self._hop_start    = time.monotonic()
        self._active       = True
        state.pending_freq = self._slots[0]['freq_hz']

    def _stop_hop(self, state: AppState) -> None:
        self._active       = False
        state.pending_freq = self._saved_center

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('h'):
            if self._active:
                self._stop_hop(state)
            else:
                self._start_hop(state)
            return True

        if key in (curses.KEY_UP, ord('k')):
            self._cursor = max(0, self._cursor - 1)
            return True

        if key in (curses.KEY_DOWN, ord('j')):
            self._cursor = min(max(0, len(self._slots) - 1), self._cursor + 1)
            return True

        if key in (10, 13, curses.KEY_ENTER):
            if 0 <= self._cursor < len(self._slots):
                state.pending_freq = self._slots[self._cursor]['freq_hz']
            return True

        if key == ord('a'):
            self._slots.append({'freq_hz': state.center_hz, 'dwell_s': _DWELL_DEFAULT})
            self._cursor = len(self._slots) - 1
            return True

        if key == ord('A'):
            state.path_input       = ''
            state.path_input_label = 'Add frequency (e.g. 136.9M)'
            plugin = self
            state.path_input_cb    = lambda val: plugin._add_freq_input(val)
            return True

        if key == ord('r'):
            if 0 <= self._cursor < len(self._slots):
                self._slots.pop(self._cursor)
                self._cursor   = min(self._cursor, max(0, len(self._slots) - 1))
                if self._slot_idx >= len(self._slots):
                    self._slot_idx = 0
                if not self._slots and self._active:
                    self._stop_hop(state)
            return True

        if key == ord('['):
            if 0 <= self._cursor < len(self._slots):
                s = self._slots[self._cursor]
                s['dwell_s'] = round(max(_DWELL_MIN, s['dwell_s'] - _DWELL_STEP), 1)
            return True

        if key == ord(']'):
            if 0 <= self._cursor < len(self._slots):
                s = self._slots[self._cursor]
                s['dwell_s'] = round(min(_DWELL_MAX, s['dwell_s'] + _DWELL_STEP), 1)
            return True

        return False

    def _add_freq_input(self, val: str) -> None:
        parsed = parse_freq(val)
        if parsed is not None:
            self._slots.append({'freq_hz': parsed, 'dwell_s': _DWELL_DEFAULT})
            self._cursor = len(self._slots) - 1

    def status_text(self, state: AppState, result: dict) -> str:
        if not result or not result.get('active'):
            return ''
        slots    = result.get('slots', [])
        slot_idx = result.get('slot_idx', 0)
        rem      = result.get('remaining')
        if slots and 0 <= slot_idx < len(slots):
            freq    = slots[slot_idx]['freq_hz']
            rem_str = '  {:.1f}s'.format(rem) if rem is not None else ''
            return '[HOP {}  {}/{}{}] '.format(
                fmt_freq(freq), slot_idx + 1, len(slots), rem_str)
        return ''

    def save_state(self) -> dict:
        return {'slots': [dict(s) for s in self._slots]}

    def load_state(self, d: dict) -> None:
        raw = d.get('slots', [])
        self._slots = [
            {'freq_hz': float(s.get('freq_hz', 0.0)),
             'dwell_s': float(s.get('dwell_s', _DWELL_DEFAULT))}
            for s in raw if isinstance(s, dict) and s.get('freq_hz')
        ]
        self._cursor   = 0
        self._slot_idx = 0

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        if not result:
            return

        active    = result.get('active', False)
        slots     = result.get('slots', [])
        slot_idx  = result.get('slot_idx', 0)
        remaining = result.get('remaining')
        cursor    = self._cursor

        # ── Header ─────────────────────────────────────────────────────────
        if active and slots:
            hop_str = '  [HOPPING  {}/{}  next in {:.1f}s]'.format(
                slot_idx + 1, len(slots),
                max(0.0, (remaining or 0.0) - _SETTLE_S))
        elif slots:
            hop_str = '  [idle — h to start]'
        else:
            hop_str = '  [no slots — a=add current freq  A=type a freq]'

        try:
            h_attr = curses.A_BOLD
            if active:
                try:
                    h_attr = curses.color_pair(3) | curses.A_BOLD   # green
                except Exception:
                    pass
            screen_obj.addstr(1, 2, ('FreqHop' + hop_str)[:cols - 3], h_attr)
        except curses.error:
            pass

        # ── Column header ───────────────────────────────────────────────────
        hdr = '   {:>2}  {:>16}  {:>7}  '.format('#', 'Frequency', 'Dwell')
        try:
            screen_obj.addstr(2, 2, hdr[:cols - 3], curses.A_UNDERLINE)
        except curses.error:
            pass

        # ── Slot list ──────────────────────────────────────────────────────
        body_rows = max(0, rows - 5)
        if slots:
            cursor = max(0, min(len(slots) - 1, cursor))
        scroll = max(0, cursor - body_rows + 1) if body_rows > 0 else 0

        for i, slot in enumerate(slots[scroll: scroll + body_rows]):
            abs_i  = i + scroll
            is_cur = active and abs_i == slot_idx
            marker = '▶' if is_cur else ' '
            line   = ' {} {:>2}  {:>16}  {:>6.1f}s  '.format(
                marker, abs_i + 1, fmt_freq(slot['freq_hz']), slot['dwell_s'])

            if abs_i == cursor:
                attr = curses.A_REVERSE
            elif is_cur:
                try:
                    attr = curses.color_pair(3) | curses.A_BOLD   # green = active slot
                except Exception:
                    attr = curses.A_BOLD
            else:
                attr = curses.A_NORMAL

            try:
                screen_obj.addstr(3 + i, 2, line[:cols - 3], attr)
            except curses.error:
                pass

        if not slots:
            try:
                screen_obj.addstr(3, 4, 'Hop list is empty.', curses.A_DIM)
                screen_obj.addstr(4, 4,
                    'Press  a  to add the current frequency,  A  to type one.',
                    curses.A_DIM)
            except curses.error:
                pass

        # ── Footer ─────────────────────────────────────────────────────────
        footer = ('h=hop  a=add current  A=add freq  r=remove  '
                  '[/]=dwell ±{:.1f}s  ↑↓/jk=select  ret=tune').format(_DWELL_STEP)
        try:
            screen_obj.addstr(rows - 2, 2, footer[:cols - 4], curses.A_DIM)
        except curses.error:
            pass
