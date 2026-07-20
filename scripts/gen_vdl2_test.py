#!/usr/bin/env python3
"""
Generate a synthetic VDL Mode 2 test signal.

Signal parameters
─────────────────
  Modulation  : D8PSK (differential 8PSK, Gray-coded)
  Symbol rate : 10 500 sym/s
  Bit rate    : 31 500 bit/s
  Pulse shape : Root-raised cosine, α = 0.60
  Sample rate : 250 000 Hz
  Carrier     : 136.900 MHz (primary VDL2 frequency)
  SNR         : 20 dB
  Content     : 4 AVLC frames with ACARS-style fun text

Usage
─────
  uv run python scripts/gen_vdl2_test.py [OUTPUT_BASE]

  uv run python main.py --file samples/vdl2_test.sigmf-data --bw 250000 --f 136.9M
  Switch to VDL2 tab (v) — no peak_marker needed.
"""
import json, os, sys, numpy as np
from math import gcd
from datetime import datetime, timezone
from scipy.signal import resample_poly

SR          = 250_000
SYMBOL_RATE = 10_500
RRC_ALPHA   = 0.60
_GEN_SPS    = 24
_GEN_SR     = SYMBOL_RATE * _GEN_SPS    # 252 000 Hz
DURATION    = 15.0
CENTER_HZ   = 136_900_000.0
SNR_DB      = 20.0

# Gray encode: tribit integer (0-7) → phase-change index
_GRAY_ENC = [i ^ (i >> 1) for i in range(8)]   # [0,1,3,2,6,7,5,4]

# Fun messages — ACARS-style but content is deliberately readable
MESSAGES = [
    "N-SDR01 B737 EGLL-KLAX: HELLO FROM VDL2! SDRTERM IS DECODING THIS",
    "N-SDR01 FL350 FUEL 8.2T ETA 1823Z: ENJOYING THE RIDE AT 35000FT",
    "N-SDR01 ACARS MSG: IF YOU CAN READ THIS YOUR DECODER IS WORKING GREAT",
    "N-SDR01 VDL MODE 2 TEST: SQUAWK 7700  JK JK  ALL SYSTEMS NOMINAL",
]


# ── RRC filter ─────────────────────────────────────────────────────────────

def _rrc(n_taps: int, alpha: float, sps: int) -> np.ndarray:
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


# ── CRC-CCITT (reflected, matches protocol.py) ─────────────────────────────

def _crc_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else crc >> 1
    return crc ^ 0xFFFF


# ── HDLC frame builder ─────────────────────────────────────────────────────

def _bytes_to_bits_lsb(data: bytes) -> list:
    """Bytes → bit list, LSB first per byte (HDLC order)."""
    bits = []
    for b in data:
        for i in range(8):
            bits.append((b >> i) & 1)
    return bits

def _bit_stuff(bits: list) -> list:
    """Insert 0 after every run of 5 consecutive 1s."""
    out, count = [], 0
    for b in bits:
        out.append(b)
        count = count + 1 if b else 0
        if count == 5:
            out.append(0)
            count = 0
    return out

def _build_avlc_frame(text: str) -> bytes:
    """Build simplified AVLC frame body (without HDLC flags)."""
    dest    = bytes([0xFF, 0x01])    # broadcast destination
    src     = bytes([0xAC, 0x01])    # aircraft source
    control = bytes([0x03])          # UI (unnumbered information)
    proto   = bytes([0xCF])          # ACARS protocol discriminator
    payload = text.encode('ascii')
    body    = dest + src + control + proto + payload
    fcs     = _crc_ccitt(body)
    return body + bytes([fcs & 0xFF, (fcs >> 8) & 0xFF])

def _hdlc_bits(text: str) -> list:
    """Complete HDLC frame as a bit list (opening flag + stuffed data + closing flag)."""
    flag_bits  = _bytes_to_bits_lsb(bytes([0x7E]))
    frame      = _build_avlc_frame(text)
    data_bits  = _bit_stuff(_bytes_to_bits_lsb(frame))
    return flag_bits + data_bits + flag_bits

def _idle_bits(n_flags: int) -> list:
    """n_flags × 0x7E as bit list (inter-frame fill, no stuffing)."""
    flag_bits = _bytes_to_bits_lsb(bytes([0x7E]))
    return flag_bits * n_flags


# ── Self-synchronising scrambler  G(x) = 1 + x + x⁶ ──────────────────────

def _scramble(bits: list) -> list:
    """s[n] = d[n] ^ s[n-1] ^ s[n-6]  (feedback from scrambled output)."""
    out: list = []
    for i, d in enumerate(bits):
        s = d
        if i >= 1: s ^= out[i - 1]
        if i >= 6: s ^= out[i - 6]
        out.append(s)
    return out


# ── D8PSK modulator ────────────────────────────────────────────────────────

def _bits_to_symbols(bits: list) -> np.ndarray:
    """
    Bits → D8PSK complex baseband symbols.

    Groups bits into tribits (LSB = first bit in stream).
    Initial reference symbol at phase 0 is prepended so the decoder can
    establish differential context on the very first symbol.
    """
    # Pad to multiple of 3
    while len(bits) % 3:
        bits = bits + [0]

    n_sym  = len(bits) // 3
    phase  = 0.0
    syms   = np.zeros(n_sym + 1, dtype=np.complex128)
    syms[0] = 1.0 + 0j    # reference symbol

    for i in range(n_sym):
        # Tribit: bit[0]=LSB, bit[1], bit[2]=MSB
        tribit    = bits[3*i] | (bits[3*i+1] << 1) | (bits[3*i+2] << 2)
        gray_idx  = _GRAY_ENC[tribit]
        phase    += gray_idx * (np.pi / 4)
        syms[i+1] = np.exp(1j * phase)

    return syms


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    out_base = sys.argv[1] if len(sys.argv) > 1 else 'samples/vdl2_test'
    n_out    = int(SR * DURATION)

    # Build bitstream: preamble + 4 ACARS frames + idle fill to target duration.
    # Filling with idle flags (0x7E) rather than zeros makes the signal a
    # continuous D8PSK transmission, which is what VDL2 actually does between
    # frames.  The constellation plugin can then accumulate clusters from the
    # full file rather than from the brief bursts only.
    n_bits_target = int(SYMBOL_RATE * 3 * DURATION)   # bits needed to fill DURATION
    bits = _idle_bits(16)                    # preamble
    for msg in MESSAGES:
        bits += _hdlc_bits(msg)
        bits += _idle_bits(8)                # inter-frame gap
    bits += _idle_bits(16)                   # postamble
    # Pad to target duration with idle flags (valid HDLC inter-frame fill)
    n_idle_needed = max(0, n_bits_target - len(bits))
    bits += _idle_bits((n_idle_needed + 7) // 8)

    # Scramble (G(x) = 1 + x + x⁶) then D8PSK modulate
    bits = _scramble(bits)
    syms = _bits_to_symbols(bits)

    # Upsample to GEN_SR
    up_sig = np.zeros(len(syms) * _GEN_SPS, dtype=np.complex128)
    up_sig[::_GEN_SPS] = syms

    # TX RRC pulse shaping
    rrc    = _rrc(8 * _GEN_SPS + 1, RRC_ALPHA, _GEN_SPS)
    shaped = np.convolve(up_sig, rrc, mode='same')

    # Resample 252 000 → 250 000 Hz (gcd=2000, up=125, down=126)
    resampled = resample_poly(shaped, 125, 126)

    # Trim / pad to exact duration
    if len(resampled) >= n_out:
        iq_clean = resampled[:n_out]
    else:
        iq_clean = np.concatenate(
            [resampled, np.zeros(n_out - len(resampled), dtype=np.complex128)])

    # AWGN
    sig_pwr   = np.mean(np.abs(iq_clean) ** 2)
    noise_amp = np.sqrt(sig_pwr / (2.0 * 10 ** (SNR_DB / 10.0)))
    rng       = np.random.default_rng(42)
    noise     = noise_amp * (rng.standard_normal(n_out) + 1j * rng.standard_normal(n_out))
    iq        = (iq_clean + noise).astype(np.complex64)

    # Write SigMF
    os.makedirs(os.path.dirname(out_base) or '.', exist_ok=True)
    data_path = out_base + '.sigmf-data'
    meta_path = out_base + '.sigmf-meta'

    iq.tofile(data_path)

    meta = {
        'global': {
            'core:datatype':    'cf32_le',
            'core:sample_rate': SR,
            'core:version':     '1.0.0',
            'core:recorder':    'SDRTerm gen_vdl2_test.py',
            'core:description': (
                'Synthetic VDL Mode 2 test. '
                'D8PSK {} sym/s RRC α={} SNR {} dB.'.format(
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
            'core:sample_count': n_out,
            'core:description':  'D8PSK {} sym/s  {} frames'.format(
                SYMBOL_RATE, len(MESSAGES)),
        }],
    }
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)

    size_mb = os.path.getsize(data_path) / 1e6
    print('Wrote {} ({:.1f} MB)'.format(data_path, size_mb))
    print('Wrote {}'.format(meta_path))
    print()
    print('Signal: D8PSK  {} sym/s  RRC α={}  SNR={} dB'.format(
        SYMBOL_RATE, RRC_ALPHA, SNR_DB))
    print('Frames: {}'.format(len(MESSAGES)))
    for i, m in enumerate(MESSAGES):
        print('  [{}] {}'.format(i + 1, m))
    print()
    print('Replay:')
    print('  uv run python main.py --file {} --bw 250000 --f 136.9M'.format(data_path))
    print()
    print('Then: vdl2 tab (v)  — no peak_marker needed')
    print('      constellation (c) + m×3 for 8PSK to see clusters')


if __name__ == '__main__':
    main()
