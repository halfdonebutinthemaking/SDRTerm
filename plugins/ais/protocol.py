"""
AIS protocol decode chain — pure functions, no curses.

Pipeline (per channel, after the baseband IQ reaches this module):

  complex baseband  →  gmsk_demod()   →  raw bit list (NRZI-encoded, pre-destuff)
  raw bit list      →  nrzi_decode()  →  decoded bit list (still bit-stuffed)
  decoded bit list  →  hdlc_frames()  →  (payload_bytes, crc_ok) pairs
  payload_bytes     →  parse_ais()    →  dict (fields vary by message type)

Spec references
───────────────
ITU-R M.1371-5 — Technical characteristics for an automatic identification
                 system (AIS) using time-division multiple access.

Key framing choices (same as HDLC, same as VDL2 — kept duplicated here
to keep the plugin self-contained; refactor to a shared module later):

* Flag byte 0x7E (`01111110`), LSB-first on the wire (HDLC convention).
* Bit stuffing: a 0 is inserted after every five consecutive 1s inside
  the frame (between flags).
* FCS: CRC-16-CCITT **reflected** — poly 0x8408, init 0xFFFF, XOR-out
  0xFFFF (same variant VDL2 uses; standard HDLC FCS).

AIS-specific bits
─────────────────
* **NRZI** encoding (ITU-R M.1371 §3.3.6.3): a `0` on the application
  stream causes a transition on the wire, a `1` causes no transition.
  Decoder: `d[n] = 1 if raw[n] == raw[n-1] else 0`.
* **Bit order within the AIS payload is MSB-first** at the field level,
  but HDLC transmits LSB-first within each byte.  After HDLC extraction
  we therefore bit-reverse each payload byte so field accessors can
  read ranges MSB-first in the natural order.
"""
from __future__ import annotations

import numpy as np


# ── GMSK demodulator ──────────────────────────────────────────────────────────
#
# AIS is GMSK 9600 bps with BT ≈ 0.4.  We use the classic frequency-
# discriminator approach: compute per-sample phase delta, smooth over
# one bit period with a boxcar, then slice at bit centres.  Good enough
# at ≥ 10 dB SNR which is typical for coastal reception.

def gaussian_pulse_response(bt: float, sps: int, span_bits: int = 4) -> np.ndarray:
    """Discrete Gaussian pulse response for a GMSK receiver's matched
    filter.  Length = `span_bits * sps + 1` taps.  Normalised to unit
    sum so DC gain is 1.

    This is the Gaussian low-pass that follows the frequency
    discriminator in a discriminator-based GMSK receiver.  It
    integrates the instantaneous frequency over roughly one bit
    period, weighted by the matched pulse shape; using a Gaussian
    tuned to the transmitter's BT product improves bit-error rate
    over a 1-bit boxcar by several dB at the AIS SNR range
    (~10-16 dB) that coastal reception actually produces.
    """
    # σ in sample units such that σ_t = sqrt(ln 2) / (2 π BT) in bit
    # periods — the standard GMSK pulse-width definition where BT is
    # the 3 dB bandwidth × bit duration product.
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt) * sps
    n     = span_bits * sps + 1
    t     = np.arange(n) - n // 2
    h     = np.exp(-(t ** 2) / (2 * sigma ** 2))
    return (h / h.sum()).astype(np.float32)


def gmsk_demod(baseband: np.ndarray, sps: int,
               prev_iq: complex | None = None,
               sym_offset: int = 0,
               filter_state: np.ndarray | None = None,
               mu: float = 0.0,
               prev_soft: float = 0.0,
               bt: float = 0.4,
               mm_gain: float = 0.01,
               ) -> tuple[list[int], complex, int, np.ndarray, float, float]:
    """
    Demodulate GMSK baseband to raw NRZI bits with Mueller-Müller
    symbol-timing recovery.

    `baseband` must already be centred near DC and resampled so that
    `sps` samples span exactly one bit.  Returns a tuple
    `(bits, new_prev_iq, new_sym_offset, new_filter_state, new_mu,
    new_prev_soft)` where the first four are the same cross-chunk
    continuity state as before, plus:

      * `new_mu` is the floating-point sub-sample timing offset
        tracked by the Mueller-Müller TED — the error-driven correction
        on top of the integer `sym_offset`.  Carried forward so the
        timing loop stays locked across chunks instead of resetting
        every ~16 ms.
      * `new_prev_soft` is the last matched-filter sample used as the
        "previous symbol" seed for the next chunk's first M&M update.

    How the TED works:

        error = (y[n-1] - y[n+1]) * sign(y[n])

    with y[n] the matched-filter output at bit centre.  Positive error
    means the current sample is TOO LATE and timing should retard;
    negative means too early.  We integrate the error at gain `mm_gain`
    (0.01 is a conservative start — fast enough to lock on AIS's 24-bit
    preamble, slow enough not to be rattled by noise mid-frame) and
    quantise to integer sample steps.

    First-call usage: pass `prev_iq=None`, `sym_offset=sps // 2`,
    `filter_state=None`, `mu=0.0`, `prev_soft=0.0`.
    """
    if len(baseband) < 2:
        empty = np.zeros(0, dtype=np.float32)
        return ([], prev_iq, sym_offset,
                filter_state if filter_state is not None else empty,
                mu, prev_soft)

    # ── frequency discriminator with cross-chunk continuity ─────────
    if prev_iq is not None:
        baseband = np.concatenate([[prev_iq], baseband])
    new_prev_iq = complex(baseband[-1])

    phi = np.angle(baseband[1:] * np.conj(baseband[:-1])).astype(np.float32)

    # ── Gaussian matched filter with cross-chunk history ────────────
    kernel      = gaussian_pulse_response(bt, sps)
    kernel_tail = len(kernel) - 1
    if filter_state is None:
        filter_state = np.zeros(kernel_tail, dtype=np.float32)
    history = filter_state
    stream  = np.concatenate([history, phi]).astype(np.float32)
    smoothed = np.convolve(stream, kernel, mode='valid')
    new_filter_state = (phi[-kernel_tail:].copy() if len(phi) >= kernel_tail
                       else np.concatenate([history[-(kernel_tail - len(phi)):], phi]))

    # ── Mueller-Müller timing recovery + bit slicing ────────────────
    # The sampling pointer is `sym_offset + mu`, incremented by `sps`
    # each symbol.  M&M nudges `mu` by `-mm_gain * error`, which is
    # quantised when it rolls past ±0.5 of a sample.
    bits = []
    n_out = len(smoothed)
    pos   = float(sym_offset) + mu
    y_prev = prev_soft
    while True:
        idx = int(round(pos))
        if idx >= n_out:
            break
        y = float(smoothed[idx])
        # Decide early so we can use sign(y) for the TED.
        bits.append(1 if y > 0 else 0)
        # Need the NEXT symbol sample to compute the M&M error for THIS
        # one.  Peek ahead; if we can't, don't update timing this chunk.
        idx_next = int(round(pos + sps))
        if idx_next < n_out:
            y_next = float(smoothed[idx_next])
            err    = (y_prev - y_next) * (1.0 if y > 0 else -1.0)
            mu    -= mm_gain * err
        y_prev = y
        pos   += sps

    # Convert the resulting float pos back to (sym_offset, mu) for the
    # next chunk.  Keep sym_offset in [0, sps) and mu in (-0.5, 0.5].
    pos_mod = pos - n_out
    new_sym_offset = int(round(pos_mod)) % sps
    new_mu         = pos_mod - int(round(pos_mod))
    # Clamp mu to a sane range in case the loop ran away on noise.
    if abs(new_mu) > 0.5:
        new_mu = max(-0.5, min(0.5, new_mu))

    return (bits, new_prev_iq, new_sym_offset, new_filter_state,
            new_mu, float(y_prev))


# ── NRZI ──────────────────────────────────────────────────────────────────────

def nrzi_decode(bits: list[int]) -> list[int]:
    """AIS NRZI decode: 0 = transition, 1 = no transition.

    `decoded[n] = 1 if bits[n] == bits[n-1] else 0`.  The first input
    bit has no predecessor and is consumed as the reference; the output
    is one bit shorter than the input.
    """
    if len(bits) < 2:
        return []
    out  = []
    prev = bits[0]
    for b in bits[1:]:
        out.append(1 if b == prev else 0)
        prev = b
    return out


def nrzi_encode(bits: list[int], initial: int = 0) -> list[int]:
    """Inverse of `nrzi_decode`, used by the tests.  `initial` is the
    first wire-level bit, prepended to the output so the full stream
    round-trips through `nrzi_decode(nrzi_encode(x))`."""
    out    = [initial]
    state  = initial
    for b in bits:
        state = state if b == 1 else 1 - state
        out.append(state)
    return out


# ── CRC-16-CCITT (reflected) — same variant as VDL2 ──────────────────────────

def crc16_ccitt(data: bytes) -> int:
    """HDLC FCS: poly 0x8408 (reflected 0x1021), init 0xFFFF, XOR-out 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else crc >> 1
    return crc ^ 0xFFFF


# ── HDLC framing (flag detect + destuff + FCS) ───────────────────────────────

_FLAG_BITS = [0, 1, 1, 1, 1, 1, 1, 0]   # 0x7E LSB-first on the wire


def _bits_to_bytes_lsb(bits: list[int]) -> bytes:
    """Pack LSB-first bits into bytes (HDLC convention)."""
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        b = 0
        for j in range(8):
            b |= bits[i + j] << j
        out.append(b)
    return bytes(out)


def _destuff(raw: list[int]) -> list[int] | None:
    """Remove bit-stuffed zeros.  Returns None if a stuffing violation is
    found (six consecutive 1s without an intervening 0).

    The subtle bit: at count == 5, HDLC guarantees the NEXT wire bit is
    the stuffed 0.  Verify that before skipping — if it's actually a 1,
    we're looking at 6 consecutive 1s (frame abort / unexpected flag)
    and must bail.  Previously this function skipped unconditionally
    and silently swallowed the 6th 1, producing frames that were one
    bit short and would never pass CRC.
    """
    out, count = [], 0
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 1:
            count += 1
            if count == 6:
                return None
            out.append(b)
            if count == 5:
                # Peek at the stuffed slot.  Must be 0, else six-in-row.
                if i + 1 >= len(raw):
                    return None
                if raw[i + 1] == 1:
                    return None
                i += 1          # skip the verified stuffed 0
                count = 0
        else:
            count = 0
            out.append(b)
        i += 1
    return out


def _find_flag(bits: list[int], start: int) -> int:
    """Index of next FLAG_BITS in bits[start:], or -1 if none."""
    n = len(bits)
    for i in range(start, n - 7):
        if bits[i:i + 8] == _FLAG_BITS:
            return i
    return -1


def hdlc_frames_diag(bits: list[int]):
    """Diagnostic variant of `hdlc_frames` that yields a status for EVERY
    candidate inter-flag region, not just the ones that destuff cleanly.

    Yields `(payload | None, crc_ok: bool, status: str, raw_len: int)`
    for every pair of flags found, where `status` is one of:

      * `'crc_ok'`      — destuff OK, CRC matched; `payload` is set.
      * `'crc_fail'`    — destuff OK, CRC wrong;   `payload` is set.
      * `'destuff_fail'`— bit-stuffing violation;  `payload` is None.
      * `'too_short'`   — < 40 bits or not byte-aligned.
      * `'short_frame'` — bytes OK but < 3 (no room for FCS).

    `raw_len` is the number of inter-flag bits before destuffing.
    Used by the plugin's diagnostic console (press 'd' on the AIS tab).
    """
    i = 0
    while True:
        start = _find_flag(bits, i)
        if start < 0:
            break
        end_of_open = start
        while _find_flag(bits, end_of_open) == end_of_open:
            end_of_open += 8
        close = _find_flag(bits, end_of_open)
        if close < 0:
            break

        raw     = bits[end_of_open:close]
        raw_len = len(raw)
        cooked  = _destuff(raw)

        if cooked is None:
            yield None, False, 'destuff_fail', raw_len
        elif len(cooked) < 40 or len(cooked) % 8 != 0:
            yield None, False, 'too_short', raw_len
        else:
            frame = _bits_to_bytes_lsb(cooked)
            if len(frame) < 3:
                yield None, False, 'short_frame', raw_len
            else:
                payload = frame[:-2]
                fcs_rx  = frame[-2] | (frame[-1] << 8)
                crc_ok  = (crc16_ccitt(payload) == fcs_rx)
                status  = 'crc_ok' if crc_ok else 'crc_fail'
                yield payload, crc_ok, status, raw_len

        i = close


def hdlc_frames(bits: list[int]):
    """Scan a bit list for HDLC frames.

    Yields `(payload: bytes, crc_ok: bool)` for every candidate frame.
    `payload` is the inter-flag content with the trailing 2-byte FCS
    removed.  Consecutive opening flags (interframe fill) are silently
    skipped.  AIS slot payloads are 168–424 bits (21–53 bytes) + 16-bit
    FCS; we accept any ≥ 24 bits of payload (3 bytes) to leave room for
    short administrative types.
    """
    i = 0
    while True:
        start = _find_flag(bits, i)
        if start < 0:
            break
        end_of_open = start
        while _find_flag(bits, end_of_open) == end_of_open:
            end_of_open += 8
        close = _find_flag(bits, end_of_open)
        if close < 0:
            break

        raw    = bits[end_of_open:close]
        cooked = _destuff(raw)

        if (cooked is not None
                and len(cooked) >= 40           # ≥ 3-byte payload + 2-byte FCS
                and len(cooked) % 8 == 0):
            frame = _bits_to_bytes_lsb(cooked)
            if len(frame) >= 3:
                payload  = frame[:-2]
                fcs_rx   = frame[-2] | (frame[-1] << 8)
                crc_ok   = (crc16_ccitt(payload) == fcs_rx)
                yield payload, crc_ok

        i = close


# ── Bit-order flip (HDLC LSB-first → AIS field MSB-first) ────────────────────

_REV8 = bytes(int(f'{b:08b}'[::-1], 2) for b in range(256))


def ais_payload_from_hdlc(payload: bytes) -> bytes:
    """Convert an HDLC-extracted payload (LSB-first within byte) to the
    MSB-first byte order AIS field decoders expect.  Equivalent to
    bit-reversing each byte."""
    return payload.translate(_REV8)


# ── Bitfield reader for AIS payloads ─────────────────────────────────────────

def _bits(data: bytes, offset: int, width: int) -> int:
    """Unsigned big-endian bit extraction from `data` starting at bit `offset`."""
    v = 0
    for i in range(width):
        byte_i = (offset + i) >> 3
        bit_i  = 7 - ((offset + i) & 7)
        v = (v << 1) | ((data[byte_i] >> bit_i) & 1)
    return v


def _signed(value: int, width: int) -> int:
    """Convert `width`-bit two's-complement to Python int."""
    if value & (1 << (width - 1)):
        value -= 1 << width
    return value


# ── Validity-normalising decoders for individual fields ─────────────────────
#
# Spec-defined "not available" sentinels per ITU-R M.1371 — surface them as
# None so a browser/UI reader can distinguish "unknown" from an actual value.

def _mmsi_class(mmsi: int) -> str:
    """Classify an MMSI per ITU-R M.1371 § 3.1.1 / Annex 1.  Returns a
    short tag the UI can show next to the number — useful for spotting
    SARTs, AtoNs, and other non-ship transmissions, and for flagging
    values that don't match any standard range (which happens with
    test devices, the occasional defective transponder, and whatever
    the Ibiza coast was broadcasting in the live diagnostic).

    Standard MIDs (country prefixes) run 201-775; anything with a MID
    outside that range in a position-sensitive slot is non-standard
    even when the overall format is otherwise plausible.
    """
    if not 0 <= mmsi <= 999_999_999:
        return 'non-standard'
    s = str(mmsi).zfill(9)

    def _valid_mid(three_digits: str) -> bool:
        try:
            return 201 <= int(three_digits) <= 775
        except ValueError:
            return False

    if s.startswith('00'):
        # Coast station: 00 + MID (3) + station (4).  Flag bad MIDs.
        return 'coast-station' if _valid_mid(s[2:5]) else 'non-standard'
    if s.startswith('0'):
        # Group of ships: 0 + MID (3) + group (5).  Flag bad MIDs.
        return 'group-of-ships' if _valid_mid(s[1:4]) else 'non-standard'
    if s[:3] == '111':          return 'sar-aircraft'
    if s[:3] == '970':          return 'sart'
    if s[:3] == '972':          return 'mob'
    if s[:3] == '974':          return 'epirb'
    if s[:3] == '982':          return 'craft-identifier'
    if s[:2] == '99':           return 'aton'
    if s[0] in '2345678' and _valid_mid(s[:3]):
        return 'ship'
    return 'non-standard'


def _heading_or_none(raw: int) -> int | None:
    """9-bit True Heading field: valid 0-359 or 511 = N/A.  Return the
    raw value when valid, None for the N/A sentinel OR anything in the
    reserved 360-510 range (shouldn't be transmitted per spec; often
    comes from a defective transmitter or a decode error)."""
    if 0 <= raw <= 359:
        return raw
    return None


def _cog_or_none(raw: int) -> float | None:
    """12-bit COG: valid 0..3599 (0.0°..359.9°), 3600 = N/A."""
    if raw == 3600:
        return None
    if 0 <= raw <= 3599:
        return raw / 10.0
    return None


def _sog_or_none(raw: int) -> float | None:
    """10-bit SOG: 0..1022 valid (0.0..102.2 kt), 1023 = N/A."""
    if raw == 1023:
        return None
    return raw / 10.0


# ── Position-report parser (Type 1 / 2 / 3) ─────────────────────────────────
#
# ITU-R M.1371-5 Table 44.  168-bit payload.

def _parse_position_report(data: bytes) -> dict:
    """Decode a Class A position report (message types 1, 2, 3)."""
    mmsi = _bits(data, 8, 30)
    return {
        'type':      _bits(data,   0,  6),
        'repeat':    _bits(data,   6,  2),
        'mmsi':      mmsi,
        'mmsi_class': _mmsi_class(mmsi),
        'nav_status': _bits(data,  38,  4),
        'rot_raw':   _signed(_bits(data, 42, 8), 8),
        'sog':       _sog_or_none(_bits(data, 50, 10)),
        'pos_acc':   _bits(data,  60,  1),
        'lon':       _signed(_bits(data, 61, 28), 28) / 600_000.0,  # degrees
        'lat':       _signed(_bits(data, 89, 27), 27) / 600_000.0,
        'cog':       _cog_or_none(_bits(data, 116, 12)),
        'heading':   _heading_or_none(_bits(data, 128, 9)),
        'timestamp': _bits(data, 137,  6),            # UTC second
    }


# ── Class B position report parser (Type 18) ────────────────────────────────
#
# ITU-R M.1371-5 Table 51.  168-bit payload.

def _parse_class_b(data: bytes) -> dict:
    mmsi = _bits(data, 8, 30)
    return {
        'type':      _bits(data,   0,  6),
        'repeat':    _bits(data,   6,  2),
        'mmsi':      mmsi,
        'mmsi_class': _mmsi_class(mmsi),
        'sog':       _sog_or_none(_bits(data, 46, 10)),
        'pos_acc':   _bits(data,  56,  1),
        'lon':       _signed(_bits(data, 57, 28), 28) / 600_000.0,
        'lat':       _signed(_bits(data, 85, 27), 27) / 600_000.0,
        'cog':       _cog_or_none(_bits(data, 112, 12)),
        'heading':   _heading_or_none(_bits(data, 124, 9)),
        'timestamp': _bits(data, 133, 6),
    }


def parse_ais(payload: bytes) -> dict | None:
    """Dispatch an HDLC-extracted AIS payload to the appropriate per-type
    decoder.  Returns a dict with at least `type` + `mmsi`, or `None`
    if the payload is too short to read a type field."""
    if len(payload) < 1:
        return None
    msg_type = payload[0] >> 2        # top 6 bits, MSB-first
    if msg_type in (1, 2, 3):
        if len(payload) < 21:
            return None
        return _parse_position_report(payload)
    if msg_type == 18:
        if len(payload) < 21:
            return None
        return _parse_class_b(payload)
    # Fallback: just surface the type + MMSI so higher layers can at
    # least count messages per vessel.
    if len(payload) < 5:
        return {'type': msg_type}
    return {
        'type': msg_type,
        'mmsi': _bits(payload, 8, 30) if len(payload) >= 5 else None,
    }
