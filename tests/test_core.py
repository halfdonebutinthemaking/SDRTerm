"""Tests for core.py: helpers, AppState, base classes, registry utilities."""
import numpy as np
import pytest

from core import (
    fmt_freq, parse_freq, correct_iq,
    _nearest_bw, _required_bw, toggle_decoder,
    AppState, Decoder, Device,
    CENTER_HZ, BW_STEPS, GAIN_DEF, GAIN_MIN, GAIN_MAX,
)


# ── fmt_freq ──────────────────────────────────────────────────────────────────

class TestFmtFreq:
    def test_mhz(self):
        assert fmt_freq(105.8e6) == '105.800 MHz'

    def test_khz(self):
        assert fmt_freq(433.5e3) == '433.500 kHz'

    def test_hz(self):
        assert fmt_freq(999) == '999 Hz'

    def test_negative_mhz(self):
        assert fmt_freq(-105.8e6) == '-105.800 MHz'

    def test_exactly_1mhz_uses_mhz(self):
        assert 'MHz' in fmt_freq(1_000_000)

    def test_exactly_1khz_uses_khz(self):
        assert 'kHz' in fmt_freq(1_000)

    def test_zero_uses_hz(self):
        assert 'Hz' in fmt_freq(0)


# ── parse_freq ────────────────────────────────────────────────────────────────

class TestParseFreq:
    @pytest.mark.parametrize('s,expected', [
        ('105.8M',   105.8e6),
        ('105.8m',   105.8e6),
        ('433.5K',   433.5e3),
        ('433.5k',   433.5e3),
        ('1.42G',    1.42e9),
        ('1.42g',    1.42e9),
        ('162000000', 162e6),
        ('  105.8M', 105.8e6),   # leading whitespace
    ])
    def test_valid(self, s, expected):
        assert parse_freq(s) == pytest.approx(expected)

    @pytest.mark.parametrize('s', ['', '   ', 'abc', '10.5X', 'M'])
    def test_invalid_returns_none(self, s):
        assert parse_freq(s) is None


# ── correct_iq ────────────────────────────────────────────────────────────────

class TestCorrectIQ:
    @pytest.fixture
    def biased_samples(self):
        rng = np.random.default_rng(0)
        i = rng.standard_normal(4096).astype(np.float32) + 5.0
        q = rng.standard_normal(4096).astype(np.float32) * 0.5 + 3.0
        return (i + 1j * q).astype(np.complex64)

    def test_dc_offset_removed(self, biased_samples):
        out = correct_iq(biased_samples)
        assert abs(np.mean(out.real)) < 0.05
        assert abs(np.mean(out.imag)) < 0.05

    def test_output_shape_preserved(self, biased_samples):
        out = correct_iq(biased_samples)
        assert out.shape == biased_samples.shape

    def test_output_is_complex(self, biased_samples):
        out = correct_iq(biased_samples)
        assert np.iscomplexobj(out)

    def test_balanced_input_unchanged_in_power(self):
        rng = np.random.default_rng(1)
        s = (rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
        out = correct_iq(s)
        # Power should be similar after correction on already-balanced signal
        assert np.mean(np.abs(out) ** 2) == pytest.approx(np.mean(np.abs(s) ** 2), rel=0.5)


# ── _nearest_bw ───────────────────────────────────────────────────────────────

class TestNearestBw:
    def test_exact_match(self):
        assert _nearest_bw(2_400_000, BW_STEPS) == 2_400_000

    def test_rounds_up_to_next(self):
        # 300 kHz is between 250 k and 1024 k → should return 1_024_000
        assert _nearest_bw(300_000, BW_STEPS) == 1_024_000

    def test_above_all_returns_max(self):
        assert _nearest_bw(99_999_999, BW_STEPS) == max(BW_STEPS)

    def test_zero_returns_min(self):
        assert _nearest_bw(0, BW_STEPS) == min(BW_STEPS)

    def test_custom_list(self):
        assert _nearest_bw(500, [100, 1000, 5000]) == 1000


# ── _required_bw ─────────────────────────────────────────────────────────────

class TestRequiredBw:
    def test_empty_set_returns_zero(self):
        assert _required_bw(set(), {}) == 0

    def test_single_plugin(self):
        p = Decoder()
        p.min_sample_rate = 250_000
        assert _required_bw({'a'}, {'a': p}) == 250_000

    def test_returns_max_of_all(self):
        p1, p2, p3 = Decoder(), Decoder(), Decoder()
        p1.min_sample_rate = 250_000
        p2.min_sample_rate = 1_800_000
        p3.min_sample_rate = 1_024_000
        reg = {'a': p1, 'b': p2, 'c': p3}
        assert _required_bw({'a', 'b', 'c'}, reg) == 1_800_000


# ── AppState ──────────────────────────────────────────────────────────────────

class TestAppStateDefaults:
    def test_center_hz(self, state):
        assert state.center_hz == CENTER_HZ

    def test_bw_hz_is_max_step(self, state):
        assert state.bw_hz == BW_STEPS[-1]

    def test_gain_db_default(self, state):
        assert state.gain_db == GAIN_DEF

    def test_gain_auto_off(self, state):
        assert state.gain_auto is False

    def test_iq_corr_off(self, state):
        assert state.iq_corr is False

    def test_spectrum_always_in_active_decoders(self, state):
        assert 'spectrum' in state.active_decoders

    def test_not_quitting(self, state):
        assert state.quit is False

    def test_no_freq_input_open(self, state):
        assert state.freq_input is None

    def test_no_path_input_open(self, state):
        assert state.path_input is None

    def test_path_input_label_default(self, state):
        assert state.path_input_label == 'Path'

    def test_waterfall_inactive(self, state):
        assert state.waterfall_active is False

    def test_tab_idx_zero(self, state):
        assert state.tab_idx == 0


# ── Decoder base class ────────────────────────────────────────────────────────

class TestDecoderBase:
    def test_save_state_returns_dict(self):
        assert isinstance(Decoder().save_state(), dict)

    def test_load_state_does_not_raise(self):
        Decoder().load_state({'key': 'value'})

    def test_handle_key_returns_false(self):
        assert Decoder().handle_key(ord('x'), AppState(), None) is False

    def test_process_returns_none(self):
        samples = np.zeros(256, dtype=np.complex64)
        assert Decoder().process(samples, AppState()) is None

    def test_status_text_returns_none(self):
        assert Decoder().status_text(AppState(), {}) is None

    def test_record_ext_none_by_default(self):
        assert Decoder().record_ext is None

    def test_full_view_false_by_default(self):
        assert Decoder().full_view is False

    def test_realtime_true_by_default(self):
        assert Decoder().realtime is True


# ── toggle_decoder ────────────────────────────────────────────────────────────

class TestToggleDecoder:
    def _make_plugin(self, name, min_sr=250_000):
        p = Decoder()
        p.name = name
        p.min_sample_rate = min_sr
        return p

    def test_enables_inactive_plugin(self, state, fake_sdr):
        p = self._make_plugin('x')
        toggle_decoder('x', {'x': p}, state, fake_sdr)
        assert 'x' in state.active_decoders

    def test_disables_active_plugin(self, state, fake_sdr):
        p = self._make_plugin('x')
        state.active_decoders.add('x')
        toggle_decoder('x', {'x': p}, state, fake_sdr)
        assert 'x' not in state.active_decoders

    def test_bw_raised_when_plugin_needs_more(self, state, fake_sdr):
        p = self._make_plugin('hungry', min_sr=2_400_000)
        state.bw_hz = 250_000
        toggle_decoder('hungry', {'hungry': p}, state, fake_sdr)
        assert state.bw_hz >= 2_400_000

    def test_bw_not_lowered_when_enabling(self, state, fake_sdr):
        p = self._make_plugin('light', min_sr=250_000)
        state.bw_hz = 2_400_000
        toggle_decoder('light', {'light': p}, state, fake_sdr)
        assert state.bw_hz == 2_400_000
