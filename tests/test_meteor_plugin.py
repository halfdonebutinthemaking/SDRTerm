"""Tests for the meteor plugin class itself (state, keys, web contract)."""
from unittest.mock import patch

import pytest

from plugins.meteor.meteor import MeteorDecoder


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
                                       'known_sats', 'generated_at'}
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
