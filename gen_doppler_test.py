#!/usr/bin/env python3
"""
Generate a synthetic FM-modulated signal with LEO Doppler drift, saved as SigMF.

Physics model
─────────────
A LEO satellite at ~400 km altitude moving at ~7.7 km/s transmitting at 1 GHz
produces a maximum Doppler shift of ±f·v/c ≈ ±25 kHz and a drift rate of
roughly 5 kHz/s through the overhead pass.  This script simulates a 10-second
window centred near closest approach, where the drift rate is highest:

  t = 0 s  →  signal is +20 kHz above the SDR centre frequency  (approaching)
  t = 5 s  →  signal crosses 0 Hz offset                        (overhead)
  t = 10 s →  signal is −20 kHz below the SDR centre frequency  (receding)

The carrier is also FM-modulated with a 1 kHz audio tone at ±3 kHz deviation
so it looks like a real narrowband FM transmitter rather than a bare tone.

Output
──────
  doppler_test.sigmf-data   raw cf32_le IQ samples
  doppler_test.sigmf-meta   JSON metadata (sample rate, centre frequency, …)

Usage
─────
  uv run python gen_doppler_test.py [OUTPUT_BASE]

  # then replay in SDRTerm:
  uv run python main.py --file doppler_test.sigmf-data --f 1G

  Enable the peak marker plugin (k), tab to its view, then press t to
  activate follow mode.  The SDR centre will chase the drifting signal.
"""

import json
import os
import sys
import numpy as np
from datetime import datetime, timezone

# ── parameters ─────────────────────────────────────────────────────────────────
SR              = 250_000          # Hz — narrow BW keeps file ≈ 20 MB
DURATION        = 10.0             # seconds
CENTER_HZ       = 1_000_000_000.0  # 1 GHz — SDR tuning frequency

DOPPLER_START   =  20_000.0        # Hz offset at t = 0  (approaching)
DOPPLER_END     = -20_000.0        # Hz offset at t = 10 s  (receding)

FM_TONE_HZ      =   1_000.0        # audio tone frequency
FM_DEVIATION    =   3_000.0        # ± Hz deviation (narrowband FM)

SIGNAL_AMP      = 0.5              # −6 dBFS
NOISE_AMP       = 0.025            # noise floor ≈ −26 dBFS relative to signal


def main() -> None:
    out_base  = sys.argv[1] if len(sys.argv) > 1 else 'doppler_test'
    n         = int(SR * DURATION)
    t         = np.arange(n, dtype=np.float64) / SR

    # ── Doppler drift (linear sweep through the overhead pass) ─────────────────
    doppler   = DOPPLER_START + (DOPPLER_END - DOPPLER_START) * t / DURATION

    # ── FM modulation ──────────────────────────────────────────────────────────
    audio     = np.sin(2.0 * np.pi * FM_TONE_HZ * t)
    f_inst    = doppler + FM_DEVIATION * audio          # instantaneous frequency
    phase     = 2.0 * np.pi * np.cumsum(f_inst) / SR   # integrate → phase

    # ── complex baseband IQ + AWGN ─────────────────────────────────────────────
    rng    = np.random.default_rng(42)   # fixed seed → reproducible
    signal = SIGNAL_AMP * np.exp(1j * phase)
    noise  = NOISE_AMP * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    iq     = (signal + noise).astype(np.complex64)

    # ── write SigMF data ───────────────────────────────────────────────────────
    data_path = out_base + '.sigmf-data'
    iq.tofile(data_path)
    size_mb = os.path.getsize(data_path) / 1e6
    print('Wrote {} ({:.1f} MB, {} samples)'.format(data_path, size_mb, n))

    # ── write SigMF meta ───────────────────────────────────────────────────────
    meta = {
        'global': {
            'core:datatype':    'cf32_le',
            'core:sample_rate': SR,
            'core:version':     '1.0.0',
            'core:recorder':    'SDRTerm gen_doppler_test.py',
            'core:description': (
                'Synthetic LEO Doppler test signal at 1 GHz.  '
                'FM tone {:.0f} Hz / ±{:.0f} Hz dev.  '
                'Doppler {:+.0f} kHz → {:+.0f} kHz over {:.0f} s.'
            ).format(FM_TONE_HZ, FM_DEVIATION,
                     DOPPLER_START / 1e3, DOPPLER_END / 1e3, DURATION),
        },
        'captures': [{
            'core:sample_start': 0,
            'core:frequency':    CENTER_HZ,
            'core:datetime':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        }],
        'annotations': [{
            'core:sample_start':  0,
            'core:sample_count':  n,
            'core:description':   'Doppler sweep {:+.0f} → {:+.0f} Hz  /  FM ±{:.0f} Hz'.format(
                DOPPLER_START, DOPPLER_END, FM_DEVIATION),
        }],
    }
    meta_path = out_base + '.sigmf-meta'
    with open(meta_path, 'w') as mf:
        json.dump(meta, mf, indent=2)
    print('Wrote', meta_path)

    print()
    print('Signal summary')
    print('  Centre frequency : {:.3f} GHz'.format(CENTER_HZ / 1e9))
    print('  Sample rate      : {} kHz'.format(SR // 1000))
    print('  Duration         : {:.0f} s  (loops in SDRTerm)'.format(DURATION))
    print('  Doppler start    : {:+.0f} kHz  (approaching)'.format(DOPPLER_START / 1e3))
    print('  Doppler end      : {:+.0f} kHz  (receding)'.format(DOPPLER_END / 1e3))
    print('  Drift rate       : {:.1f} kHz/s'.format(
          (DOPPLER_END - DOPPLER_START) / DURATION / 1e3))
    print('  FM tone          : {:.0f} Hz  ±{:.0f} Hz dev'.format(FM_TONE_HZ, FM_DEVIATION))
    print()
    print('Replay:')
    print('  uv run python main.py --file {} --f 1G'.format(data_path))
    print()
    print('Then: enable peak_marker (k)  →  tab to its view  →  press t to follow')


if __name__ == '__main__':
    main()
