"""Unit tests for the ADS-B decoder.

All frames used here are the canonical worked examples from
mode-s.org so the values (ICAO, callsign, altitude, lat/lon,
velocity) are known and easy to cross-reference.
"""
import time
import numpy as np
import pytest

from plugins.adsb.adsb import (
    _crc24,
    _bytes_to_bits,
    _bits_to_bytes,
    _bits_slice_int,
    _parse_identification,
    _decode_ac12,
    _parse_airborne_position,
    _parse_velocity,
    _cpr_nl,
    _cpr_global,
    _envelope,
    _correlate,
    _find_peaks,
    _slice_bits,
    _make_preamble_template,
    _PREAMBLE_LEN,
    _SPS,
    _LONG_MSG_BITS,
    AdsbDecoder,
)


# ── CRC-24 ────────────────────────────────────────────────────────────────────

class TestCrc:
    def test_valid_frame_gives_zero(self):
        # KLM1023 identification message (mode-s.org book example 3.1)
        msg = bytes.fromhex('8D4840D6202CC371C32CE0576098')
        assert _crc24(msg) == 0

    def test_flipped_bit_gives_nonzero(self):
        msg = bytearray.fromhex('8D4840D6202CC371C32CE0576098')
        msg[0] ^= 1
        assert _crc24(bytes(msg)) != 0

    def test_multiple_valid_frames(self):
        # A handful of real DF17 squitters from public traces
        for hex_frame in [
            '8D4840D6202CC371C32CE0576098',   # KLM1023 identification
            '8D40621D58C382D690C8AC2863A7',   # airborne position even
            '8D40621D58C386435CC412692AD6',   # airborne position odd
            '8D485020994409940838175B284F',   # velocity
        ]:
            assert _crc24(bytes.fromhex(hex_frame)) == 0, hex_frame


# ── bit helpers ───────────────────────────────────────────────────────────────

class TestBitHelpers:
    def test_bytes_bits_roundtrip(self):
        b = bytes.fromhex('8D4840D6')
        bits = _bytes_to_bits(b)
        assert bits.tolist() == [
            1, 0, 0, 0, 1, 1, 0, 1,   # 8D
            0, 1, 0, 0, 1, 0, 0, 0,   # 48
            0, 1, 0, 0, 0, 0, 0, 0,   # 40
            1, 1, 0, 1, 0, 1, 1, 0,   # D6
        ]
        assert _bits_to_bytes(bits) == b

    def test_slice_int_msb_first(self):
        bits = [1, 0, 1, 1, 0, 0, 1, 0]
        assert _bits_slice_int(bits, 0, 4) == 0b1011
        assert _bits_slice_int(bits, 4, 4) == 0b0010
        assert _bits_slice_int(bits, 0, 8) == 0xB2


# ── identification (TC 1..4) ─────────────────────────────────────────────────

class TestIdentification:
    def test_klm1023(self):
        # From mode-s.org: DF17 ident with callsign 'KLM1023 '
        msg = bytes.fromhex('8D4840D6202CC371C32CE0576098')
        bits = _bytes_to_bits(msg)
        me = bits[32:88]
        assert _parse_identification(me) == 'KLM1023'


# ── altitude (AC12) ──────────────────────────────────────────────────────────

class TestAltitude:
    def test_38000ft_ac12(self):
        # AC12 = 0xC38 → Q=1, N=1560 → alt = 1560*25 - 1000 = 38000 ft
        # bits: 1100 0011 1000
        #        ^^^ ^^^ Q(bit4=1) ^^^^
        ac12 = 0xC38
        assert _decode_ac12(ac12) == 38000

    def test_zero_altitude_is_invalid(self):
        assert _decode_ac12(0) is None


# ── airborne position parsing ────────────────────────────────────────────────

class TestAirbornePosition:
    def test_even_frame_fields(self):
        # From mode-s.org tutorial: 8D40621D58C382D690C8AC2863A7
        # Expected: F=0 (even), lat_cpr=93000, lon_cpr=51372, alt=38000 ft
        msg = bytes.fromhex('8D40621D58C382D690C8AC2863A7')
        me = _bytes_to_bits(msg)[32:88]
        odd, alt, lat_cpr, lon_cpr = _parse_airborne_position(me)
        assert odd == 0
        assert alt == 38000
        assert lat_cpr == 93000
        assert lon_cpr == 51372

    def test_odd_frame_fields(self):
        # 8D40621D58C386435CC412692AD6
        # Expected: F=1 (odd), lat_cpr=74158, lon_cpr=50194, alt=38000 ft
        msg = bytes.fromhex('8D40621D58C386435CC412692AD6')
        me = _bytes_to_bits(msg)[32:88]
        odd, alt, lat_cpr, lon_cpr = _parse_airborne_position(me)
        assert odd == 1
        assert alt == 38000
        assert lat_cpr == 74158
        assert lon_cpr == 50194


# ── CPR position decoding ────────────────────────────────────────────────────

class TestCpr:
    def test_nl_at_equator(self):
        assert _cpr_nl(0.0) == 59

    def test_nl_high_latitude(self):
        assert _cpr_nl(88.0) == 1

    def test_nl_mid_latitudes_monotonic(self):
        # NL is non-increasing as |lat| grows away from equator
        prev = _cpr_nl(0.0)
        for lat in range(1, 88):
            v = _cpr_nl(float(lat))
            assert v <= prev
            prev = v

    def test_global_decode_klm_position(self):
        # Canonical mode-s.org example: even+odd pair should decode to
        # approx (52.2572°N, 3.919°E)
        lat, lon = _cpr_global(93000, 51372, 74158, 50194, use_odd=False)
        assert lat == pytest.approx(52.2572, abs=0.005)
        assert lon == pytest.approx(3.919, abs=0.005)

    def test_global_decode_use_odd_variant(self):
        lat_o, lon_o = _cpr_global(93000, 51372, 74158, 50194, use_odd=True)
        # Odd frame should be within a small distance of the even
        assert abs(lat_o - 52.2572) < 0.05
        assert abs(lon_o - 3.919) < 0.05


# ── velocity ──────────────────────────────────────────────────────────────────

class TestVelocity:
    def test_ground_referenced(self):
        # From mode-s.org: 8D485020994409940838175B284F
        # Expected: ~159 kt, heading ~183°, vertical rate -832 fpm
        msg = bytes.fromhex('8D485020994409940838175B284F')
        me = _bytes_to_bits(msg)[32:88]
        v = _parse_velocity(me)
        assert v is not None
        assert v['speed']    == pytest.approx(159, abs=2)
        assert v['heading']  == pytest.approx(183, abs=2)
        assert v['vr']       == pytest.approx(-832, abs=64)
        assert v['spd_type'] == 'GS'

    def test_airspeed_referenced_subtype_3(self):
        # From mode-s.org: 8DA05F219B06B6AF189400CBC33F
        # Expected: heading 244°, TAS 375 kt, vertical rate -2304 fpm
        msg = bytes.fromhex('8DA05F219B06B6AF189400CBC33F')
        me = _bytes_to_bits(msg)[32:88]
        v = _parse_velocity(me)
        assert v is not None
        assert v['speed']    == 375
        assert v['heading']  == 244
        assert v['vr']       == -2304
        assert v['spd_type'] == 'TAS'


# ── preamble correlator ─────────────────────────────────────────────────────

class TestCorrelator:
    def test_template_sums_to_zero(self):
        t = _make_preamble_template()
        assert abs(t.sum()) < 1e-6

    def test_synthetic_burst_is_found(self):
        # Build a fake envelope with a preamble at index 500
        env = np.random.default_rng(0).uniform(0, 0.05, size=4000).astype(np.float32)
        start = 500
        for pulse_us in (0.0, 1.0, 3.5, 4.5):
            s = int(round(pulse_us * _SPS))
            w = max(1, int(round(0.5 * _SPS)))
            env[start + s:start + s + w] = 1.0
        score = _correlate(env)
        peak = int(np.argmax(score))
        assert abs(peak - start) <= 1

    def test_find_peaks_orders_and_gaps(self):
        # Two bursts far apart should both be reported
        env = np.random.default_rng(1).uniform(0, 0.05, size=8000).astype(np.float32)
        for start in (500, 3000):
            for pulse_us in (0.0, 1.0, 3.5, 4.5):
                s = int(round(pulse_us * _SPS))
                w = max(1, int(round(0.5 * _SPS)))
                env[start + s:start + s + w] = 1.0
        score = _correlate(env)
        thr = 4.0 * float(np.median(np.abs(score)))
        peaks = _find_peaks(score, thr, 240)
        assert 500 in [p for p in peaks if abs(p - 500) <= 1] or \
               any(abs(p - 500) <= 1 for p in peaks)
        assert any(abs(p - 3000) <= 1 for p in peaks)


# ── end-to-end pipeline smoke test ───────────────────────────────────────────

class TestCsvLogging:
    class _S:
        bw_hz = 2_000_000

    def _mk(self, tmp_path):
        d = AdsbDecoder()
        d._log_dir = str(tmp_path)
        d.start(self._S())
        return d

    def _log_lines(self, tmp_path):
        files = list(tmp_path.glob('*.csv'))
        assert len(files) == 1
        return files[0].read_text().splitlines()

    def _feed(self, d, hex_frame: str):
        """Feed a valid DF17 frame directly through the parser (bypasses DSP)."""
        msg = bytes.fromhex(hex_frame)
        bits = _bytes_to_bits(msg)
        d._parse_df17(msg, bits)

    # ── behavior ─────────────────────────────────────────────────────────────

    def test_first_position_message_creates_row(self, tmp_path):
        d = self._mk(tmp_path)
        # Airborne-position even frame → sets lat_cpr/lon_cpr but not yet lat/lon
        # (needs even+odd pair for global decode).  Alt is set (38000 ft).
        self._feed(d, '8D40621D58C382D690C8AC2863A7')
        lines = self._log_lines(tmp_path)
        assert lines[0] == 'timestamp,icao,callsign,lat,lon,alt,gs,ias,tas,heading,vr'
        assert len(lines) == 2                       # header + 1 data row
        assert '40621D' in lines[1]
        assert ',38000,' in lines[1]                  # altitude present

    def test_ident_only_first_appearance_is_logged(self, tmp_path):
        # First squitter from an aircraft is always logged, even if it only
        # carries a callsign — the timestamp is proof-of-hearing.
        d = self._mk(tmp_path)
        self._feed(d, '8D4840D6202CC371C32CE0576098')  # KLM1023 ident
        lines = self._log_lines(tmp_path)
        assert len(lines) == 2
        # Row layout: timestamp,icao,callsign,lat,lon,alt,gs,ias,tas,heading,vr
        cells = lines[1].split(',')
        assert cells[1] == '4840D6'
        assert cells[2] == 'KLM1023'
        # Telemetry cells are all empty for an ident-only first squitter
        assert all(c == '' for c in cells[3:])

    def test_callsign_change_writes_new_row(self, tmp_path):
        d = self._mk(tmp_path)
        # First: position frame → row with empty callsign
        self._feed(d, '8D40621D58C382D690C8AC2863A7')
        # Manually attach a callsign to the same aircraft and re-log
        d._aircraft['40621D']['callsign'] = 'BAW285'
        d._log_aircraft('40621D', d._aircraft['40621D'])
        lines = self._log_lines(tmp_path)
        assert len(lines) == 3                       # header + pos + callsign
        assert lines[1].split(',')[2] == ''          # empty callsign originally
        assert lines[2].split(',')[2] == 'BAW285'    # new row with callsign

    def test_repeated_identical_message_writes_no_new_row(self, tmp_path):
        d = self._mk(tmp_path)
        self._feed(d, '8D40621D58C382D690C8AC2863A7')
        self._feed(d, '8D40621D58C382D690C8AC2863A7')
        assert len(self._log_lines(tmp_path)) == 2   # header + 1 row still

    def test_different_position_writes_new_row(self, tmp_path):
        d = self._mk(tmp_path)
        self._feed(d, '8D40621D58C382D690C8AC2863A7')   # even  (alt 38000)
        self._feed(d, '8D40621D58C386435CC412692AD6')   # odd   (alt 38000, different CPR)
        # The odd frame + prior even should also globally decode → lat/lon appears
        lines = self._log_lines(tmp_path)
        assert len(lines) >= 3                       # header + 2+ rows

    def test_speed_type_switch_uses_separate_column(self, tmp_path):
        d = self._mk(tmp_path)

        # First: ground-referenced velocity frame → fills GS slot
        self._feed(d, '8D485020994409940838175B284F')   # GS ~159 kt, hdg 183, vr -832
        # Fake a subsequent IAS report for the same aircraft at the SAME numeric
        # speed to prove that the extra column captures the type change
        # (rather than the single 'speed' cell going 159→159 = no change).
        icao = '485020'
        ac = d._aircraft[icao]
        ac['spd_type'] = 'IAS'
        ac['speed']    = 159
        d._log_aircraft(icao, ac)

        lines = self._log_lines(tmp_path)
        # header + first (GS=159, IAS empty) + second (GS=159 preserved, IAS=159 new) = 3
        assert len(lines) == 3
        # Extract gs / ias columns from each data row:
        # timestamp,icao,callsign,lat,lon,alt,gs,ias,tas,heading,vr
        row1 = lines[1].split(',')
        row2 = lines[2].split(',')
        assert row1[6] == '159' and row1[7] == ''    # GS=159, IAS=empty
        assert row2[6] == '159' and row2[7] == '159' # GS preserved, IAS=159 filled in

    def test_single_file_appends_across_restart(self, tmp_path):
        # First session: log one row and stop (which closes the file).
        d1 = self._mk(tmp_path)
        self._feed(d1, '8D40621D58C382D690C8AC2863A7')
        d1.stop()

        # Second session on the same log dir: file must be appended to,
        # header must NOT be re-written.
        d2 = self._mk(tmp_path)
        self._feed(d2, '8D40621D58C386435CC412692AD6')
        d2.stop()

        files = list(tmp_path.glob('*.csv'))
        assert [f.name for f in files] == ['adsb.csv']    # single file, no rotation
        lines = files[0].read_text().splitlines()
        # header + row from session 1 + row from session 2 = 3
        assert lines[0].startswith('timestamp,')
        assert len(lines) == 3
        assert lines.count(lines[0]) == 1                 # header appears exactly once

    def test_every_line_has_iso_utc_timestamp(self, tmp_path):
        import re
        d = self._mk(tmp_path)
        self._feed(d, '8D40621D58C382D690C8AC2863A7')
        lines = self._log_lines(tmp_path)
        assert len(lines) >= 2
        for line in lines[1:]:
            ts = line.split(',', 1)[0]
            # Match e.g. 2026-08-07T12:34:56.123Z
            assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$', ts), \
                'bad timestamp: ' + ts


class TestLoggingToggle:
    class _S:
        bw_hz = 2_000_000

    def _mk(self, tmp_path):
        d = AdsbDecoder()
        d._log_dir = str(tmp_path)
        d.start(self._S())
        return d

    def test_default_enabled(self, tmp_path):
        assert self._mk(tmp_path)._logging_enabled is True

    def test_s_key_toggles(self, tmp_path):
        d = self._mk(tmp_path)
        d.handle_key(ord('s'), None, None)
        assert d._logging_enabled is False
        d.handle_key(ord('s'), None, None)
        assert d._logging_enabled is True

    def test_disabled_does_not_write_file(self, tmp_path):
        d = self._mk(tmp_path)
        d.handle_key(ord('s'), None, None)         # OFF
        msg = bytes.fromhex('8D40621D58C382D690C8AC2863A7')
        d._parse_df17(msg, _bytes_to_bits(msg))
        assert not list(tmp_path.glob('*.csv'))    # nothing written

    def test_toggle_off_closes_file(self, tmp_path):
        d = self._mk(tmp_path)
        msg = bytes.fromhex('8D40621D58C382D690C8AC2863A7')
        d._parse_df17(msg, _bytes_to_bits(msg))    # opens file
        assert d._log_file is not None
        d.handle_key(ord('s'), None, None)         # toggle OFF
        assert d._log_file is None                 # closed

    def test_state_persists_via_save_load(self, tmp_path):
        d = self._mk(tmp_path)
        d._logging_enabled = False
        saved = d.save_state()
        assert saved == {'logging_enabled': False}

        d2 = self._mk(tmp_path)
        d2.load_state(saved)
        assert d2._logging_enabled is False


class TestPipelineEndToEnd:
    def _synthesise_iq(self, hex_frame: str) -> np.ndarray:
        """Build a complex IQ stream at 2 MSPS carrying one clean squitter."""
        env = np.random.default_rng(2).uniform(0, 0.02, size=4000).astype(np.float32)
        start = 500

        # Preamble
        for pulse_us in (0.0, 1.0, 3.5, 4.5):
            s = int(round(pulse_us * _SPS))
            w = max(1, int(round(0.5 * _SPS)))
            env[start + s:start + s + w] = 1.0

        # PPM data: pulse in first half of slot for bit=1, second half for bit=0
        bits = _bytes_to_bits(bytes.fromhex(hex_frame))
        data_start = start + _PREAMBLE_LEN
        for i, b in enumerate(bits):
            slot = data_start + i * _SPS
            if b:
                env[slot:slot + _SPS // 2] = 1.0
            else:
                env[slot + _SPS // 2:slot + _SPS] = 1.0

        # Turn power envelope into complex IQ (phase doesn't matter for AM/PPM)
        amp = np.sqrt(env)
        return amp.astype(np.complex64)

    def test_synthetic_ident_frame_decodes(self):
        iq = self._synthesise_iq('8D4840D6202CC371C32CE0576098')

        class _State:
            bw_hz = 2_000_000

        d = AdsbDecoder()
        d.start(_State())
        result = d.process(iq, _State())

        assert result['n_crc_ok'] >= 1
        assert '4840D6' in result['aircraft']
        assert result['aircraft']['4840D6'].get('callsign') == 'KLM1023'
