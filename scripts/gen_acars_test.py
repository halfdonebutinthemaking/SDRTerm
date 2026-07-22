#!/usr/bin/env python3
"""
Generate a synthetic classic ACARS test signal.

Signal parameters
─────────────────
  Modulation  : AM with AFSK subcarrier (non-suppressed carrier)
  Subcarrier  : continuous-phase FSK
                mark = 2400 Hz (bit 1)  space = 1200 Hz (bit 0)
  Bit rate    : 2400 bps
  Char format : 7-bit ASCII, ODD parity in bit 7, LSB transmitted first
  Sample rate : 250 000 Hz
  Carrier     : 129.125 MHz (primary US ACARS frequency)
  SNR         : 20 dB

Usage
─────
  uv run python scripts/gen_acars_test.py [OUTPUT_BASE]

  uv run python main.py --file samples/acars_test.sigmf-data --bw 250000 --f 129.125M
  Enable the acars plugin from the plugin menu (p) and switch to its tab.
  No peak_marker needed.
"""

import json, os, sys
import numpy as np
from datetime import datetime, timezone
from scipy.signal import resample_poly

SR        = 250_000
BAUD      = 2_400
MARK_HZ   = 2_400
SPACE_HZ  = 1_200
MOD_DEPTH = 0.85        # AM modulation index
SNR_DB    = 20.0
DURATION  = 12.0
CENTER_HZ = 129_125_000.0

# Internal generation rate: exact 5 samples per bit
_GEN_SR  = 12_000       # 12000 / 2400 = exactly 5 samples/bit
_GEN_SPB = _GEN_SR // BAUD   # = 5

# ACARS control bytes (pre-parity)
_SYN = 0x16
_SOH = 0x01
_STX = 0x02
_ETX = 0x03


# ── character helpers ──────────────────────────────────────────────────────

def _add_parity(byte):
    """Set bit 7 so total 1-bits in the byte is ODD."""
    b = byte & 0x7F
    return b | (0x80 if bin(b).count('1') % 2 == 0 else 0x00)


def _byte_to_bits(byte):
    """Byte → 8-bit list, LSB first (ACARS transmission order)."""
    return [(byte >> i) & 1 for i in range(8)]


def _char_bits(c):
    """ASCII char → 8-bit list with ODD parity, LSB first."""
    return _byte_to_bits(_add_parity(ord(c) & 0x7F))


def _ctrl_bits(b):
    """Control byte → 8-bit list with ODD parity, LSB first."""
    return _byte_to_bits(_add_parity(b))


# ── BCS ───────────────────────────────────────────────────────────────────

def _compute_bcs(bcs_bytes):
    """XOR of all parity-inclusive bytes from Mode through ETX."""
    bcs = 0
    for b in bcs_bytes:
        bcs ^= b
    return bcs & 0xFF


def _bcs_to_bits(bcs):
    """Encode 8-bit BCS as two 4-bit nibble chars, high nibble first."""
    hi = _add_parity((bcs >> 4) + 0x30)
    lo = _add_parity((bcs & 0x0F) + 0x30)
    return _byte_to_bits(hi) + _byte_to_bits(lo)


# ── ACARS frame builder ────────────────────────────────────────────────────

def _build_frame(reg, flight, text):
    """
    Full ACARS frame as a bit list (LSB first per character).

    Structure: preamble(16×'+') + SYN×2 + SOH + header + STX + text + ETX
               + BCS(2 chars) + DEL
    """
    bits = []

    # Preamble: 16 × 0x2B ('+') — clock sync / bit-sync
    for _ in range(16):
        bits += _ctrl_bits(0x2B)

    # SYN × 2 + SOH
    bits += _ctrl_bits(_SYN) + _ctrl_bits(_SYN) + _ctrl_bits(_SOH)

    # ── header (covered by BCS) ────────────────────────────────────────────
    bcs_bytes = []

    def add(byte):
        pb = _add_parity(byte & 0x7F)
        bits.extend(_byte_to_bits(pb))
        bcs_bytes.append(pb)

    def add_str(s):
        for c in s:
            add(ord(c))

    # Mode ('2' = downlink data)
    add(ord('2'))
    # Aircraft registration (7 chars, space-padded)
    add_str(reg[:7].ljust(7))
    # Type indicator + block ID + sequence number
    add(ord('.'))
    add(ord('1'))
    add(ord('0'))
    # Flight ID (6 chars, space-padded)
    add_str(flight[:6].ljust(6))

    # STX
    add(_STX)

    # Message text
    for c in text:
        add(ord(c) & 0x7F)

    # ETX
    add(_ETX)
    # ── end of BCS coverage ────────────────────────────────────────────────

    # BCS
    bcs = _compute_bcs(bcs_bytes)
    bits += _bcs_to_bits(bcs)

    # DEL (no parity needed — sync char)
    bits += _byte_to_bits(0x7F)

    return bits


# ── AFSK modulator ─────────────────────────────────────────────────────────

def _bits_to_audio(bits):
    """
    Continuous-phase AFSK at _GEN_SR samples/s.
    Returns normalised float64 array in [-1, +1].
    """
    n = len(bits) * _GEN_SPB
    freqs = np.empty(n, dtype=np.float64)
    for i, b in enumerate(bits):
        freqs[i * _GEN_SPB : (i + 1) * _GEN_SPB] = MARK_HZ if b else SPACE_HZ

    # Phase integral: Σ freq/SR gives cycles
    phase = np.cumsum(freqs) / _GEN_SR
    audio = np.cos(2.0 * np.pi * phase)
    return audio  # peak amplitude = 1


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    out_base = sys.argv[1] if len(sys.argv) > 1 else 'samples/acars_test'

    messages = [
        ('N12345', 'AA0123', 'HELLO FROM ACARS! SDRTERM IS DECODING THIS.'),
        ('N67890', 'DL0456', 'FL350 FUEL 8.2T ETA 1823Z ENJOYING THE RIDE'),
        ('N11111', 'UA0789', 'ATIS MIA WINDS 080/10 VIS 10 CLR TEMP 28/22'),
        ('N99999', 'SW0321', 'ACARS TEST FRAME FOUR  ALL SYSTEMS NOMINAL'),
    ]

    # ── build signal at GEN_SR ─────────────────────────────────────────────
    gap_bits  = int(_GEN_SR * 1.5)   # 1.5-second carrier-only gap (no audio)
    n_gen_out = int(_GEN_SR * DURATION)

    audio_gen = np.zeros(n_gen_out, dtype=np.float64)
    pos       = int(_GEN_SR * 0.5)   # start first frame at 0.5 s

    for reg, flight, text in messages:
        frame_bits = _build_frame(reg, flight, text)
        frame_audio = _bits_to_audio(frame_bits)
        end = pos + len(frame_audio)
        if end > n_gen_out:
            break
        audio_gen[pos:end] = frame_audio
        pos = end + gap_bits

    # AM: s(t) = 1 + m × audio(t); carrier always present
    am_gen = 1.0 + MOD_DEPTH * audio_gen

    # ── resample GEN_SR → SR (12000 → 250000, up=125 down=6) ──────────────
    am_250k = resample_poly(am_gen, SR // _GEN_SR * 2, 1)   # temp upsample
    # Correct ratio: 250000/12000 = 125/6
    am_250k = resample_poly(am_gen, 125, 6)

    n_out = int(SR * DURATION)
    if len(am_250k) >= n_out:
        iq_clean = am_250k[:n_out].astype(np.complex64)
    else:
        iq_clean = np.pad(am_250k, (0, n_out - len(am_250k))).astype(np.complex64)

    # AWGN
    sig_pwr   = float(np.mean(np.abs(iq_clean) ** 2))
    noise_amp = np.sqrt(sig_pwr / (2.0 * 10 ** (SNR_DB / 10.0)))
    rng       = np.random.default_rng(42)
    noise     = noise_amp * (
        rng.standard_normal(n_out) + 1j * rng.standard_normal(n_out)
    ).astype(np.complex64)
    iq = (iq_clean + noise).astype(np.complex64)

    # ── write SigMF ────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(out_base) or '.', exist_ok=True)
    data_path = out_base + '.sigmf-data'
    meta_path = out_base + '.sigmf-meta'

    iq.tofile(data_path)

    meta = {
        'global': {
            'core:datatype':    'cf32_le',
            'core:sample_rate': SR,
            'core:version':     '1.0.0',
            'core:recorder':    'SDRTerm gen_acars_test.py',
            'core:description': (
                'Synthetic classic ACARS test.  '
                'AM/AFSK {} baud  mark={}Hz space={}Hz  SNR={} dB'.format(
                    BAUD, MARK_HZ, SPACE_HZ, SNR_DB)
            ),
        },
        'captures': [{
            'core:sample_start': 0,
            'core:frequency':    CENTER_HZ,
            'core:datetime':     datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%S.%fZ'),
        }],
        'annotations': [{
            'core:sample_start': 0,
            'core:sample_count': n_out,
            'core:description':  'ACARS 2400 baud  {} frames'.format(len(messages)),
        }],
    }
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)

    size_mb = os.path.getsize(data_path) / 1e6
    print('Wrote {} ({:.1f} MB)'.format(data_path, size_mb))
    print('Wrote {}'.format(meta_path))
    print()
    print('Signal: AM/AFSK  {} baud  mark={}Hz space={}Hz  SNR={} dB'.format(
        BAUD, MARK_HZ, SPACE_HZ, SNR_DB))
    print('Frames: {}'.format(len(messages)))
    for reg, flight, text in messages:
        print('  {} {} {}'.format(reg, flight, text))
    print()
    print('Replay:')
    print('  uv run python main.py --file {} --bw 250000 --f 129.125M'.format(data_path))
    print('Then: enable the acars plugin from the plugin menu (p) and switch to its tab.')


if __name__ == '__main__':
    main()
