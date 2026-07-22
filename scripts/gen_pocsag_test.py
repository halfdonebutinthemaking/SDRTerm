#!/usr/bin/env python3
"""
Generate a synthetic POCSAG test signal.

Signal parameters
─────────────────
  Modulation  : Direct 2-FSK (no subcarrier)
  Deviation   : ±4.5 kHz
  Baud rate   : 1200 bps
  Encoding    : NRZ, mark(1)=negative deviation, space(0)=positive
  Sample rate : 250 000 Hz
  Carrier     : 439.9875 MHz (EU DAPNET amateur paging)
  SNR         : 20 dB
  Duration    : 12 seconds

Usage
─────
  uv run python scripts/gen_pocsag_test.py [OUTPUT_BASE]

  uv run python main.py --file samples/pocsag_test.sigmf-data --bw 250000 --f 439.9875M
  Switch to POCSAG tab (g).
"""

import json, os, sys
import numpy as np
from datetime import datetime, timezone
from scipy.signal import resample_poly

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from plugins.pocsag.bch import encode

# ── signal params ────────────────────────────────────────────────────────────
SR           = 250_000
BAUD         = 1_200
DEVIATION_HZ = 4_500
SNR_DB       = 20.0
DURATION     = 12.0
CENTER_HZ    = 439_987_500.0

# 40 samples per bit at 1200 baud is a convenient GEN_SR
_GEN_SR  = 48_000
_GEN_SPB = _GEN_SR // BAUD   # = 40

# ── POCSAG constants ─────────────────────────────────────────────────────────
_SYNC = 0x7CD215D8
_IDLE = 0x7A89C197


def _even_parity(word31: int) -> int:
    return bin(word31 & 0x7FFFFFFF).count('1') & 1


def _make_codeword32(data21: int) -> int:
    cw31 = encode(data21)
    return (cw31 << 1) | _even_parity(cw31)


def _address_codeword(ric: int, func: int):
    """Return (32-bit codeword, frame_num) for the given RIC + function."""
    addr18    = (ric >> 3) & 0x3FFFF
    data21    = (addr18 << 2) | (func & 3)   # bit 20 = 0 (address indicator)
    return _make_codeword32(data21), ric & 7


def _message_codeword(data20: int) -> int:
    data21 = (1 << 20) | (data20 & 0xFFFFF)
    return _make_codeword32(data21)


# ── message packing ──────────────────────────────────────────────────────────

def _pack_alpha(text: str) -> list:
    """Encode text as message codewords: 7-bit ASCII, LSB first, 20 bits/codeword."""
    bits = []
    for c in text:
        v = ord(c) & 0x7F
        for k in range(7):
            bits.append((v >> k) & 1)
    while len(bits) % 20 != 0:
        bits.append(0)
    words = []
    for i in range(0, len(bits), 20):
        chunk  = bits[i:i + 20]
        data20 = 0
        for k, b in enumerate(chunk):
            data20 |= b << (19 - k)
        words.append(_message_codeword(data20))
    return words


def _pack_numeric(text: str) -> list:
    """Encode text as message codewords: 4-bit BCD, LSB first, 5 digits/codeword."""
    charset = '0123456789SU -)('
    bits = []
    for c in text:
        d = charset.index(c) if c in charset else 12   # unknown → space
        for k in range(4):
            bits.append((d >> k) & 1)
    while len(bits) % 20 != 0:
        bits.append(0)
    words = []
    for i in range(0, len(bits), 20):
        chunk  = bits[i:i + 20]
        data20 = 0
        for k, b in enumerate(chunk):
            data20 |= b << (19 - k)
        words.append(_message_codeword(data20))
    return words


# ── batch assembly ───────────────────────────────────────────────────────────

def _cw32_to_bits(cw: int) -> list:
    return [(cw >> (31 - k)) & 1 for k in range(32)]


def _build_batch(address_cw: int, frame_num: int, message_cws: list) -> list:
    """16-codeword batch with the address in its frame slot, message after, rest idle."""
    batch = [_IDLE] * 16
    batch[2 * frame_num] = address_cw
    for i, cw in enumerate(message_cws):
        pos = 2 * frame_num + 1 + i
        if pos >= 16:
            break   # message truncated; multi-batch messages not implemented
        batch[pos] = cw
    return batch


def _build_frame(ric: int, func: int, text: str, is_numeric: bool) -> list:
    """Preamble + sync + one batch containing address + message."""
    bits = [(i + 1) & 1 for i in range(576)]     # 576 bits of 1010… preamble
    address_cw, frame_num = _address_codeword(ric, func)
    msg_cws = _pack_numeric(text) if is_numeric else _pack_alpha(text)
    batch   = _build_batch(address_cw, frame_num, msg_cws)
    bits.extend(_cw32_to_bits(_SYNC))
    for cw in batch:
        bits.extend(_cw32_to_bits(cw))
    return bits


# ── FSK modulator ────────────────────────────────────────────────────────────

def _bits_to_iq(bits: list) -> np.ndarray:
    """Continuous-phase 2-FSK at GEN_SR: mark(1)=-dev, space(0)=+dev."""
    n = len(bits) * _GEN_SPB
    freqs = np.empty(n, dtype=np.float64)
    for i, b in enumerate(bits):
        freqs[i * _GEN_SPB : (i + 1) * _GEN_SPB] = -DEVIATION_HZ if b else DEVIATION_HZ
    phase = np.cumsum(2.0 * np.pi * freqs / _GEN_SR)
    return np.exp(1j * phase).astype(np.complex64)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    out_base = sys.argv[1] if len(sys.argv) > 1 else 'samples/pocsag_test'

    # RICs chosen so frame_num works well (see notes in module docstring)
    messages = [
        (100200, 0, '12345',                             True),   # numeric
        (200304, 3, 'TEST 1234',                         False),  # alpha short (frame_num=0)
        (300400, 3, 'HELLO FROM SDRTERM POCSAG DECODER', False),  # alpha long (frame_num=0)
    ]

    n_gen_out = int(_GEN_SR * DURATION)
    iq_gen    = np.zeros(n_gen_out, dtype=np.complex64)
    pos       = int(_GEN_SR * 0.5)

    for ric, func, text, is_numeric in messages:
        bits = _build_frame(ric, func, text, is_numeric)
        iq_frame = _bits_to_iq(bits)
        end = pos + len(iq_frame)
        if end > n_gen_out:
            break
        iq_gen[pos:end] = iq_frame
        pos = end + int(_GEN_SR * 1.0)   # 1 s gap between messages

    # 48 kHz → 250 kHz (125/24 ratio)
    iq_250k = resample_poly(iq_gen, 125, 24)
    n_out   = int(SR * DURATION)
    if len(iq_250k) >= n_out:
        iq_clean = iq_250k[:n_out].astype(np.complex64)
    else:
        iq_clean = np.pad(iq_250k, (0, n_out - len(iq_250k))).astype(np.complex64)

    # AWGN
    sig_pwr   = float(np.mean(np.abs(iq_clean) ** 2))
    noise_amp = np.sqrt(sig_pwr / (2.0 * 10 ** (SNR_DB / 10.0)))
    rng       = np.random.default_rng(42)
    noise = noise_amp * (rng.standard_normal(n_out)
                         + 1j * rng.standard_normal(n_out)).astype(np.complex64)
    iq = (iq_clean + noise).astype(np.complex64)

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
            'core:recorder':    'SDRTerm gen_pocsag_test.py',
            'core:description': (
                'Synthetic POCSAG 2-FSK {} baud ±{}Hz SNR={} dB'.format(
                    BAUD, DEVIATION_HZ, SNR_DB)
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
            'core:description':  'POCSAG 2-FSK {} baud  {} messages'.format(
                BAUD, len(messages)),
        }],
    }
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)

    print('Wrote {} ({:.1f} MB)'.format(data_path, os.path.getsize(data_path) / 1e6))
    print('Wrote {}'.format(meta_path))
    print()
    print('Signal: 2-FSK  {} baud  ±{} Hz  SNR={} dB'.format(BAUD, DEVIATION_HZ, SNR_DB))
    print('Messages: {}'.format(len(messages)))
    for ric, func, text, is_numeric in messages:
        kind = 'num  ' if is_numeric else 'alpha'
        print('  RIC:{:7d}  F{}  {}  {}'.format(ric, func, kind, text))
    print()
    print('Replay:')
    print('  uv run python main.py --file {} --bw 250000 --f 439.9875M'.format(data_path))
    print('Then: activate pocsag plugin (p menu) and switch to its tab.')


if __name__ == '__main__':
    main()
