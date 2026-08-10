"""
Weather-satellite pass planner + LRPT/APT decoder plugin.

Despite the directory name, this plugin handles both:
  - NOAA-15 / 18 / 19  → analog APT     via satdump `noaa_apt` pipeline
  - METEOR-M2 2 / 3 / 4 → digital LRPT   via satdump `meteor_m2-x_lrpt`

All six satellites are LEO polar orbiters at 137 MHz, so a single
QFH + SAWbird+NOAA + SDR is enough for the whole set.

Phase 1 (this file): pass prediction only.  Fetches TLEs from CelesTrak
(cached 12 h), predicts the next 24 h of passes for the configured
receiver location, and exposes them via the webserver plugin as a
pass-schedule table.

Phase 2 (later): auto-capture IQ during predicted passes into
plugins/meteor/passes/<timestamp>_<sat>.cf32, then subprocess
`satdump <pipeline> baseband <file>` to produce a PNG image next to it.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from core import Decoder, AppState

from . import passes as _passes


# Storage layout — same pattern as plugins/adsb/adsb_logs.  Resolved
# relative to this file so it's independent of the process cwd.
_HERE       = os.path.dirname(os.path.abspath(__file__))
_TLE_CACHE  = os.path.join(_HERE, 'tle_cache.txt')
_PASSES_DIR = os.path.join(_HERE, 'passes')


class MeteorDecoder(Decoder):
    name            = 'meteor'
    key             = 'l'                         # 'l' = LRPT
    key_help        = 'r=refresh'
    min_sample_rate = 200_000                     # what LRPT needs post-decimation
    realtime        = False
    bg_queue_depth  = 4
    full_view       = False

    # ── webserver plugin contract ────────────────────────────────────────────
    web_title       = 'Weather sats — pass schedule'
    web_slug        = 'meteor'
    web_static_dir  = 'web'
    web_poll_ms     = 60000                       # passes only change every few minutes

    def __init__(self):
        # Config (via preset plugin_states.meteor)
        self._location_lat: float  = None
        self._location_lon: float  = None
        self._location_alt_km      = 0.0
        self._min_elevation_deg    = 15.0
        self._horizon_hours        = 24.0
        # Runtime state — passes are computed on demand and cached briefly
        self._passes_cache         = None         # list of pass dicts
        self._passes_cache_time    = 0.0          # monotonic timestamp
        self._passes_cache_ttl     = 5 * 60       # recompute every 5 min

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, state) -> None:
        pass                                       # nothing to warm up yet

    def stop(self) -> None:
        pass

    # ── DSP (phase 2 will add IQ capture here) ───────────────────────────────

    def process(self, samples, state: AppState, results=None, sdr=None):
        # No-op for phase 1.  Returns a stub result so the plugin behaves
        # like the other Decoders in status_text / rendering.
        return {'active': False}

    def status_text(self, state: AppState, result: dict) -> str:
        cur = self._current_or_next_summary()
        return '[meteor {}] '.format(cur) if cur else '[meteor idle] '

    # ── keys ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            # Invalidate the pass cache so the next web_json refetches TLEs
            # and recomputes.  Useful after moving the antenna or if the
            # user just wants a manual refresh.
            self._passes_cache = None
            self._passes_cache_time = 0.0
            return True
        return False

    # ── persistence ──────────────────────────────────────────────────────────

    def save_state(self) -> dict:
        d = {}
        if self._location_lat is not None:  d['location_lat'] = self._location_lat
        if self._location_lon is not None:  d['location_lon'] = self._location_lon
        if self._location_alt_km:           d['location_alt_km'] = self._location_alt_km
        if self._min_elevation_deg != 15.0: d['min_elevation_deg'] = self._min_elevation_deg
        if self._horizon_hours     != 24.0: d['horizon_hours'] = self._horizon_hours
        return d

    def load_state(self, d: dict) -> None:
        lat = d.get('location_lat')
        lon = d.get('location_lon')
        if lat is not None and lon is not None:
            try:
                self._location_lat = float(lat)
                self._location_lon = float(lon)
            except (TypeError, ValueError):
                pass
        if 'location_alt_km' in d:
            try:    self._location_alt_km = float(d['location_alt_km'])
            except (TypeError, ValueError): pass
        if 'min_elevation_deg' in d:
            try:    self._min_elevation_deg = float(d['min_elevation_deg'])
            except (TypeError, ValueError): pass
        if 'horizon_hours' in d:
            try:    self._horizon_hours = float(d['horizon_hours'])
            except (TypeError, ValueError): pass

    # ── pass computation (with light caching) ────────────────────────────────

    def _get_passes(self) -> list:
        """Predict passes for every satellite in _passes.SATELLITES.
        Cached in-memory for _passes_cache_ttl seconds."""
        if (self._passes_cache is not None
                and time.monotonic() - self._passes_cache_time < self._passes_cache_ttl):
            return self._passes_cache
        if self._location_lat is None or self._location_lon is None:
            return []
        try:
            tle_path = _passes.fetch_tle_cache(_TLE_CACHE)
            all_passes = _passes.predict_all(
                _passes.SATELLITES, tle_path,
                self._location_lat, self._location_lon, self._location_alt_km,
                self._horizon_hours, self._min_elevation_deg,
            )
        except Exception:
            all_passes = []
        self._passes_cache = all_passes
        self._passes_cache_time = time.monotonic()
        return all_passes

    def _current_or_next_summary(self) -> str:
        """A tiny one-line description of what's happening now, for the
        SDRTerm status bar."""
        passes = self._get_passes()
        if not passes:
            return ''
        cur = _passes.find_current_pass(passes)
        if cur:
            return '{} @ {:.0f}°'.format(cur['sat'], cur['max_elev'])
        nxt = _passes.find_next_pass(passes)
        if nxt:
            secs = int((nxt['rise'] - datetime.now(timezone.utc)).total_seconds())
            return '{} in {}m'.format(nxt['sat'], secs // 60)
        return ''

    # ── web view ─────────────────────────────────────────────────────────────

    def web_json(self, query: dict = None) -> dict:
        passes = self._get_passes()
        now = datetime.now(timezone.utc)

        def pack(p):
            return {
                'sat':          p['sat'],
                'rise':         p['rise'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                'peak':         p['peak'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                'fall':         p['fall'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                'max_elev':     round(p['max_elev'], 1),
                'max_az':       round(p['max_az'], 1),
                'rise_az':      round(p['rise_az'], 1),
                'duration_s':   p['duration_s'],
                'sat_info':     _passes.SATELLITES.get(p['sat'], {}),
                'in_progress':  p['rise'] <= now <= p['fall'],
            }

        return {
            'passes':        [pack(p) for p in passes],
            'receiver':      ({'lat': self._location_lat, 'lon': self._location_lon}
                              if self._location_lat is not None else None),
            'satdump':       shutil.which('satdump') is not None,
            'known_sats':    list(_passes.SATELLITES.keys()),
            'generated_at':  now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
