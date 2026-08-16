"""VDL2 plugin multi-channel refactor tests.

Focus:
  1. Backward compatibility: no `channels` configured → single-channel
     behaviour (uses peak_marker offset, `_default_ch`) is unchanged.
  2. Multi-channel mode: preset supplies `channels` list → per-channel
     downconvert + demod, all fed from one IQ chunk.
  3. Config plumbing: load_state / save_state round-trip.
  4. Cross-chunk continuity: NCO phase, sym_offset, prev_sym,
     descramble_ctx all bounded and evolving per channel.
"""
import numpy as np
import pytest

from plugins.vdl2.vdl2 import VDL2Decoder, _ChannelState
from core import AppState


def _mk_state(sr: int, center_hz: float) -> AppState:
    s = AppState()
    s.bw_hz     = sr
    s.center_hz = center_hz
    return s


def _tiny_noise(n: int, ampl: float = 1e-4) -> np.ndarray:
    return (ampl * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


# ── _ChannelState behaviour ─────────────────────────────────────────────────

class TestChannelState:
    def test_defaults(self):
        c = _ChannelState('CPDLC', 136_700_000)
        assert c.name == 'CPDLC'
        assert c.freq_hz == 136_700_000
        assert c.carrier_phase == 0.0
        assert c.prev_sym is None
        assert c.sym_offset == 0
        assert c.descramble_ctx == [0] * 6
        assert len(c.bit_buf) == 0

    def test_reset_clears_per_channel_state(self):
        c = _ChannelState('AOC', 136_925_000)
        c.carrier_phase = 1.5
        c.prev_sym = 3+2j
        c.sym_offset = 4
        c.descramble_ctx = [1, 0, 1, 1, 0, 1]
        c.bit_buf.extend([0, 1, 0, 1])
        c.reset()
        assert c.carrier_phase == 0.0
        assert c.prev_sym is None
        assert c.sym_offset == 0
        assert c.descramble_ctx == [0] * 6
        assert len(c.bit_buf) == 0


# ── preset persistence ──────────────────────────────────────────────────────

class TestLoadState:
    def test_no_channels_by_default(self):
        d = VDL2Decoder()
        assert d._channels == []

    def test_load_channels_from_preset(self):
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'CPDLC-1'},
                {'freq': 136_925_000, 'name': 'AOC-E'},
            ],
        })
        assert len(d._channels) == 2
        assert d._channels[0].freq_hz == 136_700_000
        assert d._channels[0].name    == 'CPDLC-1'
        assert d._channels[1].name    == 'AOC-E'

    def test_load_state_wipes_previous_channels(self):
        d = VDL2Decoder()
        d.load_state({'channels': [{'freq': 136_700_000, 'name': 'A'}]})
        d.load_state({'channels': [{'freq': 136_925_000, 'name': 'B'}]})
        assert len(d._channels) == 1
        assert d._channels[0].name == 'B'

    def test_load_ignores_malformed_entries(self):
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'OK'},
                {'name': 'no-freq'},                # missing freq → skipped
                'not-a-dict',                        # wrong type   → skipped
                {'freq': 136_925_000},               # no name      → auto
            ],
        })
        assert len(d._channels) == 2
        # Missing-name entry gets an auto-generated name from freq_hz.
        assert d._channels[1].name == '136.925'

    def test_save_state_only_when_configured(self):
        d = VDL2Decoder()
        assert d.save_state() == {}          # single-mode: nothing to persist
        d.load_state({'channels': [{'freq': 136_700_000, 'name': 'A'}]})
        assert d.save_state() == {'channels': [{'freq': 136_700_000, 'name': 'A'}]}


# ── dispatch behaviour ──────────────────────────────────────────────────────

class TestProcessDispatch:
    """process() picks between single- and multi-channel based on whether
    load_state configured any channels — verify each dispatch path."""

    SR    = 2_000_000
    CTR   = 136_837_500

    def test_single_channel_shape_when_no_channels(self):
        d = VDL2Decoder()
        r = d.process(_tiny_noise(4096), _mk_state(self.SR, self.CTR))
        # Single-channel shape: no 'channels' key.
        assert 'channels' not in r
        assert 'n_frames' in r
        assert 'n_msgs'   in r

    def test_multi_channel_shape_when_channels_configured(self):
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'CPDLC'},
                {'freq': 136_925_000, 'name': 'AOC'},
            ],
        })
        r = d.process(_tiny_noise(4096), _mk_state(self.SR, self.CTR))
        assert 'channels' in r
        assert r['channels'] == ['CPDLC', 'AOC']

    def test_multi_channel_skips_out_of_band(self):
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'InBand'},       # -137.5 kHz offset
                {'freq': 135_000_000, 'name': 'FarLow'},       # -1.84 MHz — out
                {'freq': 138_500_000, 'name': 'FarHigh'},      # +1.66 MHz — out
            ],
        })
        # bw=2 MHz, guardband 10% → |offset| > 900 kHz is out-of-band.
        # In-band's carrier_phase advances by 2π·(-137.5k)·n/sr per call;
        # out-of-band channels are skipped entirely so their state stays untouched.
        state = _mk_state(self.SR, self.CTR)
        d.process(_tiny_noise(4096), state)
        in_band, far_low, far_high = d._channels
        assert in_band.carrier_phase != 0.0     # in-band → phase advanced
        assert far_low.carrier_phase == 0.0     # out-of-band → skipped
        assert far_high.carrier_phase == 0.0    # out-of-band → skipped


# ── cross-chunk continuity ──────────────────────────────────────────────────

class TestCrossChunkContinuity:
    """Multi-channel state must evolve independently and stay bounded
    across many chunks — this is what avoids per-chunk boundary artefacts
    (phase discontinuities, sym-offset slips, descrambler restarts)."""

    SR  = 2_000_000
    CTR = 136_837_500

    def test_each_channel_has_independent_state(self):
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'A'},   # offset -137.5 kHz
                {'freq': 136_925_000, 'name': 'B'},   # offset  +87.5 kHz
            ],
        })
        state = _mk_state(self.SR, self.CTR)
        for _ in range(3):
            d.process(_tiny_noise(4096), state)
        # Both channels should have advanced their carrier_phase but by
        # DIFFERENT amounts (different offsets → different accumulated phase).
        a, b = d._channels
        assert a.carrier_phase != 0.0
        assert b.carrier_phase != 0.0
        assert a.carrier_phase != b.carrier_phase, \
            'channels should not share carrier_phase state'

    def test_carrier_phase_stays_bounded(self):
        d = VDL2Decoder()
        d.load_state({'channels': [{'freq': 136_700_000, 'name': 'A'}]})
        state = _mk_state(self.SR, self.CTR)
        for _ in range(50):
            d.process(_tiny_noise(4096), state)
        # Modulo 2π every call, so drift stays bounded.
        p = d._channels[0].carrier_phase
        assert -2 * np.pi <= p <= 2 * np.pi

    def test_start_resets_all_channel_state(self):
        d = VDL2Decoder()
        d.load_state({'channels': [{'freq': 136_700_000, 'name': 'A'}]})
        state = _mk_state(self.SR, self.CTR)
        d.process(_tiny_noise(4096), state)
        # Fake up some state as if bursts had been processed
        d._channels[0].prev_sym = 1+1j
        d._channels[0].sym_offset = 3
        d._n_frames = 42
        d._n_errors = 5
        d.start(state)
        assert d._channels[0].carrier_phase == 0.0
        assert d._channels[0].prev_sym is None
        assert d._channels[0].sym_offset == 0
        assert d._n_frames == 0
        assert d._n_errors == 0

    def test_shared_rrc_cache_across_channels(self):
        # All channels at the same sample rate should share ONE cached
        # RRC filter — otherwise memory + CPU grow linearly in n_channels.
        d = VDL2Decoder()
        d.load_state({
            'channels': [
                {'freq': 136_700_000, 'name': 'A'},
                {'freq': 136_925_000, 'name': 'B'},
                {'freq': 136_975_000, 'name': 'C'},
            ],
        })
        state = _mk_state(self.SR, self.CTR)
        d.process(_tiny_noise(4096), state)
        # RRC cache indexed by bw_hz — should have exactly one entry
        # regardless of how many channels processed the same sample rate.
        assert len(d._rrc_cache) == 1
        assert self.SR in d._rrc_cache
