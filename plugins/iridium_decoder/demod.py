"""Iridium DQPSK demodulation — Phase 1.

Consumes narrow-band burst IQ (~50 kHz sample rate, centred on the channel
after the iridium plugin's shift + decimate) and produces a raw bit stream.

Pipeline:
  1. Matched RRC filter (α = 0.4, 8 samples per symbol at 25 ksym/s
     assumes an input sample rate of exactly 200 kHz — we resample to
     that if the input rate differs).
  2. Auto-symbol-timing: try all 8 sampling phases within one symbol
     period and pick the one whose sample slice has the highest mean
     magnitude.  Same trick the constellation plugin uses, cheap and
     stateless.
  3. DQPSK bit extraction: symbols[k] × conj(symbols[k-1]) → phase
     difference → 2 bits per symbol via Gray-coded mapping
     (0°→00, 90°→01, 180°→11, 270°→10).

What's intentionally NOT here (Phase 2 or later):
  - Unique-word / preamble correlation to align frame start
  - Frame-type classification (IRA / IIQ / IBC / IIP / IU3 / MSG / etc.)
  - Field parsing (RIC, timestamp, message body)
  - Carrier-frequency-offset correction beyond what the fixed-phase
    sampler tolerates

Even without those, Phase 1 output is useful as ground truth to
compare against iridium-toolkit — the raw bits per burst give you a
foothold to start building the parser.
"""
import numpy as np
from math import gcd
from scipy.signal import resample_poly

# ── constants ────────────────────────────────────────────────────────────
IRIDIUM_SYMRATE = 25_000            # sym/s
_TARGET_SR      = 200_000           # 8 samples per symbol at 25 ksym/s
_TARGET_SPS     = _TARGET_SR // IRIDIUM_SYMRATE   # 8
_RRC_ALPHA      = 0.4               # Iridium RRC roll-off (approx)


def _rrc(n_taps: int, alpha: float, sps: int) -> np.ndarray:
    """Root-raised-cosine filter coefficients."""
    t = (np.arange(n_taps) - n_taps // 2) / sps
    h = np.zeros(n_taps)
    for i, ti in enumerate(t):
        if ti == 0:
            h[i] = 1.0 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * ti) - 1.0) < 1e-6:
            h[i] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha))
            )
        else:
            h[i] = (
                np.sin(np.pi * ti * (1 - alpha))
                + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha))
            ) / (np.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float32)


# Cache the RRC taps once (module-load cost) — same taps for every burst.
_RRC_TAPS = _rrc(8 * _TARGET_SPS + 1, _RRC_ALPHA, _TARGET_SPS)


def _resample_to_target(iq: np.ndarray, src_sr: int) -> np.ndarray:
    """Resample input to _TARGET_SR Hz for the matched filter."""
    if src_sr == _TARGET_SR:
        return iq
    g = gcd(src_sr, _TARGET_SR)
    up = _TARGET_SR // g
    down = src_sr // g
    return resample_poly(iq, up, down).astype(np.complex64)


def _pick_best_phase(matched: np.ndarray, sps: int) -> np.ndarray:
    """Try all `sps` symbol-sampling phases, return the one with highest
    mean sample magnitude (symbol centres have the RRC peak, zero-crossings
    are near 0)."""
    best_mag = -1.0
    best = matched[::sps]
    for phase in range(sps):
        cand = matched[phase::sps]
        if len(cand) < 4:
            continue
        m = float(np.mean(np.abs(cand)))
        if m > best_mag:
            best_mag, best = m, cand
    return best


# Gray-coded DQPSK phase-difference → 2-bit mapping.  Phase differences
# are quantised into 4 bins by the sign of (real, imag) of the diff
# product; each combination maps to a Gray-adjacent 2-bit code.
def _dqpsk_bits(symbols: np.ndarray) -> str:
    """Return a string of '0'/'1' bits from consecutive symbol pairs."""
    if len(symbols) < 2:
        return ''
    diff = symbols[1:] * np.conj(symbols[:-1])
    # Quantise by quadrant.  Standard Iridium DQPSK Gray mapping:
    #   quadrant → 2 bits
    #     I,Q signs (+,+) → 00
    #     I,Q signs (-,+) → 01
    #     I,Q signs (-,-) → 11
    #     I,Q signs (+,-) → 10
    q_i = (diff.real >= 0).astype(np.uint8)
    q_q = (diff.imag >= 0).astype(np.uint8)
    # Compose per Gray code above
    #   (i, q) → high bit, low bit
    #   (1, 1) → 0, 0
    #   (0, 1) → 0, 1
    #   (0, 0) → 1, 1
    #   (1, 0) → 1, 0
    high = (1 - q_i) & (q_q ^ 1) | (1 - q_i) & q_q     # 1 when i==0
    high = (1 - q_i).astype(np.uint8)
    low  = (q_i ^ q_q).astype(np.uint8)
    out = np.empty(2 * len(diff), dtype=np.uint8)
    out[0::2] = high
    out[1::2] = low
    return ''.join('1' if b else '0' for b in out)


def demod_burst(iq: np.ndarray, sample_rate: int) -> dict:
    """Full pipeline on one burst.  Returns a dict of results.

    Returns keys:
      bits          : str of '0'/'1', 2 per DQPSK symbol
      n_symbols     : int
      snr_rough_db  : float (from magnitude ratio — burst peak vs
                              trailing/leading noise)
    """
    if len(iq) < 100:
        return {'bits': '', 'n_symbols': 0, 'snr_rough_db': 0.0}

    resampled = _resample_to_target(iq.astype(np.complex64), sample_rate)
    matched = np.convolve(resampled, _RRC_TAPS, mode='same').astype(np.complex64)
    symbols = _pick_best_phase(matched, _TARGET_SPS)
    bits = _dqpsk_bits(symbols)

    # Rough SNR: peak sample magnitude vs edge (first/last 10%) noise.
    n = len(matched)
    edge = np.concatenate([matched[:n // 10], matched[-n // 10:]])
    edge_pow = float(np.mean(np.abs(edge) ** 2)) + 1e-30
    peak_pow = float(np.max(np.abs(matched) ** 2))
    snr_db = 10.0 * np.log10(peak_pow / edge_pow)

    return {
        'bits':         bits,
        'n_symbols':    int(len(symbols)),
        'snr_rough_db': snr_db,
    }
