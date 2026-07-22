"""
Classic POCSAG (paging) decoder plugin.

POCSAG is a wireless paging protocol using direct 2-FSK modulation at
512, 1200, or 2400 baud with ±4.5 kHz deviation.  Mark (bit = 1) is negative
deviation, space (bit = 0) is positive deviation.  Codewords are protected
by BCH(31,21) and grouped into 16-codeword batches, each preceded by the
32-bit sync codeword 0x7CD215D8.

Signal path:
  IQ (250 kHz) → shift to peak → decimate to 50 kHz → FM discriminate
  → auto-slice → try each baud rate × several clock phases × both polarities
  → sync search → 16 codewords per batch
  → BCH(31,21) correct → address/message parse → numeric or alphanumeric text
"""

import time
from collections import deque

import numpy as np
from scipy.signal import decimate as sp_decimate

from core import Decoder, AppState
from .bch import decode_codeword

# ── signal constants ─────────────────────────────────────────────────────────
_DECIM_FACTOR    = 5                        # 250 kHz IQ → 50 kHz baseband
_AUDIO_SR        = 50_000
_BAUD_RATES      = (512, 1200, 2400)
_N_CLOCK_PHASES  = 4                        # per baud rate

# ── frame constants ──────────────────────────────────────────────────────────
_SYNC_WORD       = 0x7CD215D8
_IDLE_WORD       = 0x7A89C197
_SYNC_LEN        = 32
_BATCH_CW_COUNT  = 16
_BATCH_BIT_LEN   = _SYNC_LEN + _BATCH_CW_COUNT * 32   # 544
_MAX_SYNC_ERRORS = 1     # 32-bit sync ⇒ 1-bit tolerance keeps noise false-matches rare

# Sync codeword as a bit array (MSB first) — used by the vectorised search
_SYNC_BITS_ARR = np.array(
    [(_SYNC_WORD >> (31 - k)) & 1 for k in range(_SYNC_LEN)], dtype=np.uint8)

# ── message-decoding constants ───────────────────────────────────────────────
# 4-bit BCD → character map for numeric messages (bits transmitted LSB first)
_NUMERIC_CHARSET = '0123456789SU -)('

# ── noise-rejection thresholds ───────────────────────────────────────────────
# Reject a batch if fewer than this many codewords are idle or need zero BCH
# correction.  Real POCSAG batches are mostly idle padding + a few clean
# address/message codewords; pure noise gets BCH-corrected into scattered
# "valid" garbage that rarely produces clean idles.
_MIN_CLEAN_CW    = 8    # out of 16

# Alpha-message printability floor: fraction of chars that must be letters,
# digits, or space to be considered a real message rather than noise.
_MIN_ALPHA_RATIO = 0.60

# Numeric-message digit floor: real numeric pages are mostly digits;
# noise decodes are heavy on the punctuation entries of the BCD charset.
_MIN_DIGIT_RATIO = 0.50

# ── buffering ────────────────────────────────────────────────────────────────
# 3 s at 50 kHz is enough to hold the longest batch (~1.1 s at 512 baud)
# even if it starts near the end of one process() call and ends in the next.
_AUDIO_BUF_MAX   = _AUDIO_SR * 3
_MAX_MESSAGES    = 128


# ── FM demod + bit slicing ───────────────────────────────────────────────────

def _fm_demod(iq_baseband: np.ndarray) -> np.ndarray:
    """Discriminator output = phase difference between consecutive samples."""
    if len(iq_baseband) < 2:
        return np.zeros(0, dtype=np.float32)
    return np.angle(iq_baseband[1:] * np.conj(iq_baseband[:-1])).astype(np.float32)


def _slice_bits(audio: np.ndarray, sps: float, phase: float,
                invert: bool) -> np.ndarray:
    """Sample discriminator output at bit centres.

    sps    : samples per bit (may be fractional)
    phase  : starting offset within the first bit period
    invert : True → bit=1 when audio<0 (POCSAG convention, mark=negative)
    """
    if len(audio) < int(sps + phase) + 1:
        return np.empty(0, dtype=np.uint8)
    n_bits = int((len(audio) - phase - 1) / sps)
    if n_bits < 1:
        return np.empty(0, dtype=np.uint8)
    idx = phase + np.arange(n_bits) * sps
    idx = np.clip(idx.astype(np.int64), 0, len(audio) - 1)
    values = audio[idx]
    return (values < 0).astype(np.uint8) if invert else (values > 0).astype(np.uint8)


# ── sync search + batch parsing ──────────────────────────────────────────────

def _bits_to_int_msb(bits: np.ndarray, offset: int, n: int) -> int:
    v = 0
    for k in range(n):
        v = (v << 1) | int(bits[offset + k])
    return v


def _find_sync(bits: np.ndarray) -> list:
    """Return every bit position where the sync codeword appears.

    Vectorised: a single sliding-window compare over the whole bit array,
    ~1000× faster than a Python loop and releases the GIL so the main
    spectrum/waterfall thread isn't starved.
    """
    if len(bits) < _SYNC_LEN:
        return []
    windows   = np.lib.stride_tricks.sliding_window_view(bits, _SYNC_LEN)
    n_matches = (windows == _SYNC_BITS_ARR).sum(axis=1)
    return np.where(n_matches >= _SYNC_LEN - _MAX_SYNC_ERRORS)[0].tolist()


def _decode_batch(bits: np.ndarray, sync_pos: int) -> list:
    """Decode the 16 codewords following the sync word into message dicts.

    Returns an empty list if the batch fails the quality check (too many
    codewords needed correction — probably noise, not a real batch).
    """
    messages    = []
    current_msg = None
    n_clean_cw  = 0     # idle codewords + zero-error BCH decodes

    for cw_i in range(_BATCH_CW_COUNT):
        cw_start = sync_pos + _SYNC_LEN + cw_i * 32
        if cw_start + 32 > len(bits):
            break
        raw_word = _bits_to_int_msb(bits, cw_start, 32)

        # Idle codewords terminate the current message and are skipped
        if bin(raw_word ^ _IDLE_WORD).count('1') <= 2:
            n_clean_cw += 1
            if current_msg is not None:
                messages.append(_finalize_msg(current_msg))
                current_msg = None
            continue

        word, n_err, parity_ok = decode_codeword(raw_word)
        if n_err < 0:
            if current_msg is not None:
                current_msg['has_errors'] = True
            continue
        if n_err == 0 and parity_ok:
            n_clean_cw += 1

        indicator = (word >> 31) & 1
        if indicator == 0:
            # Address codeword — starts a new message
            if current_msg is not None:
                messages.append(_finalize_msg(current_msg))
            addr18    = (word >> 13) & 0x3FFFF
            func2     = (word >> 11) & 0x3
            frame_num = cw_i // 2
            ric       = (addr18 << 3) | frame_num
            current_msg = {
                'ric':        ric,
                'func':       func2,
                'bits':       [],
                'has_errors': (n_err > 0) or (not parity_ok),
            }
        else:
            # Message codeword — 20 payload bits, transmission order
            if current_msg is not None:
                for k in range(20):
                    current_msg['bits'].append((word >> (30 - k)) & 1)
                if n_err > 0 or not parity_ok:
                    current_msg['has_errors'] = True

    if current_msg is not None:
        messages.append(_finalize_msg(current_msg))

    # Batch quality gate: pure-noise batches rarely produce this many idle
    # or zero-error codewords.
    if n_clean_cw < _MIN_CLEAN_CW:
        return []

    return messages


# ── payload decoding ─────────────────────────────────────────────────────────

def _decode_alphanumeric(bits: list) -> str:
    """7-bit ASCII, LSB first per character; NUL/ETX/EOT terminate."""
    chars = []
    for i in range(0, len(bits) - 6, 7):
        c = 0
        for k in range(7):
            c |= bits[i + k] << k
        if c in (0x00, 0x03, 0x04):
            break
        chars.append(chr(c) if 0x20 <= c < 0x7F else '.')
    return ''.join(chars)


def _decode_numeric(bits: list) -> str:
    """4-bit BCD digit, LSB first per nibble."""
    chars = []
    for i in range(0, len(bits) - 3, 4):
        d = bits[i] | (bits[i + 1] << 1) | (bits[i + 2] << 2) | (bits[i + 3] << 3)
        chars.append(_NUMERIC_CHARSET[d])
    return ''.join(chars)


def _finalize_msg(msg: dict) -> dict:
    """Pick numeric or alphanumeric decoding based on function code."""
    # Function 1 (tone A) and function 2 (tone B) are beep-only per POCSAG spec —
    # any payload that appears after their address codeword is almost always
    # spurious BCH-corrected noise. Drop the payload.
    if msg['func'] in (1, 2):
        return {
            'ric':        msg['ric'],
            'func':       msg['func'],
            'mode':       'beep',
            'text':       '',
            'has_errors': msg['has_errors'],
        }

    bits = msg['bits']
    if msg['func'] == 3:
        text, mode = _decode_alphanumeric(bits), 'alpha'
    else:                                          # func == 0
        text, mode = _decode_numeric(bits), 'numeric'

    trimmed = text.rstrip(' \x00\x03\x04.')

    # Text-quality gate: reject decoded strings that look like noise
    if trimmed:
        if mode == 'alpha':
            readable = sum(1 for c in trimmed if c.isalnum() or c == ' ')
            if readable / len(trimmed) < _MIN_ALPHA_RATIO:
                trimmed = ''
        else:  # numeric
            digits = sum(1 for c in trimmed if c.isdigit())
            if digits / len(trimmed) < _MIN_DIGIT_RATIO:
                trimmed = ''

    return {
        'ric':        msg['ric'],
        'func':       msg['func'],
        'mode':       mode,
        'text':       trimmed,
        'has_errors': msg['has_errors'],
    }


# ── Plugin ───────────────────────────────────────────────────────────────────

class PocsagDecoder(Decoder):
    name            = 'pocsag'
    key             = 'g'
    key_help        = 'r=clear'
    min_sample_rate = 250_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._messages  = deque(maxlen=_MAX_MESSAGES)
        self._seen      = set()
        self._audio_buf = np.empty(0, dtype=np.float32)

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._seen.clear()
        self._audio_buf = np.empty(0, dtype=np.float32)

    def stop(self) -> None:
        self._messages.clear()
        self._seen.clear()
        self._audio_buf = np.empty(0, dtype=np.float32)

    # ── process ─────────────────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:

        # Shift to peak_marker frequency if available
        peak      = (results or {}).get('peak_marker', {})
        peak_hz   = peak.get('peak_hz', state.center_hz)
        offset_hz = peak_hz - state.center_hz
        if abs(offset_hz) > 1.0:
            t       = np.arange(len(samples), dtype=np.float32) / state.bw_hz
            samples = (samples * np.exp(-2j * np.pi * offset_hz * t)).astype(np.complex64)

        # 250 kHz → 50 kHz
        try:
            re = sp_decimate(samples.real.astype(np.float64), _DECIM_FACTOR,
                             ftype='fir', zero_phase=True).astype(np.float32)
            im = sp_decimate(samples.imag.astype(np.float64), _DECIM_FACTOR,
                             ftype='fir', zero_phase=True).astype(np.float32)
        except Exception:
            return {'messages': list(self._messages), 'n_messages': len(self._messages)}
        iq_bb = (re + 1j * im).astype(np.complex64)

        # FM discriminate and accumulate
        chunk_audio = _fm_demod(iq_bb)
        if len(chunk_audio) < 100:
            return {'messages': list(self._messages), 'n_messages': len(self._messages)}
        self._audio_buf = np.concatenate([self._audio_buf, chunk_audio])
        if len(self._audio_buf) > _AUDIO_BUF_MAX:
            self._audio_buf = self._audio_buf[-_AUDIO_BUF_MAX:]

        # Wait until we have at least one batch worth of the slowest baud rate
        min_samples = int(_AUDIO_SR * _BATCH_BIT_LEN / _BAUD_RATES[0])
        if len(self._audio_buf) < min_samples:
            return {'messages': list(self._messages), 'n_messages': len(self._messages)}

        audio_ac = self._audio_buf - float(np.mean(self._audio_buf))

        new_count = 0
        for baud in _BAUD_RATES:
            sps = _AUDIO_SR / baud
            for phase_i in range(_N_CLOCK_PHASES):
                phase = phase_i * sps / _N_CLOCK_PHASES
                for invert in (True, False):
                    bits = _slice_bits(audio_ac, sps, phase, invert)
                    if len(bits) < _BATCH_BIT_LEN:
                        continue
                    for sync_pos in _find_sync(bits):
                        for msg in _decode_batch(bits, sync_pos):
                            if not msg['text']:
                                continue
                            key = (msg['ric'], msg['text'][:30])
                            if key in self._seen:
                                continue
                            # Always dedupe — a repeat within the 3s ring buffer
                            # is either a real duplicate or a stable false decode.
                            # Either way we only want to show it once.
                            self._seen.add(key)
                            if len(self._seen) > 512:
                                self._seen.pop()
                            msg['ts']   = time.strftime('%H:%M:%S')
                            msg['baud'] = baud
                            self._messages.appendleft(msg)
                            new_count += 1

        return {
            'messages':   list(self._messages),
            'n_messages': len(self._messages),
            'new':        new_count,
        }

    # ── key handling / status / view ────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._messages.clear()
            self._seen.clear()
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        return '[POCSAG {} msg] '.format(result.get('n_messages', 0))

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return
        messages = result.get('messages', [])

        header = 'POCSAG  (auto-baud 512 / 1200 / 2400)  [r=clear]'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        if not messages:
            try:
                screen_obj.addstr(3, 2, 'Listening for POCSAG frames…')
            except curses.error:
                pass
            return

        y = 3
        for msg in messages:
            if y >= rows - 1:
                break
            ts   = msg.get('ts', '??:??:??')
            ric  = msg.get('ric', 0)
            func = msg.get('func', 0)
            baud = msg.get('baud', 0)
            mode = msg.get('mode', '?')
            text = msg.get('text', '')
            errs = msg.get('has_errors', False)

            prefix = '[{}] RIC:{:7d} F{}  {:>4}bps  {:<7}  '.format(
                ts, ric, func, baud, mode)
            line = prefix + text
            if len(line) > cols - 4:
                line = line[:cols - 7] + '…'

            attr = curses.A_DIM if errs else curses.A_BOLD
            try:
                screen_obj.addstr(y, 2, line, attr)
            except curses.error:
                pass
            y += 1

    # ── state persistence ───────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:
        pass
