"""
RDS (Radio Data System) decoder plugin.

Pipeline:
  IQ samples (at SDR sample rate)
    → FM discriminator (instantaneous frequency)
    → mix 57 kHz pilot harmonic to baseband
    → low-pass filter (≤ 2.4 kHz = half of 4.8 kHz RDS BW)
    → decimate to ~19 ksps symbol clock domain
    → biphase-mark symbol timing recovery
    → differential BPSK bit decisions
    → block sync via CRC-10
    → group decoding (PS, RadioText, PTY, PI, TP/TA)
"""

import threading
import time
import numpy as np
from collections import deque
from core import Decoder, AppState

# ── RDS constants ──────────────────────────────────────────────────────────────
_RDS_SUBCARRIER  = 57_000.0      # Hz above FM carrier
_RDS_BITRATE     = 1_187.5       # bps (= 57 000 / 48)
_PILOT_HZ        = 19_000.0      # 19 kHz pilot tone
_BLOCK_BITS      = 26            # 16 info + 10 CRC per block
_BLOCKS_PER_GRP  = 4
_GROUP_BITS      = _BLOCK_BITS * _BLOCKS_PER_GRP  # 104 bits
_CRC_POLY        = 0x5B9         # x^10+x^8+x^7+x^5+x^4+x^3+1
_CRC_LEN         = 10

# Offset words (xor with raw syndrome to check each block position)
_OFFSETS = {'A': 0x0FC, 'B': 0x198, 'C': 0x168, "C'": 0x350, 'D': 0x1B4}
_OFFSET_SEQ = ['A', 'B', 'C', 'D']   # normal sequence

_PTY_NAMES = [
    'None','News','Affairs','Info','Sport','Educate','Drama','Culture',
    'Science','Varied','Pop M','Rock M','Easy M','Light M','Classics',
    'Other M','Weather','Finance','Children','Social','Religion','Phone In',
    'Travel','Leisure','Jazz','Country','Nation M','Oldies','Folk M',
    'Document','Test','Alarm',
]

# ── CRC-10 helpers ─────────────────────────────────────────────────────────────

def _crc10(bits: np.ndarray) -> int:
    """Compute CRC-10 over a bit array (MSB first)."""
    reg = 0
    for b in bits:
        reg = ((reg << 1) | int(b)) ^ (_CRC_POLY if reg & 0x200 else 0)
    for _ in range(_CRC_LEN):
        reg = (reg << 1) ^ (_CRC_POLY if reg & 0x200 else 0)
    return reg & 0x3FF


def _syndrome(bits: np.ndarray) -> int:
    """Syndrome of 26-bit block (data+CRC); equals offset word if valid."""
    return _crc10(bits[:16]) ^ (int(bits[16:26].dot(1 << np.arange(9, -1, -1))) & 0x3FF)


# ── Main decoder class ─────────────────────────────────────────────────────────

class RDS(Decoder):
    name            = 'rds'
    key             = 'r'
    key_help        = ''
    min_sample_rate = 250_000

    def __init__(self):
        self._lock     = threading.Lock()
        self._thread   = None
        self._stop_evt = threading.Event()
        self._result   = {}
        self._samples  = deque(maxlen=2_000_000)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._stop_evt.clear()
        self._result   = {}
        self._ps_chars = ['_'] * 8
        self._rt_chars = ['_'] * 64
        self._samples.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread  = None
        self._result   = {}
        self._ps_chars = ['_'] * 8
        self._rt_chars = ['_'] * 64

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        self._samples.extend(samples)
        with self._lock:
            return dict(self._result)

    # ── status display ─────────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        pi  = result.get('pi')
        ps  = result.get('ps', '')
        pty = result.get('pty', 0)
        rt  = result.get('rt', '')
        tp  = result.get('tp', False)
        ta  = result.get('ta', False)

        if pi is None:
            return '[RDS:searching] '

        flags = ''
        if tp: flags += 'TP '
        if ta: flags += 'TA '
        pty_name = _PTY_NAMES[pty] if 0 <= pty < len(_PTY_NAMES) else ''
        ps_str   = ps.strip() if ps else '?'

        line = '[RDS PI:{:04X} PS:{} PTY:{}'.format(pi, ps_str, pty_name)
        if flags:
            line += ' ' + flags.strip()
        if rt:
            line += ' RT:{}'.format(rt.strip())
        line += '] '
        return line

    # ── background worker ──────────────────────────────────────────────────────

    def _worker(self) -> None:
        MIN_SAMPLES = 1_000_000
        while not self._stop_evt.is_set():
            n = len(self._samples)
            if n < MIN_SAMPLES:
                time.sleep(0.2)
                continue

            chunk = np.array(list(self._samples), dtype=np.complex64)
            self._samples.clear()

            try:
                result = self._decode(chunk)
                if result:
                    with self._lock:
                        self._result.update(result)
            except Exception as e:
                self._dbg('RDS worker error: {}'.format(e))

            time.sleep(0.05)

    # ── full decode pipeline ───────────────────────────────────────────────────

    def _decode(self, iq: np.ndarray) -> dict:
        sr = self._detect_sr(iq)
        if sr is None:
            return {}

        # 1. FM discriminator → audio baseband
        audio = self._fm_demod(iq)

        # 2. Extract RDS subcarrier at 57 kHz by mixing to baseband
        t     = np.arange(len(audio), dtype=np.float64) / sr
        mixer = np.exp(-1j * 2.0 * np.pi * _RDS_SUBCARRIER * t).astype(np.complex64)
        rds_bb = audio.astype(np.complex64) * mixer

        # 3. Low-pass filter to ≤ 2 kHz (keeps RDS, kills everything else)
        rds_lp = self._lpf(rds_bb, sr, cutoff=2_000.0, ntaps=101)

        # 4. Decimate to ~19 ksps (symbol rate × 8 oversampling before clock rec)
        target_sr   = _RDS_BITRATE * 16    # ~19 ksps
        decim       = max(1, int(sr / target_sr))
        rds_dec     = rds_lp[::decim].copy()
        actual_sr   = sr / decim

        # 5. Symbol timing via biphase mark decode
        bits = self._biphase_decode(rds_dec, actual_sr)
        if bits is None or len(bits) < _GROUP_BITS * 2:
            return {}

        # 6. Block sync + group decode
        return self._decode_groups(bits)

    def _detect_sr(self, iq: np.ndarray) -> float:
        # Caller always supplies raw IQ from the SDR; we know sample rate from
        # the deque size relative to time, but that's unreliable.  Instead we
        # infer it from the pilot tone position in the FFT — the 19 kHz pilot
        # is the strongest tonal above 15 kHz in FM audio, which lets us pin
        # down sr without needing AppState here.
        n = min(len(iq), 65536)
        fm = self._fm_demod(iq[:n])   # length n-1
        spec  = np.abs(np.fft.rfft(fm)) ** 2
        freqs = np.fft.rfftfreq(len(fm))   # in cycles/sample
        # Look for pilot in 14–24 kHz range; convert freq to Hz using candidate sr
        # Try a few common SDR sample rates
        for sr in [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]:
            f = freqs * sr
            mask = (f >= 14_000) & (f <= 24_000)
            if not mask.any():
                continue
            idx   = int(np.argmax(spec * mask))
            pilot = float(freqs[idx] * sr)
            if abs(pilot - _PILOT_HZ) < 1_500:
                self._dbg('pilot={:.0f} Hz → sr={}'.format(pilot, sr))
                return float(sr)
        # Fallback: assume 250 ksps
        return 250_000.0

    def _fm_demod(self, iq: np.ndarray) -> np.ndarray:
        """Instantaneous frequency (unnormalised) from IQ samples."""
        diff = iq[1:] * np.conj(iq[:-1])
        return np.angle(diff).astype(np.float32)

    def _lpf(self, sig, sr: float, cutoff: float, ntaps: int = 63):
        """Simple windowed-sinc FIR low-pass filter (complex or real)."""
        fc  = cutoff / sr
        n   = np.arange(ntaps) - ntaps // 2
        h   = np.sinc(2 * fc * n).astype(np.float64)
        h  *= np.blackman(ntaps)
        h  /= h.sum()
        if np.iscomplexobj(sig):
            return np.convolve(sig.real, h, mode='same').astype(np.float32) + \
                   1j * np.convolve(sig.imag, h, mode='same').astype(np.float32)
        return np.convolve(sig.astype(np.float64), h, mode='same').astype(np.float32)

    # ── biphase-mark symbol decoder ────────────────────────────────────────────

    def _biphase_decode(self, bb: np.ndarray, sr: float):
        """
        Biphase-mark coding: each bit occupies two chips at 2375 Hz.
        A transition at the chip boundary always occurs (clock); an additional
        transition at the mid-point encodes a 0-bit (transition) or 1-bit (no
        transition) — or vice versa depending on convention.  We use the
        squared-signal approach to recover the 2375 Hz clock from the 1187.5 Hz
        data component.
        """
        chips_per_bit = 2
        chip_rate     = _RDS_BITRATE * chips_per_bit   # 2375 chips/s

        sps = sr / chip_rate   # samples per chip (fractional)
        if sps < 2.0:
            return None        # sample rate too low

        # Use the magnitude as a polarity-independent signal for timing
        mag = np.abs(bb)
        mag -= mag.mean()

        # Correlate with ideal chip pattern to find best clock phase
        chip_len = int(round(sps))
        if chip_len < 1:
            return None
        corr_len = chip_len * 4
        template = np.tile([1.0, -1.0], corr_len // 2 + 1)[:corr_len].astype(np.float32)
        corr = np.correlate(mag[:len(template) * 8], template, mode='valid')
        phase = int(np.argmax(np.abs(corr))) % chip_len

        # Sample at chip centres
        chip_centres = np.arange(phase + chip_len // 2, len(bb), sps).astype(int)
        chip_centres = chip_centres[chip_centres < len(bb)]
        chips = np.real(bb[chip_centres])

        # Differential decode chip pairs → bits
        if len(chips) < 4:
            return None

        # Normalise chips to ±1
        rms = np.sqrt(np.mean(chips ** 2))
        if rms < 1e-10:
            return None
        chips = chips / rms

        # Biphase: bit = 1 if pair (c0, c1) have same sign, 0 if opposite
        n_pairs = len(chips) // chips_per_bit
        c0 = chips[:n_pairs * chips_per_bit:chips_per_bit]
        c1 = chips[1:n_pairs * chips_per_bit:chips_per_bit]
        bits = ((c0 * c1) > 0).astype(np.uint8)

        self._dbg('biphase: {} chips → {} bits  sps={:.2f}'.format(
            len(chips), len(bits), sps))
        return bits

    # ── block sync and group decode ────────────────────────────────────────────

    def _decode_groups(self, bits: np.ndarray) -> dict:
        """Slide through bits looking for valid RDS block sync."""
        result = {}
        found  = 0

        n = len(bits)
        i = 0
        while i < n - _GROUP_BITS:
            # Try to lock onto a group starting at offset i
            group_bits = bits[i:i + _GROUP_BITS]
            blocks     = [group_bits[b * _BLOCK_BITS:(b + 1) * _BLOCK_BITS]
                          for b in range(_BLOCKS_PER_GRP)]

            syndromes  = [_syndrome(blk) for blk in blocks]
            expected   = [_OFFSETS[o] for o in _OFFSET_SEQ]

            valid = [s == e for s, e in zip(syndromes, expected)]
            # Require at least blocks A and B valid
            if valid[0] and valid[1]:
                info = blocks[0][:16]
                data = blocks[1][:16]

                pi  = int(info.dot(1 << np.arange(15, -1, -1)))
                b1  = int(data.dot(1 << np.arange(15, -1, -1)))

                group_type = (b1 >> 12) & 0x0F
                version    = (b1 >> 11) & 0x01   # 0=A, 1=B
                tp         = bool((b1 >> 10) & 1)
                pty        = (b1 >> 5) & 0x1F
                ta         = bool((b1 >> 4) & 1)

                result['pi']  = pi
                result['pty'] = pty
                result['tp']  = tp
                result['ta']  = ta
                found += 1

                if group_type == 0 and valid[3]:
                    # Group 0A/0B: PS name (2 chars per group, 4 groups = 8 chars)
                    seg   = b1 & 0x03
                    c     = blocks[3][:16]
                    c_val = int(c.dot(1 << np.arange(15, -1, -1)))
                    ch0   = chr((c_val >> 8) & 0x7F)
                    ch1   = chr(c_val & 0x7F)
                    self._ps_chars[seg * 2]     = ch0 if ch0.isprintable() else ' '
                    self._ps_chars[seg * 2 + 1] = ch1 if ch1.isprintable() else ' '
                    result['ps'] = ''.join(self._ps_chars)

                elif group_type == 2 and version == 0:
                    # Group 2A: RadioText (4 chars per group, 16 groups = 64 chars)
                    seg     = b1 & 0x0F
                    ab_flag = (b1 >> 4) & 1
                    # A/B flag change signals a new RadioText message — clear buffer
                    if not hasattr(self, '_rt_ab') or self._rt_ab != ab_flag:
                        self._rt_ab    = ab_flag
                        self._rt_chars = ['_'] * 64
                    for k, blk_idx in enumerate([2, 3]):
                        if valid[blk_idx]:
                            w   = int(blocks[blk_idx][:16].dot(1 << np.arange(15, -1, -1)))
                            pos = seg * 4 + k * 2
                            if pos + 1 < 64:
                                ch0 = chr((w >> 8) & 0x7F)
                                ch1 = chr(w & 0x7F)
                                if ch0 == '\r':
                                    self._rt_chars[pos:] = [' '] * (64 - pos)
                                else:
                                    self._rt_chars[pos] = ch0 if ch0.isprintable() else ' '
                                if ch1 == '\r':
                                    self._rt_chars[pos + 1:] = [' '] * (63 - pos)
                                else:
                                    self._rt_chars[pos + 1] = ch1 if ch1.isprintable() else ' '
                    result['rt'] = ''.join(self._rt_chars)

                i += _GROUP_BITS
                continue

            i += 1

        self._dbg('decode_groups: {} bits → {} valid groups  PI={}'.format(
            n, found, result.get('pi')))
        return result
