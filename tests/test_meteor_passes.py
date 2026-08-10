"""Tests for plugins.meteor.passes — TLE cache + pass prediction.

TLEs used here are frozen in-tree so the suite runs offline.  Because
the TLEs are a snapshot from around the time the test was written, the
`predict_passes` tests seed pyorbital with an epoch reference matching
the TLE (the get_next_passes call itself uses datetime.now() which is
fine — passes shift a few seconds per day but the qualitative checks
below don't care).
"""
import os
import time
from datetime import datetime, timezone

import pytest

from plugins.meteor import passes as _passes


# ── a small, self-contained TLE bundle ───────────────────────────────────────

# Real TLEs fetched from CelesTrak and frozen in-tree so the suite
# runs offline.  ISS is the stand-in for "any real LEO" — 93 min
# orbit at 51.6° inclination passes over any mid-latitude receiver
# ~5x/day.  METEOR-M2 3 is included so we can exercise SATELLITES
# lookups without a live network fetch.  Checksums are valid.
_ISS_TLE = """ISS (ZARYA)
1 25544U 98067A   26222.18186727  .00004351  00000+0  85915-4 0  9998
2 25544  51.6326  32.8714 0007377  31.2060 328.9366 15.49401464580127
"""

_METEOR_TLE = """METEOR-M2 3
1 57166U 23091A   26222.20034450 -.00000012  00000+0  13457-4 0  9999
2 57166  98.6046 276.0626 0004331  29.4667 330.6756 14.24050621162200
"""

_TLE_BUNDLE = _ISS_TLE + _METEOR_TLE


@pytest.fixture
def tle_file(tmp_path):
    p = tmp_path / 'weather.txt'
    p.write_text(_TLE_BUNDLE)
    return str(p)


# ── TLE cache logic ──────────────────────────────────────────────────────────

class TestTleCache:
    def test_fresh_cache_is_returned_as_is(self, tmp_path, monkeypatch):
        # If the file exists and is younger than max_age, no network hit.
        p = tmp_path / 'weather.txt'
        p.write_text(_TLE_BUNDLE)
        # Make it look 1h old with max_age 12h
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        # Any network call in this code path would fail this test.
        def _forbidden(*a, **kw): raise AssertionError('network hit')
        monkeypatch.setattr('urllib.request.urlopen', _forbidden)
        got = _passes.fetch_tle_cache(str(p), max_age_s=12 * 3600)
        assert got == str(p)

    def test_stale_cache_refetched(self, tmp_path, monkeypatch):
        p = tmp_path / 'weather.txt'
        p.write_text('old\n')
        os.utime(p, (time.time() - 999_999, time.time() - 999_999))  # very old

        class _FakeResp:
            def __init__(self, data): self._data = data
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._data

        monkeypatch.setattr(
            'urllib.request.urlopen',
            lambda url, timeout=15: _FakeResp(_TLE_BUNDLE.encode()),
        )
        _passes.fetch_tle_cache(str(p), max_age_s=1)
        assert p.read_text() == _TLE_BUNDLE

    def test_stale_cache_survives_network_failure(self, tmp_path, monkeypatch):
        # If refetch fails but a stale copy exists, use it silently.
        p = tmp_path / 'weather.txt'
        p.write_text(_TLE_BUNDLE)
        os.utime(p, (0, 0))                     # very stale
        monkeypatch.setattr(
            'urllib.request.urlopen',
            lambda *a, **kw: (_ for _ in ()).throw(OSError('offline')),
        )
        got = _passes.fetch_tle_cache(str(p), max_age_s=1)
        assert got == str(p)                    # returned anyway

    def test_missing_cache_and_network_failure_raises(self, tmp_path, monkeypatch):
        p = tmp_path / 'nope.txt'
        monkeypatch.setattr(
            'urllib.request.urlopen',
            lambda *a, **kw: (_ for _ in ()).throw(OSError('offline')),
        )
        with pytest.raises(OSError):
            _passes.fetch_tle_cache(str(p))


# ── pass prediction ──────────────────────────────────────────────────────────

class TestPredictPasses:
    def test_iss_has_passes_over_europe(self, tle_file):
        # ISS at 51.6° inclination passes over 49°N ~5x per day
        result = _passes.predict_passes(
            'ISS (ZARYA)', tle_file,
            rx_lat=49.74, rx_lon=6.66,
            horizon_hours=24.0, min_elevation_deg=10.0,
        )
        assert len(result) > 0
        first = result[0]
        assert first['sat'] == 'ISS (ZARYA)'
        # Sanity: peak comes after rise, fall after peak
        assert first['rise'] <= first['peak'] <= first['fall']
        # Elevation range sanity
        assert 10.0 <= first['max_elev'] <= 90.0
        # Duration is at least a minute for a real overhead pass
        assert first['duration_s'] >= 60

    def test_min_elevation_filter_works(self, tle_file):
        low  = _passes.predict_passes('ISS (ZARYA)', tle_file, 49.74, 6.66,
                                      horizon_hours=24.0, min_elevation_deg=5.0)
        high = _passes.predict_passes('ISS (ZARYA)', tle_file, 49.74, 6.66,
                                      horizon_hours=24.0, min_elevation_deg=60.0)
        assert len(low) >= len(high)
        for p in high:
            assert p['max_elev'] >= 60.0

    def test_unknown_sat_raises(self, tle_file):
        with pytest.raises(KeyError):
            _passes.predict_passes('NOT-A-SAT', tle_file, 49.74, 6.66)


class TestPredictAll:
    def test_skips_missing_sats_silently(self, tle_file):
        # Ask for a mix of present + absent sats; result contains only
        # the present ones' passes without raising.
        sats = {
            'ISS (ZARYA)':    {},
            'NOT-IN-TLE':     {},
            'METEOR-M2 3':    {},
        }
        result = _passes.predict_all(sats, tle_file, 49.74, 6.66,
                                     horizon_hours=24.0,
                                     min_elevation_deg=10.0)
        # ISS is guaranteed to have passes; METEOR should too (98.6° polar
        # orbit passes anywhere twice a day)
        sat_names = {p['sat'] for p in result}
        assert 'ISS (ZARYA)' in sat_names
        assert 'NOT-IN-TLE'  not in sat_names

    def test_result_is_sorted_by_rise_time(self, tle_file):
        result = _passes.predict_all(
            {'ISS (ZARYA)': {}, 'METEOR-M2 3': {}},
            tle_file, 49.74, 6.66,
            horizon_hours=24.0, min_elevation_deg=10.0,
        )
        rises = [p['rise'] for p in result]
        assert rises == sorted(rises)


class TestCurrentNextHelpers:
    def _mk(self, rise_min, fall_min, sat='X'):
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        return {
            'sat': sat,
            'rise': now + timedelta(minutes=rise_min),
            'peak': now + timedelta(minutes=(rise_min + fall_min) / 2),
            'fall': now + timedelta(minutes=fall_min),
            'max_elev': 30.0, 'max_az': 180.0, 'rise_az': 90.0,
            'duration_s': int((fall_min - rise_min) * 60),
        }

    def test_current_pass_found(self):
        p_now = self._mk(-5, +5)       # started 5 min ago, ends in 5 min
        p_later = self._mk(+60, +72)
        assert _passes.find_current_pass([p_now, p_later]) is p_now

    def test_no_current_pass_between(self):
        p_past   = self._mk(-30, -20)
        p_future = self._mk(+30, +40)
        assert _passes.find_current_pass([p_past, p_future]) is None

    def test_next_pass_is_first_future(self):
        p_past   = self._mk(-30, -20)
        p_soon   = self._mk(+10, +20)
        p_later  = self._mk(+60, +70)
        assert _passes.find_next_pass([p_past, p_soon, p_later]) is p_soon
