"""Tests for the meteor plugin class itself (state, keys, web contract)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from plugins.meteor.meteor import MeteorDecoder


class _FakeState:
    def __init__(self, center_hz=100_000_000):
        self.center_hz = center_hz


class _FakeSdr:
    def __init__(self):
        self.center_freq = 0.0


class TestConfig:
    def test_default_state_has_no_location(self):
        d = MeteorDecoder()
        assert d._location_lat is None
        assert d._location_lon is None

    def test_save_state_omits_defaults(self):
        d = MeteorDecoder()
        assert d.save_state() == {}

    def test_load_state_sets_location(self):
        d = MeteorDecoder()
        d.load_state({'location_lat': 49.74, 'location_lon': 6.66})
        assert d._location_lat == 49.74
        assert d._location_lon == 6.66

    def test_load_state_ignores_lone_lat(self):
        d = MeteorDecoder()
        d.load_state({'location_lat': 49.74})
        assert d._location_lat is None

    def test_load_state_handles_altitude_and_elevation(self):
        d = MeteorDecoder()
        d.load_state({'location_lat': 49.74, 'location_lon': 6.66,
                      'location_alt_km': 0.15, 'min_elevation_deg': 25})
        assert d._location_alt_km == pytest.approx(0.15)
        assert d._min_elevation_deg == pytest.approx(25.0)

    def test_save_state_roundtrip_includes_non_defaults(self):
        d = MeteorDecoder()
        d.load_state({'location_lat': 49.74, 'location_lon': 6.66,
                      'min_elevation_deg': 20.0})
        s = d.save_state()
        assert s == {'location_lat': 49.74, 'location_lon': 6.66,
                     'min_elevation_deg': 20.0}


class TestKeys:
    def test_r_invalidates_cache(self):
        d = MeteorDecoder()
        d._passes_cache = ['fake']
        d._passes_cache_time = 999.0
        assert d.handle_key(ord('r'), None, None) is True
        assert d._passes_cache is None
        assert d._passes_cache_time == 0.0

    def test_other_keys_ignored(self):
        assert MeteorDecoder().handle_key(ord('x'), None, None) is False


class TestWebJson:
    def test_shape_without_location(self):
        d = MeteorDecoder()
        payload = d.web_json()
        assert set(payload.keys()) == {'passes', 'receiver', 'satdump',
                                       'known_sats', 'generated_at',
                                       'auto_tune', 'tuned_sat', 'tuned_freq_hz'}
        assert payload['receiver'] is None
        assert payload['passes'] == []
        assert 'METEOR-M2 3' in payload['known_sats']
        assert 'NOAA 19' in payload['known_sats']

    def test_shape_with_location_but_no_tle(self):
        # No TLE cache exists in tmp env → _get_passes swallows the
        # exception and returns empty.  Payload should still be valid.
        d = MeteorDecoder()
        d.load_state({'location_lat': 49.74, 'location_lon': 6.66})
        # Force a TLE fetch failure
        with patch('plugins.meteor.passes.fetch_tle_cache',
                   side_effect=OSError('no net')):
            payload = d.web_json()
        assert payload['receiver'] == {'lat': 49.74, 'lon': 6.66}
        assert payload['passes'] == []

    def test_satdump_detected(self):
        # satdump is installed at /opt/homebrew/bin/satdump on the dev
        # machine; on other envs this will just be False and that's fine.
        payload = MeteorDecoder().web_json()
        assert isinstance(payload['satdump'], bool)


class TestAutoTune:
    """process() should retune the SDR to a pass's advertised frequency
    while the pass is in progress, and leave things alone otherwise."""

    def _pass(self, sat, offset_min_from_now=(-1, +5)):
        """Craft a fake pass dict whose rise/fall window contains 'now'."""
        now = datetime.now(timezone.utc)
        return {
            'sat':        sat,
            'rise':       now + timedelta(minutes=offset_min_from_now[0]),
            'peak':       now + timedelta(minutes=(offset_min_from_now[0] + offset_min_from_now[1]) / 2),
            'fall':       now + timedelta(minutes=offset_min_from_now[1]),
            'max_elev':   50.0, 'max_az': 180.0, 'rise_az': 90.0,
            'duration_s': int((offset_min_from_now[1] - offset_min_from_now[0]) * 60),
        }

    def _mk(self, passes):
        """Build a decoder whose _get_passes() returns `passes` — no TLE
        network involved."""
        d = MeteorDecoder()
        d._passes_cache = passes
        d._passes_cache_time = 1e18   # never expire during this test
        return d

    def test_retunes_when_pass_starts(self):
        d = self._mk([self._pass('METEOR-M2 3')])   # 137.100 MHz
        state, sdr = _FakeState(center_hz=100_000_000), _FakeSdr()
        result = d.process(None, state, sdr=sdr)
        assert result['active'] is True
        assert result['current_sat'] == 'METEOR-M2 3'
        assert result['current_freq'] == 137_100_000
        assert state.center_hz == 137_100_000
        assert sdr.center_freq == 137_100_000
        assert d._tuned_sat == 'METEOR-M2 3'

    def test_does_not_retune_when_already_on_freq(self):
        d = self._mk([self._pass('METEOR-M2 3')])
        state, sdr = _FakeState(center_hz=137_100_000), _FakeSdr()
        d.process(None, state, sdr=sdr)
        # sdr.center_freq stays at 0 because the 1 kHz-tolerance early-out
        # skips both the state and the sdr update.
        assert sdr.center_freq == 0.0
        assert state.center_hz == 137_100_000

    def test_does_not_retune_between_passes(self):
        # No pass active — process() must leave center_hz alone entirely,
        # so the user is free to tune elsewhere between passes.
        d = self._mk([])
        state, sdr = _FakeState(center_hz=100_000_000), _FakeSdr()
        d.process(None, state, sdr=sdr)
        assert state.center_hz == 100_000_000
        assert sdr.center_freq == 0.0

    def test_disabled_auto_tune_does_not_retune(self):
        d = self._mk([self._pass('METEOR-M2 4')])
        d._auto_tune = False
        state, sdr = _FakeState(center_hz=100_000_000), _FakeSdr()
        result = d.process(None, state, sdr=sdr)
        assert result['auto_tune'] is False
        assert state.center_hz == 100_000_000
        assert sdr.center_freq == 0.0

    def test_retunes_when_pass_switches(self):
        # First pass = METEOR-M2 3 (137.1); it ends, then METEOR-M2 4
        # (137.9) starts.  Plugin should retune to the new freq.
        p1 = self._pass('METEOR-M2 3')
        d = self._mk([p1])
        state, sdr = _FakeState(center_hz=100_000_000), _FakeSdr()
        d.process(None, state, sdr=sdr)
        assert state.center_hz == 137_100_000
        # Simulate the second pass replacing the current cache
        d._passes_cache = [self._pass('METEOR-M2 4')]
        d.process(None, state, sdr=sdr)
        assert state.center_hz == 137_900_000
        assert d._tuned_sat == 'METEOR-M2 4'

    def test_a_key_toggles_auto_tune(self):
        d = MeteorDecoder()
        assert d._auto_tune is True
        d.handle_key(ord('a'), None, None)
        assert d._auto_tune is False
        d.handle_key(ord('a'), None, None)
        assert d._auto_tune is True

    def test_save_state_records_disabled_auto_tune(self):
        d = MeteorDecoder()
        assert 'auto_tune' not in d.save_state()      # default omitted
        d._auto_tune = False
        assert d.save_state()['auto_tune'] is False

    def test_load_state_reads_auto_tune(self):
        d = MeteorDecoder()
        d.load_state({'auto_tune': False})
        assert d._auto_tune is False
        d.load_state({'auto_tune': True})
        assert d._auto_tune is True

    def test_no_pass_clears_tuned_sat(self):
        # After a pass ends, _tuned_sat must clear so the next fresh
        # pass triggers retune, not the "same as before" early-out.
        d = self._mk([self._pass('METEOR-M2 3')])
        state, sdr = _FakeState(center_hz=100_000_000), _FakeSdr()
        d.process(None, state, sdr=sdr)
        assert d._tuned_sat == 'METEOR-M2 3'
        # Pass ends
        d._passes_cache = []
        d.process(None, state, sdr=sdr)
        assert d._tuned_sat is None
