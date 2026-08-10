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
                                       'auto_tune', 'auto_capture',
                                       'tuned_sat', 'tuned_freq_hz',
                                       'capturing', 'capture_sat', 'captures'}
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


class _FakeStateWithDecoders(_FakeState):
    def __init__(self, center_hz=100_000_000, bw_hz=2_000_000, active=('rtl-tcp-passive',)):
        super().__init__(center_hz)
        self.bw_hz = bw_hz
        self.active_decoders = set(active)


class TestAutoCapture:
    """process() should launch satdump on pass rise and terminate on fall."""

    def _pass(self, sat='METEOR-M2 3', offset_min=(-1, +5)):
        now = datetime.now(timezone.utc)
        return {
            'sat':        sat,
            'rise':       now + timedelta(minutes=offset_min[0]),
            'peak':       now + timedelta(minutes=(offset_min[0] + offset_min[1]) / 2),
            'fall':       now + timedelta(minutes=offset_min[1]),
            'max_elev':   50.0, 'max_az': 180.0, 'rise_az': 90.0,
            'duration_s': int((offset_min[1] - offset_min[0]) * 60),
        }

    def _mk(self, passes, tmp_path):
        # Redirect the capture root to tmp so tests never write to the
        # real plugins/meteor/web/captures directory.
        import plugins.meteor.meteor as mod
        d = MeteorDecoder()
        d._passes_cache = passes
        d._passes_cache_time = 1e18
        # Monkey-patch _HERE for this instance's file operations by
        # patching the module constant referenced inside meteor.py.
        return d, mod, str(tmp_path)

    def test_capture_launches_on_pass_rise(self, tmp_path, monkeypatch):
        d, mod, root = self._mk([self._pass()], tmp_path)
        monkeypatch.setattr(mod, '_HERE', root)
        # Capture the Popen call without actually spawning satdump
        captured_args = {}
        class _FakeProc:
            def __init__(self): self._alive = True
            def poll(self): return None if self._alive else 0
            def terminate(self): self._alive = False
            def wait(self, timeout=None): pass
            def kill(self): pass
        def _fake_popen(args, **kw):
            captured_args['argv'] = args
            captured_args['kw'] = kw
            return _FakeProc()
        monkeypatch.setattr(mod.subprocess, 'Popen', _fake_popen)
        monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/local/bin/satdump')

        state = _FakeStateWithDecoders()
        result = d.process(None, state)
        assert result['capturing'] is True
        assert result['capture_sat'] == 'METEOR-M2 3'
        assert 'satdump' in captured_args['argv'][0]
        assert 'live' in captured_args['argv']
        assert 'meteor_m2-x_lrpt' in captured_args['argv']
        assert '--source' in captured_args['argv']
        assert 'rtltcp' in captured_args['argv']
        assert '137100000' in captured_args['argv']

    def test_capture_terminates_on_pass_fall(self, tmp_path, monkeypatch):
        d, mod, root = self._mk([self._pass()], tmp_path)
        monkeypatch.setattr(mod, '_HERE', root)
        terminated = {'flag': False}
        class _FakeProc:
            def poll(self): return None
            def terminate(self): terminated['flag'] = True
            def wait(self, timeout=None): pass
            def kill(self): pass
        monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **kw: _FakeProc())
        monkeypatch.setattr(mod.shutil, 'which', lambda name: '/x/satdump')
        state = _FakeStateWithDecoders()
        d.process(None, state)              # rise
        assert d._capture_proc is not None
        # Pass ends
        d._passes_cache = []
        d.process(None, state)
        assert terminated['flag'] is True
        assert d._capture_proc is None

    def test_no_capture_when_rtltcp_not_active(self, tmp_path, monkeypatch):
        d, mod, root = self._mk([self._pass()], tmp_path)
        monkeypatch.setattr(mod, '_HERE', root)
        called = {'flag': False}
        monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **kw: called.update(flag=True))
        monkeypatch.setattr(mod.shutil, 'which', lambda name: '/x/satdump')
        state = _FakeStateWithDecoders(active=())  # rtl-tcp-passive NOT active
        d.process(None, state)
        assert called['flag'] is False
        assert d._capture_proc is None

    def test_no_capture_when_satdump_missing(self, tmp_path, monkeypatch):
        d, mod, root = self._mk([self._pass()], tmp_path)
        monkeypatch.setattr(mod, '_HERE', root)
        called = {'flag': False}
        monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **kw: called.update(flag=True))
        monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
        state = _FakeStateWithDecoders()
        d.process(None, state)
        assert called['flag'] is False

    def test_auto_capture_toggle_disables(self, tmp_path, monkeypatch):
        d, mod, root = self._mk([self._pass()], tmp_path)
        monkeypatch.setattr(mod, '_HERE', root)
        d._auto_capture = False
        called = {'flag': False}
        monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **kw: called.update(flag=True))
        monkeypatch.setattr(mod.shutil, 'which', lambda name: '/x/satdump')
        state = _FakeStateWithDecoders()
        d.process(None, state)
        assert called['flag'] is False

    def test_c_key_toggles_capture(self, monkeypatch):
        d = MeteorDecoder()
        assert d._auto_capture is True
        d.handle_key(ord('c'), None, None)
        assert d._auto_capture is False
        d.handle_key(ord('c'), None, None)
        assert d._auto_capture is True

    def test_save_load_state_capture_options(self):
        d = MeteorDecoder()
        d.load_state({'auto_capture': False, 'rtltcp_port': 9999,
                      'rtltcp_host': '10.0.0.5'})
        assert d._auto_capture is False
        assert d._rtltcp_port == 9999
        assert d._rtltcp_host == '10.0.0.5'
        s = d.save_state()
        assert s['auto_capture'] is False
        assert s['rtltcp_port'] == 9999
        assert s['rtltcp_host'] == '10.0.0.5'


class TestListCaptures:
    def test_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        import plugins.meteor.meteor as mod
        monkeypatch.setattr(mod, '_HERE', str(tmp_path))
        d = MeteorDecoder()
        assert d._list_captures() == []

    def test_lists_pngs_sorted_by_mtime(self, tmp_path, monkeypatch):
        import plugins.meteor.meteor as mod
        monkeypatch.setattr(mod, '_HERE', str(tmp_path))
        # Two capture dirs, three PNGs total.
        cap_root = tmp_path / 'web' / 'captures'
        cap_root.mkdir(parents=True)
        (cap_root / '2026-08-10T10-00-00Z_METEOR-M2_3').mkdir()
        (cap_root / '2026-08-10T10-00-00Z_METEOR-M2_3' / 'ir.png').write_bytes(b'fake')
        (cap_root / '2026-08-10T10-00-00Z_METEOR-M2_3' / 'vis.png').write_bytes(b'fake')
        import time as time_mod
        time_mod.sleep(0.01)                 # ensure the second is newer
        (cap_root / '2026-08-10T11-00-00Z_NOAA_19').mkdir()
        (cap_root / '2026-08-10T11-00-00Z_NOAA_19' / 'apt.png').write_bytes(b'fake')
        d = MeteorDecoder()
        caps = d._list_captures()
        assert len(caps) == 2
        assert caps[0]['dir'].startswith('2026-08-10T11-')      # newer first
        assert caps[0]['images'] == ['apt.png']
        assert caps[1]['preview'] == 'ir.png'
        assert len(caps[1]['images']) == 2
