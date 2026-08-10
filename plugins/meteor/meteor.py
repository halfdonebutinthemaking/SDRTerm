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
    key_help        = 'r=refresh  a=auto-tune  c=auto-capture'
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
        self._auto_tune            = True         # retune SDR to pass freq on rise
        self._auto_capture         = True         # spawn satdump on pass rise
        self._rtltcp_port          = 1234         # SDRTerm's rtl-tcp-passive default
        self._rtltcp_host          = '127.0.0.1'
        # Runtime state — passes are computed on demand and cached briefly
        self._passes_cache         = None         # list of pass dicts
        self._passes_cache_time    = 0.0          # monotonic timestamp
        self._passes_cache_ttl     = 5 * 60       # recompute every 5 min
        # Track the currently-tuned pass so we don't re-tune every process() call
        self._tuned_sat            = None
        self._tuned_freq_hz        = None
        # Capture subprocess bookkeeping
        self._capture_proc         = None         # subprocess.Popen or None
        self._capture_sat          = None         # which sat we're capturing
        self._capture_dir          = None         # output dir this satdump is writing to
        self._capture_started_at   = None         # datetime UTC

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, state) -> None:
        pass                                       # nothing to warm up yet

    def stop(self) -> None:
        # If we're mid-capture, kill satdump cleanly on shutdown so it
        # doesn't linger as an orphaned process.
        self._end_capture(reason='stop')

    # ── DSP + auto-tune + auto-capture ───────────────────────────────────────

    def process(self, samples, state: AppState, results=None, sdr=None):
        """No IQ processing here — satdump reads the samples directly from
        SDRTerm's rtl-tcp-passive server.  This method is used only for
        pass-state bookkeeping: auto-tune on rise, launch satdump on
        rise, and terminate satdump on fall.
        """
        cur = _passes.find_current_pass(self._get_passes()) if state is not None else None

        if cur is None:
            self._tuned_sat = None
            self._tuned_freq_hz = None
            # Pass just ended? Terminate satdump cleanly.
            if self._capture_proc is not None:
                self._end_capture(reason='pass_fall')
            return {'active': False, 'current_sat': None,
                    'auto_tune': self._auto_tune, 'capturing': False}

        target_hz = _passes.SATELLITES.get(cur['sat'], {}).get('freq_hz')
        already_on_pass = (self._tuned_sat == cur['sat']
                           and self._tuned_freq_hz == target_hz)

        if (self._auto_tune and target_hz is not None
                and state is not None and not already_on_pass):
            self._retune(state, sdr, target_hz)
            self._tuned_sat = cur['sat']
            self._tuned_freq_hz = target_hz

        # Launch satdump exactly once per pass (idempotent guard on
        # _capture_sat match); poll and reap on subsequent calls.
        if self._auto_capture and self._capture_proc is None \
                and (self._capture_sat != cur['sat']):
            self._start_capture(cur, state)

        # Reap zombie satdump if it exited on its own (e.g. --timeout hit)
        if self._capture_proc is not None:
            rc = self._capture_proc.poll()
            if rc is not None:
                self._capture_proc = None      # process is done, keep dir

        return {
            'active':        True,
            'current_sat':   cur['sat'],
            'current_freq':  target_hz,
            'auto_tune':     self._auto_tune,
            'capturing':     self._capture_proc is not None,
            'capture_sat':   self._capture_sat,
        }

    # ── satdump subprocess control ───────────────────────────────────────────

    def _start_capture(self, current_pass: dict, state: AppState) -> None:
        """Spawn satdump live-mode against SDRTerm's rtl-tcp-passive server.

        No-op with a status message on failure (satdump missing, rtl-tcp
        plugin not active, output dir not writable, etc.) — we do NOT
        raise from here because we're on the SDRTerm worker thread.
        """
        sat_name = current_pass['sat']
        sat_info = _passes.SATELLITES.get(sat_name, {})
        pipeline = sat_info.get('satdump_pipeline')
        freq_hz  = sat_info.get('freq_hz')
        if not pipeline or not freq_hz:
            return

        satdump_bin = shutil.which('satdump')
        if satdump_bin is None:
            self._capture_sat = sat_name             # remember we tried
            return

        # Require rtl-tcp-passive to be active — that's the only way
        # satdump gets the samples.  Missing/absent active_decoders
        # attribute counts as "no plugins active" and skips capture.
        active_decoders = getattr(state, 'active_decoders', None) or set()
        if 'rtl-tcp-passive' not in active_decoders:
            self._capture_sat = sat_name
            return

        # Where satdump will drop its output.  Placed inside the plugin's
        # web/ tree so the webserver's /static/meteor/ route can serve
        # the resulting PNGs directly.
        ts_slug   = current_pass['rise'].strftime('%Y-%m-%dT%H-%M-%SZ')
        safe_sat  = sat_name.replace(' ', '_').replace('/', '_')
        cap_dir   = os.path.join(_HERE, 'web', 'captures',
                                 '{}_{}'.format(ts_slug, safe_sat))
        try:
            os.makedirs(cap_dir, exist_ok=True)
        except OSError:
            return

        samplerate = int(state.bw_hz) if state is not None else 250_000
        # Cap the timeout at the pass's remaining seconds + 30 s slack —
        # satdump will exit on its own if the pass ends before we send
        # SIGTERM (e.g. if SDRTerm was killed uncleanly).
        remaining = int((current_pass['fall'] -
                         datetime.now(timezone.utc)).total_seconds())
        timeout   = max(60, remaining + 30)

        args = [
            satdump_bin, 'live', pipeline, cap_dir,
            '--source',     'rtltcp',
            '--frequency',  str(int(freq_hz)),
            '--samplerate', str(samplerate),
            '--tcp_address', self._rtltcp_host,
            '--tcp_port',   str(self._rtltcp_port),
            '--timeout',    str(timeout),
        ]
        log_path = os.path.join(cap_dir, 'satdump.log')
        try:
            log_fh = open(log_path, 'w')
            self._capture_proc = subprocess.Popen(
                args, stdout=log_fh, stderr=subprocess.STDOUT,
            )
        except Exception:
            self._capture_proc = None
            return

        self._capture_sat = sat_name
        self._capture_dir = cap_dir
        self._capture_started_at = datetime.now(timezone.utc)

    def _end_capture(self, reason: str = 'pass_fall') -> None:
        """SIGTERM the satdump subprocess (if any), give it up to 10 s to
        flush + exit, then release our handles."""
        if self._capture_proc is None:
            self._capture_sat = None
            return
        try:
            self._capture_proc.terminate()
            self._capture_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:    self._capture_proc.kill()
            except Exception: pass
        except Exception:
            pass
        self._capture_proc = None
        self._capture_sat = None
        self._capture_dir = None
        self._capture_started_at = None

    def _retune(self, state, sdr, target_hz: int) -> None:
        """Move both the AppState's tuned frequency and (if available) the
        live SDR to `target_hz`.  Safe to call when the frequency is
        already close — a 1 kHz tolerance avoids twiddling for rounding
        noise.
        """
        try:
            current = float(state.center_hz)
        except AttributeError:
            return
        if abs(current - target_hz) < 1000:
            return
        state.center_hz = float(target_hz)
        if sdr is not None:
            try:
                sdr.center_freq = float(target_hz)
            except Exception:
                # Some SDR backends can't retune mid-stream; the state
                # update alone will apply at the next reconfig.
                pass

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
        if key == ord('a'):
            self._auto_tune = not self._auto_tune
            # Toggling off doesn't retune away; toggling on will fire on
            # the next process() call if a pass is active.
            return True
        if key == ord('c'):
            self._auto_capture = not self._auto_capture
            # Turning capture off mid-pass terminates the running satdump.
            if not self._auto_capture:
                self._end_capture(reason='user_disabled')
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
        if not self._auto_tune:             d['auto_tune'] = False
        if not self._auto_capture:          d['auto_capture'] = False
        if self._rtltcp_port != 1234:       d['rtltcp_port'] = self._rtltcp_port
        if self._rtltcp_host != '127.0.0.1': d['rtltcp_host'] = self._rtltcp_host
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
        if 'auto_tune' in d:
            self._auto_tune = bool(d['auto_tune'])
        if 'auto_capture' in d:
            self._auto_capture = bool(d['auto_capture'])
        if 'rtltcp_port' in d:
            try:    self._rtltcp_port = int(d['rtltcp_port'])
            except (TypeError, ValueError): pass
        if 'rtltcp_host' in d:
            self._rtltcp_host = str(d['rtltcp_host'])

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
            'auto_tune':     self._auto_tune,
            'auto_capture':  self._auto_capture,
            'tuned_sat':     self._tuned_sat,
            'tuned_freq_hz': self._tuned_freq_hz,
            'capturing':     self._capture_proc is not None,
            'capture_sat':   self._capture_sat,
            'captures':      self._list_captures(),
            'generated_at':  now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

    def _list_captures(self, max_items: int = 30) -> list:
        """Enumerate satdump output subdirectories under web/captures/,
        newest first.  Each item lists the PNGs satdump produced so the
        browser can render a thumbnail gallery via the /static/meteor/
        route.  Silently returns [] if the captures dir doesn't exist
        yet."""
        root = os.path.join(_HERE, 'web', 'captures')
        if not os.path.isdir(root):
            return []
        try:
            names = os.listdir(root)
        except OSError:
            return []
        items = []
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            try:
                pngs = sorted(f for f in os.listdir(path) if f.lower().endswith('.png'))
            except OSError:
                continue
            items.append({
                'dir':      name,                       # dir name = URL segment
                'mtime':    os.path.getmtime(path),
                'images':   pngs,
                'preview':  pngs[0] if pngs else None,  # first PNG = thumbnail
            })
        items.sort(key=lambda x: -x['mtime'])
        return items[:max_items]
