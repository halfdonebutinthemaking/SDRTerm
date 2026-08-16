"""ACARS plugin regression + multi-channel tests.

Focus (in order):
  1. Backward compatibility: no channels configured → old single-channel
     tune-at-centre behaviour is unchanged.
  2. Multi-channel mode: preset configures a channel list → downconvert +
     per-channel demod, all channels seen from one IQ capture.
  3. Config plumbing: load_state / save_state round-trip.
"""
import numpy as np
import pytest

from plugins.acars.acars import (
    AcarsDecoder,
    _ChannelState,
    _grow_ring,
)
from core import AppState


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_state(sr: int, center_hz: float) -> AppState:
    s = AppState()
    s.bw_hz     = sr
    s.center_hz = center_hz
    return s


def _tiny_noise(n: int, ampl: float = 1e-5) -> np.ndarray:
    return (ampl * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


# ── module helpers ────────────────────────────────────────────────────────────

class TestGrowRing:
    def test_appends_within_cap(self):
        buf  = np.array([1, 2, 3], dtype=np.float32)
        incm = np.array([4, 5],    dtype=np.float32)
        out  = _grow_ring(buf, incm, cap=10)
        assert list(out) == [1, 2, 3, 4, 5]

    def test_trims_when_over_cap(self):
        buf  = np.array([1, 2, 3, 4, 5], dtype=np.float32)
        incm = np.array([6, 7, 8],       dtype=np.float32)
        out  = _grow_ring(buf, incm, cap=4)
        assert list(out) == [5, 6, 7, 8]     # tail-preserving

    def test_does_not_mutate_input(self):
        buf  = np.array([1.0, 2.0], dtype=np.float32)
        incm = np.array([3.0],      dtype=np.float32)
        _grow_ring(buf, incm, cap=10)
        assert list(buf) == [1.0, 2.0]


# ── channel state class ──────────────────────────────────────────────────────

class TestChannelState:
    def test_defaults(self):
        c = _ChannelState('Tower', 131_725_000)
        assert c.name == 'Tower'
        assert c.freq_hz == 131_725_000
        assert len(c.audio_buf) == 0
        assert c.nco_phase == 0.0


# ── preset persistence ────────────────────────────────────────────────────────

class TestLoadState:
    def test_no_channels_by_default(self):
        d = AcarsDecoder()
        assert d._channels == []

    def test_load_channels_from_preset(self):
        d = AcarsDecoder()
        d.load_state({
            'channels': [
                {'freq': 131_725_000, 'name': 'AOA'},
                {'freq': 131_525_000, 'name': 'SITA'},
            ],
        })
        assert len(d._channels) == 2
        assert d._channels[0].freq_hz == 131_725_000
        assert d._channels[0].name    == 'AOA'

    def test_load_ignores_malformed_entries(self):
        d = AcarsDecoder()
        d.load_state({
            'channels': [
                {'freq': 131_725_000, 'name': 'OK'},
                {'name': 'no-freq'},                  # missing freq → skipped
                'not-a-dict',                          # wrong type → skipped
                {'freq': 131_525_000},                 # no name → auto-generated
            ],
        })
        assert len(d._channels) == 2
        # Second surviving channel gets an auto-name derived from its freq.
        assert d._channels[1].name == '131.525'

    def test_save_state_only_when_configured(self):
        d = AcarsDecoder()
        assert d.save_state() == {}     # nothing to persist in single-mode
        d.load_state({'channels': [{'freq': 131_725_000, 'name': 'A'}]})
        saved = d.save_state()
        assert saved == {'channels': [{'freq': 131_725_000, 'name': 'A'}]}

    def test_load_state_wipes_previous_channels(self):
        d = AcarsDecoder()
        d.load_state({'channels': [{'freq': 131_725_000, 'name': 'A'}]})
        d.load_state({'channels': [{'freq': 131_525_000, 'name': 'B'}]})
        assert len(d._channels) == 1
        assert d._channels[0].name == 'B'


# ── behaviour of the dispatcher ──────────────────────────────────────────────

class TestProcessDispatch:
    """process() picks between single-channel and multi-channel based on
    whether load_state configured any channels — verify each dispatch."""

    SR    = 250_000
    CTR   = 131_725_000

    def test_single_channel_shape_when_no_channels(self):
        d = AcarsDecoder()
        r = d.process(_tiny_noise(4096), _mk_state(self.SR, self.CTR))
        # Single-channel shape: no 'channels' or 'active' keys.
        assert 'channels' not in r
        assert 'active'   not in r
        assert 'messages' in r
        assert 'n_frames' in r

    def test_multi_channel_shape_when_channels_configured(self):
        d = AcarsDecoder()
        d.load_state({
            'channels': [
                {'freq': 131_725_000, 'name': 'AOA'},
                {'freq': 131_525_000, 'name': 'SITA'},
            ],
        })
        # Enough samples that at least one channel has ≥ 0.5 s of audio.
        n = 262_144
        r = d.process(_tiny_noise(n), _mk_state(2_000_000, 131_650_000))
        assert 'channels' in r
        assert r['channels'] == ['AOA', 'SITA']
        # 'active' lists channels that got past the min-audio threshold.
        assert isinstance(r['active'], list)

    def test_multi_channel_skips_out_of_band(self):
        d = AcarsDecoder()
        d.load_state({
            'channels': [
                {'freq': 131_725_000, 'name': 'InBand'},
                {'freq': 130_000_000, 'name': 'FarLow'},      # 1.65 MHz below centre
                {'freq': 133_000_000, 'name': 'FarHigh'},     # 1.35 MHz above centre
            ],
        })
        # bw_hz=2e6, so |offset| > 900 kHz is out-of-band.  Only 'InBand'
        # (offset 75 kHz) should ever accumulate audio.
        r = d.process(_tiny_noise(262_144),
                      _mk_state(2_000_000, 131_650_000))
        # 'active' should only include the in-band channel (if it got enough
        # audio) — the out-of-band ones must not appear.
        for name in r.get('active', []):
            assert name == 'InBand', f'out-of-band {name} was demodulated'

    def test_multi_channel_reset_on_start(self):
        d = AcarsDecoder()
        d.load_state({'channels': [{'freq': 131_725_000, 'name': 'A'}]})
        # Fake up an audio buffer as if process() had run.
        d._channels[0].audio_buf = np.ones(1000, dtype=np.float32)
        d._channels[0].nco_phase = 1.23
        d.start(_mk_state(2_000_000, 131_650_000))
        assert len(d._channels[0].audio_buf) == 0
        assert d._channels[0].nco_phase == 0.0


# ── NCO continuous phase across chunks (regression) ─────────────────────────

class TestNcoContinuity:
    """The multi-channel downconvert uses a continuous-phase NCO so
    consecutive chunks stitch together seamlessly.  If the phase resets
    between chunks we'd see a click / phase discontinuity at every
    boundary that would confuse the AM envelope + FSK demod."""

    def test_nco_phase_advances_across_chunks(self):
        d = AcarsDecoder()
        d.load_state({'channels': [{'freq': 131_725_000, 'name': 'A'}]})
        state = _mk_state(2_000_000, 131_650_000)
        p0 = d._channels[0].nco_phase
        d.process(_tiny_noise(4096), state)
        p1 = d._channels[0].nco_phase
        d.process(_tiny_noise(4096), state)
        p2 = d._channels[0].nco_phase
        # Phase should have advanced (been mutated) between calls — the
        # exact value depends on offset*n/sr modulo 2π.
        assert p0 != p1 or p1 != p2

    def test_nco_phase_stays_bounded(self):
        d = AcarsDecoder()
        d.load_state({'channels': [{'freq': 131_725_000, 'name': 'A'}]})
        state = _mk_state(2_000_000, 131_650_000)
        for _ in range(20):
            d.process(_tiny_noise(4096), state)
        # We modulo 2π every call, so the phase never drifts unbounded.
        assert -2 * np.pi <= d._channels[0].nco_phase <= 2 * np.pi
