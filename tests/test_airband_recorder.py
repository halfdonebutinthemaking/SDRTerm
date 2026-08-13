"""Airband voice recorder plugin tests.

Focus: verify the plugin discovers channels from the preset, demodulates
an AM tone offset from the tuned centre correctly, respects the squelch
threshold, writes a valid WAV, and indexes it in the CSV.
"""
import csv
import os
import time
import wave

import numpy as np
import pytest

from plugins.airband_recorder.airband_recorder import (
    AirbandRecorderDecoder,
    _mhz_str,
    _safe_name,
)
from core import AppState


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_state(sr: int, center_hz: float) -> AppState:
    s = AppState()
    s.bw_hz     = sr
    s.center_hz = center_hz
    return s


def _make_am_signal(sr: int, n: int, carrier_offset_hz: float,
                    audio_hz: float = 1_000.0,
                    mod_depth: float = 0.7,
                    carrier_ampl: float = 0.5) -> np.ndarray:
    """A single AM voice-like tone at (baseband_centre + carrier_offset_hz).
    Envelope = carrier_ampl * (1 + mod_depth * sin(2π·audio_hz·t))."""
    t = np.arange(n, dtype=np.float64) / sr
    env = carrier_ampl * (1.0 + mod_depth * np.sin(2.0 * np.pi * audio_hz * t))
    x   = env * np.exp(1j * 2.0 * np.pi * carrier_offset_hz * t)
    return x.astype(np.complex64)


def _tiny_noise(sr: int, n: int, ampl: float = 1e-5) -> np.ndarray:
    """Pure noise, mostly for testing the closed-squelch state."""
    return (ampl * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


# ── plugin config / discovery ────────────────────────────────────────────────

class TestLoadState:
    def test_disabled_by_default(self):
        d = AirbandRecorderDecoder()
        assert d._enabled is False
        assert d._channels == []

    def test_load_state_populates_channels(self, tmp_path):
        d = AirbandRecorderDecoder()
        d.load_state({
            'enabled':    True,
            'output_dir': str(tmp_path),
            'channels': [
                {'freq': 118_100_000, 'name': 'Tower'},
                {'freq': 121_500_000, 'name': 'Guard'},
            ],
        })
        assert d._enabled is True
        assert len(d._channels) == 2
        assert d._channels[0].freq_hz == 118_100_000
        assert d._channels[0].name    == 'Tower'
        assert d._channels[1].name    == 'Guard'

    def test_save_state_roundtrip(self, tmp_path):
        d = AirbandRecorderDecoder()
        d.load_state({
            'enabled':    True,
            'output_dir': str(tmp_path),
            'channels':   [{'freq': 121_900_000, 'name': 'Ground'}],
        })
        saved = d.save_state()
        assert saved['enabled'] is True
        assert saved['output_dir'] == str(tmp_path)
        assert saved['channels'] == [{'freq': 121_900_000, 'name': 'Ground'}]

    def test_load_state_ignores_bad_channel_entries(self):
        d = AirbandRecorderDecoder()
        d.load_state({
            'enabled': True,
            'channels': [
                {'freq': 118_100_000, 'name': 'OK'},
                {'name': 'no-freq'},              # missing freq → skipped
                'not-a-dict',                      # wrong type → skipped
                {'freq': 121_500_000},             # missing name → OK, name defaults
            ],
        })
        assert len(d._channels) == 2


# ── DSP + squelch + WAV / index ──────────────────────────────────────────────

class TestRecording:
    SR         = 2_000_000
    CENTER_HZ  = 122_000_000
    CHANNEL_HZ = 121_500_000        # 500 kHz offset from centre
    OFFSET_HZ  = CHANNEL_HZ - CENTER_HZ

    N_CHUNK    = 262_144            # matches READ_MAX

    def _mk(self, tmp_path, squelch=-55.0, hangover=0.4) -> AirbandRecorderDecoder:
        d = AirbandRecorderDecoder()
        d.load_state({
            'enabled':            True,
            'output_dir':         str(tmp_path),
            'squelch_dbfs':       squelch,
            'silence_hangover_s': hangover,
            'audio_rate_hz':      8000,
            'channels':           [{'freq': self.CHANNEL_HZ, 'name': 'Guard'}],
        })
        return d

    def test_disabled_does_not_process(self, tmp_path):
        d = AirbandRecorderDecoder()      # never loaded → disabled
        r = d.process(_tiny_noise(self.SR, 1024),
                      _mk_state(self.SR, self.CENTER_HZ))
        assert r['enabled'] is False
        assert r['channels'] == []

    def test_out_of_range_channel_marked_off_band(self, tmp_path):
        d = self._mk(tmp_path)
        # Move the tuned centre far from the configured channel.
        r = d.process(_tiny_noise(self.SR, 1024),
                      _mk_state(self.SR, self.CENTER_HZ + 5_000_000))
        assert len(r['channels']) == 1
        assert r['channels'][0]['in_range'] is False
        assert r['channels'][0]['is_open']  is False

    def test_am_signal_opens_squelch_and_writes_wav(self, tmp_path):
        d = self._mk(tmp_path)
        sig = _make_am_signal(self.SR, self.N_CHUNK, self.OFFSET_HZ)

        # First chunk — enough to open squelch.
        r1 = d.process(sig, _mk_state(self.SR, self.CENTER_HZ))
        assert r1['channels'][0]['is_open'] is True
        assert r1['channels'][0]['rms_db'] > -55.0

        # Feed silence for a few chunks — enough for the hangover
        # to expire.  Advance the clock via monkeypatching.
        import plugins.airband_recorder.airband_recorder as mod
        real_time = time.time
        t0 = real_time()
        # Fake now to jump 5 s after the last active chunk.
        try:
            mod.time.time = lambda: t0 + 5.0
            d.process(_tiny_noise(self.SR, self.N_CHUNK),
                      _mk_state(self.SR, self.CENTER_HZ))
        finally:
            mod.time.time = real_time

        # WAV should exist and the CSV index should have one entry.
        wavs = [f for f in os.listdir(tmp_path) if f.endswith('.wav')]
        assert len(wavs) == 1, f"expected 1 WAV, got {wavs}"
        index_path = os.path.join(str(tmp_path), 'index.csv')
        assert os.path.isfile(index_path)
        with open(index_path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert int(rows[0]['freq_hz']) == self.CHANNEL_HZ
        assert rows[0]['name'] == 'Guard'
        assert float(rows[0]['duration_s']) > 0.05

    def test_wav_is_valid_16bit_mono(self, tmp_path):
        d = self._mk(tmp_path)
        sig = _make_am_signal(self.SR, self.N_CHUNK, self.OFFSET_HZ)
        d.process(sig, _mk_state(self.SR, self.CENTER_HZ))

        # Trigger finalise via hangover expiry.
        import plugins.airband_recorder.airband_recorder as mod
        real_time = time.time
        t0 = real_time()
        try:
            mod.time.time = lambda: t0 + 5.0
            d.process(_tiny_noise(self.SR, self.N_CHUNK),
                      _mk_state(self.SR, self.CENTER_HZ))
        finally:
            mod.time.time = real_time

        wavs = [os.path.join(str(tmp_path), f)
                for f in os.listdir(tmp_path) if f.endswith('.wav')]
        assert wavs, "no wav produced"
        with wave.open(wavs[0], 'rb') as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2       # 16-bit
            assert wf.getframerate() == 8000
            assert wf.getnframes()   > 0

    def test_silent_input_does_not_open_squelch(self, tmp_path):
        d = self._mk(tmp_path)
        r = d.process(_tiny_noise(self.SR, self.N_CHUNK),
                      _mk_state(self.SR, self.CENTER_HZ))
        assert r['channels'][0]['is_open'] is False
        # No WAV should have been created.
        assert not any(f.endswith('.wav') for f in os.listdir(tmp_path))

    def test_reset_key_clears_stats(self, tmp_path):
        d = self._mk(tmp_path)
        d._n_recorded = 7
        d._n_dropped_short = 3
        d._channels[0].rms_db    = -30.0
        d._channels[0].peak_dbfs = -20.0
        d.handle_key(ord('r'), _mk_state(self.SR, self.CENTER_HZ), None)
        assert d._n_recorded == 0
        assert d._n_dropped_short == 0
        assert d._channels[0].rms_db    == -120.0
        assert d._channels[0].peak_dbfs == -120.0


# ── module helpers ────────────────────────────────────────────────────────────

class TestModuleHelpers:
    def test_mhz_str(self):
        assert _mhz_str(118_100_000) == '118.100MHz'
        assert _mhz_str(121_500_000) == '121.500MHz'

    def test_safe_name_strips_unsafe_chars(self):
        assert _safe_name('Tower/Ground')  == 'Tower_Ground'
        assert _safe_name('  ATIS 1 ')     == '__ATIS_1_'
        assert _safe_name('')              == 'ch'
        assert _safe_name('a' * 100)       == 'a' * 40
