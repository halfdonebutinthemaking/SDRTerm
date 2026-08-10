"""
Pass prediction for Meteor-M weather satellites.

Thin wrapper over pyorbital: fetches CelesTrak's weather-satellite TLE
bundle (cached locally for 12 h) and computes the upcoming visibility
passes over a receiver location.  Nothing SDR-specific here — that
lives in meteor.py.
"""

import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from pyorbital.orbital import Orbital


# Public CelesTrak endpoint — always current-day TLEs for the "weather"
# group (NOAA, Meteor, Metop, FengYun, etc).  ~200 sats, ~14 kB payload.
_TLE_URL      = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle'
_TLE_MAX_AGE  = 12 * 60 * 60      # refresh from CelesTrak every 12 h

# Weather satellites reachable with a 137 MHz QFH + SAWbird+NOAA.  Two
# families, both LEO polar orbiters, same antenna + LNA — the only
# per-sat difference is the tuning frequency and which satdump pipeline
# decodes the resulting IQ.  Names must match CelesTrak's line-1 label
# exactly or pyorbital.Orbital() raises KeyError.
#
# Not included: GOES (geostationary, 1.7 GHz dish only), Metop HRPT
# (1.7 GHz, needs dish + tracker), FengYun HRPT (same), DMSP (military
# 1.7 GHz).  Different band, different antenna.
SATELLITES = {
    # ── NOAA POES: analog APT (Automatic Picture Transmission) ────────
    'NOAA 15': {'freq_hz': 137_620_000, 'mode': 'APT',
                'satdump_pipeline': 'noaa_apt',
                'bw_hz':      50_000,
                'note': 'intermittent transmitter since 2019'},
    'NOAA 18': {'freq_hz': 137_912_500, 'mode': 'APT',
                'satdump_pipeline': 'noaa_apt',
                'bw_hz':      50_000,
                'note': 'active'},
    'NOAA 19': {'freq_hz': 137_100_000, 'mode': 'APT',
                'satdump_pipeline': 'noaa_apt',
                'bw_hz':      50_000,
                'note': 'active (shares 137.100 with METEOR-M2 3)'},

    # ── Meteor-M: digital QPSK LRPT (Low-Rate Picture Transmission) ───
    'METEOR-M2 2': {'freq_hz': 137_900_000, 'mode': 'LRPT',
                    'satdump_pipeline': 'meteor_m2-x_lrpt',
                    'symbol_rate': 72_000, 'bw_hz': 150_000,
                    'note': 'degraded thermal control, sporadic'},
    'METEOR-M2 3': {'freq_hz': 137_100_000, 'mode': 'LRPT',
                    'satdump_pipeline': 'meteor_m2-x_lrpt',
                    'symbol_rate': 72_000, 'bw_hz': 150_000,
                    'note': 'active (shares 137.100 with NOAA 19)'},
    'METEOR-M2 4': {'freq_hz': 137_900_000, 'mode': 'LRPT',
                    'satdump_pipeline': 'meteor_m2-x_lrpt',
                    'symbol_rate': 72_000, 'bw_hz': 150_000,
                    'note': 'active'},
}
# Alias kept for anyone who imported the old name — will be removed
# once the meteor.py migration lands.
METEOR_SATS = SATELLITES


# ── TLE cache ────────────────────────────────────────────────────────────────

def fetch_tle_cache(cache_path: str, max_age_s: int = _TLE_MAX_AGE) -> str:
    """Return path to a cached CelesTrak TLE file, downloading if stale.

    The cache lives next to the plugin (or wherever the caller asks).  If
    the network is unreachable and a stale file already exists, we return
    it anyway — better an out-of-date pass than none at all.
    """
    if os.path.isfile(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < max_age_s:
            return cache_path
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with urllib.request.urlopen(_TLE_URL, timeout=15) as r:
            data = r.read()
        with open(cache_path, 'wb') as f:
            f.write(data)
    except Exception:
        # If a stale copy is still on disk, fall back to it silently.
        if not os.path.isfile(cache_path):
            raise
    return cache_path


# ── pass prediction ──────────────────────────────────────────────────────────

def predict_passes(sat_name: str, tle_path: str,
                   rx_lat: float, rx_lon: float, rx_alt_km: float = 0.0,
                   horizon_hours: float = 24.0,
                   min_elevation_deg: float = 15.0) -> list:
    """Return upcoming passes of ``sat_name`` visible from the receiver.

    Each pass is a dict with:
        sat        — satellite name (as passed in)
        rise       — UTC datetime of AOS (acquisition of signal)
        peak       — UTC datetime of maximum elevation
        fall       — UTC datetime of LOS (loss of signal)
        max_elev   — degrees above horizon at peak
        max_az     — azimuth at peak (degrees, CW from N)
        rise_az    — azimuth at AOS
        duration_s — LOS − AOS in seconds
    Passes below ``min_elevation_deg`` at their peak are filtered out.
    """
    orb = Orbital(sat_name, tle_file=tle_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # pyorbital wants naive UTC
    # pyorbital's get_next_passes wants an int for horizon length in hours.
    raw = orb.get_next_passes(now, int(horizon_hours),
                              rx_lon, rx_lat, rx_alt_km,
                              tol=0.001, horizon=0.0)
    result = []
    for rise, fall, peak in raw:
        max_az, max_el = orb.get_observer_look(peak, rx_lon, rx_lat, rx_alt_km)
        if max_el < min_elevation_deg:
            continue
        rise_az, _ = orb.get_observer_look(rise, rx_lon, rx_lat, rx_alt_km)
        result.append({
            'sat':        sat_name,
            'rise':       rise.replace(tzinfo=timezone.utc),
            'peak':       peak.replace(tzinfo=timezone.utc),
            'fall':       fall.replace(tzinfo=timezone.utc),
            'max_elev':   float(max_el),
            'max_az':     float(max_az),
            'rise_az':    float(rise_az),
            'duration_s': int((fall - rise).total_seconds()),
        })
    return result


def predict_all(sats: dict, tle_path: str,
                rx_lat: float, rx_lon: float, rx_alt_km: float = 0.0,
                horizon_hours: float = 24.0,
                min_elevation_deg: float = 15.0) -> list:
    """Predict passes for every satellite in ``sats`` and return them
    merged and sorted by rise time — easy to feed straight into a
    schedule table."""
    all_passes = []
    for name in sats:
        try:
            all_passes.extend(predict_passes(
                name, tle_path, rx_lat, rx_lon, rx_alt_km,
                horizon_hours, min_elevation_deg,
            ))
        except KeyError:
            # TLE for that satellite isn't in the CelesTrak bundle —
            # probably decommissioned. Skip silently.
            continue
    all_passes.sort(key=lambda p: p['rise'])
    return all_passes


def find_current_pass(passes: list, now=None):
    """Return the pass currently in progress (rise ≤ now ≤ fall), or None."""
    now = now or datetime.now(timezone.utc)
    for p in passes:
        if p['rise'] <= now <= p['fall']:
            return p
    return None


def find_next_pass(passes: list, now=None):
    """Return the next pass whose rise is in the future, or None."""
    now = now or datetime.now(timezone.utc)
    for p in passes:
        if p['rise'] > now:
            return p
    return None
