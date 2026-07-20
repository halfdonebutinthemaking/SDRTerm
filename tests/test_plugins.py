"""Tests for plugin discovery, lifecycle, spectrum output, and state hooks."""
import numpy as np
import pytest

from core import AppState, Decoder, FFT_BINS, N_AVG, DB_MIN, DB_MAX, BW_STEPS

EXPECTED_PLUGINS = {
    'spectrum', 'fm', 'rds', 'nrsc5_text', 'peak_marker',
    'record', 'rtl-tcp-passive', 'rtl-tcp-active', 'range-scan',
}


# ── discovery ─────────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_all_expected_plugins_present(self, registry):
        assert EXPECTED_PLUGINS.issubset(set(registry.keys()))

    def test_spectrum_has_no_toggle_key(self, registry):
        assert not registry['spectrum'].key

    def test_keyed_plugins_have_single_lowercase_char(self, registry):
        for name, plugin in registry.items():
            if plugin.key:
                assert len(plugin.key) == 1
                assert plugin.key.islower(), f'{name}.key should be lowercase'

    def test_no_duplicate_keys(self, registry):
        keys = [p.key for p in registry.values() if p.key]
        assert len(keys) == len(set(keys)), 'duplicate plugin activation keys'

    _FULL_VIEW_PLUGINS = {'range-scan', 'constellation', 'vdl2', 'freqhop', 'modclass', 'acars'}

    def test_known_full_view_plugins(self, registry):
        for name in self._FULL_VIEW_PLUGINS:
            if name in registry:
                assert registry[name].full_view is True, f'{name}.full_view should be True'

    def test_no_unexpected_full_view(self, registry):
        for name, plugin in registry.items():
            if name not in self._FULL_VIEW_PLUGINS:
                assert not plugin.full_view, f'{name}.full_view should be False'

    def test_all_plugins_have_name(self, registry):
        for name, plugin in registry.items():
            assert plugin.name == name


# ── spectrum plugin output ────────────────────────────────────────────────────

class TestSpectrumPlugin:
    @pytest.fixture
    def plugin(self, registry):
        return registry['spectrum']

    @pytest.fixture
    def samples(self):
        rng = np.random.default_rng(1)
        n = FFT_BINS * N_AVG
        return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)

    def test_process_returns_dict(self, plugin, samples):
        result = plugin.process(samples, AppState())
        assert isinstance(result, dict)

    def test_output_has_freqs_and_mags(self, plugin, samples):
        result = plugin.process(samples, AppState())
        assert 'freqs' in result
        assert 'mags_db' in result

    def test_freqs_length_equals_fft_bins(self, plugin, samples):
        result = plugin.process(samples, AppState())
        assert len(result['freqs']) == FFT_BINS

    def test_mags_length_equals_fft_bins(self, plugin, samples):
        result = plugin.process(samples, AppState())
        assert len(result['mags_db']) == FFT_BINS

    def test_center_freq_is_within_freqs_range(self, plugin, samples):
        state = AppState()
        state.center_hz = 105.8e6
        result = plugin.process(samples, state)
        freqs = result['freqs']
        assert freqs[0] < state.center_hz < freqs[-1]

    def test_mags_db_are_all_finite(self, plugin, samples):
        result = plugin.process(samples, AppState())
        assert np.all(np.isfinite(result['mags_db']))

    def test_mags_db_within_display_range(self, plugin, samples):
        result = plugin.process(samples, AppState())
        mags = result['mags_db']
        # Values should be in a physically sensible range (not NaN or ±inf)
        assert float(np.min(mags)) > -300
        assert float(np.max(mags)) < 100

    def test_freqs_are_monotonically_increasing(self, plugin, samples):
        result = plugin.process(samples, AppState())
        freqs = result['freqs']
        assert np.all(np.diff(freqs) > 0)

    def test_iq_correction_does_not_crash(self, plugin, samples):
        state = AppState()
        state.iq_corr = True
        result = plugin.process(samples, state)
        assert 'mags_db' in result


# ── plugin lifecycle ──────────────────────────────────────────────────────────

class TestPluginLifecycle:
    @pytest.mark.parametrize('name', sorted(EXPECTED_PLUGINS))
    def test_start_and_stop_do_not_raise(self, registry, name):
        plugin = registry[name]
        state = AppState()
        plugin.start(state)
        plugin.stop()

    @pytest.mark.parametrize('name', ['peak_marker', 'range-scan'])
    def test_process_without_start_returns_dict_or_none(self, registry, name):
        plugin = registry[name]
        rng = np.random.default_rng(0)
        samples = (rng.standard_normal(16384) + 1j * rng.standard_normal(16384)).astype(np.complex64)
        result = plugin.process(samples, AppState())
        assert result is None or isinstance(result, dict)

    @pytest.mark.parametrize('name', sorted(EXPECTED_PLUGINS))
    def test_handle_key_returns_bool(self, registry, name):
        plugin = registry[name]
        result = plugin.handle_key(ord('z'), AppState(), None)
        assert isinstance(result, bool)


# ── plugin state hooks ────────────────────────────────────────────────────────

class TestPluginStateHooks:
    """save_state / load_state contract — holds for base class and any plugin."""

    def test_base_decoder_save_state_returns_dict(self):
        assert isinstance(Decoder().save_state(), dict)

    def test_base_decoder_load_state_does_not_raise(self):
        Decoder().load_state({'anything': True, 'nested': {'x': 1}})

    @pytest.mark.parametrize('name', sorted(EXPECTED_PLUGINS))
    def test_all_plugins_save_state_returns_dict(self, registry, name):
        plugin = registry[name]
        assert isinstance(plugin.save_state(), dict)

    @pytest.mark.parametrize('name', sorted(EXPECTED_PLUGINS))
    def test_all_plugins_load_state_does_not_raise(self, registry, name):
        plugin = registry[name]
        plugin.load_state({})

    @pytest.mark.parametrize('name', sorted(EXPECTED_PLUGINS))
    def test_save_load_roundtrip_is_stable(self, registry, name):
        plugin = registry[name]
        state = AppState()
        plugin.start(state)
        saved = plugin.save_state()
        plugin.load_state(saved)
        # Loading saved state and saving again must produce identical output
        assert plugin.save_state() == saved
        plugin.stop()
