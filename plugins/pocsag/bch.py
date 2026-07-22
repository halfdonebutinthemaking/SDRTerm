"""
BCH(31,21) encoder/decoder for POCSAG codewords.

Generator polynomial: g(x) = x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1
This BCH code can correct any error pattern of weight ≤ 2 and detect all
patterns of weight ≤ 3.

Bit numbering (used throughout this module):
  - A 31-bit BCH codeword is a Python int with bit 30 as MSB, bit 0 as LSB.
  - Data bits occupy bits 30..10 (21 bits, MSB = message/address indicator).
  - Parity bits occupy bits 9..0 (10 bits).
  - A 32-bit POCSAG codeword adds one even-parity bit at bit 0, shifting BCH
    bits 30..0 to positions 31..1.
"""

_G = 0x769  # generator polynomial: 0b11101101001, 11 bits (x^10 term = MSB)


def _remainder(poly31: int) -> int:
    """Return poly31 mod g(x) — the 10-bit BCH syndrome."""
    r = poly31
    for i in range(30, 9, -1):
        if r & (1 << i):
            r ^= _G << (i - 10)
    return r & 0x3FF


def encode(data21: int) -> int:
    """Encode 21-bit data into a 31-bit BCH codeword.

    Data occupies bits 30..10 of the returned codeword; the 10 BCH parity
    bits fill bits 9..0.
    """
    shifted = (data21 & 0x1FFFFF) << 10
    return shifted | _remainder(shifted)


def _build_syndrome_table() -> dict:
    """Precompute syndrome → error-mask table for 0, 1, and 2-bit errors."""
    table = {0: 0}
    for i in range(31):
        e = 1 << i
        s = _remainder(e)
        table.setdefault(s, e)
    for i in range(31):
        for j in range(i + 1, 31):
            e = (1 << i) | (1 << j)
            s = _remainder(e)
            table.setdefault(s, e)
    return table


_SYNDROME_TABLE = _build_syndrome_table()


def correct(codeword31: int) -> tuple:
    """Try to correct up to 2 errors in a 31-bit codeword.

    Returns (corrected_codeword, n_errors).  n_errors = -1 if the syndrome
    has no ≤2-error explanation.
    """
    syn = _remainder(codeword31)
    if syn == 0:
        return codeword31, 0
    err = _SYNDROME_TABLE.get(syn)
    if err is None:
        return codeword31, -1
    return codeword31 ^ err, bin(err).count('1')


def decode_codeword(word32: int) -> tuple:
    """Decode a 32-bit POCSAG codeword (31 BCH + 1 even-parity bit).

    Returns (corrected_word32, n_bch_errors, parity_ok).
    n_bch_errors = -1 if the BCH syndrome is unfixable.
    """
    poly31     = word32 >> 1
    parity_bit = word32 & 1
    corrected31, n = correct(poly31)
    if n < 0:
        return word32, -1, False
    total_ones = bin(corrected31).count('1') + parity_bit
    parity_ok  = (total_ones & 1) == 0
    return (corrected31 << 1) | parity_bit, n, parity_ok
