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

    # Spectrum of just the burst region — tells us if this looks like
    # a 25 ksym/s DQPSK signal (should span ~25 kHz + roll-off) or
    # something else (CW spur, chirp, wideband noise).
    _burst_spectrum(narrow, sr)
    print()

    # Raw first 24 symbols of the burst body (mag/phase) — should be
    # constant magnitude with distinct phase steps for DQPSK.
    _raw_burst_snapshot(narrow, sr)
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
    print('  diff-angle histogram (ALL {} symbols):'.format(len(symbols)))
    for bin_name, (n, pct) in hist.items():
        bar = '█' * int(pct / 2)
        print(f'    {bin_name:>5s}:  {n:>5d}  {pct:5.1f}%  {bar}')

    # Magnitude distribution — reveals in-burst vs out-of-burst symbols
    mags = np.abs(symbols)
    print()
    print(f'  symbol magnitudes:  min={mags.min():.3f}  '
          f'median={np.median(mags):.3f}  max={mags.max():.3f}')

    # Histogram over the top-25% by magnitude (the actual burst symbols)
    threshold = float(np.percentile(mags, 75))
    mask = mags >= threshold
    hi_syms = symbols[mask]
    print(f'  in-burst symbols (top 25% by |·|, {len(hi_syms)} total):')
    if len(hi_syms) >= 3:
        hist_hi = _diff_angle_histogram(hi_syms)
        for bin_name, (n, pct) in hist_hi.items():
            bar = '█' * int(pct / 2)
            print(f'    {bin_name:>5s}:  {n:>5d}  {pct:5.1f}%  {bar}')
    print('    Clustered peaks here + flat above = demod is fine, trim')
    print('    is just too wide (noise symbols dominate the aggregate).')
    print()

    result = demod_burst(narrow, sr)
    print(f'  bits produced:   {len(result["bits"])}')
    print(f'  best UW:         {result["uw"]}')
    print()
    print('  Top-6 UW candidates (across all 8 variants and positions):')
    for hd, name, pos in _top_uw_hits(result['bits']):
        print(f'    HD={hd:2d}   {name:>7s}   pos={pos:>5d}')
    print()

    # Also try a TIGHT trim centred on the true burst peak (a few symbols
    # wide) — if this recovers the UW at low HD, the trim width in demod.py
    # needs to shrink dramatically.
    _try_tight_demod(matched, _TARGET_SPS)

    print()
    # Iridium-toolkit's demod.py doesn't do matched filtering — it uses
    # adaptive zero-crossing symbol timing directly on the narrow-band
    # signal.  Try that here: skip the RRC filter entirely and demod
    # directly from `narrow` at 2 samples/symbol.  If this works better
    # than the matched-filtered version we know Iridium isn't RRC-α=0.4
    # shaped and our matched filter is the problem.
    _try_no_rrc(narrow, sr)

    print()
    print(f'  First 96 bits:   {result["bits"][:96]}')


def _burst_spectrum(narrow: np.ndarray, sr: int):
    """Find the burst region in the narrow-band signal and FFT it.
    Prints a text bar chart of the spectrum from -25 to +25 kHz.

    For 25 ksym/s DQPSK with typical roll-off, energy spans about
    ±15-20 kHz.  A narrow spike near 0 Hz means a CW carrier.  Energy
    spread wider than the whole 50 kHz window is a wideband signal
    (LTE leak, noise burst, etc.)."""
    power = np.abs(narrow) ** 2
    win_syms = 100
    win = win_syms * 2   # narrow is at ~2 SPS
    if win >= len(narrow):
        return
    csum = np.cumsum(power)
    csum0 = np.concatenate([[0.0], csum])
    wsum = csum0[win:] - csum0[:-win]
    start = int(np.argmax(wsum))
    burst = narrow[start:start + win].astype(np.complex64)

    # FFT and bin into a text histogram
    N = 64
    if len(burst) < N:
        return
    spec = np.abs(np.fft.fftshift(np.fft.fft(burst[:N] * np.hanning(N))))
    freqs = np.fft.fftshift(np.fft.fftfreq(N, d=1.0 / sr))
    peak = spec.max() + 1e-30
    print(f'  narrow spectrum at burst peak ({len(burst)} samples '
          f'= {1000*len(burst)/sr:.1f} ms):')
    for i in range(len(spec)):
        bar = '█' * int(30 * spec[i] / peak)
        marker = ' ← 0 Hz' if abs(freqs[i]) < 100 else ''
        print(f'    {freqs[i]/1e3:+7.2f} kHz  {bar}{marker}')


def _raw_burst_snapshot(narrow: np.ndarray, sr: int):
    """Print the first 24 samples of the burst body as (mag, phase_deg).

    Constant-mag with 90°-step phase changes → DQPSK.  Constant mag
    with linear phase drift → CW carrier.  Erratic mag → not the
    modulation we think it is (or timing very wrong)."""
    power = np.abs(narrow) ** 2
    win = 200
    if win >= len(narrow):
        return
    csum = np.cumsum(power)
    csum0 = np.concatenate([[0.0], csum])
    wsum = csum0[win:] - csum0[:-win]
    start = int(np.argmax(wsum))
    print(f'  first 24 narrow samples of burst region (from sample {start}):')
    print('    idx    mag       phase       Δφ')
    prev_phase = None
    for i in range(min(24, len(narrow) - start)):
        s = narrow[start + i]
        mag = float(abs(s))
        phase = float(np.angle(s) * 180 / np.pi)
        dphi = '     -' if prev_phase is None else \
               f'{(phase - prev_phase + 180) % 360 - 180:+7.1f}°'
        print(f'    {i:>3d}   {mag:.4f}   {phase:+7.1f}°   {dphi}')
        prev_phase = phase


def _try_no_rrc(narrow: np.ndarray, sr: int):
    """Demod without any matched filter — just decimate to symbol rate."""
    from plugins.iridium_decoder.demod import _dqpsk_bits, find_uw
    print('  No-RRC demod experiment (skip matched filter):')
    # Resample narrow → 2 SPS at 25 ksym/s (i.e. 50 kHz)
    if sr != 2 * IRIDIUM_SYMRATE:
        target = 2 * IRIDIUM_SYMRATE
        g = gcd(int(sr), target)
        sig = resample_poly(narrow, target // g, sr // g).astype(np.complex64)
    else:
        sig = narrow

    # Sliding-window energy trim on the narrow signal (~800 samples = 16 ms)
    trim_syms = 400
    win = trim_syms * 2
    if win < len(sig):
        power = np.abs(sig) ** 2
        csum  = np.cumsum(power, dtype=np.float64)
        csum0 = np.concatenate([[0.0], csum])
        wsum  = csum0[win:] - csum0[:-win]
        start = int(np.argmax(wsum))
        sig   = sig[start:start + win]

    # Try both sampling phases (even vs odd index)
    print('    phase   bestUW    HD   pos   cfo/Hz   [top-25% diff-angle spread]')
    for phase in (0, 1):
        syms = sig[phase::2]
        cfo  = _estimate_cfo_dqpsk(syms, IRIDIUM_SYMRATE)
        if abs(cfo) > 1.0:
            t = np.arange(len(syms), dtype=np.float64) / IRIDIUM_SYMRATE
            syms = (syms * np.exp(-2j * np.pi * cfo * t)
                    ).astype(np.complex64)
        # Angle spread on top-25% by magnitude
        mags = np.abs(syms)
        if len(mags) >= 4:
            thr = float(np.percentile(mags, 75))
            hi_syms = syms[mags >= thr]
            if len(hi_syms) >= 3:
                hist = _diff_angle_histogram(hi_syms)
                spread = ' '.join(f'{k.strip()}={v[1]:4.1f}%' for k, v in hist.items())
            else:
                spread = '(too few)'
        else:
            spread = '(too few)'
        bits = _dqpsk_bits(syms)
        r = find_uw(bits)
        print(f'    {phase}       {r["name"]:>6s}   {r["hd"]:>3d}  pos={r["pos"]:>4d}  '
              f'{cfo:+7.1f}   [{spread}]')


def _try_tight_demod(matched: np.ndarray, sps: int):
    """Demodulate a tight window around the sliding-window energy peak,
    trying a range of window widths.  If any width recovers the UW at
    HD ≤ 2 with one rotation clearly winning, that width is close to
    the true burst size."""
    from plugins.iridium_decoder.demod import (
        _dqpsk_bits, find_uw,
    )
    print('  Tight-trim experiment (varying trim width):')
    print('    width_syms   best_UW              HD   pos')
    for width_syms in (60, 100, 150, 200, 300, 400, 600):
        win = width_syms * sps
        if win > len(matched):
            continue
        power = (matched.real ** 2 + matched.imag ** 2).astype(np.float32)
        csum  = np.cumsum(power, dtype=np.float64)
        csum0 = np.concatenate([[0.0], csum])
        wsum  = csum0[win:] - csum0[:-win]
        start = int(np.argmax(wsum))
        slice_ = matched[start:start + win]
        syms   = _pick_best_phase(slice_, sps)
        cfo_hz = _estimate_cfo_dqpsk(syms, IRIDIUM_SYMRATE)
        if abs(cfo_hz) > 1.0:
            t_sym = np.arange(len(syms), dtype=np.float64) / IRIDIUM_SYMRATE
            syms  = (syms * np.exp(-2j * np.pi * cfo_hz * t_sym)
                     ).astype(np.complex64)
        bits = _dqpsk_bits(syms)
        r = find_uw(bits)
        print(f'    {width_syms:>10d}   {r["name"]:>10s}   {r["hd"]:>4d}  '
              f'pos={r["pos"]:>4d}   cfo={cfo_hz:+7.1f} Hz')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(
            'Usage: python -m plugins.iridium_decoder.inspect_burst '
            '<path/to/burst.cf32>')
    main(sys.argv[1])
