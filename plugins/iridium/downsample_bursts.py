#!/usr/bin/env python3
"""
Downsample captured Iridium bursts to a rate iridium-extractor accepts.

iridium-extractor requires `sample_rate / decimation` to be an integer
multiple of 250 kHz.  Common SDRTerm-friendly capture rates like 2.4 MHz
(the RTL-SDR v3 max) don't satisfy this constraint under any integer
decimation, so the extractor rejects those files before opening them.

This script batch-resamples .cf32 files in a captures directory to a
target rate that does satisfy the constraint (default: 2 000 000 Hz) and
updates each matching .json sidecar with the new sample_rate and
sample count.  Idempotent — files already at the target rate are skipped.

Usage
─────
  # Downsample everything under plugins/iridium/iridium_bursts/ to 2 MHz
  python3 plugins/iridium/downsample_bursts.py

  # Custom source dir + target rate, with originals moved aside first
  python3 plugins/iridium/downsample_bursts.py \\
      --in ~/some_captures --rate 2000000 --backup
"""

import argparse
import json
import shutil
import sys
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly


def process(cf32_path: Path, target_rate: int, backup_dir: Path | None) -> str:
    """Downsample one .cf32 + sidecar in place.  Returns a status label."""
    json_path = cf32_path.with_suffix('.json')
    if not json_path.exists():
        return 'no-sidecar'

    meta = json.loads(json_path.read_text())
    src_rate = int(meta.get('sample_rate', 0))
    if src_rate == 0:
        return 'no-rate-in-sidecar'
    if src_rate == target_rate:
        return 'already-at-target'

    g    = gcd(src_rate, target_rate)
    up   = target_rate // g
    down = src_rate    // g

    iq  = np.fromfile(cf32_path, dtype=np.complex64)
    out = resample_poly(iq, up, down).astype(np.complex64)

    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(cf32_path, backup_dir / cf32_path.name)
        shutil.copy(json_path, backup_dir / json_path.name)

    out.tofile(cf32_path)
    meta['sample_rate']       = target_rate
    meta['n_samples']          = int(len(out))
    meta['_downsampled_from']  = src_rate
    json_path.write_text(json.dumps(meta, indent=2))
    return 'ok'


def main() -> int:
    default_dir = Path(__file__).parent / 'iridium_bursts'
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--in', dest='in_dir', default=str(default_dir),
                    help='directory of .cf32 + .json pairs (default: %(default)s)')
    ap.add_argument('--rate', type=int, default=2_000_000,
                    help='target sample rate in Hz (default: %(default)s — the largest '
                         'iridium-extractor-compatible rate below 2.4 MHz)')
    ap.add_argument('--backup', action='store_true',
                    help='copy originals to <in_dir>/backup_original_rate/ before '
                         'overwriting')
    args = ap.parse_args()

    in_dir = Path(args.in_dir).expanduser()
    if not in_dir.is_dir():
        print('Not a directory: {}'.format(in_dir), file=sys.stderr)
        return 2

    if args.rate % 250_000 != 0:
        print('warning: target rate {} is not a multiple of 250 000 Hz; '
              'iridium-extractor may still reject the output'.format(args.rate),
              file=sys.stderr)

    backup_dir = (in_dir / 'backup_original_rate') if args.backup else None
    files = sorted(in_dir.glob('*.cf32'))
    if not files:
        print('No .cf32 files in {}'.format(in_dir))
        return 0

    print('Downsampling {} files to {} Hz{}'.format(
        len(files), args.rate,
        ' (originals backed up to {})'.format(backup_dir) if backup_dir else ' (in place)'))

    counts: dict[str, int] = {}
    for i, f in enumerate(files, 1):
        status = process(f, args.rate, backup_dir)
        counts[status] = counts.get(status, 0) + 1
        # Progress line every 20 files so a big batch doesn't look stalled
        if i % 20 == 0 or i == len(files):
            print('  [{:>4}/{:<4}] {}'.format(i, len(files), f.name))
    print('Done.  ' + '  '.join('{}={}'.format(k, v) for k, v in sorted(counts.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
