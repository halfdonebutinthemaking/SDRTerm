"""Tests for the AIS plugin.

Covers:
  - CRC-16-CCITT against the "123456789" HDLC FCS reference vector.
  - NRZI round-trip (encode → decode is identity).
  - HDLC frame extraction, bit-stuffing, FCS validation.
  - MSB-first bitfield reader + Type 1 position-report decode via
    round-trip (field → packed bytes → parse_ais → fields).
  - End-to-end GMSK demod on a synthetic burst, confirming the full
    chain (modulator → demod → NRZI → HDLC → CRC → parse) recovers
    the original payload.
  - Plugin housekeeping: default channels, status text shape, dedup,
    CSV format sanity.
"""
import os
import tempfile

import numpy as np
import pytest

from plugins.ais.protocol import (
    gmsk_demod, nrzi_encode, nrzi_decode,
    crc16_ccitt, hdlc_frames,
    ais_payload_from_hdlc, parse_ais,
    _bits, _signed,                      # internal — ok to test here
)
from plugins.ais.ais import AisDecoder, _crc_key


# ── CRC-16-CCITT ──────────────────────────────────────────────────────────────

class TestCrc:
    def test_123456789_reference_vector(self):
        """Standard HDLC FCS test vector (CRC-16-X.25 reflected).
        See https://users.ece.cmu.edu/~koopman/crc/crc16.html"""
        assert crc16_ccitt(b'123456789') == 0x906E

    def test_empty_input_init_only(self):
        # init=0xFFFF, XOR-out=0xFFFF → empty-input CRC is zero.
        assert crc16_ccitt(b'') == 0x0000

    def test_single_byte_deterministic(self):
        # Same input always produces the same output.
        v = crc16_ccitt(b'\xAB')
        assert v == crc16_ccitt(b'\xAB')
        assert 0 <= v <= 0xFFFF


# ── NRZI ──────────────────────────────────────────────────────────────────────

class TestNrzi:
    @pytest.mark.parametrize('bits', [
        [0, 0, 0, 0, 0],
        [1, 1, 1, 1, 1, 1, 1, 1],
        [0, 1, 0, 1, 0, 1, 0, 1],
        [1, 0, 0, 1, 1, 1, 0, 1, 0],
    ])
    def test_roundtrip(self, bits):
        """nrzi_decode(nrzi_encode(x)) == x for any bit sequence."""
        assert nrzi_decode(nrzi_encode(bits, initial=0)) == bits
        assert nrzi_decode(nrzi_encode(bits, initial=1)) == bits

    def test_zero_causes_transition(self):
        # NRZI: 0 → transition; starting from state 0, a single 0 goes to 1.
        wire = nrzi_encode([0], initial=0)
        assert wire == [0, 1]
        assert nrzi_decode(wire) == [0]

    def test_one_preserves_state(self):
        # Three 1s in a row: state never flips.
        wire = nrzi_encode([1, 1, 1], initial=0)
        assert wire == [0, 0, 0, 0]
        assert nrzi_decode(wire) == [1, 1, 1]


# ── HDLC framing ──────────────────────────────────────────────────────────────

def _bits_from_bytes_lsb(data: bytes) -> list[int]:
    """Convert bytes to a bit list, LSB-first within each byte."""
    out = []
    for b in data:
        for i in range(8):
            out.append((b >> i) & 1)
    return out


def _bit_stuff(bits: list[int]) -> list[int]:
    """Insert a 0 after every five consecutive 1s (HDLC bit stuffing)."""
    out, count = [], 0
    for b in bits:
        out.append(b)
        if b == 1:
            count += 1
            if count == 5:
                out.append(0)
                count = 0
        else:
            count = 0
    return out


def _build_hdlc_frame(payload: bytes) -> list[int]:
    """Build a complete HDLC bit stream around `payload`: flag + stuffed
    (payload + FCS) + flag."""
    fcs       = crc16_ccitt(payload)
    body_bits = _bits_from_bytes_lsb(payload + bytes([fcs & 0xFF, (fcs >> 8) & 0xFF]))
    flag      = [0, 1, 1, 1, 1, 1, 1, 0]
    return flag + _bit_stuff(body_bits) + flag


class TestHdlc:
    def test_single_frame_crc_ok(self):
        payload = b'HELLO AIS'
        stream  = _build_hdlc_frame(payload)
        frames  = list(hdlc_frames(stream))
        assert len(frames) == 1
        got_payload, crc_ok = frames[0]
        assert got_payload == payload
        assert crc_ok is True

    def test_bit_stuffing_round_trip(self):
        # Payload full of 0xFF triggers bit stuffing on every byte.
        payload = b'\xff\xff\xff\xff\xff'
        stream  = _build_hdlc_frame(payload)
        frames  = list(hdlc_frames(stream))
        assert len(frames) == 1
        assert frames[0] == (payload, True)

    def test_crc_corruption_detected(self):
        payload = b'TAMPER ME'
        stream  = _build_hdlc_frame(payload)
        # Flip one bit well inside the payload, after the opening flag.
        stream[20] ^= 1
        frames = list(hdlc_frames(stream))
        assert len(frames) == 1
        assert frames[0][1] is False       # crc_ok

    def test_consecutive_flags_skipped(self):
        payload = b'INTERFRAME FILL'
        stream  = _build_hdlc_frame(payload)
        # Add a second opening flag immediately after the first.
        flag = [0, 1, 1, 1, 1, 1, 1, 0]
        stream = flag + stream
        frames = list(hdlc_frames(stream))
        assert len(frames) == 1
        assert frames[0] == (payload, True)

    def test_no_flag_no_frames(self):
        assert list(hdlc_frames([0, 1, 0, 1, 0, 1, 0, 1] * 10)) == []


# ── Bitfield reader + MSB-first byte order ───────────────────────────────────

class TestBitfield:
    def test_bits_reads_msb_first(self):
        # 0xA5 = 1010 0101
        data = b'\xA5'
        assert _bits(data, 0, 4) == 0b1010
        assert _bits(data, 4, 4) == 0b0101
        assert _bits(data, 0, 8) == 0xA5

    def test_bits_crosses_byte_boundary(self):
        # Pack bits across two bytes: 0xA5 0x3C = 1010 0101 0011 1100
        data = b'\xA5\x3C'
        assert _bits(data, 4, 8) == 0b01010011   # 0x53
        assert _bits(data, 6, 4) == 0b0100

    def test_signed_negative(self):
        # 8-bit two's-complement: 0xFF = -1
        assert _signed(0xFF, 8) == -1
        assert _signed(0x80, 8) == -128
        assert _signed(0x7F, 8) == 127

    def test_signed_positive(self):
        assert _signed(0, 8) == 0
        assert _signed(0x40, 8) == 64

    def test_ais_payload_from_hdlc_reverses_each_byte(self):
        # 0x01 = 0000 0001 LSB-first-on-wire; bit-reversed = 1000 0000 = 0x80.
        assert ais_payload_from_hdlc(b'\x01') == b'\x80'
        assert ais_payload_from_hdlc(b'\xA5') == b'\xA5'   # palindromic byte
        assert ais_payload_from_hdlc(b'\x01\x02') == b'\x80\x40'


# ── AIS Type 1 position-report round-trip ────────────────────────────────────

def _pack_bits(fields: list[tuple[int, int]]) -> bytes:
    """Pack a list of (value, width_in_bits) tuples into bytes, MSB-first."""
    total_bits = sum(w for _, w in fields)
    n_bytes    = (total_bits + 7) // 8
    out        = bytearray(n_bytes)
    pos        = 0
    for value, width in fields:
        value &= (1 << width) - 1
        for i in range(width):
            bit = (value >> (width - 1 - i)) & 1
            if bit:
                out[(pos + i) >> 3] |= 1 << (7 - ((pos + i) & 7))
        pos += width
    return bytes(out)


class TestAisType1Decode:
    """Construct a synthetic 168-bit Type 1 payload with known fields,
    push it through parse_ais, and verify every field decodes to the
    value we encoded.  This locks down the bit-offset layout against
    accidental drift."""

    MMSI    = 227_006_760
    LAT     = 48.580217
    LON     = -3.980950
    SOG     = 11.1          # knots
    COG     = 125.0
    HEADING = 125
    NAV     = 0             # under way using engine
    TS      = 42            # UTC second

    def _build_payload(self):
        # Field order per ITU-R M.1371-5 Table 44 (bit offsets 0..167).
        lat_i = int(round(self.LAT * 600_000)) & ((1 << 27) - 1)
        lon_i = int(round(self.LON * 600_000)) & ((1 << 28) - 1)
        fields = [
            (1,                 6),   # type
            (0,                 2),   # repeat
            (self.MMSI,        30),
            (self.NAV,          4),
            (0,                 8),   # rot_raw (not under test)
            (int(self.SOG * 10), 10),
            (0,                 1),   # pos_acc
            (lon_i,            28),
            (lat_i,            27),
            (int(self.COG * 10), 12),
            (self.HEADING,      9),
            (self.TS,           6),
            (0,                 2),   # special manoeuvre
            (0,                 3),   # spare
            (0,                 1),   # RAIM
            (0,                19),   # radio status
        ]
        return _pack_bits(fields)

    def test_type_field(self):
        p = parse_ais(self._build_payload())
        assert p['type'] == 1

    def test_mmsi_field(self):
        p = parse_ais(self._build_payload())
        assert p['mmsi'] == self.MMSI

    def test_position_fields(self):
        p = parse_ais(self._build_payload())
        assert p['lat'] == pytest.approx(self.LAT, abs=1 / 600_000)
        assert p['lon'] == pytest.approx(self.LON, abs=1 / 600_000)

    def test_motion_fields(self):
        p = parse_ais(self._build_payload())
        assert p['sog'] == pytest.approx(self.SOG, abs=0.1)
        assert p['cog'] == pytest.approx(self.COG, abs=0.1)
        assert p['heading'] == self.HEADING

    def test_navigation_status(self):
        p = parse_ais(self._build_payload())
        assert p['nav_status'] == self.NAV

    def test_timestamp(self):
        p = parse_ais(self._build_payload())
        assert p['timestamp'] == self.TS


class TestAisType18Decode:
    """Class B position report has a different field layout but reuses
    the same bit-level primitives.  Lock the key offsets."""

    MMSI = 265_619_000
    LAT  = 59.321
    LON  = 18.062

    def _build_payload(self):
        lat_i = int(round(self.LAT * 600_000)) & ((1 << 27) - 1)
        lon_i = int(round(self.LON * 600_000)) & ((1 << 28) - 1)
        fields = [
            (18,                 6),   # type
            (0,                  2),   # repeat
            (self.MMSI,         30),
            (0,                  8),   # reserved
            (37,                10),   # sog = 3.7 kn
            (1,                  1),   # pos_acc
            (lon_i,             28),
            (lat_i,             27),
            (900,               12),   # cog = 90.0°
            (511,                9),   # heading not available
            (10,                 6),   # timestamp
            (0,                 29),   # reserved + flags + radio
        ]
        return _pack_bits(fields)

    def test_type_and_mmsi(self):
        p = parse_ais(self._build_payload())
        assert p['type'] == 18
        assert p['mmsi'] == self.MMSI

    def test_position(self):
        p = parse_ais(self._build_payload())
        assert p['lat'] == pytest.approx(self.LAT, abs=1 / 600_000)
        assert p['lon'] == pytest.approx(self.LON, abs=1 / 600_000)


class TestParseAisDispatch:
    def test_too_short_returns_none(self):
        assert parse_ais(b'') is None

    def test_unknown_type_returns_minimal_dict(self):
        # Build a 5-byte payload with type=27 (long-range AIS broadcast,
        # which parse_ais does not field-decode yet).  Type field is
        # 6 bits so 0-63 only.
        p = _pack_bits([
            (27, 6), (0, 2), (123_456_789, 30), (0, 2),
        ])
        got = parse_ais(p)
        assert got == {'type': 27, 'mmsi': 123_456_789}


# ── End-to-end: GMSK modulator → demod → HDLC → parse ────────────────────────

def _modulate_gmsk(bits: list[int], sps: int, bt: float = 0.4) -> np.ndarray:
    """Minimal GMSK modulator for test only — Gaussian-filtered NRZ,
    integrated to phase, radiated as complex exp.  Not exactly the
    AIS filter (we don't apply the BT=0.4 Gaussian precisely), but
    produces a signal our discriminator decodes cleanly at high SNR."""
    # Upsample ±1 NRZ.
    nrz = np.repeat(np.where(np.array(bits) == 1, 1.0, -1.0), sps)
    # Light Gaussian smoothing.
    sigma = sps / (2 * np.pi * bt)
    radius = int(3 * sigma)
    t = np.arange(-radius, radius + 1)
    g = np.exp(-(t ** 2) / (2 * sigma ** 2))
    g = g / g.sum()
    shaped = np.convolve(nrz, g, mode='same')
    # Integrate to phase; modulation index h=0.5 for MSK/GMSK.
    phase = np.cumsum(shaped) * (np.pi / 2) / sps
    return np.exp(1j * phase).astype(np.complex64)


class TestGmskRoundTrip:
    """Modulator → demod → NRZI → HDLC should return the original payload."""

    def _build_burst(self, payload: bytes, sps: int = 8) -> np.ndarray:
        stream   = _build_hdlc_frame(payload)
        wire     = nrzi_encode(stream, initial=0)
        preamble = ([0, 1] * 12) + [0] * 4
        wire     = preamble + wire + ([0] * 24)
        return _modulate_gmsk(wire, sps)

    def test_short_payload_recovery(self):
        payload  = b'AISTEST'
        sps      = 8
        baseband = self._build_burst(payload, sps)

        bits, _, _, _ = gmsk_demod(
            baseband, sps, prev_iq=None, sym_offset=sps // 2, filter_state=None,
        )
        decoded = nrzi_decode(bits)
        frames  = list(hdlc_frames(decoded))

        assert any(f == (payload, True) for f in frames), \
            f'synthetic burst did not round-trip; got: {frames}'

    def test_frame_recovery_across_chunk_boundaries(self):
        """A real AIS slot is 26.67 ms; one main-loop chunk at 1.024 MHz
        is 16 ms.  Every burst spans ≥ 1 boundary.  Feeding the synthetic
        baseband in 500-sample slices (deliberately not a multiple of
        sps) must still recover the payload, confirming that prev_iq +
        sym_offset keep the discriminator and bit-centre slicer aligned."""
        payload  = b'ACROSSCHUNKS'
        sps      = 8
        baseband = self._build_burst(payload, sps)

        chunk_size   = 500         # 62.5 bits per chunk — intentionally fractional
        prev_iq      = None
        sym_offset   = sps // 2
        filter_state = None
        wire_bits    = []

        for start in range(0, len(baseband), chunk_size):
            chunk = baseband[start:start + chunk_size]
            if len(chunk) < 2:
                continue
            bits, prev_iq, sym_offset, filter_state = gmsk_demod(
                chunk, sps, prev_iq=prev_iq, sym_offset=sym_offset,
                filter_state=filter_state,
            )
            wire_bits.extend(bits)

        decoded = nrzi_decode(wire_bits)
        frames  = list(hdlc_frames(decoded))

        assert any(f == (payload, True) for f in frames), \
            f'split-chunk burst did not round-trip; got: {frames}'

    def test_single_chunk_matches_split_chunk(self):
        """Chunked and un-chunked demod must produce the same bits
        (modulo the 1-sample startup difference at chunk 0 when
        prev_iq is None).  This is a direct regression lock against
        timing drift at chunk boundaries."""
        payload  = b'IDEM'
        sps      = 8
        baseband = self._build_burst(payload, sps)

        full_bits, _, _, _ = gmsk_demod(
            baseband, sps, prev_iq=None, sym_offset=sps // 2, filter_state=None,
        )

        chunk_size   = 123         # tiny, nothing-special size
        prev_iq      = None
        sym_offset   = sps // 2
        filter_state = None
        split_bits   = []
        for start in range(0, len(baseband), chunk_size):
            chunk = baseband[start:start + chunk_size]
            if len(chunk) < 2:
                continue
            bits, prev_iq, sym_offset, filter_state = gmsk_demod(
                chunk, sps, prev_iq=prev_iq, sym_offset=sym_offset,
                filter_state=filter_state,
            )
            split_bits.extend(bits)

        # Allow ± 1 bit difference at the very start (prev_iq=None path
        # drops one sample on chunk 0 — expected) and ± 1 at the end
        # (boxcar edge effect on the final chunk).
        min_len = min(len(full_bits), len(split_bits))
        diffs   = sum(1 for i in range(1, min_len - 1)
                      if full_bits[i] != split_bits[i])
        assert diffs <= 2, \
            f'chunked demod drifted vs un-chunked: {diffs} bit mismatches'


# ── Plugin housekeeping ──────────────────────────────────────────────────────

class TestAisDecoderDefaults:
    def test_default_two_channels(self):
        d = AisDecoder()
        names = [ch.name for ch in d._channels]
        freqs = [ch.freq_hz for ch in d._channels]
        assert names == ['AIS 1', 'AIS 2']
        assert freqs == [161_975_000.0, 162_025_000.0]

    def test_min_sample_rate_covers_both_channels(self):
        d = AisDecoder()
        # 161.975 and 162.025 are 50 kHz apart; min_sample_rate must
        # exceed that with headroom for filter transition bands.
        assert d.min_sample_rate >= 100_000
        assert d.min_sample_rate == 1_024_000

    def test_full_view_true(self):
        assert AisDecoder.full_view is True

    def test_realtime_false(self):
        assert AisDecoder.realtime is False

    def test_key_binding(self):
        # 'y' because every mnemonic-friendly letter is already taken
        # (a=acars, b=adsb, c=constellation, …).  The plugin-menu letter
        # is a visual tag only.
        assert AisDecoder.key == 'y'

    def test_status_text_shape(self):
        d = AisDecoder()
        text = d.status_text(None, {'n_vessels': 3, 'n_crc_ok': 42,
                                    'n_bursts': 50})
        assert text.startswith('[AIS')
        assert '3v' in text
        assert '42msg' in text

    def test_status_text_empty_result(self):
        assert AisDecoder().status_text(None, None) == ''


class TestAisDecoderPersistence:
    def test_default_save_state_is_empty(self):
        """A fresh instance (defaults for everything) should serialise
        to {}, so a preset only writes what the user actually changed."""
        assert AisDecoder().save_state() == {}

    def test_disabling_logging_is_persisted(self):
        d = AisDecoder()
        d._logging_enabled = False
        assert d.save_state() == {'logging_enabled': False}

    def test_custom_channels_round_trip(self):
        d = AisDecoder()
        d.load_state({'channels': [
            {'freq': 161_975_000, 'name': 'AIS 1'},
            {'freq': 161_950_000, 'name': 'Chinese A'},
        ]})
        assert [ch.name for ch in d._channels] == ['AIS 1', 'Chinese A']
        saved = d.save_state()
        assert saved['channels'][1]['freq'] == 161_950_000

    def test_malformed_channel_entry_skipped(self):
        d = AisDecoder()
        d.load_state({'channels': [
            {'freq': 161_975_000, 'name': 'ok'},
            {'name': 'missing-freq'},
            'not-a-dict',
        ]})
        assert len(d._channels) == 1


class TestCrcKey:
    def test_short_input_returns_zero(self):
        assert _crc_key(b'') == 0
        assert _crc_key(b'\x01') == 0

    def test_last_two_bytes_little_endian(self):
        assert _crc_key(b'\x12\x34\x56\x78') == 0x7856
