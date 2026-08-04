"""Diagnostic tool: dissect one captured Iridium burst.

Reads a .cf32 wide-IQ file (plus its .json sidecar) written by the
iridium plugin, runs it through the exact same signal chain that the
in-process iridium_decoder plugin uses, and prints instrumentation
that reveals *where* the demod is failing on live captures.

Usage:
    python -m plugins.iridium_decoder.inspect_burst <path/to/burst.cf32>

The .json sidecar next to the .cf32 file is read automatically for
sample rate, channel ID, and channel offset.

Key thing to look at: the differential-angle histogram.  For real
Iridium bursts most differentials land near {0°, 90°, 180°, -90°} — a
uniform sprinkle across all angles means symbol timing / CFO / matched
filter shape are broken and no rotation-search can recover the UW.

The top-5 UW candidates section shows the best hits across all
variants and positions — if one rotation consistently wins with much
lower Hamming distance than the others, we've found the true bit
mapping and can hardcode it.
"""
import json
import sys
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve, resample_poly

from plugins.iridium_decoder.demod import (
    demod_burst, _RRC_TAPS, _TARGET_SR, _TARGET_SPS, IRIDIUM_SYMRATE,
    _trim_to_burst, _pick_best_phase, _estimate_cfo_dqpsk,
    _UW_VARIANTS, _UW_LEN, _bits_to_bipolar,
)


def _channelise(iq_wide: np.ndarray, wide_sr: int,
                chan_offset_hz: float, target_sr: int = 50_000):
    """Mirror what plugins/iridium/iridium.py::_push_to_decoder does."""
    n = len(iq_wide)
    t  = np.arange(n, dtype=np.float32) / wide_sr
    lo = np.exp(-2j * np.pi * chan_offset_hz * t).astype(np.complex64)
    shifted = (iq_wide * lo).astype(np.complex64)
    factor  = max(1, int(round(wide_sr / target_sr)))
    narrow  = resample_poly(shifted, 1, factor).astype(np.complex64)
    return narrow, wide_sr // factor


def _resample_to_target(narrow: np.ndarray, src_sr: int) -> np.ndarray:
    if src_sr == _TARGET_SR:
        return narrow
    g = gcd(int(src_sr), int(_TARGET_SR))
    return resample_poly(narrow, _TARGET_SR // g, src_sr // g).astype(np.complex64)


def _diff_angle_histogram(symbols: np.ndarray) -> dict:
    """Bin diff-angles into 4 quadrants centred on {0°, 90°, 180°, -90°}."""
    if len(symbols) < 2:
        return {}
    diff = symbols[1:] * np.conj(symbols[:-1])
    ang = np.angle(diff) * 180 / np.pi
    q00  = int(np.sum((ang >  -45) & (ang <=   45)))
    q90  = int(np.sum((ang >   45) & (ang <=  135)))
    q180 = int(np.sum((ang > 135) | (ang <= -135)))
    q270 = int(np.sum((ang > -135) & (ang <= -45)))
    tot = len(diff)
    return {
        '  0°': (q00,  100 * q00  / tot),
        ' 90°': (q90,  100 * q90  / tot),
        '180°': (q180, 100 * q180 / tot),
        '-90°': (q270, 100 * q270 / tot),
    }


def _top_uw_hits(bits_str: str, top_n: int = 6):
    """Top-N best UW hits across ALL variants and positions.

    Reveals whether one rotation dominates the leaderboard or if hits
    are scattered ~equally — the second is a signature of noise-only
    correlations."""
    if len(bits_str) < _UW_LEN + 8:
        return []
    bipolar = _bits_to_bipolar(bits_str)
    hits = []
    for name, uw in _UW_VARIANTS:
        corr = np.correlate(bipolar, uw, mode='valid').copy()
        for _ in range(3):
            pos = int(np.argmax(corr))
            hd  = (_UW_LEN - int(corr[pos])) // 2
            hits.append((hd, name, pos))
            corr[max(0, pos - 12):pos + 12] = -_UW_LEN
    hits.sort()
    return hits[:top_n]


def _envelope_shape(matched: np.ndarray, sps: int, buckets: int = 40):
    """Print a text bar chart of the smoothed envelope so we can eyeball
    whether the burst is where we think it is inside the pushed window."""
    power = (matched.real ** 2 + matched.imag ** 2).astype(np.float32)
    n = len(power)
    per_bucket = n // buckets
    if per_bucket < 1:
        return
    trimmed_len = per_bucket * buckets
    means = power[:trimmed_len].reshape(buckets, per_bucket).mean(axis=1)
    peak = means.max() + 1e-30
    print('  envelope (100 ms window, mean |matched|² per {}μs bucket):'
          .format(int(1e6 * per_bucket / _TARGET_SR)))
    for i, m in enumerate(means):
        bar = '█' * int(30 * m / peak)
        ms  = 1000 * i * per_bucket / _TARGET_SR
        print(f'    {ms:5.1f} ms  {bar}')


def main(cf32_path: str):
    path = Path(cf32_path)
    meta_path = path.with_suffix('.json')
    if not path.exists():
        raise SystemExit(f'File not found: {path}')
    if not meta_path.exists():
        raise SystemExit(f'Sidecar not found: {meta_path}')

    meta    = json.loads(meta_path.read_text())
    iq_wide = np.fromfile(path, dtype=np.complex64)

    print(f'=== {path.name} ===')
    print(f'  wide samples:    {len(iq_wide)}  '
          f'({len(iq_wide)/meta["sample_rate"]*1000:.1f} ms)')
    print(f'  wide SR:         {meta["sample_rate"]:>9d} Hz')
    print(f'  channel:         {meta["chan_id"]}  '
          f'@ {meta["chan_freq_hz"]/1e6:.4f} MHz')
    print(f'  chan offset:     {meta["chan_offset_hz"]:+.0f} Hz')
    print(f'  reported SNR:    {meta["snr_db"]:.1f} dB')
    print()

    narrow, sr = _channelise(iq_wide, meta['sample_rate'],
                             meta['chan_offset_hz'])
    print(f'  narrow samples:  {len(narrow)}  '
          f'({len(narrow)/sr*1000:.1f} ms)')
    print(f'  narrow SR:       {sr:>9d} Hz')
    print()

    resampled = _resample_to_target(narrow, sr)
    matched   = fftconvolve(resampled, _RRC_TAPS, mode='same').astype(np.complex64)
    print(f'  matched full:    {len(matched)} samples '
          f'({len(matched)/_TARGET_SR*1000:.1f} ms)')

    _envelope_shape(matched, _TARGET_SPS)
    print()

    matched_trim = _trim_to_burst(matched, _TARGET_SPS)
    print(f'  matched trim:    {len(matched_trim)} samples '
          f'({len(matched_trim)/_TARGET_SR*1000:.1f} ms)')

    symbols_raw = _pick_best_phase(matched_trim, _TARGET_SPS)
    cfo_hz      = _estimate_cfo_dqpsk(symbols_raw, IRIDIUM_SYMRATE)
    print(f'  symbols:         {len(symbols_raw)}')
    print(f'  CFO estimate:    {cfo_hz:+7.1f} Hz  '
          f'(unambiguous range ±{IRIDIUM_SYMRATE/8:.0f})')

    if abs(cfo_hz) > 1.0:
        t_sym = np.arange(len(symbols_raw), dtype=np.float64) / IRIDIUM_SYMRATE
        symbols = (symbols_raw * np.exp(-2j * np.pi * cfo_hz * t_sym)
                   ).astype(np.complex64)
    else:
        symbols = symbols_raw

    hist = _diff_angle_histogram(symbols)
    print('  diff-angle histogram (after CFO correction):')
    for bin_name, (n, pct) in hist.items():
        bar = '█' * int(pct / 2)
        print(f'    {bin_name:>5s}:  {n:>5d}  {pct:5.1f}%  {bar}')
    print('    A healthy demod on Iridium shows peaks clustered near the')
    print('    4 quadrant centres.  Flat ~25% each = timing/CFO broken.')
    print()

    result = demod_burst(narrow, sr)
    print(f'  bits produced:   {len(result["bits"])}')
    print(f'  best UW:         {result["uw"]}')
    print()
    print('  Top-6 UW candidates (across all 8 variants and positions):')
    for hd, name, pos in _top_uw_hits(result['bits']):
        print(f'    HD={hd:2d}   {name:>7s}   pos={pos:>5d}')
    print()
    print(f'  First 96 bits:   {result["bits"][:96]}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(
            'Usage: python -m plugins.iridium_decoder.inspect_burst '
            '<path/to/burst.cf32>')
    main(sys.argv[1])
