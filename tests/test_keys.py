"""Tests for handle_keys() state machine: modal transitions and parameter changes.

handle_keys() calls redraw() internally; because results={} the draw() function
returns immediately at the 'if sp is None: return' guard without touching the
curses screen, so a MagicMock stdscr is sufficient.
"""
import curses
from collections import deque
from unittest.mock import MagicMock

import pytest

from core import (
    AppState, Device, BW_STEPS,
    GAIN_MIN, GAIN_MAX, GAIN_STEP, CENTER_HZ,
)
from main import handle_keys


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def stdscr():
    m = MagicMock()
    m.getmaxyx.return_value = (50, 200)
    return m


@pytest.fixture
def sdr():
    s = MagicMock(spec=Device)
    s.supported_bandwidths = BW_STEPS
    s.sample_rate = BW_STEPS[-1]
    s.center_freq = CENTER_HZ
    s.gain = 0.0
    s.key_help = ''
    s.status_text.return_value = ''
    s.handle_key.return_value = False
    return s


@pytest.fixture
def ctx(stdscr, sdr):
    """Return a dict with everything handle_keys needs; mutate state freely."""
    return dict(
        stdscr=stdscr,
        state=AppState(),
        registry={},
        tab_plugins=[],
        all_plugins=[],
        sdr=sdr,
        results={},
        wf_rows=deque(),
    )


def press(key, ctx):
    handle_keys(
        key, ctx['stdscr'], ctx['state'], ctx['registry'],
        ctx['tab_plugins'], ctx['all_plugins'],
        ctx['sdr'], ctx['results'], ctx['wf_rows'],
    )


# ── quit ──────────────────────────────────────────────────────────────────────

class TestQuit:
    def test_q_sets_quit(self, ctx):
        press(ord('q'), ctx)
        assert ctx['state'].quit is True

    def test_Q_sets_quit(self, ctx):
        press(ord('Q'), ctx)
        assert ctx['state'].quit is True


# ── frequency input modal ─────────────────────────────────────────────────────

class TestFreqInputModal:
    def test_f_opens_modal(self, ctx):
        press(ord('f'), ctx)
        assert ctx['state'].freq_input == ''

    def test_characters_appended(self, ctx):
        ctx['state'].freq_input = ''
        for ch in '1058':
            press(ord(ch), ctx)
        assert ctx['state'].freq_input == '1058'

    def test_suffix_characters_accepted(self, ctx):
        ctx['state'].freq_input = ''
        for ch in '105M':
            press(ord(ch), ctx)
        assert ctx['state'].freq_input == '105M'

    def test_backspace_removes_last_char(self, ctx):
        ctx['state'].freq_input = '105'
        press(curses.KEY_BACKSPACE, ctx)
        assert ctx['state'].freq_input == '10'

    def test_backspace_on_empty_stays_empty(self, ctx):
        ctx['state'].freq_input = ''
        press(curses.KEY_BACKSPACE, ctx)
        assert ctx['state'].freq_input == ''

    def test_enter_valid_updates_center_hz(self, ctx):
        ctx['state'].freq_input = '100M'
        press(10, ctx)   # newline = confirm
        assert ctx['state'].center_hz == pytest.approx(100e6)

    def test_enter_valid_closes_modal(self, ctx):
        ctx['state'].freq_input = '100M'
        press(10, ctx)
        assert ctx['state'].freq_input is None

    def test_enter_invalid_does_not_change_center(self, ctx):
        original = ctx['state'].center_hz
        ctx['state'].freq_input = 'not_a_number'
        press(10, ctx)
        assert ctx['state'].center_hz == original

    def test_esc_closes_without_changing_center(self, ctx):
        original = ctx['state'].center_hz
        ctx['state'].freq_input = '999M'
        press(27, ctx)
        assert ctx['state'].freq_input is None
        assert ctx['state'].center_hz == original

    def test_modal_blocks_global_keys(self, ctx):
        ctx['state'].freq_input = ''
        press(ord('a'), ctx)              # 'a' = AGC toggle normally
        assert ctx['state'].gain_auto is False   # modal intercepted it


# ── gain mode ─────────────────────────────────────────────────────────────────

class TestGainMode:
    def test_g_enters_gain_mode(self, ctx):
        press(ord('g'), ctx)
        assert ctx['state'].gain_mode is True

    def test_g_again_exits_gain_mode(self, ctx):
        ctx['state'].gain_mode = True
        press(ord('g'), ctx)
        assert ctx['state'].gain_mode is False

    def test_up_increases_gain(self, ctx):
        ctx['state'].gain_mode = True
        ctx['state'].gain_db = 10.0
        press(curses.KEY_UP, ctx)
        assert ctx['state'].gain_db == pytest.approx(10.0 + GAIN_STEP)

    def test_down_decreases_gain(self, ctx):
        ctx['state'].gain_mode = True
        ctx['state'].gain_db = 10.0
        press(curses.KEY_DOWN, ctx)
        assert ctx['state'].gain_db == pytest.approx(10.0 - GAIN_STEP)

    def test_gain_clamped_at_max(self, ctx):
        ctx['state'].gain_mode = True
        ctx['state'].gain_db = GAIN_MAX
        press(curses.KEY_UP, ctx)
        assert ctx['state'].gain_db == pytest.approx(GAIN_MAX)

    def test_gain_clamped_at_min(self, ctx):
        ctx['state'].gain_mode = True
        ctx['state'].gain_db = GAIN_MIN
        press(curses.KEY_DOWN, ctx)
        assert ctx['state'].gain_db == pytest.approx(GAIN_MIN)

    def test_up_outside_gain_mode_changes_bw_not_gain(self, ctx):
        ctx['state'].bw_hz = BW_STEPS[0]
        ctx['state'].gain_mode = False
        ctx['state'].gain_db = 10.0
        press(curses.KEY_UP, ctx)
        assert ctx['state'].gain_db == pytest.approx(10.0)   # unchanged
        assert ctx['state'].bw_hz == BW_STEPS[1]             # BW stepped


# ── AGC toggle ────────────────────────────────────────────────────────────────

class TestAgcToggle:
    def test_a_enables_agc(self, ctx):
        ctx['state'].gain_auto = False
        press(ord('a'), ctx)
        assert ctx['state'].gain_auto is True

    def test_a_disables_agc(self, ctx):
        ctx['state'].gain_auto = True
        press(ord('a'), ctx)
        assert ctx['state'].gain_auto is False


# ── bandwidth stepping ────────────────────────────────────────────────────────

class TestBandwidthStepping:
    def test_up_steps_to_next_bw(self, ctx):
        ctx['state'].bw_hz = BW_STEPS[0]
        press(curses.KEY_UP, ctx)
        assert ctx['state'].bw_hz == BW_STEPS[1]

    def test_down_steps_to_previous_bw(self, ctx):
        ctx['state'].bw_hz = BW_STEPS[-1]
        press(curses.KEY_DOWN, ctx)
        assert ctx['state'].bw_hz == BW_STEPS[-2]

    def test_up_at_max_stays(self, ctx):
        ctx['state'].bw_hz = BW_STEPS[-1]
        press(curses.KEY_UP, ctx)
        assert ctx['state'].bw_hz == BW_STEPS[-1]

    def test_down_at_min_stays(self, ctx):
        ctx['state'].bw_hz = BW_STEPS[0]
        press(curses.KEY_DOWN, ctx)
        assert ctx['state'].bw_hz == BW_STEPS[0]


# ── path input modal ──────────────────────────────────────────────────────────

class TestPathInputModal:
    def test_enter_calls_callback_with_input(self, ctx):
        received = []
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = lambda val: received.append(val)
        press(10, ctx)
        assert received == ['88M']

    def test_enter_clears_path_input(self, ctx):
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = lambda val: None
        press(10, ctx)
        assert ctx['state'].path_input is None

    def test_enter_clears_callback(self, ctx):
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = lambda val: None
        press(10, ctx)
        assert ctx['state'].path_input_cb is None

    def test_esc_does_not_call_callback(self, ctx):
        called = []
        ctx['state'].path_input = 'anything'
        ctx['state'].path_input_cb = lambda val: called.append(val)
        press(27, ctx)
        assert called == []

    def test_esc_clears_path_input(self, ctx):
        ctx['state'].path_input = 'anything'
        ctx['state'].path_input_cb = lambda val: None
        press(27, ctx)
        assert ctx['state'].path_input is None

    def test_esc_resets_label_to_default(self, ctx):
        ctx['state'].path_input = 'anything'
        ctx['state'].path_input_cb = lambda val: None
        ctx['state'].path_input_label = 'Scan min freq'
        press(27, ctx)
        assert ctx['state'].path_input_label == 'Path'

    def test_enter_resets_label_to_default(self, ctx):
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = lambda val: None
        ctx['state'].path_input_label = 'Scan min freq'
        press(10, ctx)
        assert ctx['state'].path_input_label == 'Path'

    def test_no_callback_enter_still_clears(self, ctx):
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = None
        press(10, ctx)
        assert ctx['state'].path_input is None

    def test_characters_appended(self, ctx):
        ctx['state'].path_input = ''
        ctx['state'].path_input_cb = lambda val: None
        for ch in '88M':
            press(ord(ch), ctx)
        assert ctx['state'].path_input == '88M'

    def test_backspace_removes_char(self, ctx):
        ctx['state'].path_input = '88M'
        ctx['state'].path_input_cb = lambda val: None
        press(curses.KEY_BACKSPACE, ctx)
        assert ctx['state'].path_input == '88'


# ── IQ correction toggle ──────────────────────────────────────────────────────

class TestIqToggle:
    def test_i_toggles_iq_corr_on(self, ctx):
        ctx['state'].iq_corr = False
        press(ord('i'), ctx)
        assert ctx['state'].iq_corr is True

    def test_i_toggles_iq_corr_off(self, ctx):
        ctx['state'].iq_corr = True
        press(ord('i'), ctx)
        assert ctx['state'].iq_corr is False


# ── tab navigation ────────────────────────────────────────────────────────────

class TestTabNavigation:
    def test_tab_cycles_to_plugin_tab(self, ctx):
        from core import Decoder
        p = Decoder()
        p.name = 'fm'
        ctx['all_plugins'] = [p]
        ctx['tab_plugins'] = [p]
        ctx['state'].active_decoders.add('fm')
        press(9, ctx)   # TAB
        assert ctx['state'].tab_idx == 1

    def test_tab_wraps_back_to_core(self, ctx):
        from core import Decoder
        p = Decoder()
        p.name = 'fm'
        ctx['all_plugins'] = [p]
        ctx['tab_plugins'] = [p]
        ctx['state'].active_decoders.add('fm')
        ctx['state'].tab_idx = 1
        press(9, ctx)
        assert ctx['state'].tab_idx == 0
