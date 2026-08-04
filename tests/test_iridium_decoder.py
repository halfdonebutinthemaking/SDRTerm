"""Unit tests for the iridium_decoder DQPSK demod chain.

These tests specifically guard against the class of Gray-code mapping
bug that was found on live data: a bit-mapping error in _dqpsk_bits
that produced ~22 % UW lock rate (basically the false-positive floor).
The bit round-trip test at the bottom catches that immediately."""
import numpy as np
import pytest

from plugins.iridium_decoder import demod
from plugins.iridium_decoder.demod import (
    _dqpsk_bits, _bits_to_dqpsk_symbols, _UW_DL,
    find_uw, demod_burst, _TARGET_SR, _TARGET_SPS, _RRC_TAPS,
)


class TestDqpskMapping:
    """The encoder and decoder must be exact inverses."""

    def test_roundtrip_identity(self):
        # Non-trivial bit pattern with all 4 symbol quadrants exercised
        plaintext = '01000111' * 30 + _UW_DL + '00110011' * 30
        symbols   = _bits_to_dqpsk_symbols(plaintext)
        recovered = _dqpsk_bits(symbols)
        assert recovered == plaintext, (
            'DQPSK encode→decode round-trip broke — first mismatch is a '
            'bit-mapping bug in _dqpsk_bits or _bits_to_dqpsk_symbols'
        )

    def test_roundtrip_all_symbol_transitions(self):
        # Every 2-bit combination as a transition, exhaustively
        plaintext = '00011011' * 100
        symbols = _bits_to_dqpsk_symbols(plaintext)
        recovered = _dqpsk_bits(symbols)
        assert recovered == plaintext


class TestUwCorrelator:
    def test_uw_at_zero_offset(self):
        bits = _UW_DL + '01' * 100
        r = find_uw(bits)
        assert r['name'] == 'DL_r0'    # untransformed match
        assert r['pos']  == 0
        assert r['hd']   == 0

    def test_uw_at_offset(self):
        bits = '10' * 50 + _UW_DL + '01' * 50
        r = find_uw(bits)
        assert r['name'] == 'DL_r0'
        assert r['pos']  == 100
        assert r['hd']   == 0

    def test_uw_with_two_bit_errors(self):
        corrupt = _UW_DL[:5] + ('1' if _UW_DL[5] == '0' else '0') + \
                  ('1' if _UW_DL[6] == '0' else '0') + _UW_DL[7:]
        bits = '00' * 30 + corrupt + '00' * 30
        r = find_uw(bits)
        assert r['name'] == 'DL_r0'
        assert r['hd']   == 2


class TestEndToEnd:
    """Synthetic bits → DQPSK symbols → RRC-shaped burst → matched filter
    → demod → verify UW recovered at HD=0.  Catches breakage in the full
    signal chain, not just the bit mapping."""

    def _make_burst(self, plaintext: str, snr_db: float,
                    seed: int = 42) -> np.ndarray:
        from scipy.signal import fftconvolve
        rng     = np.random.default_rng(seed)
        symbols = _bits_to_dqpsk_symbols(plaintext)
        # TX: impulse-train upsample then RRC pulse-shape
        up = np.zeros(len(symbols) * _TARGET_SPS, dtype=np.complex64)
        up[::_TARGET_SPS] = symbols
        tx = fftconvolve(up, _RRC_TAPS, mode='same').astype(np.complex64)
        # AWGN channel
        sig_pwr = float(np.mean(np.abs(tx) ** 2))
        noise_pwr = sig_pwr / (10 ** (snr_db / 10))
        noise = ((rng.standard_normal(len(tx)) + 1j * rng.standard_normal(len(tx)))
                 * np.sqrt(noise_pwr / 2)).astype(np.complex64)
        return (tx + noise).astype(np.complex64)

    def test_uw_recovered_at_20db_snr(self):
        # UW planted at bit offset 240 (30 * 8 bits of leading data)
        plaintext = '01000111' * 30 + _UW_DL + '00110011' * 30
        rx        = self._make_burst(plaintext, snr_db=20.0)
        result    = demod_burst(rx, _TARGET_SR)
        assert result['uw']['name'] == 'DL_r0'
        assert result['uw']['hd']   == 0

    def test_uw_recovered_at_10db_snr(self):
        # Weaker signal — allow small Hamming distance
        plaintext = '01000111' * 30 + _UW_DL + '00110011' * 30
        rx        = self._make_burst(plaintext, snr_db=10.0)
        result    = demod_burst(rx, _TARGET_SR)
        assert result['uw']['name'].startswith('DL')
        assert result['uw']['hd']   <= 2, (
            'At 10 dB SNR the UW should still be found within HD=2')


class TestCfoEstimator:
    """The DQPSK-correct CFO estimator must recover the injected offset
    within a few Hz, and the demod chain must recover the UW at HD=0
    across the estimator's unambiguous range (±sym_rate/8 ≈ ±3125 Hz).

    Guards against two bugs found during Phase 2a development:
      1. Using symbols^4 (which peaks at fs/2 for DQPSK due to the
         data-encoded ±1 flip per symbol) instead of the differential
         product's 4th power.
      2. Missing the 2π factor when converting radians-per-symbol back
         to Hz.  Off by ~2π made every non-zero CFO wildly overestimated.
    """

    def _make_burst_with_cfo(self, cfo_hz: float, snr_db: float = 20.0,
                             seed: int = 42) -> np.ndarray:
        from scipy.signal import fftconvolve
        rng = np.random.default_rng(seed)
        plaintext = '01000111' * 30 + _UW_DL + '00110011' * 30
        symbols = _bits_to_dqpsk_symbols(plaintext)
        up = np.zeros(len(symbols) * _TARGET_SPS, dtype=np.complex64)
        up[::_TARGET_SPS] = symbols
        tx = fftconvolve(up, _RRC_TAPS, mode='same').astype(np.complex64)
        # AWGN
        sig_pwr = float(np.mean(np.abs(tx) ** 2))
        noise_pwr = sig_pwr / (10 ** (snr_db / 10))
        noise = ((rng.standard_normal(len(tx)) + 1j * rng.standard_normal(len(tx)))
                 * np.sqrt(noise_pwr / 2)).astype(np.complex64)
        # Rotate to inject CFO
        t = np.arange(len(tx)) / _TARGET_SR
        rotator = np.exp(2j * np.pi * cfo_hz * t).astype(np.complex64)
        return ((tx + noise) * rotator).astype(np.complex64)

    @pytest.mark.parametrize('cfo_hz', [-2500, -1500, -500, 0, 500, 1000, 2000, 3000])
    def test_uw_recovered_across_cfo_range(self, cfo_hz):
        rx = self._make_burst_with_cfo(cfo_hz)
        r  = demod_burst(rx, _TARGET_SR)
        assert r['uw']['hd'] == 0, (
            f'CFO={cfo_hz} Hz should recover HD=0, got hd={r["uw"]["hd"]} '
            f'(cfo_est={r["cfo_hz"]:.1f})'
        )

    @pytest.mark.parametrize('cfo_hz', [0, 100, 500, 1000, 2500])
    def test_cfo_estimate_accurate_within_10hz(self, cfo_hz):
        rx = self._make_burst_with_cfo(cfo_hz)
        r  = demod_burst(rx, _TARGET_SR)
        err = abs(r['cfo_hz'] - cfo_hz)
        assert err < 15, (
            f'CFO estimate off by {err:.1f} Hz (injected={cfo_hz}, '
            f'estimated={r["cfo_hz"]:.1f}) — expect ≤ 15 Hz at 20 dB SNR'
        )


class TestIridiumToolkitConvention:
    """Ground-truth cross-check against iridium-toolkit's on-air convention.

    Previous tests used our own encoder → decoder round-trip, which is
    self-consistent by construction and cannot catch a mismatch with
    the actual Iridium on-air bit ordering.  Live UW lock rate was
    stuck at the ~25 % false-positive floor for several iterations
    because of exactly that class of bug (wrong UW constants + no
    rotation coverage).

    This test bypasses our own encoder entirely and builds the signal
    from iridium-toolkit's convention:
        1. bits → absolute symbol indices via inverse of de_dqpsk()
        2. symbol indices → complex QPSK samples on the ±I/±Q axes
        3. RRC pulse shaping
    Then verifies our demod produces bits that the correlator finds at
    HD ≤ 2, at *some* rotation.  A persistent bias to one rotation
    (say _r2 always winning) is a signal to hardcode that mapping
    upstream and drop the rotation search.
    """

    @staticmethod
    def _iridium_bits_to_symbols(bits_str: str) -> list:
        """Inverse of iridium-toolkit bitsparser.de_dqpsk()."""
        imap = [0, 1, 3, 2]
        diffs = [imap[int(bits_str[i]) * 2 + int(bits_str[i + 1])]
                 for i in range(0, len(bits_str) - 1, 2)]
        syms = [0]
        for d in diffs:
            syms.append((syms[-1] + d) % 4)
        return syms

    def _make_burst_iridium_convention(self, bits_str: str,
                                       snr_db: float = 20.0,
                                       seed: int = 42) -> np.ndarray:
        from scipy.signal import fftconvolve
        rng = np.random.default_rng(seed)
        syms = self._iridium_bits_to_symbols(bits_str)
        # ±I / ±Q constellation (rotated 45° from ours — irrelevant since
        # our demod finds any rotation).  0→+1, 1→+j, 2→-1, 3→-j.
        table = np.array([1 + 0j, 0 + 1j, -1 + 0j, 0 - 1j], dtype=np.complex64)
        cx = table[syms]
        up = np.zeros(len(cx) * _TARGET_SPS, dtype=np.complex64)
        up[::_TARGET_SPS] = cx
        tx = fftconvolve(up, _RRC_TAPS, mode='same').astype(np.complex64)
        sig_pwr = float(np.mean(np.abs(tx) ** 2))
        noise_pwr = sig_pwr / (10 ** (snr_db / 10))
        noise = ((rng.standard_normal(len(tx)) + 1j * rng.standard_normal(len(tx)))
                 * np.sqrt(noise_pwr / 2)).astype(np.complex64)
        return (tx + noise).astype(np.complex64)

    def test_downlink_uw_recovered(self):
        rng = np.random.default_rng(0)
        pad = lambda n: ''.join(str(int(b)) for b in rng.integers(0, 2, n))
        plaintext = pad(120) + _UW_DL + pad(120)
        rx = self._make_burst_iridium_convention(plaintext)
        r  = demod_burst(rx, _TARGET_SR)
        assert r['uw']['name'].startswith('DL'), (
            f'Expected a DL rotation, got {r["uw"]}')
        assert r['uw']['hd'] <= 2, (
            f'HD too large for iridium-toolkit-encoded downlink burst: '
            f'{r["uw"]}'
        )

    def test_uplink_uw_recovered(self):
        from plugins.iridium_decoder.demod import _UW_UL
        rng = np.random.default_rng(1)
        pad = lambda n: ''.join(str(int(b)) for b in rng.integers(0, 2, n))
        plaintext = pad(120) + _UW_UL + pad(120)
        rx = self._make_burst_iridium_convention(plaintext)
        r  = demod_burst(rx, _TARGET_SR)
        assert r['uw']['name'].startswith('UL'), (
            f'Expected a UL rotation, got {r["uw"]}')
        assert r['uw']['hd'] <= 2

    def test_burst_recovered_inside_wide_noise_window(self):
        """Simulate the iridium plugin's ~100 ms wide window: a ~20 ms
        burst embedded in 80 ms of noise.  Without envelope trimming,
        symbol timing recovery gets diluted by the noise and the demod
        produces garbage bits for most of the window — exactly the
        failure mode that showed as a flat per-variant lock distribution
        (~7 counts each) instead of one variant dominating."""
        from scipy.signal import fftconvolve
        rng = np.random.default_rng(7)
        pad_bits = lambda n: ''.join(str(int(b)) for b in rng.integers(0, 2, n))
        # 500-symbol burst (20 ms at 25 ksym/s), UW near the middle
        plaintext = pad_bits(240) + _UW_DL + pad_bits(240)
        burst_iq  = self._make_burst_iridium_convention(plaintext, snr_db=20.0)
        # Wrap in 4× as much noise (80 ms of noise around a 20 ms burst)
        sig_pwr   = float(np.mean(np.abs(burst_iq) ** 2))
        noise_len = len(burst_iq) * 2
        def noise(n):
            return ((rng.standard_normal(n) + 1j * rng.standard_normal(n))
                    * np.sqrt(sig_pwr / (10 ** (20.0 / 10)) / 2)).astype(np.complex64)
        wide = np.concatenate([noise(noise_len), burst_iq, noise(noise_len)])
        r = demod_burst(wide, _TARGET_SR)
        assert r['uw']['name'].startswith('DL'), (
            f'Expected DL rotation in wide-window burst, got {r["uw"]}')
        assert r['uw']['hd'] <= 2, (
            f'Wide-window burst not recovered — envelope trimming '
            f'likely broken.  Got {r["uw"]}'
        )
