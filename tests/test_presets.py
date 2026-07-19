"""Tests for preset save / load / apply logic in main.py."""
import json
import os
import tempfile

import pytest

from core import AppState, BW_STEPS
from main import _save_preset_to, _load_preset, _PRESET_FIELDS


# ── helpers ───────────────────────────────────────────────────────────────────

def _round_trip(state: AppState) -> AppState:
    """Save state to a temp file and load it into a fresh AppState."""
    with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
        path = f.name
    try:
        _save_preset_to(path, state, [])
        s2 = AppState()
        _load_preset(path, s2)
        return s2
    finally:
        os.unlink(path)


# ── field round-trips ─────────────────────────────────────────────────────────

class TestPresetRoundtrip:
    def test_center_hz(self, state):
        state.center_hz = 433.92e6
        assert _round_trip(state).center_hz == pytest.approx(433.92e6)

    def test_bw_hz(self, state):
        state.bw_hz = 1_024_000
        assert _round_trip(state).bw_hz == 1_024_000

    def test_gain_db(self, state):
        state.gain_db = 28.0
        assert _round_trip(state).gain_db == pytest.approx(28.0)

    def test_gain_auto(self, state):
        state.gain_auto = True
        assert _round_trip(state).gain_auto is True

    def test_iq_corr(self, state):
        state.iq_corr = True
        assert _round_trip(state).iq_corr is True

    def test_waterfall_active(self, state):
        state.waterfall_active = True
        assert _round_trip(state).waterfall_active is True

    def test_active_decoders_restored(self, state):
        state.active_decoders = {'spectrum', 'fm'}
        s2 = _round_trip(state)
        assert 'fm' in s2.active_decoders


# ── invariants ────────────────────────────────────────────────────────────────

class TestPresetInvariants:
    def test_spectrum_always_present_after_load(self, state):
        state.active_decoders = {'fm'}      # deliberately omit spectrum
        s2 = _round_trip(state)
        assert 'spectrum' in s2.active_decoders

    def test_active_decoders_saved_as_list(self, state):
        state.active_decoders = {'spectrum', 'fm'}
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [])
            with open(path) as fh:
                data = json.load(fh)
            assert isinstance(data['active_decoders'], list)
        finally:
            os.unlink(path)

    def test_all_preset_fields_written(self, state):
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [])
            with open(path) as fh:
                data = json.load(fh)
            for field in _PRESET_FIELDS:
                assert field in data, f'preset missing field: {field}'
        finally:
            os.unlink(path)

    def test_plugin_order_written(self, state):
        from core import Decoder
        p = Decoder()
        p.name = 'fm'
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [p])
            with open(path) as fh:
                data = json.load(fh)
            assert 'plugin_order' in data
            assert 'fm' in data['plugin_order']
        finally:
            os.unlink(path)

    def test_version_written(self, state):
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [])
            with open(path) as fh:
                data = json.load(fh)
            assert data.get('version') == 1
        finally:
            os.unlink(path)

    def test_plugin_states_written_for_plugins_with_state(self, state):
        from plugins.range_scan import RangeScan
        p = RangeScan()
        p._scan_freq_min = 88e6
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [p])
            with open(path) as fh:
                data = json.load(fh)
            assert 'plugin_states' in data
            assert data['plugin_states']['range-scan']['scan_freq_min'] == pytest.approx(88e6)
        finally:
            os.unlink(path)

    def test_plugin_states_loaded_on_load(self, state):
        from plugins.range_scan import RangeScan
        p = RangeScan()
        p._scan_freq_min = 88e6
        p._scan_freq_max = 108e6
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            path = f.name
        try:
            _save_preset_to(path, state, [p])
            p2 = RangeScan()
            _load_preset(path, AppState(), [p2])
            assert p2._scan_freq_min == pytest.approx(88e6)
            assert p2._scan_freq_max == pytest.approx(108e6)
        finally:
            os.unlink(path)


# ── preset migration ──────────────────────────────────────────────────────────

class TestPresetMigration:
    def test_v0_scan_freq_migrated_to_plugin_states(self, state):
        from main import _migrate_preset
        data = {'center_hz': 100e6, 'scan_freq_min': 88e6, 'scan_freq_max': 108e6}
        out = _migrate_preset(data)
        assert 'scan_freq_min' not in out
        assert out['plugin_states']['range-scan']['scan_freq_min'] == pytest.approx(88e6)
        assert out['plugin_states']['range-scan']['scan_freq_max'] == pytest.approx(108e6)
        assert out['version'] == 1

    def test_v1_not_migrated_again(self, state):
        from main import _migrate_preset
        data = {'version': 1, 'center_hz': 100e6}
        out = _migrate_preset(data)
        assert 'plugin_states' not in out   # nothing added

    def test_v0_without_scan_freq_still_upgrades_version(self, state):
        from main import _migrate_preset
        data = {'center_hz': 100e6}
        out = _migrate_preset(data)
        assert out['version'] == 1
        assert 'plugin_states' not in out


# ── error handling ────────────────────────────────────────────────────────────

class TestPresetErrorHandling:
    def test_missing_file_returns_false(self, state):
        assert _load_preset('/nonexistent/path/preset.sdrterm', state) is False

    def test_corrupt_json_returns_false(self, state):
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            f.write('not { valid } json')
            path = f.name
        try:
            assert _load_preset(path, state) is False
        finally:
            os.unlink(path)

    def test_unknown_fields_ignored(self, state):
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            json.dump({'center_hz': 100e6, 'future_field': 99}, f)
            path = f.name
        try:
            assert _load_preset(path, state) is True   # must not raise or return False
        finally:
            os.unlink(path)

    def test_partial_preset_leaves_other_fields_at_defaults(self, state):
        original_gain = state.gain_db
        with tempfile.NamedTemporaryFile(suffix='.sdrterm', delete=False, mode='w') as f:
            json.dump({'center_hz': 200e6}, f)
            path = f.name
        try:
            _load_preset(path, state)
            assert state.gain_db == original_gain
        finally:
            os.unlink(path)
