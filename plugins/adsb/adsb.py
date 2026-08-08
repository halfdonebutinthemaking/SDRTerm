"""
ADS-B (Automatic Dependent Surveillance – Broadcast) decoder.

Decodes Mode S long-format extended squitters (DF17) at 1090 MHz.
See docs/pipeline.tex for the mathematical walkthrough.

Signal chain:
  IQ @ ≥2 MSPS → resample to 2 MSPS → |x|² envelope
   → matched preamble correlator → local-max peak find
   → PPM bit slice (P0 vs P1 in each 1 µs slot)
   → CRC-24 check → DF17 dispatch by type code
   → callsign / airborne position (CPR) / velocity
   → aircraft table + on-screen list
"""

import math
import os
import time
from collections import deque

import numpy as np
from scipy.signal import resample_poly

from core import Decoder, AppState


# ── signal constants ─────────────────────────────────────────────────────────

_INTERNAL_SR       = 2_000_000                     # internal working rate
_SPS               = _INTERNAL_SR // 1_000_000     # 2 samples per bit
_PREAMBLE_US       = 8
_PREAMBLE_LEN      = _PREAMBLE_US * _SPS           # 16 samples
_SHORT_MSG_BITS    = 56
_LONG_MSG_BITS     = 112
_LONG_MSG_SAMPLES  = (_PREAMBLE_US + _LONG_MSG_BITS) * _SPS   # 240 samples ≈ 120 µs
_MIN_SAMPLE_RATE   = 2_000_000

_DF_ADSB           = 17

# CRC-24 polynomial (24-bit reduction form of G(x) = x^24 + ... + 1)
_CRC_POLY          = 0xFFF409


# ── preamble template (built once) ───────────────────────────────────────────

def _make_preamble_template() -> np.ndarray:
    """+val at pulse-on samples, −val at pulse-off samples, sum = 0."""
    t = np.zeros(_PREAMBLE_LEN, dtype=np.float32)
    for pulse_us in (0.0, 1.0, 3.5, 4.5):
        s = int(round(pulse_us * _SPS))
        w = max(1, int(round(0.5 * _SPS)))
        t[s:s + w] = 1.0
    n_pulse = int(t.sum())
    n_dead  = len(t) - n_pulse
    return np.where(t > 0, 1.0 / n_pulse, -1.0 / n_dead).astype(np.float32)


_TEMPLATE     = _make_preamble_template()
_TEMPLATE_REV = _TEMPLATE[::-1].copy()   # for correlation via np.convolve


# ── CRC-24 (bitwise, exact) ──────────────────────────────────────────────────

def _crc24(msg_bytes) -> int:
    """Mode-S CRC-24 remainder. For a valid 112-bit frame the result is 0.

    Bit-serial implementation: 112 bits × 14 frames/s worst case = trivial CPU.
    """
    crc = 0
    for byte in msg_bytes:
        for i in range(7, -1, -1):
            top = crc & 0x800000
            crc = ((crc << 1) | ((byte >> i) & 1)) & 0xFFFFFF
            if top:
                crc ^= _CRC_POLY
    return crc


# ── envelope, correlator, peak detection ─────────────────────────────────────

def _envelope(x: np.ndarray) -> np.ndarray:
    xr = x.real
    xi = x.imag
    return (xr * xr + xi * xi).astype(np.float32)


def _correlate(env: np.ndarray) -> np.ndarray:
    """Correlate envelope with the preamble template.

    score[k] is high when a preamble starts at env[k].
    """
    if len(env) < _PREAMBLE_LEN:
        return np.empty(0, dtype=np.float32)
    return np.convolve(env, _TEMPLATE_REV, mode='valid').astype(np.float32)


def _find_peaks(score: np.ndarray, threshold: float, min_gap: int,
                refine_window: int = _PREAMBLE_LEN):
    """Local-max-refined threshold crossings, spaced by at least min_gap.

    The correlator response to a real preamble has strong side-lobes ±1..±9
    samples around the true peak (partial-overlap artefacts, not noise), so
    a plain "first above threshold" scan can lock onto a side-lobe and then
    the min_gap skip misses the real peak.  For every window where the
    signal is above threshold, take the argmax inside the next
    ``refine_window`` samples.
    """
    peaks = []
    n = len(score)
    i = 0
    while i < n:
        if score[i] > threshold:
            window_end = min(n, i + refine_window)
            local_max_off = int(np.argmax(score[i:window_end]))
            peak_idx = i + local_max_off
            peaks.append(peak_idx)
            i = peak_idx + min_gap
        else:
            i += 1
    return peaks


# ── PPM bit slicing ──────────────────────────────────────────────────────────

def _slice_bits(env: np.ndarray, start: int, n_bits: int = _LONG_MSG_BITS):
    """Return (bits, confidences) for `n_bits` PPM slots starting after preamble."""
    data_start = start + _PREAMBLE_LEN
    end = data_start + n_bits * _SPS
    if end > len(env):
        return None, None
    reshaped = env[data_start:end].reshape(n_bits, _SPS)
    half = _SPS // 2
    P0 = reshaped[:, :half].sum(axis=1)
    P1 = reshaped[:, half:].sum(axis=1)
    bits = (P0 > P1).astype(np.uint8)
    conf = np.abs(P0 - P1) / (P0 + P1 + 1e-12)
    return bits, conf.astype(np.float32)


# ── bit helpers ──────────────────────────────────────────────────────────────

def _bits_to_bytes(bits) -> bytes:
    """MSB-first packing: bits[0] is byte0's MSB."""
    n = len(bits) // 8
    out = bytearray(n)
    for i in range(n):
        b = 0
        for j in range(8):
            b = (b << 1) | int(bits[i * 8 + j])
        out[i] = b
    return bytes(out)


def _bytes_to_bits(msg_bytes) -> np.ndarray:
    """MSB-first unpacking."""
    bits = np.zeros(len(msg_bytes) * 8, dtype=np.uint8)
    for i, byte in enumerate(msg_bytes):
        for j in range(8):
            bits[i * 8 + j] = (byte >> (7 - j)) & 1
    return bits


def _bits_slice_int(bits, start: int, length: int) -> int:
    v = 0
    for i in range(length):
        v = (v << 1) | int(bits[start + i])
    return v


# ── DF17 sub-parsers ─────────────────────────────────────────────────────────

# Position 0 = '#' (illegal); 1..26 = A..Z; 32 = space; 48..57 = 0..9; else '#'.
_CALLSIGN_CHARS = '#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######'


def _parse_identification(me_bits) -> str:
    """TC 1..4 — 8-char callsign, 6 bits per char, from ME bits 8..55."""
    chars = []
    for i in range(8):
        idx = _bits_slice_int(me_bits, 8 + i * 6, 6)
        chars.append(_CALLSIGN_CHARS[idx])
    return ''.join(chars).replace('#', '').strip()


def _decode_ac12(ac12: int):
    """12-bit AC field → altitude in feet, or None if invalid.

    Only Q=1 (25-ft increments) is handled — the Gillham/Mode-C form (Q=0)
    is rare on modern civilian aircraft and skipped here.
    """
    if ac12 == 0:
        return None
    q_bit = (ac12 >> 4) & 1
    if q_bit == 1:
        n = ((ac12 & 0xFE0) >> 1) | (ac12 & 0x0F)   # strip the Q bit
        return n * 25 - 1000
    return None


def _parse_airborne_position(me_bits):
    """TC 9..18, 20..22 — returns (odd_flag, alt_ft_or_None, cpr_lat, cpr_lon)."""
    ac12    = _bits_slice_int(me_bits, 8, 12)
    alt_ft  = _decode_ac12(ac12)
    odd     = int(me_bits[21])
    lat_cpr = _bits_slice_int(me_bits, 22, 17)
    lon_cpr = _bits_slice_int(me_bits, 39, 17)
    return odd, alt_ft, lat_cpr, lon_cpr


def _parse_velocity(me_bits):
    """TC=19 velocity message. Handles all four subtypes:

      1/2 → ground-referenced (v_EW, v_NS → speed, ground-track heading)
      3/4 → airspeed-referenced (magnetic heading, IAS or TAS)
      2/4 → supersonic (4× the subsonic units)

    Returns dict with 'speed', 'heading', 'vr', 'spd_type' — or None if
    the frame carries no usable data (e.g. all-zero velocity fields, or a
    heading-status bit that says 'no heading available').  Note the
    'heading' semantics differ between subtype groups: for 1/2 it is the
    ground track (crab-angle-corrected); for 3/4 it is the magnetic
    heading the aircraft's nose is pointing.  Both go to the same 'HDG'
    column in the display, as per pyModeS / dump1090 convention.
    """
    subtype = _bits_slice_int(me_bits, 5, 3)

    vr_sign = int(me_bits[36])
    vr_raw  = _bits_slice_int(me_bits, 37, 9) - 1
    vr      = (-1 if vr_sign else 1) * vr_raw * 64

    if subtype in (1, 2):
        ew_dir = int(me_bits[13])
        ew_vel = _bits_slice_int(me_bits, 14, 10) - 1
        ns_dir = int(me_bits[24])
        ns_vel = _bits_slice_int(me_bits, 25, 10) - 1
        if ew_vel < 0 or ns_vel < 0:
            return None
        if subtype == 2:                       # supersonic: 4× units
            ew_vel *= 4
            ns_vel *= 4
        v_ew = -ew_vel if ew_dir else ew_vel
        v_ns = -ns_vel if ns_dir else ns_vel
        speed   = math.sqrt(v_ew * v_ew + v_ns * v_ns)
        heading = math.degrees(math.atan2(v_ew, v_ns)) % 360.0
        return {'speed': int(round(speed)), 'heading': int(round(heading)) % 360,
                'vr': int(vr), 'spd_type': 'GS'}

    if subtype in (3, 4):
        hdg_valid = int(me_bits[13])
        spd_raw   = _bits_slice_int(me_bits, 25, 10)
        if spd_raw == 0:
            return None                        # no airspeed → nothing usable
        spd = spd_raw - 1
        if subtype == 4:                       # supersonic: 4× units
            spd *= 4
        spd_type = 'TAS' if int(me_bits[24]) else 'IAS'

        result = {'speed': int(spd), 'vr': int(vr), 'spd_type': spd_type}
        if hdg_valid:
            hdg_raw = _bits_slice_int(me_bits, 14, 10)
            result['heading'] = int(round(hdg_raw * 360.0 / 1024.0)) % 360
        return result

    return None


# ── CPR position decoding ────────────────────────────────────────────────────

_CPR_NZ = 15


def _cpr_nl(lat: float) -> int:
    """Number of longitude zones at latitude `lat` (ICAO closed-form)."""
    a = abs(lat)
    if a >= 87.0:
        return 1
    numerator = 1.0 - math.cos(math.pi / (2 * _CPR_NZ))
    denom = math.cos(math.radians(a)) ** 2
    try:
        return int(math.floor(2 * math.pi / math.acos(1 - numerator / denom)))
    except (ValueError, ZeroDivisionError):
        return 1


def _cpr_global(lat_e_cpr, lon_e_cpr, lat_o_cpr, lon_o_cpr, use_odd: bool):
    """Global CPR decode from one even + one odd frame. Returns (lat, lon) or None."""
    Dlat_e = 360.0 / (4 * _CPR_NZ)             # 6.0
    Dlat_o = 360.0 / (4 * _CPR_NZ - 1)         # ≈ 6.10169

    lat_e = lat_e_cpr / 131072.0
    lat_o = lat_o_cpr / 131072.0
    lon_e = lon_e_cpr / 131072.0
    lon_o = lon_o_cpr / 131072.0

    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    lat_e_deg = Dlat_e * ((j % 60) + lat_e)
    lat_o_deg = Dlat_o * ((j % 59) + lat_o)
    if lat_e_deg >= 270: lat_e_deg -= 360
    if lat_o_deg >= 270: lat_o_deg -= 360

    if _cpr_nl(lat_e_deg) != _cpr_nl(lat_o_deg):
        return None                            # frames straddle a zone boundary

    lat = lat_o_deg if use_odd else lat_e_deg
    nl  = _cpr_nl(lat)
    ni_e = max(1, nl)
    ni_o = max(1, nl - 1)
    Dlon_e = 360.0 / ni_e
    Dlon_o = 360.0 / ni_o
    m = math.floor(lon_e * (nl - 1) - lon_o * nl + 0.5)

    if use_odd:
        lon = Dlon_o * ((m % ni_o) + lon_o)
    else:
        lon = Dlon_e * ((m % ni_e) + lon_e)
    if lon >= 180: lon -= 360
    return lat, lon


def _cpr_local(lat_ref: float, lon_ref: float, cpr_lat, cpr_lon, odd: int):
    """Local CPR decode using a reference position (last known or antenna)."""
    Dlat = 360.0 / (4 * _CPR_NZ - odd)
    lat_frac = cpr_lat / 131072.0
    lon_frac = cpr_lon / 131072.0

    j = math.floor(lat_ref / Dlat) + math.floor(
        0.5 + ((lat_ref % Dlat) / Dlat) - lat_frac
    )
    lat = Dlat * (j + lat_frac)

    nl = _cpr_nl(lat)
    ni = max(1, nl - odd)
    Dlon = 360.0 / ni
    m = math.floor(lon_ref / Dlon) + math.floor(
        0.5 + ((lon_ref % Dlon) / Dlon) - lon_frac
    )
    lon = Dlon * (m + lon_frac)
    return lat, lon


# ── plugin ──────────────────────────────────────────────────────────────────

_CPR_PAIR_MAX_AGE  = 10.0      # global decode requires even+odd within this
# Keep the log dir next to this file (plugins/adsb/adsb_logs/) instead of
# in the process cwd so the log path is stable no matter where SDRTerm is
# launched from.
_LOG_DIR_DEFAULT   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'adsb_logs')
_LOG_FILE_NAME     = 'adsb.csv'                # single append-only file
_CSV_HEADER        = ('timestamp,icao,callsign,lat,lon,alt,'
                      'gs,ias,tas,heading,vr\n')

# Fields whose change triggers a new CSV row.  Speed is split into three
# columns (gs / ias / tas) so a speed-type switch alone does NOT invalidate
# the previously-reported value in the other slot — see _log_aircraft().
_TRACKED_LOG_FIELDS = ('lat', 'lon', 'alt', 'gs', 'ias', 'tas', 'heading', 'vr')


class AdsbDecoder(Decoder):
    name            = 'adsb'
    key             = 'b'                      # 'b' = ADS-**B** / beacon
    key_help        = 'r=clear  s=log'
    min_sample_rate = _MIN_SAMPLE_RATE
    realtime        = False
    bg_queue_depth  = 8
    full_view       = True

    def __init__(self):
        self._aircraft: dict     = {}
        self._messages           = deque(maxlen=64)
        self._n_bursts           = 0            # correlator peaks past threshold
        self._n_crc_ok           = 0            # frames whose CRC-24 = 0
        self._tail               = np.empty(0, dtype=np.float32)
        self._resample_ratio     = None         # cached (up, down) for state.bw_hz
        # CSV telemetry log — single append-only file `adsb_logs/adsb.csv`
        # (grows across runs; timestamps carry the date, so no rotation needed).
        self._log_dir            = _LOG_DIR_DEFAULT
        self._log_file           = None         # opened lazily on first write
        self._log_last: dict     = {}           # icao → dict of last-written field snapshot
        self._logging_enabled    = True         # 's' key toggles at runtime

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, state) -> None:
        self._aircraft.clear()
        self._messages.clear()
        self._n_bursts = 0
        self._n_crc_ok = 0
        self._tail = np.empty(0, dtype=np.float32)
        self._resample_ratio = None
        self._log_last.clear()
        # File itself stays open across a start()/stop() cycle if already open;
        # closed and re-opened lazily on next _log_aircraft() call otherwise.

    def stop(self) -> None:
        self.start(None)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    # ── main pipeline ────────────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        bw = int(state.bw_hz) if state is not None else _INTERNAL_SR

        # Resample source → INTERNAL_SR (skip if already at target)
        if bw != _INTERNAL_SR:
            if self._resample_ratio is None or self._resample_ratio[2] != bw:
                g = math.gcd(_INTERNAL_SR, bw)
                self._resample_ratio = (_INTERNAL_SR // g, bw // g, bw)
            up, down, _ = self._resample_ratio
            samples = resample_poly(samples, up, down).astype(np.complex64)

        env = _envelope(samples)
        if len(self._tail):
            env = np.concatenate([self._tail, env])
            self._tail = np.empty(0, dtype=np.float32)

        score = _correlate(env)
        if len(score) == 0:
            return self._empty_result()

        # Adaptive threshold: 10× median-|score| per chunk.  Robust to bursts
        # (median ignores the small fraction of correlator peaks) and high
        # enough to reject the correlator's own side-lobes without needing
        # per-signal tuning.  The CRC-24 filter downstream handles remaining
        # false alarms cheaply.
        noise = float(np.median(np.abs(score)))
        threshold = max(1e-6, 10.0 * noise)

        peaks = _find_peaks(score, threshold, _LONG_MSG_SAMPLES)
        self._n_bursts += len(peaks)

        # Carry over the last long-message-worth of envelope for the next chunk
        if len(env) >= _LONG_MSG_SAMPLES:
            self._tail = env[-_LONG_MSG_SAMPLES:].copy()

        for p in peaks:
            self._try_decode(p, env)

        return self._result()

    def _empty_result(self):
        return {'aircraft': dict(self._aircraft),
                'messages': list(self._messages),
                'n_bursts': self._n_bursts,
                'n_crc_ok': self._n_crc_ok}

    def _result(self):
        return self._empty_result()

    # ── decode one candidate burst ───────────────────────────────────────────

    def _try_decode(self, start: int, env: np.ndarray) -> None:
        bits, _conf = _slice_bits(env, start, _LONG_MSG_BITS)
        if bits is None:
            return
        msg_bytes = _bits_to_bytes(bits)
        if len(msg_bytes) != 14:
            return

        df = (msg_bytes[0] >> 3) & 0x1F
        if df != _DF_ADSB:
            return

        if _crc24(msg_bytes) != 0:
            return

        self._n_crc_ok += 1
        self._parse_df17(msg_bytes, bits)

    # ── DF17 top-level dispatch ──────────────────────────────────────────────

    def _parse_df17(self, msg_bytes: bytes, bits: np.ndarray) -> None:
        icao = '{:02X}{:02X}{:02X}'.format(msg_bytes[1], msg_bytes[2], msg_bytes[3])
        me_bits = bits[32:88]                  # ME field = bits 32..87
        tc = _bits_slice_int(me_bits, 0, 5)
        now = time.time()

        ac = self._aircraft.setdefault(icao, {'icao': icao})
        ac['last_seen'] = now

        if 1 <= tc <= 4:
            ac['callsign'] = _parse_identification(me_bits)
        elif (9 <= tc <= 18) or (20 <= tc <= 22):
            odd, alt, lat_cpr, lon_cpr = _parse_airborne_position(me_bits)
            if alt is not None:
                ac['alt'] = alt
            self._update_position(ac, odd, lat_cpr, lon_cpr, now)
        elif tc == 19:
            v = _parse_velocity(me_bits)
            if v is not None:
                ac.update(v)

        self._messages.appendleft({
            'ts':   time.strftime('%H:%M:%S'),
            'icao': icao,
            'df':   17,
            'tc':   tc,
            'hex':  msg_bytes.hex().upper(),
        })

        # CSV log — writes a row only when the aircraft's telemetry
        # snapshot (lat/lon/alt/gs/ias/tas/heading/vr) actually changed.
        self._log_aircraft(icao, ac)

    # ── CSV telemetry log ────────────────────────────────────────────────────

    def _log_aircraft(self, icao: str, ac: dict) -> None:
        """Append one CSV row for `icao` if any tracked value changed.

        The three speed columns (gs / ias / tas) are kept independent so
        switching the transmitted speed reference from, say, GS to IAS
        does not clobber the last GS reading — the previous slot value is
        preserved in the log until a fresh value of the same type arrives.

        No-op when logging has been toggled off via the 's' key.
        """
        if not self._logging_enabled:
            return
        last     = self._log_last.get(icao)
        spd_type = ac.get('spd_type')
        speed    = ac.get('speed')

        # Merge this message's fields on top of the last snapshot.  For the
        # speed slots that this message did NOT report, keep the last value.
        row = {
            'callsign': ac.get('callsign') or '',
            'lat':      ac.get('lat'),
            'lon':      ac.get('lon'),
            'alt':      ac.get('alt'),
            'gs':       speed if spd_type == 'GS'  else (last['gs']  if last else None),
            'ias':      speed if spd_type == 'IAS' else (last['ias'] if last else None),
            'tas':      speed if spd_type == 'TAS' else (last['tas'] if last else None),
            'heading':  ac.get('heading'),
            'vr':       ac.get('vr'),
        }

        # Always log the first squitter from an ICAO (marks "first heard at" —
        # useful metadata even if the frame only carried a callsign).  After
        # that, log only when the merged snapshot actually changes.
        if last is not None and row == last:
            return

        self._log_last[icao] = row

        f = self._get_log_file()
        if f is None:
            return

        now      = time.time()
        ts       = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now)) + \
                   '.{:03d}Z'.format(int((now * 1000) % 1000))
        callsign = row['callsign'].replace(',', '').replace('\n', '')

        def cell(k):
            v = row[k]
            return '' if v is None else str(v)

        line = ','.join([ts, icao, callsign] + [cell(k) for k in _TRACKED_LOG_FIELDS]) + '\n'
        try:
            f.write(line)
        except Exception:
            pass                # never let a log-write failure break decoding

    def _get_log_file(self):
        """Return the (append-only) log file handle, opening it if needed."""
        if self._log_file is not None:
            return self._log_file

        try:
            os.makedirs(self._log_dir, exist_ok=True)
        except OSError:
            return None

        path = os.path.join(self._log_dir, _LOG_FILE_NAME)
        need_header = not os.path.exists(path)
        try:
            self._log_file = open(path, 'a', buffering=1)   # line-buffered
        except OSError:
            self._log_file = None
            return None
        if need_header:
            self._log_file.write(_CSV_HEADER)
        return self._log_file

    # ── position update: pair frames for global, else use local ──────────────

    def _update_position(self, ac: dict, odd: int, lat_cpr: int, lon_cpr: int,
                         now: float) -> None:
        cpr_key = 'cpr_odd' if odd else 'cpr_even'
        ac[cpr_key] = (lat_cpr, lon_cpr, now)

        even = ac.get('cpr_even')
        odd_pair = ac.get('cpr_odd')
        if even is not None and odd_pair is not None and \
           abs(even[2] - odd_pair[2]) < _CPR_PAIR_MAX_AGE:
            # Use whichever frame was received most recently for the returned coordinate
            use_odd = odd_pair[2] > even[2]
            pos = _cpr_global(even[0], even[1], odd_pair[0], odd_pair[1], use_odd)
            if pos is not None:
                ac['lat'], ac['lon'] = pos
                return

        # Fallback: local decode from previous position, if any
        if 'lat' in ac and 'lon' in ac:
            try:
                ac['lat'], ac['lon'] = _cpr_local(
                    ac['lat'], ac['lon'], lat_cpr, lon_cpr, odd,
                )
            except Exception:
                pass

    # ── keys ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._aircraft.clear()
            self._messages.clear()
            self._n_bursts = 0
            self._n_crc_ok = 0
            return True
        if key == ord('s'):
            self._logging_enabled = not self._logging_enabled
            # When turning logging off, close the file so it can be moved/
            # rotated externally between sessions.
            if not self._logging_enabled and self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
            return True
        return False

    # ── UI ───────────────────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        log_flag = 'LOG' if self._logging_enabled else 'log'
        return '[ADS-B {}ac / {}msg / {}pks / {}] '.format(
            len(result.get('aircraft', {})),
            result.get('n_crc_ok', 0),
            result.get('n_bursts', 0),
            log_flag,
        )

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        log_state = 'LOG:ON' if self._logging_enabled else 'LOG:OFF'
        header = 'ADS-B  (1090 MHz Mode S extended squitter)  [r=clear  s={}]'.format(log_state)
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        col_header = ' {:<6}  {:<8}  {:>8}  {:>9}  {:>6}  {:>4} {:<3} {:>4}  {:>5}  {:>4}'.format(
            'ICAO', 'CALLSIGN', 'LAT', 'LON', 'ALT', 'SPD', 'TYP', 'HDG', 'VR', 'AGE')
        try:
            screen_obj.addstr(3, 2, col_header[:cols - 4], curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass

        aircraft = result.get('aircraft', {})
        if not aircraft:
            try:
                screen_obj.addstr(5, 2, 'Listening for ADS-B squitters on 1090 MHz…')
                screen_obj.addstr(6, 2, '(tune with `f 1090M` and set BW ≥ 2M)')
                screen_obj.addstr(8, 2, 'peaks={}  crc_ok={}'.format(
                    result.get('n_bursts', 0), result.get('n_crc_ok', 0)))
            except curses.error:
                pass
            return

        now = time.time()
        y = 4
        for _icao, ac in sorted(aircraft.items(),
                                key=lambda kv: -kv[1].get('last_seen', 0)):
            if y >= rows - 1:
                break
            age = int(now - ac.get('last_seen', now))
            line = ' {:<6}  {:<8}  {:>8}  {:>9}  {:>6}  {:>4} {:<3} {:>4}  {:>5}  {:>3}s'.format(
                ac.get('icao', '')[:6],
                (ac.get('callsign') or '')[:8],
                '{:.4f}'.format(ac['lat']) if 'lat' in ac else '',
                '{:.4f}'.format(ac['lon']) if 'lon' in ac else '',
                '{}'.format(ac.get('alt', '')) if 'alt' in ac else '',
                '{}'.format(ac.get('speed', '')) if 'speed' in ac else '',
                (ac.get('spd_type') or '')[:3],
                '{}'.format(ac.get('heading', '')) if 'heading' in ac else '',
                '{}'.format(ac.get('vr', '')) if 'vr' in ac else '',
                age,
            )
            try:
                screen_obj.addstr(y, 2, line[:cols - 4])
            except curses.error:
                pass
            y += 1

    # ── persistence ──────────────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {'logging_enabled': self._logging_enabled}

    def load_state(self, d: dict) -> None:
        if 'logging_enabled' in d:
            self._logging_enabled = bool(d['logging_enabled'])
