"""
VDL Mode 2 protocol decode chain — pure functions, no curses.

Pipeline:
  complex symbols  →  d8psk_demod()  →  bit list
  bit list         →  descramble()   →  bit list
  bit list         →  hdlc_frames()  →  (payload_bytes, crc_ok) pairs
  payload_bytes    →  parse_avlc()   →  dict with text field

Notes on spec conformance
─────────────────────────
* Scrambler: G(x) = 1 + x + x⁶ self-synchronising scrambler, applied to
  the complete HDLC bit stream (including flags) after bit stuffing.
  descramble() / scramble() are implemented below.
* CRC variant: CRC-CCITT reflected (poly 0x8408, init 0xFFFF, XOR-out 0xFFFF)
  — this is the standard HDLC FCS.
* Bit order: LSB first within each byte, matching HDLC convention.
"""
import numpy as np

# ── VDL2 self-synchronising scrambler  G(x) = 1 + x + x⁶ ─────────────────
#
# TX:  s[n] = d[n] ^ s[n-1] ^ s[n-6]   (feedback from scrambled output)
# RX:  d[n] = r[n] ^ r[n-1] ^ r[n-6]   (feedback from received bits)
#
# The RX form is self-synchronising: after 6 bits of received data the
# descrambler is in sync regardless of its initial state.  Applied to the
# complete HDLC bit stream (flags + stuffed payload) before flag search.


def scramble(bits: list[int]) -> list[int]:
    """Scramble a bit list with the VDL2 self-synchronising scrambler."""
    out: list[int] = []
    for i, d in enumerate(bits):
        s = d
        if i >= 1: s ^= out[i - 1]
        if i >= 6: s ^= out[i - 6]
        out.append(s)
    return out


def descramble(bits: list[int],
               ctx: list[int] | None = None) -> tuple[list[int], list[int]]:
    """
    Descramble a bit list.  ctx is the last 6 *received* bits from the
    previous call; pass it back on the next call for cross-chunk continuity.
    Returns (descrambled_bits, new_ctx).
    """
    if ctx is None:
        ctx = [0] * 6
    received = list(ctx) + list(bits)          # prepend context
    out = [received[i + 6] ^ received[i + 5] ^ received[i]
           for i in range(len(bits))]
    return out, received[-6:]                   # return last 6 received bits as new ctx


# ── D8PSK tables ───────────────────────────────────────────────────────────

# Gray encode: tribit integer (0-7) → phase-change index (0-7)
# gray_enc(n) = n XOR (n >> 1)
_GRAY_ENC = [i ^ (i >> 1) for i in range(8)]          # [0,1,3,2,6,7,5,4]

# Gray decode: phase-change index → tribit integer
_GRAY_DEC = [0] * 8
for _n, _g in enumerate(_GRAY_ENC):
    _GRAY_DEC[_g] = _n                                  # [0,1,3,2,7,6,4,5]


def d8psk_demod(syms: np.ndarray) -> list[int]:
    """
    Differential 8PSK demodulation: complex symbol array → bit list.

    Phase difference between consecutive symbols → nearest multiple of π/4
    → Gray decode → tribit integer → 3 bits (LSB first).

    syms[0] is the reference symbol (not decoded); output has 3*(len-1) bits.
    """
    bits = []
    for i in range(1, len(syms)):
        dphi = float(np.angle(syms[i] * np.conj(syms[i - 1])))
        if dphi < 0:
            dphi += 2 * np.pi
        gray_idx = int(round(dphi / (np.pi / 4))) % 8
        tribit   = _GRAY_DEC[gray_idx]
        bits.append(tribit & 1)
        bits.append((tribit >> 1) & 1)
        bits.append((tribit >> 2) & 1)
    return bits


# ── CRC-CCITT (reflected) ──────────────────────────────────────────────────

def _crc_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else crc >> 1
    return crc ^ 0xFFFF


# ── HDLC helpers ───────────────────────────────────────────────────────────

_FLAG_BITS = [0, 1, 1, 1, 1, 1, 1, 0]   # 0x7E LSB-first


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        b = 0
        for j in range(8):
            b |= bits[i + j] << j
        out.append(b)
    return bytes(out)


def _destuff(raw: list[int]) -> list[int] | None:
    """Remove bit-stuffed zeros.  Returns None if a stuffing violation is found."""
    out, count = [], 0
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 1:
            count += 1
            if count == 6:
                return None     # six consecutive 1s without a 0 → abort
            out.append(b)
            if count == 5:
                i += 1          # skip the stuffed 0
                count = 0
        else:
            count = 0
            out.append(b)
        i += 1
    return out


def _find_flag(bits: list[int], start: int) -> int:
    """Return index of next FLAG_BITS in bits[start:], or -1."""
    n = len(bits)
    for i in range(start, n - 7):
        if bits[i:i + 8] == _FLAG_BITS:
            return i
    return -1


# ── Public HDLC frame extractor ────────────────────────────────────────────

def hdlc_frames(bits: list[int]):
    """
    Scan a bit list for HDLC frames.

    Yields (payload: bytes, crc_ok: bool) for every candidate frame found.
    Consecutive flags (inter-frame fill) are silently skipped.
    """
    i = 0
    while True:
        start = _find_flag(bits, i)
        if start < 0:
            break
        # Skip consecutive flags
        end_of_open = start
        while _find_flag(bits, end_of_open) == end_of_open:
            end_of_open += 8
        close = _find_flag(bits, end_of_open)
        if close < 0:
            break

        raw    = bits[end_of_open: close]
        cooked = _destuff(raw)

        if (cooked is not None
                and len(cooked) >= 112          # ≥ 14 bytes minimum viable AVLC frame
                and len(cooked) % 8 == 0):      # valid HDLC frames are byte-aligned
            frame = _bits_to_bytes(cooked)
            if len(frame) >= 4:
                payload  = frame[:-2]
                fcs_rx   = frame[-2] | (frame[-1] << 8)
                crc_ok   = (_crc_ccitt(payload) == fcs_rx)
                yield payload, crc_ok

        i = close


# ── AVLC / ACARS parser ────────────────────────────────────────────────────

def parse_avlc(frame: bytes) -> dict | None:
    """
    Parse simplified AVLC frame produced by gen_vdl2_test.py.

    Layout: dest(2) | src(2) | control(1) | proto(1) | payload(…)

    Returns a dict or None if the frame is too short.
    """
    if len(frame) < 6:
        return None
    return {
        'dest':    frame[0:2].hex(':'),
        'src':     frame[2:4].hex(':'),
        'control': frame[4],
        'proto':   frame[5],
        'text':    _decode_text(frame[6:]),
    }


def _decode_text(raw: bytes) -> str:
    """Best-effort ASCII extraction, replacing non-printable bytes with '?'."""
    return ''.join(
        chr(b) if 0x20 <= b < 0x7F else ('?' if b not in (0x0A, 0x0D) else '\n')
        for b in raw
    ).strip()
