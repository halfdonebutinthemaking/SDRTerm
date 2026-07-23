"""
Classic ACARS decoder plugin.

Signal path:
  IQ (250 kHz) → AM demod → resample to 12 000 Hz → non-coherent FSK demod
  → clock sampling (5 phases) → bit stream → ACARS frame parser → display

ACARS frame structure:
  preamble (16 × 0x2B) + SYN × 2 + SOH + Mode + Reg(7) + . + blk + seq
  + FlightID(6) + STX + text + ETX + BCS(2 chars) + DEL
"""

import math
import time
from collections import deque

import numpy as np
from scipy.signal import resample_poly, butter, sosfilt

# HPF at 300 Hz on the 12 kHz envelope audio: strips DC + slow burst-shape
# variation.  Otherwise the boxcar FSK correlator leaks low-frequency energy
# more into the 1200-Hz (space) arm than the 2400-Hz (mark) arm — because
# space is inside the correlator's passband while mark sits at its first
# null — biasing decision toward SPACE on weak signals.
_HPF_SOS = butter(4, 300, 'hp', fs=12_000, output='sos')

from core import Decoder, AppState

# ── signal constants ─────────────────────────────────────────────────────────
_AUDIO_SR   = 12_000      # Hz after downsampling
_BAUD       = 2_400
_SPB        = _AUDIO_SR // _BAUD   # = 5 samples per bit
_MARK_HZ    = 2_400       # bit = 1
_SPACE_HZ   = 1_200       # bit = 0
_LP_TAPS    = _SPB        # moving-average length for FSK correlator

# ── frame constants ──────────────────────────────────────────────────────────
_SYN = 0x16
_SOH = 0x01
_STX = 0x02
_ETX = 0x03
_DEL = 0x7F

# 8-bit sync pattern: SYN SYN SOH (24 bits, LSB-first per byte)
_SYNC_BITS  = []
for _b in (_SYN, _SYN, _SOH):
    _SYNC_BITS += [(_b >> i) & 1 for i in range(8)]
_SYNC_LEN   = len(_SYNC_BITS)   # 24

_MAX_TEXT   = 220            # max text chars between STX and ETX
_MAX_FRAMES = 64
# Ring buffer: 3 s of 12 kHz audio.  A max-length ACARS frame is ~0.87 s so
# 3 s guarantees any frame that has fully arrived is decodable in one pass.
_AUDIO_BUF_MAX = _AUDIO_SR * 3   # 36 000 samples

# Intermediate IQ bandwidth for the anti-alias stage before AM envelope
# detection.  Wide enough to tolerate ± few kHz of tuning offset, narrow
# enough to reject adjacent airband channels (25 kHz spacing) and stronger
# voice signals elsewhere in the source bandwidth.
_IQ_STAGE_SR = 50_000

# Resample ratio (source_sr → _AUDIO_SR) is computed at runtime from state.bw_hz
# so recordings at higher sample rates (e.g. SDRUno WAV at 2 MHz) still decode.


def _resample_ratio(source_sr: int, target_sr: int = _AUDIO_SR):
    """Return (up, down) so scipy.signal.resample_poly maps source_sr → target_sr."""
    g = math.gcd(int(target_sr), int(source_sr))
    return int(target_sr) // g, int(source_sr) // g


def _add_parity(byte: int) -> int:
    b = byte & 0x7F
    return b | (0x80 if bin(b).count('1') % 2 == 0 else 0x00)


def _byte_to_bits(byte: int):
    return [(byte >> i) & 1 for i in range(8)]


def _bits_to_byte(bits) -> int:
    v = 0
    for i, b in enumerate(bits):
        v |= (b & 1) << i
    return v


def _strip_parity(byte: int) -> int:
    return byte & 0x7F


# ── FSK demodulator ─────────────────────────────────────────────────────────

def _fsk_demod(audio: np.ndarray) -> np.ndarray:
    """
    Non-coherent dual-tone FSK detector.
    Returns float array in [-1, +1]: positive → mark (1), negative → space (0).
    """
    n = len(audio)
    t = np.arange(n, dtype=np.float32) / _AUDIO_SR

    mark_carrier  = np.exp(2j * np.pi * _MARK_HZ  * t)
    space_carrier = np.exp(2j * np.pi * _SPACE_HZ * t)

    # Multiply and integrate over each bit period with a moving average
    mark_mix  = audio * mark_carrier
    space_mix = audio * space_carrier

    # Moving average = correlator over one bit period
    kernel = np.ones(_LP_TAPS, dtype=np.float32) / _LP_TAPS
    mark_env  = np.abs(np.convolve(mark_mix,  kernel, mode='same'))
    space_env = np.abs(np.convolve(space_mix, kernel, mode='same'))

    return (mark_env - space_env).astype(np.float32)


def _sample_bits(decision: np.ndarray, phase: int) -> list:
    """
    Sample decision signal at _SPB intervals starting at `phase`.
    Uses an adaptive threshold (running mean over ~20 bits) rather than
    a hard zero so a slow DC drift on the decision signal — common on
    weak signals when |mark_env| and |space_env| have unequal noise
    energies — doesn't force every bit to the same value.
    """
    win = _SPB * 20   # 100 samples ≈ 8 ms at 12 kHz
    if len(decision) < win * 2:
        return [1 if decision[i] > 0 else 0
                for i in range(phase, len(decision), _SPB)]
    kernel = np.ones(win, dtype=np.float32) / win
    thr = np.convolve(decision, kernel, mode='same')
    return [1 if decision[i] > thr[i] else 0
            for i in range(phase, len(decision), _SPB)]


# ── ACARS frame parser ───────────────────────────────────────────────────────

def _hamming(a: list, b: list) -> int:
    return sum(x != y for x, y in zip(a, b))


def _find_sync(bits: list) -> list:
    """
    Return list of positions where SYN SYN SOH pattern starts (≤ 1 bit error).
    """
    positions = []
    for i in range(len(bits) - _SYNC_LEN):
        if _hamming(bits[i:i + _SYNC_LEN], _SYNC_BITS) <= 1:
            positions.append(i)
    return positions


def _parse_frame(bits: list, pos: int):
    """
    Parse one ACARS frame starting at `pos` (right after the sync SYN SYN SOH).
    Returns dict or None.
    """
    bcs_bytes  = []
    text_chars = []

    def next_byte():
        nonlocal pos
        if pos + 8 > len(bits):
            return None
        b = _bits_to_byte(bits[pos:pos + 8])
        pos += 8
        return b

    # ── Mode (1 char) ────────────────────────────────────────────────────────
    b = next_byte()
    if b is None:
        return None
    mode = chr(_strip_parity(b))
    bcs_bytes.append(b)

    # ── Registration (7 chars) ───────────────────────────────────────────────
    reg_chars = []
    for _ in range(7):
        b = next_byte()
        if b is None:
            return None
        reg_chars.append(chr(_strip_parity(b)))
        bcs_bytes.append(b)
    reg = ''.join(reg_chars).strip()

    # ── Type indicator + block ID + sequence number (3 chars) ────────────────
    misc = []
    for _ in range(3):
        b = next_byte()
        if b is None:
            return None
        misc.append(chr(_strip_parity(b)))
        bcs_bytes.append(b)

    # ── Flight ID (6 chars) ─────────────────────────────────────────────────
    flight_chars = []
    for _ in range(6):
        b = next_byte()
        if b is None:
            return None
        flight_chars.append(chr(_strip_parity(b)))
        bcs_bytes.append(b)
    flight = ''.join(flight_chars).strip()

    # ── STX ──────────────────────────────────────────────────────────────────
    b = next_byte()
    if b is None or _strip_parity(b) != _STX:
        return None
    bcs_bytes.append(b)

    # ── Text → ETX ───────────────────────────────────────────────────────────
    for _ in range(_MAX_TEXT):
        b = next_byte()
        if b is None:
            return None
        bcs_bytes.append(b)
        raw = _strip_parity(b)
        if raw == _ETX:
            break
        text_chars.append(chr(raw))
    else:
        return None   # never found ETX

    text = ''.join(text_chars)

    # ── BCS (2 chars: hi nibble, lo nibble) ─────────────────────────────────
    b_hi = next_byte()
    b_lo = next_byte()
    if b_hi is None or b_lo is None:
        return None

    hi_digit = _strip_parity(b_hi) - 0x30
    lo_digit = _strip_parity(b_lo) - 0x30
    if not (0 <= hi_digit <= 15 and 0 <= lo_digit <= 15):
        return None
    bcs_rx = (hi_digit << 4) | lo_digit

    # Compute expected BCS
    bcs_calc = 0
    for x in bcs_bytes:
        bcs_calc ^= x
    bcs_calc &= 0xFF

    return {
        'mode':    mode,
        'reg':     reg,
        'flight':  flight,
        'text':    text,
        'bcs_ok':  bcs_rx == bcs_calc,
        'bcs_rx':  bcs_rx,
        'bcs_exp': bcs_calc,
    }


def _decode_frames(bits: list) -> list:
    """
    Find all ACARS frames in `bits`.  Returns list of frame dicts.
    """
    frames = []
    positions = _find_sync(bits)
    for sync_pos in positions:
        payload_pos = sync_pos + _SYNC_LEN  # skip SYN SYN SOH
        frame = _parse_frame(bits, payload_pos)
        if frame is not None:
            frames.append(frame)
    return frames


# ── Plugin ───────────────────────────────────────────────────────────────────

class AcarsDecoder(Decoder):
    name            = 'acars'
    key             = 'a'
    key_help        = 'r=clear'
    min_sample_rate = 250_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._messages  = deque(maxlen=_MAX_FRAMES)
        self._seen      = set()                          # dedup: (reg, flight, text_prefix)
        self._audio_buf = np.empty(0, dtype=np.float32) # rolling 12 kHz audio ring buffer

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._seen.clear()
        self._audio_buf = np.empty(0, dtype=np.float32)

    def stop(self) -> None:
        self._messages.clear()
        self._seen.clear()
        self._audio_buf = np.empty(0, dtype=np.float32)

    # ── process ───────────────────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:

        # ── Stage 0: shift to tuned frequency ────────────────────────────────
        # For live SDR the hardware is already tuned so the ACARS carrier is
        # at DC and offset == 0.  For a file-replay device the IQ is fixed at
        # its recorded centre (_file_center_hz) while state.center_hz follows
        # user tuning — shift so the tuned channel lands at DC.
        file_center = getattr(sdr, '_file_center_hz', None)
        if file_center is not None:
            offset_hz = state.center_hz - file_center
            if abs(offset_hz) > 1.0:
                t = np.arange(len(samples), dtype=np.float32) / state.bw_hz
                samples = (samples * np.exp(-2j * np.pi * offset_hz * t)).astype(np.complex64)

        # ── Stage 1: bandwidth-limit IQ to ~50 kHz around tuning centre ──────
        # Wide enough to keep the ACARS carrier even with a few kHz of
        # tuning offset, narrow enough to reject adjacent airband channels
        # and stronger signals elsewhere in a wide-band source recording.
        up1, down1 = _resample_ratio(state.bw_hz, _IQ_STAGE_SR)
        iq_narrow  = resample_poly(samples, up1, down1).astype(np.complex64)

        # ── Stage 2: AM envelope on the narrow-band IQ ───────────────────────
        audio = np.abs(iq_narrow).astype(np.float32)
        audio -= float(np.mean(audio))    # remove DC (carrier)

        # ── Stage 3: resample envelope to AUDIO_SR for the FSK correlators ───
        up2, down2 = _resample_ratio(_IQ_STAGE_SR, _AUDIO_SR)
        audio_12k  = resample_poly(audio, up2, down2).astype(np.float32)
        self._audio_buf = np.concatenate([self._audio_buf, audio_12k])
        if len(self._audio_buf) > _AUDIO_BUF_MAX:
            self._audio_buf = self._audio_buf[-_AUDIO_BUF_MAX:]

        # Need at least 0.5 s of audio before trying to decode (~6000 samples).
        if len(self._audio_buf) < _AUDIO_SR // 2:
            return {'messages': list(self._messages), 'n_frames': len(self._messages)}

        # ── HPF + AGC on the ring buffer before FSK demod ────────────────────
        # HPF removes DC + slow burst-envelope shape that otherwise biases the
        # FSK correlator's decision.  AGC normalises by short-term RMS with a
        # floor so quiet stretches (pure noise) don't get amplified into
        # spurious symbols; the FSK correlator's balance is what carries the
        # bit information, not absolute audio level.
        audio_bp   = sosfilt(_HPF_SOS, self._audio_buf).astype(np.float32)
        win_agc    = _AUDIO_SR // 50    # 20 ms
        kern_agc   = np.ones(win_agc, dtype=np.float32) / win_agc
        rms_local  = np.sqrt(np.convolve(audio_bp * audio_bp, kern_agc, mode='same') + 1e-20)
        # Gate: gain = 1/rms when rms is comfortably above noise floor, else 1.
        rms_floor  = max(float(np.median(rms_local)) * 1.5, 1e-6)
        gain       = np.where(rms_local > rms_floor, 1.0 / rms_local, 1.0).astype(np.float32)
        audio_agc  = (audio_bp * gain).astype(np.float32)

        decision = _fsk_demod(audio_agc)

        # ── Try all 5 clock phases, collect unique frames ─────────────────────
        new_count = 0
        for phase in range(_SPB):
            bits   = _sample_bits(decision, phase)
            frames = _decode_frames(bits)
            for f in frames:
                key = (f['reg'], f['flight'], f['text'][:20])
                if key in self._seen:
                    continue
                # Only mark confirmed frames as seen; BCS-error frames are shown
                # but don't block a later clean decode of the same message.
                if f['bcs_ok']:
                    self._seen.add(key)
                    if len(self._seen) > 512:
                        self._seen.pop()
                f['ts'] = time.strftime('%H:%M:%S')
                self._messages.appendleft(f)
                new_count += 1

        return {
            'messages':  list(self._messages),
            'n_frames':  len(self._messages),
            'new':       new_count,
        }

    # ── key handling ──────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._messages.clear()
            self._seen.clear()
            return True
        return False

    # ── status bar ────────────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        n = result.get('n_frames', 0)
        return '[ACARS {} msg] '.format(n)

    # ── full-view tab ─────────────────────────────────────────────────────────

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        messages = result.get('messages', [])

        header = 'Classic ACARS  (2400 baud AM/AFSK)  [r=clear]'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        if not messages:
            try:
                screen_obj.addstr(3, 2, 'Listening for ACARS frames…')
            except curses.error:
                pass
            return

        y = 3
        for msg in messages:
            if y >= rows - 1:
                break
            ts     = msg.get('ts', '??:??:??')
            reg    = msg.get('reg', '???????')
            flight = msg.get('flight', '??????')
            text   = msg.get('text', '')
            bcs_ok = msg.get('bcs_ok', False)

            prefix   = '[{}] {:7s} {:6s}  '.format(ts, reg, flight)
            line     = prefix + text
            if len(line) > cols - 4:
                line = line[:cols - 7] + '…'

            attr = curses.A_BOLD if bcs_ok else curses.A_DIM
            try:
                screen_obj.addstr(y, 2, line, attr)
                if not bcs_ok:
                    screen_obj.addstr(y, 2, '[CRC ERR] ', curses.A_BOLD | curses.color_pair(1)
                                      if curses.has_colors() else curses.A_BOLD)
            except curses.error:
                pass
            y += 1

    # ── state persistence ─────────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:
        pass
