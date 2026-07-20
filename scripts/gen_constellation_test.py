#!/usr/bin/env python3
"""
Generate a synthetic QPSK test signal for verifying the constellation plugin.

Signal parameters
─────────────────
  Modulation  : QPSK (π/4-offset, Gray-coded)
  Symbol rate : 10 500 sym/s   (matches VDL Mode 2 for later use)
  Pulse shape : Root-raised cosine, α = 0.35
  Sample rate : 250 000 Hz
  Carrier     : centred at 0 Hz offset (no Doppler, no tuning error)
  SNR         : 20 dB
  Duration    : 10 s

Usage
─────
  uv run python scripts/gen_constellation_test.py [OUTPUT_BASE]

  # Replay in SDRTerm:
  uv run python main.py --file samples/constellation_test.sigmf-data --f 120M

  Enable peak_marker (k), then constellation (c).
  Set symbol rate to 10 500 sym/s — four tight clusters should appear.
"""
import json
import os
import sys
import numpy as np
from datetime import datetime, timezone
from scipy.signal import lfilter

SR          = 250_000
SYMBOL_RATE = 10_500       # sym/s
SPS         = 24           # samples per symbol (SR / SPS = 10 416.7 ≈ 10 500)
DURATION    = 10.0
CENTER_HZ   = 120_000_000.0
SNR_DB      = 20.0
RRC_ALPHA   = 0.35

# QPSK: four points at 45°, 135°, 225°, 315°
QPSK_SYMBOLS = np.exp(1j * (np.pi / 4 + np.pi / 2 * np.arange(4)))


def _rrc(n_taps: int, alpha: float, sps: int) -> np.ndarray:
    """Root-raised cosine FIR coefficients."""
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
    return h / np.sqrt(np.sum(h ** 2))


def main():
    out_base = sys.argv[1] if len(sys.argv) > 1 else 'samples/constellation_test'
    n        = int(SR * DURATION)
    rng      = np.random.default_rng(0)

    # Random QPSK symbols
    n_sym    = n // SPS + 8
    syms     = QPSK_SYMBOLS[rng.integers(0, 4, n_sym)]

    # Upsample: one impulse per symbol, zeros in between
    upsampled            = np.zeros(n_sym * SPS, dtype=np.complex128)
    upsampled[::SPS]     = syms

    # RRC TX filter
    rrc_taps = _rrc(8 * SPS + 1, RRC_ALPHA, SPS)
    shaped   = np.convolve(upsampled, rrc_taps, mode='same')[:n]

    # AWGN at target SNR
    sig_pwr   = np.mean(np.abs(shaped) ** 2)
    noise_amp = np.sqrt(sig_pwr / (2.0 * 10 ** (SNR_DB / 10.0)))
    noise     = noise_amp * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    iq        = (shaped + noise).astype(np.complex64)

    data_path = out_base + '.sigmf-data'
    iq.tofile(data_path)

    meta = {
        'global': {
            'core:datatype':    'cf32_le',
            'core:sample_rate': SR,
            'core:version':     '1.0.0',
            'core:recorder':    'SDRTerm gen_constellation_test.py',
            'core:description': (
                'Synthetic QPSK test. '
                'Symbol rate {} sym/s, RRC α={}, SNR {} dB.'.format(
                    SYMBOL_RATE, RRC_ALPHA, SNR_DB)
            ),
        },
        'captures': [{
            'core:sample_start': 0,
            'core:frequency':    CENTER_HZ,
            'core:datetime':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        }],
        'annotations': [{
            'core:sample_start': 0,
            'core:sample_count': n,
            'core:description':  'QPSK {} sym/s RRC α={} SNR={}dB'.format(
                SYMBOL_RATE, RRC_ALPHA, SNR_DB),
        }],
    }
    meta_path = out_base + '.sigmf-meta'
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)

    size_mb = os.path.getsize(data_path) / 1e6
    print('Wrote {} ({:.1f} MB, {} samples)'.format(data_path, size_mb, n))
    print('Wrote', meta_path)
    print()
    print('Signal: QPSK  {} sym/s  RRC α={}  SNR={} dB'.format(
        SYMBOL_RATE, RRC_ALPHA, SNR_DB))
    print('Replay:')
    print('  uv run python main.py --file {} --f 120M'.format(data_path))
    print()
    print('Then: peak_marker (k) → constellation (c)')
    print('      set symbol rate to {} sym/s → four tight clusters'.format(SYMBOL_RATE))


if __name__ == '__main__':
    main()
