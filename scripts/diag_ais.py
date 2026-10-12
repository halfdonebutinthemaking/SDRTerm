#!/usr/bin/env python3
"""
Offline AIS diagnostic — read a recorded 162 MHz IQ file and print full
stage-by-stage statistics so a maintainer (or an LLM pair) can see
exactly where the decoder is dropping real frames.

Usage:
    uv run python scripts/diag_ais.py path/to/capture.iq
    uv run python scripts/diag_ais.py path/to/capture.sigmf-data

The file must be interleaved IQ at 1.024 MSPS, centred at 162.000 MHz,
in either raw complex64 (`.iq`) or a SigMF pair (`.sigmf-data` next to
`.sigmf-meta`).  Reports, per AIS channel (AIS 1 @ 161.975 MHz, AIS 2
@ 162.025 MHz):

  * Mean |IQ| and raw-chunk count — sanity-check the file is sane.
  * Total raw NRZI bits emitted by `gmsk_demod`.
  * Total HDLC candidate flag pairs.
  * destuff-fail / crc-fail / crc-OK breakdown.
  * Length histogram of destuff-OK candidates — real AIS slots cluster
    at 21-23 bytes (168-bit Class A position reports); random noise
    spreads across the whole size range.
  * Hex dump of the first N CRC-fail payloads — a cluster of similar
    first bytes (type field 0x04 / 0x08 / 0x0C for Class A types 1/2/3
    after MSB-first flip, i.e. 0x18 / 0x28 / 0x38 before flip) is
    strong evidence of a CRC-convention bug rather than noise.
  * Hex dump of the first N CRC-OK payloads — if any, parse_ais()
    output alongside.
"""
from __future__ import annotations

import os
import sys
from math import gcd

import numpy as np
from scipy.signal import resample_poly

# Allow `python scripts/diag_ais.py` without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.ais.protocol import (
    gmsk_demod, nrzi_decode, hdlc_frames_diag,
    ais_payload_from_hdlc, parse_ais,
)

# Same constants as plugins/ais/ais.py.
AIS_CHANNELS = [
    ('AIS 1', 161_975_000),
    ('AIS 2', 162_025_000),
]
CENTER_HZ   = 162_000_000
BW_HZ       =   1_024_000
BIT_RATE    =       9_600
TARGET_SPS  =           8
TARGET_SR   = BIT_RATE * TARGET_SPS

CRC_FAIL_DUMP_LIMIT = 10
CRC_OK_DUMP_LIMIT   = 10


def _load_iq(path: str) -> np.ndarray:
    """Read raw complex64 IQ from a `.iq` file or a SigMF data file."""
    lower = path.lower()
    if lower.endswith(('.sigmf-data', '.sigmf')):
        # SigMF default dtype is cf32_le (little-endian complex float32),
        # same memory layout as numpy complex64 on x86 / Apple Silicon.
        return np.fromfile(path, dtype=np.complex64)
    if lower.endswith('.iq'):
        return np.fromfile(path, dtype=np.complex64)
    raise SystemExit(f'unsupported extension: {path}')


def _resample_params(src_sr: int, dst_sr: int) -> tuple[int, int]:
    g        = gcd(dst_sr, src_sr)
    up, down = dst_sr // g, src_sr // g
    while up > 500 or down > 500:
        up   = max(1, up   // 2)
        down = max(1, down // 2)
    return up, down


def _analyse_channel(samples: np.ndarray, offset_hz: int,
                     src_sr: int, label: str) -> None:
    print(f'\n── {label} (offset {offset_hz:+d} Hz) ──')

    # Downconvert — single-shot, no cross-chunk phase because this is
    # offline analysis of a contiguous buffer.
    t        = np.arange(len(samples)) / src_sr
    baseband = (samples * np.exp(-1j * 2 * np.pi * offset_hz * t)
                ).astype(np.complex64)
    print(f'  mean |IQ|           : {np.abs(baseband).mean():.4f}')
    print(f'  samples             : {len(baseband):,}')

    up, down = _resample_params(src_sr, TARGET_SR)
    resampled = resample_poly(baseband, up, down)
    print(f'  resample up/down    : {up}/{down}')
    print(f'  resampled samples   : {len(resampled):,}')

    bits, _, _ = gmsk_demod(resampled, TARGET_SPS,
                            prev_iq=None, sym_offset=TARGET_SPS // 2)
    decoded = nrzi_decode(bits)
    print(f'  raw NRZI bits       : {len(bits):,}')
    print(f'  NRZI-decoded bits   : {len(decoded):,}')
    n_ones = sum(decoded)
    if decoded:
        print(f'  decoded 1-density   : {n_ones / len(decoded):.3f}  (0.5 = pure noise)')

    counts        = {'crc_ok': 0, 'crc_fail': 0,
                     'destuff_fail': 0, 'too_short': 0, 'short_frame': 0}
    len_hist: dict[int, int] = {}
    crc_fail_dumps: list[bytes] = []
    crc_ok_dumps:   list[bytes] = []

    for payload, crc_ok, status, raw_len in hdlc_frames_diag(decoded):
        counts[status] = counts.get(status, 0) + 1
        if payload is not None:
            byte_len = len(payload)
            len_hist[byte_len] = len_hist.get(byte_len, 0) + 1
            if crc_ok and len(crc_ok_dumps) < CRC_OK_DUMP_LIMIT:
                crc_ok_dumps.append(payload)
            elif not crc_ok and len(crc_fail_dumps) < CRC_FAIL_DUMP_LIMIT:
                crc_fail_dumps.append(payload)

    total = sum(counts.values()) or 1
    print(f'  total flag pairs    : {total:,}')
    for status, n in counts.items():
        print(f'    {status:<13}: {n:>7,}  ({n / total:.0%})')

    if len_hist:
        print('  length histogram (bytes:count, sorted by count):')
        hist = sorted(len_hist.items(), key=lambda x: -x[1])[:15]
        for byte_len, n in hist:
            marker = '  ← AIS Class A 168-bit payload' if 20 <= byte_len <= 24 else ''
            print(f'    {byte_len:>3} B : {n:>5}{marker}')

    if crc_fail_dumps:
        print(f'  first {len(crc_fail_dumps)} CRC-fail payloads (hex, first 16 B):')
        for p in crc_fail_dumps:
            flipped = ais_payload_from_hdlc(p)
            type_byte = flipped[0] >> 2 if flipped else -1
            print(f'    len={len(p):>3} B  wire={p[:16].hex(" ")}')
            print(f'                flipped={flipped[:16].hex(" ")}  '
                  f'type_if_ais={type_byte}')

    if crc_ok_dumps:
        print(f'  first {len(crc_ok_dumps)} CRC-OK payloads:')
        for p in crc_ok_dumps:
            flipped = ais_payload_from_hdlc(p)
            parsed  = parse_ais(flipped)
            print(f'    len={len(p):>3} B  wire={p[:16].hex(" ")}')
            print(f'                parsed={parsed}')


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f'file not found: {path}', file=sys.stderr)
        return 1

    print(f'reading : {path}')
    samples = _load_iq(path)
    duration_s = len(samples) / BW_HZ
    print(f'samples : {len(samples):,}  ({duration_s:.1f} s @ {BW_HZ} Hz)')

    for label, freq in AIS_CHANNELS:
        offset = freq - CENTER_HZ
        _analyse_channel(samples, offset, BW_HZ, label)

    return 0


if __name__ == '__main__':
    sys.exit(main())
