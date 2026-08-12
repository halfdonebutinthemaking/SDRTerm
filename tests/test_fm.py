"""FM demodulator regression tests.

Focus: the DC-blocker step at the top of process().  Direct-conversion
radios (HackRF) put a large DC spike at the tuned centre frequency;
without a mean-subtract that spike dominates the arctangent phase demod
and produces horrible audio.  Superhet radios (RTL-SDR) don't have the
spike but the fix is a harmless no-op for them.
"""
import numpy as np
import pytest


def _make_fm_signal(sr: int, n: int, audio_hz: float,
                    deviation_hz: float) -> np.ndarray:
    """Synthesize a baseband FM signal with a single audio tone."""
    t = np.arange(n) / sr
    # Instantaneous phase = integral of instantaneous frequency.
    # For m(t) = sin(2π·audio_hz·t), the integral is
    # -cos(2π·audio_hz·t)/(2π·audio_hz), so phase = 2π·dev · (-cos(…)/audio_hz).
    phase = -(deviation_hz / audio_hz) * np.cos(2 * np.pi * audio_hz * t)
    return np.exp(1j * 2 * np.pi * phase).astype(np.complex64)


def _fresh_decoder():
    from plugins.fm.fm import FMDecoder
    return FMDecoder()


def _mk_state(sr: int, fm_bw: int = 100_000):
    from core import AppState
    s = AppState()
    s.bw_hz    = sr
    s.fm_bw_hz = fm_bw
    return s


class TestDcBlocker:
    """The mean-subtract at the top of process() should render the
    demodulator invariant to any static DC offset added to the samples."""

    SR = 2_000_000
    N  = 262_144    # matches READ_MAX

    def test_dc_offset_does_not_change_audio_output(self):
        # Same FM signal, one with a big DC bias (simulating HackRF LO leakage).
        clean  = _make_fm_signal(self.SR, self.N, audio_hz=1_000, deviation_hz=50_000)
        biased = clean + (0.4 + 0.3j)   # arbitrary DC term, ~40% of full-scale

        # Fresh decoder per call so filter state doesn't carry across.
        r1 = _fresh_decoder().process(clean,  _mk_state(self.SR))
        r2 = _fresh_decoder().process(biased, _mk_state(self.SR))

        # After the DC blocker, both inputs should produce essentially
        # the same audio.  Use RMS as a robust scalar; a raw sample-wise
        # comparison would fail on tiny filter-transient differences.
        assert abs(r1['rms'] - r2['rms']) / max(r1['rms'], 1e-9) < 0.05, (
            f"DC bias changed audio RMS by more than 5% "
            f"(clean={r1['rms']:.4f}, biased={r2['rms']:.4f}) — "
            f"DC blocker probably not applied"
        )

    def test_pure_dc_input_produces_silence(self):
        # A pure-DC input (no signal, only LO leakage) should demodulate
        # to near-silence — not a loud spurious tone.
        dc_only = np.full(self.N, 0.5 + 0.5j, dtype=np.complex64)
        r = _fresh_decoder().process(dc_only, _mk_state(self.SR))
        assert r['rms'] < 0.05, (
            f"Pure DC produced non-silent audio (rms={r['rms']:.4f}) — "
            f"expected the DC blocker to zero it out"
        )

    def test_dc_blocker_does_not_hurt_clean_signal(self):
        # An already-clean signal (mean ≈ 0) should be barely touched.
        # Compare RMS with and without the blocker equivalent: it should
        # stay within noise-floor tolerance.
        clean = _make_fm_signal(self.SR, self.N, audio_hz=1_000, deviation_hz=50_000)
        r = _fresh_decoder().process(clean, _mk_state(self.SR))
        # A 1 kHz tone at 50 kHz deviation on wideband FM produces a
        # clearly non-silent audio output.  Just confirm we didn't
        # accidentally wipe the signal along with any (near-zero) DC.
        assert r['rms'] > 0.05, (
            f"Clean FM signal came out too quiet (rms={r['rms']:.4f}) — "
            f"DC blocker may be over-correcting"
        )

    def test_dc_drift_between_chunks_does_not_click(self):
        # Regression test for the crackle bug: a naïve per-chunk
        # samples - samples.mean() approach leaves a step at every
        # chunk boundary when the actual DC drifts between chunks
        # (e.g., HackRF gain-settling / thermal effects).  The stateful
        # IIR HP filter should be continuous across chunks with no
        # boundary spike, even when the DC offset shifts abruptly.
        dec = _fresh_decoder()
        st  = _mk_state(self.SR)
        # Two chunks of the same FM signal but with very different DC
        # biases — modelling a step in the receiver's DC leakage.
        base = _make_fm_signal(self.SR, self.N, audio_hz=1_000, deviation_hz=50_000)
        chunk_a = base + (0.10 + 0.05j)
        chunk_b = base + (0.30 + 0.25j)   # 3× the DC offset
        # Warm up the DC filter and audio pipeline on chunk A, then
        # measure chunk B — the transient at the very start of B's
        # audio is what would 'click' if the DC handler were per-chunk.
        _ = dec.process(chunk_a, st)
        r_b = dec.process(chunk_b, st)
        # Look at the first ~10 ms of B's audio (long enough to include
        # any boundary transient, short enough not to average it away).
        head    = r_b['audio'][: int(0.010 * 48_000)]
        tail    = r_b['audio'][int(0.020 * 48_000):]
        head_pk = float(np.max(np.abs(head))) if len(head) else 0.0
        tail_pk = float(np.max(np.abs(tail))) if len(tail) else 0.0
        # Head shouldn't massively exceed the steady-state peak — a
        # click from a chunk-boundary DC step would show up as a much
        # louder spike here than in the settled audio.
        assert head_pk < 1.5 * tail_pk + 0.05, (
            f"Chunk-boundary DC step produced a click "
            f"(head peak={head_pk:.3f}, tail peak={tail_pk:.3f}) — "
            f"DC blocker probably back to per-chunk mean-subtract"
        )
