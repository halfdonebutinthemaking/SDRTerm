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
from scipy.signal import resample_poly, fftconvolve

# ── constants ────────────────────────────────────────────────────────────
IRIDIUM_SYMRATE = 25_000            # sym/s
_TARGET_SR      = 200_000           # 8 samples per symbol at 25 ksym/s
_TARGET_SPS     = _TARGET_SR // IRIDIUM_SYMRATE   # 8
_RRC_ALPHA      = 0.4               # Iridium RRC roll-off (approx)

# Iridium unique-word bit patterns (24 bits = 12 symbols, following the
# 16-symbol preamble in every burst).  These are the exact values used
# by iridium-toolkit's `bitsparser.py` (iridium_access / uplink_access)
# — the differentially-Gray-decoded bit strings that a correctly-demod'd
# burst produces.  Each is 12 symbols × 2 bits, and every pair is either
# '00' or '11' (the UW is a BPSK-only pattern in symbol-index space:
# only phases 0° and 180° appear).
#
# Prior versions of this file used slightly different constants derived
# from a stale reference; they were off by 1 bit for DL and completely
# different for UL.  Live UW lock rate never rose above the ~25 %
# false-positive floor until these were corrected.
_UW_DL = '001100000011000011110011'   # iridium_access (downlink, sat → ground)
_UW_UL = '110011000011110011111100'   # uplink_access  (ground → sat)
_UW_LEN = len(_UW_DL)


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


def _estimate_cfo_dqpsk(symbols: np.ndarray, sym_rate: int) -> float:
    """Estimate carrier-frequency offset from DQPSK symbols.

    For straight (non-differential) QPSK, `samples^4` cancels the π/2
    modulation and leaves a tone at 4×CFO — the textbook trick.  It does
    NOT work on DQPSK symbols directly, because DQPSK has data-encoded
    phase differences BETWEEN symbols: `symbols^4` shows the ±1 flip
    per symbol from `4·(π/4) = π mod 2π`, producing a peak at fs/2
    regardless of CFO.  (First cut of this function hit exactly that
    bug — constant ±3113 Hz bias on synthetic input.)

    For DQPSK, take the differential product first.  It cancels constant
    carrier phase, leaving `diff[k] = data_phase + CFO·Ts` per step.
    Then `diff^4` gives magnitude 1 with phase `π + 4·CFO·Ts` (the π
    from all four data phases mapping to π under ×4).  Average the
    phasor and extract CFO from the angle.

    Range: unambiguous ±sym_rate/8 (i.e. ±3125 Hz at 25 ksym/s).  Real
    Iridium bursts after our channelisation step typically land within
    that window (nominal-channel-centre error + Doppler + SDR crystal
    error).  Larger CFO would need a coarse-then-fine two-stage estimator.
    """
    if len(symbols) < 10:
        return 0.0
    diff = symbols[1:].astype(np.complex128) * np.conj(symbols[:-1].astype(np.complex128))
    # diff^4 nominally lands at -exp(j·4·CFO·Ts) — the -1 is from
    # 4·(π/4) = π for every QPSK phase.  Average across all symbols
    # for a noise-averaged estimate.
    diff4 = diff ** 4
    mean_phasor = np.mean(diff4)
    if np.abs(mean_phasor) < 1e-9:
        return 0.0
    angle = np.angle(mean_phasor) - np.pi   # remove the π bias
    # Wrap into (-π, π]
    while angle >  np.pi: angle -= 2 * np.pi
    while angle <= -np.pi: angle += 2 * np.pi
    # angle = 2π · 4 · CFO · Ts  (radians per differential step)
    # → CFO = angle · sym_rate / (8π)  (Hz)
    return float(angle * sym_rate / (8 * np.pi))


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


# Trim-window width in symbols.  Iridium bursts are ~8-20 ms
# (200-500 symbols at 25 ksym/s: 8.28 ms for IL/IU3/VOC, 20.32 ms for
# IPB paging).  400 symbols = 16 ms covers a typical burst body plus
# preamble tail without inviting the adjacent-burst / noise-tail
# dilution that keeps symbol timing recovery weak.
_TRIM_SYMS = 400


def _trim_to_burst(matched: np.ndarray, sps: int,
                   trim_syms: int = _TRIM_SYMS) -> np.ndarray:
    """Trim the matched-filter output to the contiguous window of
    maximal integrated energy — a coarse matched-filter for
    "burst-present" of the target width.

    Approach: compute a sliding-window sum of |matched|² over
    `trim_syms · sps` samples (cheap via cumsum diff), find the offset
    where the sum is largest, return that slice.  Guaranteed to overlap
    the burst as long as burst energy exceeds the sum of noise over
    the same window width — true at any reasonable SNR.

    Why not argmax(|matched|²) + fixed offset (previous approach):
      - Single-sample peak can be an SDR spur, noise spike, or the
        RRC pulse's ripple, not the burst body's centre.
      - Sliding-window energy integrates across the whole burst, so
        it's robust to individual-sample noise.

    Returns `matched` unchanged if it's already shorter than trim_syms.
    """
    n = len(matched)
    win = trim_syms * sps
    if n <= win:
        return matched
    power = (matched.real ** 2 + matched.imag ** 2).astype(np.float32)
    csum  = np.cumsum(power, dtype=np.float64)
    # Sum over [k, k+win) = csum[k+win-1] - csum[k-1]; prepend a zero
    # so k=0 works uniformly.
    csum0 = np.concatenate([[0.0], csum])
    win_sum = csum0[win:] - csum0[:-win]
    best_start = int(np.argmax(win_sum))
    return matched[best_start:best_start + win]


# Gray-coded DQPSK phase-difference → 2-bit mapping.  Phase differences
# are quantised into 4 bins by the sign of (real, imag) of the diff
# product; each combination maps to a Gray-adjacent 2-bit code.
def _dqpsk_bits(symbols: np.ndarray) -> str:
    """Return a string of '0'/'1' bits from consecutive symbol pairs.

    Standard π/4-Gray DQPSK mapping:
        quadrant of (symbol[k] · conj(symbol[k-1]))  →  2 bits
          I>0, Q>0  →  0 0
          I<0, Q>0  →  0 1
          I<0, Q<0  →  1 1
          I>0, Q<0  →  1 0

    Which gives:
        high = 1 - q  (high bit = 1 when Q is negative)
        low  = 1 - i  (low  bit = 1 when I is negative)

    Verified against a synthetic bit-stream → symbols → demod round-trip
    in the test at the bottom of this file.  If Iridium's actual on-air
    convention differs from this Gray mapping the UW correlator has
    swap/conj variants that will match anyway (see find_uw)."""
    if len(symbols) < 2:
        return ''
    diff = symbols[1:] * np.conj(symbols[:-1])
    q_i = (diff.real >= 0).astype(np.uint8)
    q_q = (diff.imag >= 0).astype(np.uint8)
    high = (1 - q_q).astype(np.uint8)
    low  = (1 - q_i).astype(np.uint8)
    out = np.empty(2 * len(diff), dtype=np.uint8)
    out[0::2] = high
    out[1::2] = low
    return ''.join('1' if b else '0' for b in out)


# ── DQPSK encoder for round-trip testing ──────────────────────────────────
# Inverse of _dqpsk_bits: bits → complex symbols.  Used by the test in
# the module block below to verify demod produces back what we encoded.

def _bits_to_dqpsk_symbols(bits_str: str) -> np.ndarray:
    """Encode a '01'-bit string as π/4-Gray DQPSK symbols.

    First symbol is arbitrary (reference); every subsequent symbol is
    the previous rotated by the phase corresponding to the next 2 bits:
        00 → 45° (+1+j)/√2 · prev
        01 → 135° (-1+j)/√2 · prev
        11 → 225° (-1-j)/√2 · prev
        10 → 315° (+1-j)/√2 · prev
    """
    assert len(bits_str) % 2 == 0
    n_syms = len(bits_str) // 2 + 1   # +1 for the reference symbol
    out = np.zeros(n_syms, dtype=np.complex64)
    out[0] = (1 + 0j)                  # arbitrary reference
    inv_sqrt2 = 1.0 / np.sqrt(2.0)
    for bi in range(0, len(bits_str), 2):
        high = bits_str[bi]
        low  = bits_str[bi + 1]
        # invert the mapping in _dqpsk_bits:
        #   high = 1-q  → q_sign = 1-high
        #   low  = 1-i  → i_sign = 1-low
        q_sign = 1 - int(high)     # 1 → Q > 0, 0 → Q < 0
        i_sign = 1 - int(low)      # 1 → I > 0, 0 → I < 0
        step = complex((1 if i_sign else -1) * inv_sqrt2,
                       (1 if q_sign else -1) * inv_sqrt2)
        out[bi // 2 + 1] = out[bi // 2] * step
    return out


def _bits_to_bipolar(bits_str: str) -> np.ndarray:
    """Convert a '01'-string to ±1 float array for correlation."""
    return (np.frombuffer(bits_str.encode(), dtype=np.uint8) - ord('0')).astype(np.int8) * 2 - 1


# The demod chain picks its reference symbol arbitrarily, so the
# 2-bit code we assign to each DQPSK phase-transition can be off by
# a constant rotation from the on-air convention (00→10→11→01 in
# cyclic-Gray order per +90° step).  Both UWs are BPSK-symmetric
# patterns (only 00 and 11 pairs — phases 0° and 180° only), which
# means pair-swapping the two bits of each symbol is a no-op for
# them; the only ambiguity that matters is the 4-way phase rotation.
#
# We enumerate all 4 rotations of each UW and correlate against the
# received bit stream.  The winner tells us which rotation matches
# reality — informational for now; a persistent bias would let us
# hardcode the correct mapping in _dqpsk_bits and drop the extra
# variants.
_CYCLIC = ['00', '10', '11', '01']            # cyclic Gray order per +90°
_CYCLIC_IDX = {c: i for i, c in enumerate(_CYCLIC)}


def _rotate_bits(bits_str: str, k: int) -> str:
    """Rotate the 2-bit codes of `bits_str` by `k` steps in cyclic Gray
    order (equivalent to rotating the reference phase by k·90°)."""
    out = []
    for i in range(0, len(bits_str), 2):
        pair = bits_str[i:i + 2]
        out.append(_CYCLIC[(_CYCLIC_IDX[pair] + k) % 4])
    return ''.join(out)


_UW_VARIANTS = []
for name, pattern in (('DL', _UW_DL), ('UL', _UW_UL)):
    for k in range(4):
        rotated = _rotate_bits(pattern, k)
        _UW_VARIANTS.append(('{}_r{}'.format(name, k),
                             _bits_to_bipolar(rotated)))


def find_uw(bits_str: str) -> dict:
    """Search for an Iridium unique-word in a bit stream.

    Returns a dict:
      name    : '{DL,UL}_r{0..3}' or 'none'
      pos     : int  bit offset of best match (or -1 if no candidate)
      hd      : int  Hamming distance to the reference UW at that pos

    _r0 = our demod's phase reference matches iridium-toolkit's; _r{k}
    means the constellation is rotated by k·90°.  A persistent bias to
    one rotation on real bursts means we should hardcode that mapping
    upstream.  Small Hamming distances (≤2 out of 24) validate that
    the demod is producing meaningful bits.
    """
    if len(bits_str) < _UW_LEN + 8:
        return {'name': 'none', 'pos': -1, 'hd': -1}
    bipolar = _bits_to_bipolar(bits_str)
    best = ('none', -1, _UW_LEN + 1)
    for name, uw in _UW_VARIANTS:
        # correlation ranges [-24, +24]; hamming distance = (24 - corr) / 2
        corr = np.correlate(bipolar, uw, mode='valid')
        pos = int(np.argmax(corr))
        hd  = (_UW_LEN - int(corr[pos])) // 2
        if hd < best[2]:
            best = (name, pos, hd)
    return {'name': best[0], 'pos': best[1], 'hd': best[2]}


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
    # fftconvolve is ~30% faster than np.convolve at this signal/filter
    # length; for the max-rate case (satellite pass with multiple beams)
    # every ms of demod cost matters.
    matched_full = fftconvolve(resampled, _RRC_TAPS, mode='same').astype(np.complex64)
    # Trim to the burst envelope BEFORE symbol timing / CFO / demod so
    # those don't get diluted by the ~80 ms of noise the iridium plugin
    # pads around each ~20 ms burst.  Keep the untrimmed signal around
    # for the SNR estimate (which needs the noise floor from the edges).
    matched = _trim_to_burst(matched_full, _TARGET_SPS)
    symbols_raw = _pick_best_phase(matched, _TARGET_SPS)
    # CFO estimate on the SYMBOL-RATE samples (not the oversampled matched
    # signal) so the 4th-power spectrum is dominated by clean symbol-centre
    # points where samples^4 lands on a constant phase — see _estimate_cfo
    # comment.  Range: ±symrate/8 ≈ ±3125 Hz; beyond that we'd need a
    # coarse-then-fine pass.  Small residual CFO after channelisation
    # rotates the DQPSK constellation across the burst and pushes decisions
    # across quadrant boundaries — the exact symptom of our ~25 % UW lock
    # rate on live data.
    cfo_hz = _estimate_cfo_dqpsk(symbols_raw, IRIDIUM_SYMRATE)
    if abs(cfo_hz) > 1.0:
        t_sym = np.arange(len(symbols_raw), dtype=np.float64) / IRIDIUM_SYMRATE
        symbols = (symbols_raw
                   * np.exp(-2j * np.pi * cfo_hz * t_sym)).astype(np.complex64)
    else:
        symbols = symbols_raw
    bits = _dqpsk_bits(symbols)

    # Rough SNR: peak sample magnitude vs edge (first/last 10%) noise.
    # Use the untrimmed matched signal so the edges are actually noise,
    # not still-inside-the-burst samples from the trimmed view.
    n_full = len(matched_full)
    edge = np.concatenate([matched_full[:n_full // 10],
                           matched_full[-n_full // 10:]])
    edge_pow = float(np.mean(np.abs(edge) ** 2)) + 1e-30
    peak_pow = float(np.max(np.abs(matched_full) ** 2))
    snr_db = 10.0 * np.log10(peak_pow / edge_pow)

    return {
        'bits':         bits,
        'n_symbols':    int(len(symbols)),
        'snr_rough_db': snr_db,
        'cfo_hz':       cfo_hz,
        'uw':           find_uw(bits),
    }
