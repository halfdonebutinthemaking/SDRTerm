"""
AIS (Automatic Identification System) decoder plugin.

Maritime equivalent of ADS-B: ships transmit position + identity in
GMSK 9600 bps bursts on two tightly-spaced VHF channels:

  * AIS 1 — 161.975 MHz  (ITU channel 87B)
  * AIS 2 — 162.025 MHz  (ITU channel 88B)

The plugin follows the ACARS / VDL2 multi-channel pattern (PR #39,
#40): one capture at ~500 kHz centred between the two channels, two
per-channel NCO downconverts, two independent demod chains, one shared
MMSI-indexed vessel table + CSV log.

Scope: this scaffold decodes Class A position reports (message types
1 / 2 / 3) and Class B position reports (type 18).  Static / voyage
data (type 5), aids to navigation (type 21), and other types are
counted but not field-decoded — follow-up PR.
"""
import csv
import os
import time
from collections import deque
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from core import Decoder, AppState
from .protocol import (
    gmsk_demod, nrzi_decode, hdlc_frames_diag,
    ais_payload_from_hdlc, parse_ais, crc16_ccitt,
)


_BIT_RATE          = 9_600                        # GMSK bit rate
_TARGET_SPS        =     8                        # samples per bit after resample
_TARGET_SR         = _BIT_RATE * _TARGET_SPS      # 76 800 Hz baseband sample rate
_MAX_MESSAGES      =    64
_MAX_BIT_BUF       = 32_768                       # ~3.4 s of bits
_SEEN_CAP          =   512
_DEDUP_WINDOW_S    =   2.0                        # same frame on both channels is one message

# Standard AIS channels — overridable from preset.
_AIS_CHANNELS = [
    {'freq': 161_975_000, 'name': 'AIS 1'},
    {'freq': 162_025_000, 'name': 'AIS 2'},
]

_HERE          = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR       = os.path.join(_HERE, 'ais_logs')
_LOG_FILE_NAME = 'ais.csv'
_LOG_FIELDS    = ('timestamp', 'mmsi', 'channel', 'type',
                  'lat', 'lon', 'sog', 'cog', 'heading', 'nav_status')
# Fields whose change triggers a new CSV row (mirrors ADS-B's _TRACKED_LOG_FIELDS).
_TRACKED_LOG_FIELDS = ('lat', 'lon', 'sog', 'cog', 'heading', 'nav_status')


class _ChannelState:
    """Per-channel GMSK demod + HDLC buffer state.  One instance per
    configured AIS channel; cross-chunk continuity lives here.

    AIS slots are 26.67 ms but the main-loop IQ chunks are ~16 ms at
    1.024 MHz, so every real burst spans at least one chunk boundary.
    Both the frequency discriminator (needs the previous sample) and
    the bit-centre slicer (resample output isn't a multiple of SPS)
    will corrupt the frame unless state is carried over."""
    __slots__ = ('name', 'freq_hz', 'bit_buf',
                 'carrier_phase', 'prev_iq', 'sym_offset',
                 'filter_state', 'mu', 'prev_soft', 'nrzi_last')

    def __init__(self, name, freq_hz):
        self.name          = name
        self.freq_hz       = float(freq_hz)
        self.bit_buf       = deque(maxlen=_MAX_BIT_BUF)
        self.carrier_phase = 0.0
        self.prev_iq       = None                     # last baseband sample
        self.sym_offset    = _TARGET_SPS // 2         # centre of the first bit
        self.filter_state  = None                     # Gaussian MF edge history
        self.mu            = 0.0                      # M&M sub-sample timing offset
        self.prev_soft     = 0.0                      # last matched-filter sample
        self.nrzi_last     = None                     # last wire bit for NRZI continuity

    def reset(self):
        self.bit_buf.clear()
        self.carrier_phase = 0.0
        self.prev_iq       = None
        self.sym_offset    = _TARGET_SPS // 2
        self.filter_state  = None
        self.mu            = 0.0
        self.prev_soft     = 0.0
        self.nrzi_last     = None


class AisDecoder(Decoder):
    name            = 'ais'
    # 'a' is taken by acars and every other mnemonic letter (b/adsb,
    # c/constellation, …) is also claimed; the plugin-menu letter is
    # only used as a visual tag in the toggle list, so 'y' is fine.
    key             = 'y'
    key_help        = 'r=clear  s=log'
    min_sample_rate = 1_024_000                       # covers both 161.975 and 162.025
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        # Default to the two standard AIS channels.  load_state() can
        # override this (e.g. for the Chinese AIS 3/4 channels, or for
        # a single-channel capture).
        self._channels: list = [
            _ChannelState(spec['name'], spec['freq'])
            for spec in _AIS_CHANNELS
        ]
        self._vessels: dict  = {}        # mmsi → latest dict
        self._messages       = deque(maxlen=_MAX_MESSAGES)
        self._seen           = {}        # (len, crc_key) → monotonic_ts for cross-chan dedup
        self._rrc_cache      = {}        # bw_hz → (up, down)
        self._n_bursts       = 0         # total HDLC candidates seen (passed length check)
        self._n_crc_ok       = 0
        self._logging_enabled = True
        self._log_dir        = _LOG_DIR
        self._log_file       = None
        self._log_writer     = None
        self._log_last       = {}        # mmsi → last-written field dict (dedup CSV writes)
        # ── diagnostic counters (shown in the plugin tab's debug console, press 'd') ──
        self._n_destuff_fail = 0         # HDLC frames found but bit-stuffing invalid
        self._n_crc_fail     = 0         # destuff OK but CRC wrong
        self._len_hist       = {}        # byte-length → count of destuff-OK candidates
        self._crc_fail_dump  = deque(maxlen=20)   # recent crc-fail payload hex dumps
        self._dbg_last_t     = 0.0                # monotonic — rate-limit the debug console

    def start(self, state: AppState) -> None:
        for ch in self._channels:
            ch.reset()
        self._vessels.clear()
        self._messages.clear()
        self._seen.clear()
        self._n_bursts       = 0
        self._n_crc_ok       = 0
        self._n_destuff_fail = 0
        self._n_crc_fail     = 0
        self._len_hist.clear()
        self._crc_fail_dump.clear()
        if self._logging_enabled:
            self._open_log()

    def stop(self) -> None:
        self._close_log()

    # ── signal processing ──────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        sr = int(state.bw_hz)
        for ch in self._channels:
            offset_hz = ch.freq_hz - state.center_hz
            if abs(offset_hz) > sr / 2 * 0.9:         # 10 % guardband
                continue
            self._decode_channel(samples, offset_hz, sr, ch)
        return self._result()

    def _decode_channel(self, samples, offset_hz, sr, ch: '_ChannelState'):
        """Downconvert `samples` by `offset_hz`, resample to _TARGET_SR,
        run GMSK demod → NRZI decode → HDLC frame extract.  Each new
        valid frame is deduped against the cross-channel `_seen` cache
        and promoted to the vessel table."""

        n       = len(samples)
        t_local = np.arange(n) / sr
        baseband = (samples * np.exp(
            -1j * (ch.carrier_phase + 2 * np.pi * offset_hz * t_local)
        )).astype(np.complex64)
        ch.carrier_phase = (
            ch.carrier_phase + 2 * np.pi * offset_hz * n / sr
        ) % (2 * np.pi)

        # Resample to _TARGET_SR; cache the up/down factors per source rate.
        if sr not in self._rrc_cache:
            g        = gcd(_TARGET_SR, sr)
            up, down = _TARGET_SR // g, sr // g
            while up > 500 or down > 500:
                up   = max(1, up   // 2)
                down = max(1, down // 2)
            self._rrc_cache[sr] = (up, down)
            if len(self._rrc_cache) > 8:
                self._rrc_cache.pop(next(iter(self._rrc_cache)))
        up, down = self._rrc_cache[sr]

        try:
            resampled = resample_poly(baseband, up, down)
        except Exception:
            return

        # GMSK demod → wire-level NRZI bits.  Six items of cross-chunk
        # state: discriminator history (prev_iq), integer symbol offset
        # (sym_offset), Gaussian MF history (filter_state), M&M timing
        # loop sub-sample offset (mu), M&M "previous symbol" seed
        # (prev_soft).  All kept on _ChannelState so each channel's
        # timing loop stays locked independently.
        (raw, ch.prev_iq, ch.sym_offset, ch.filter_state,
         ch.mu, ch.prev_soft) = gmsk_demod(
            resampled, _TARGET_SPS,
            prev_iq=ch.prev_iq, sym_offset=ch.sym_offset,
            filter_state=ch.filter_state,
            mu=ch.mu, prev_soft=ch.prev_soft,
        )
        if not raw:
            return

        # NRZI decode with cross-chunk continuity: prepend the last
        # wire bit of the previous chunk so the first new bit decodes
        # against the correct predecessor.
        if ch.nrzi_last is not None:
            raw = [ch.nrzi_last] + raw
        ch.nrzi_last = raw[-1]
        decoded      = nrzi_decode(raw)
        ch.bit_buf.extend(decoded)

        # HDLC frame scan on the per-channel bit buffer (diagnostic
        # variant: surfaces destuff-fails too so the plugin's debug
        # console can report WHY candidates are being dropped).
        buf = list(ch.bit_buf)
        for payload, crc_ok, status, raw_len in hdlc_frames_diag(buf):
            self._n_bursts += 1
            if status == 'destuff_fail':
                self._n_destuff_fail += 1
                continue
            if status in ('too_short', 'short_frame'):
                continue
            # Both crc_ok and crc_fail cases have a payload — track
            # the length distribution so a debug-console reader can
            # tell "noise with random lengths" from "clustered near
            # 23 bytes = real AIS frames with a CRC convention bug".
            byte_len = len(payload)
            self._len_hist[byte_len] = self._len_hist.get(byte_len, 0) + 1
            if not crc_ok:
                self._n_crc_fail += 1
                if len(self._crc_fail_dump) < self._crc_fail_dump.maxlen:
                    self._crc_fail_dump.append(
                        '[{}] {:>3}B {}'.format(
                            ch.name, byte_len, payload[:16].hex(' ')))
                continue
            self._n_crc_ok += 1

            # Cross-channel + short-window dedup: the same vessel's
            # transmission is often heard on both AIS 1 and AIS 2
            # when ducting is strong.  Key by payload length + CRC
            # fingerprint; expire entries after _DEDUP_WINDOW_S.
            now = time.monotonic()
            key = (len(payload), _crc_key(payload))
            last_seen = self._seen.get(key)
            if last_seen is not None and now - last_seen < _DEDUP_WINDOW_S:
                self._seen[key] = now
                continue
            self._seen[key] = now
            if len(self._seen) > _SEEN_CAP:
                self._seen.pop(next(iter(self._seen)))

            # Convert HDLC payload (LSB-first in byte) to the MSB-first
            # byte order the AIS field decoders expect.
            ais_bytes = ais_payload_from_hdlc(payload)
            parsed    = parse_ais(ais_bytes)
            self._handle_message(ch, parsed, ais_bytes)

    def _handle_message(self, ch, parsed, raw_bytes):
        if parsed is None:
            return
        msg = {
            'ts':      time.strftime('%H:%M:%S'),
            'ts_iso':  time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
            'parsed':  parsed,
            'channel': ch.name,
            'raw':     raw_bytes,
        }
        self._messages.append(msg)

        mmsi = parsed.get('mmsi')
        if mmsi is None:
            return
        vessel = self._vessels.setdefault(mmsi, {'mmsi': mmsi, 'messages': 0})
        vessel['messages']  = vessel.get('messages', 0) + 1
        vessel['last_seen'] = msg['ts']
        vessel['channel']   = ch.name
        # Only overwrite positional fields when the new message carries them —
        # non-position messages (static data, binary) keep the last known fix.
        for field in ('lat', 'lon', 'sog', 'cog', 'heading', 'nav_status', 'type'):
            if field in parsed:
                vessel[field] = parsed[field]

        if self._logging_enabled:
            self._log_vessel(mmsi, vessel, msg['ts_iso'])

    def _result(self) -> dict:
        self._refresh_diag_console()
        return {
            'n_bursts':         self._n_bursts,
            'n_crc_ok':         self._n_crc_ok,
            'n_vessels':        len(self._vessels),
            'n_destuff_fail':   self._n_destuff_fail,
            'n_crc_fail':       self._n_crc_fail,
            'channels':         [ch.name for ch in self._channels],
            'logging':          self._logging_enabled,
        }

    # ── debug console snapshot ─────────────────────────────────────────────

    def _refresh_diag_console(self) -> None:
        """Rewrite the plugin's debug ring buffer with a current snapshot
        of counters + length histogram + recent CRC-fail hex dumps.
        Throttled to once per second so a `d`-key viewer sees a stable
        screen and the deque isn't thrashed every 16 ms."""
        now = time.monotonic()
        if now - self._dbg_last_t < 1.0:
            return
        self._dbg_last_t = now
        # Treat the ring buffer as a snapshot panel: clear → write.
        if self._debug_lines is None:
            self._dbg('')          # lazy-init
        self._debug_lines.clear()

        total  = max(1, self._n_bursts)
        hist   = sorted(self._len_hist.items())
        hist_s = ' '.join(f'{L}:{c}' for L, c in hist[:12])
        self._dbg('── AIS decoder diagnostics ──')
        self._dbg('bursts        : {}'.format(self._n_bursts))
        self._dbg('  destuff fail: {}  ({:.0%})'.format(
            self._n_destuff_fail, self._n_destuff_fail / total))
        self._dbg('  crc fail    : {}  ({:.0%})'.format(
            self._n_crc_fail, self._n_crc_fail / total))
        self._dbg('  crc OK      : {}  ({:.0%})'.format(
            self._n_crc_ok, self._n_crc_ok / total))
        self._dbg('vessels       : {}'.format(len(self._vessels)))
        self._dbg('')
        self._dbg('candidate length histogram (bytes:count, top 12):')
        self._dbg('  ' + hist_s if hist_s else '  (none yet)')
        self._dbg('')
        self._dbg('recent CRC-fail hex (first 16 B of payload):')
        if not self._crc_fail_dump:
            self._dbg('  (none)')
        else:
            for line in self._crc_fail_dump:
                self._dbg('  ' + line)
        self._dbg('')
        self._dbg('reading: cluster near 21-23 B → real frames (CRC bug);')
        self._dbg('         random spread      → noise finding flags.')

    # ── CSV log (mirrors the ADS-B pattern) ────────────────────────────────

    def _open_log(self):
        if self._log_file is not None:
            return
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            path = os.path.join(self._log_dir, _LOG_FILE_NAME)
            new  = not os.path.isfile(path) or os.path.getsize(path) == 0
            self._log_file   = open(path, 'a', buffering=1)   # line-buffered
            self._log_writer = csv.DictWriter(
                self._log_file, fieldnames=_LOG_FIELDS, extrasaction='ignore')
            if new:
                self._log_writer.writeheader()
        except OSError:
            # Writable-dir failure disables logging silently; status bar still shows.
            self._log_file   = None
            self._log_writer = None

    def _close_log(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file   = None
        self._log_writer = None
        self._log_last.clear()

    def _log_vessel(self, mmsi, vessel, ts_iso):
        if self._log_writer is None:
            return
        prev = self._log_last.get(mmsi, {})
        if all(vessel.get(f) == prev.get(f) for f in _TRACKED_LOG_FIELDS):
            return      # nothing worth writing changed
        row = {
            'timestamp':  ts_iso,
            'mmsi':       mmsi,
            'channel':    vessel.get('channel', ''),
            'type':       vessel.get('type', ''),
            'lat':        vessel.get('lat', ''),
            'lon':        vessel.get('lon', ''),
            'sog':        vessel.get('sog', ''),
            'cog':        vessel.get('cog', ''),
            'heading':    vessel.get('heading', ''),
            'nav_status': vessel.get('nav_status', ''),
        }
        try:
            self._log_writer.writerow(row)
        except Exception:
            self._close_log()
            return
        self._log_last[mmsi] = {f: vessel.get(f) for f in _TRACKED_LOG_FIELDS}

    # ── keys ───────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._vessels.clear()
            self._messages.clear()
            self._n_bursts = 0
            self._n_crc_ok = 0
            return True
        if key == ord('s'):
            self._logging_enabled = not self._logging_enabled
            if self._logging_enabled:
                self._open_log()
            else:
                self._close_log()
            return True
        return False

    # ── UI ─────────────────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        log_flag = 'LOG' if self._logging_enabled else 'log'
        return '[AIS {}v / {}msg / {}brst / {}] '.format(
            result.get('n_vessels', 0),
            result.get('n_crc_ok', 0),
            result.get('n_bursts', 0),
            log_flag,
        )

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        n_bursts = result.get('n_bursts', 0)
        n_crc_ok = result.get('n_crc_ok', 0)
        n_ves    = result.get('n_vessels', 0)
        chans    = result.get('channels') or []
        log_flag = 'ON' if self._logging_enabled else 'off'

        header = ('AIS   GMSK {} bps   {} vessels   {}/{} frames OK   '
                  'log={}   ({} ch)'.format(
                      _BIT_RATE, n_ves, n_crc_ok, n_bursts, log_flag,
                      len(chans)))
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header, curses.A_BOLD)
        except curses.error:
            pass

        try:
            curses.init_pair(3, curses.COLOR_GREEN,  -1)
            curses.init_pair(13, curses.COLOR_YELLOW, -1)
        except Exception:
            pass

        # Vessel table — most-recently-seen first.
        vessels = sorted(
            self._vessels.values(),
            key=lambda v: v.get('last_seen', ''),
            reverse=True,
        )
        max_rows = rows - 5

        col_hdr = '  {:<10s}  {:<7s}  {:>10s}  {:>10s}  {:>6s}  {:>5s}  {:<7s}'.format(
            'MMSI', 'CHAN', 'LAT', 'LON', 'SOG', 'COG', 'SEEN')
        try:
            screen_obj.addstr(3, 2, col_hdr[:cols - 4], curses.A_DIM)
        except curses.error:
            pass

        y = 4
        if not vessels:
            try:
                screen_obj.addstr(y, 2,
                                  'Waiting for frames — tune to 162.000 MHz, '
                                  'bw ≥ 1 024 000, antenna at the water.',
                                  curses.A_DIM)
            except curses.error:
                pass
        else:
            for v in vessels[:max_rows]:
                if y >= rows - 3:
                    break
                lat = v.get('lat'); lon = v.get('lon')
                sog = v.get('sog'); cog = v.get('cog')
                line = '  {:<10}  {:<7s}  {:>10}  {:>10}  {:>6}  {:>5}  {:<7s}'.format(
                    v['mmsi'],
                    (v.get('channel') or '')[:7],
                    '{:.5f}'.format(lat) if isinstance(lat, (int, float)) else '—',
                    '{:.5f}'.format(lon) if isinstance(lon, (int, float)) else '—',
                    '{:.1f}'.format(sog) if isinstance(sog, (int, float)) else '—',
                    '{:.0f}'.format(cog) if isinstance(cog, (int, float)) else '—',
                    v.get('last_seen', '')[:7],
                )
                try:
                    screen_obj.addstr(y, 2, line[:cols - 4],
                                      curses.color_pair(3))
                except curses.error:
                    pass
                y += 1

        footer = ('{} chan: {}   bits=9 600   r=clear   s=log'
                  .format(len(chans), ' / '.join(chans)))
        try:
            screen_obj.addstr(rows - 2, 2, footer[:cols - 4], curses.A_DIM)
        except curses.error:
            pass

    # ── persistence ────────────────────────────────────────────────────────

    def save_state(self) -> dict:
        d = {}
        if not self._logging_enabled:
            d['logging_enabled'] = False
        # Only persist a non-default channel list.  Comparison is loose:
        # same count, same (freq, name) tuples in order.
        default_pairs = [(c['freq'], c['name']) for c in _AIS_CHANNELS]
        current_pairs = [(ch.freq_hz, ch.name) for ch in self._channels]
        if current_pairs != default_pairs:
            d['channels'] = [{'freq': int(ch.freq_hz), 'name': ch.name}
                             for ch in self._channels]
        return d

    def load_state(self, d: dict) -> None:
        if 'logging_enabled' in d:
            self._logging_enabled = bool(d['logging_enabled'])
        chans = d.get('channels')
        if chans:
            self._channels = [
                _ChannelState(spec.get('name') or f'{spec["freq"] / 1e6:.3f}',
                              float(spec['freq']))
                for spec in chans
                if isinstance(spec, dict) and 'freq' in spec
            ]


# ── helpers ───────────────────────────────────────────────────────────────────

def _crc_key(data: bytes) -> int:
    """Dedup fingerprint: last two bytes as a little-endian int.  Combined
    with len() this is enough to distinguish distinct AIS slot payloads
    at the collision rates we actually see."""
    return int.from_bytes(data[-2:], 'little') if len(data) >= 2 else 0
