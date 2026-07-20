import time
from collections import deque
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from core import Decoder, AppState
from .protocol import d8psk_demod, descramble, hdlc_frames, parse_avlc

_SYMBOL_RATE  = 10_500
_RRC_ALPHA    = 0.60
_TARGET_SPS   =    8
_MAX_MESSAGES =   64
_MAX_BIT_BUF  = 65_536   # ~2 s of bits at 31.5 kbps


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
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float32)


class VDL2Decoder(Decoder):
    name            = 'vdl2'
    key             = 'v'
    key_help        = 'r=clear'
    min_sample_rate = 250_000
    realtime        = False
    bg_queue_depth  = 2
    full_view       = True

    def __init__(self):
        self._messages        = deque(maxlen=_MAX_MESSAGES)
        self._bit_buf         = deque(maxlen=_MAX_BIT_BUF)
        self._seen            = set()          # CRC+len dedup cache
        self._carrier_phase   = 0.0
        self._prev_sym        = None           # last symbol from previous chunk
        self._sym_offset      = 0             # sample offset within symbol period
        self._descramble_ctx  = [0] * 6       # last 6 received bits for cross-chunk descrambling
        self._rrc_cache       = {}             # bw_hz → (up, down, taps)
        self._n_frames        = 0
        self._n_errors        = 0

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._bit_buf.clear()
        self._seen.clear()
        self._carrier_phase   = 0.0
        self._prev_sym        = None
        self._sym_offset      = 0
        self._descramble_ctx  = [0] * 6
        self._n_frames        = 0
        self._n_errors        = 0

    def stop(self) -> None:
        self.start(None)

    # ── Signal processing ──────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        # Use peak_marker hint if available; otherwise decode at centre frequency.
        # D8PSK is differential so small offsets cancel in the phase differences,
        # but the RRC matched filter still needs the signal near DC (< ~1 kHz off).
        peak    = (results or {}).get('peak_marker', {})
        peak_hz = peak.get('peak_hz', state.center_hz)

        # Mix to baseband with accumulated phase for cross-chunk continuity
        offset_hz           = peak_hz - state.center_hz
        n                   = len(samples)
        t_local             = np.arange(n) / state.bw_hz
        baseband            = (samples * np.exp(
            -1j * (self._carrier_phase + 2 * np.pi * offset_hz * t_local)
        )).astype(np.complex128)
        self._carrier_phase = (
            self._carrier_phase + 2 * np.pi * offset_hz * n / state.bw_hz
        ) % (2 * np.pi)

        # Resample to TARGET_SPS × symbol_rate
        cache_key = int(state.bw_hz)
        if cache_key not in self._rrc_cache:
            target_sr = _SYMBOL_RATE * _TARGET_SPS
            src_sr    = cache_key
            g         = gcd(target_sr, src_sr)
            up, down  = target_sr // g, src_sr // g
            while up > 500 or down > 500:
                up   = max(1, up   // 2)
                down = max(1, down // 2)
            taps = _rrc(8 * _TARGET_SPS + 1, _RRC_ALPHA, _TARGET_SPS)
            self._rrc_cache[cache_key] = (up, down, taps)
            if len(self._rrc_cache) > 8:
                self._rrc_cache.pop(next(iter(self._rrc_cache)))
        up, down, taps = self._rrc_cache[cache_key]

        try:
            resampled = resample_poly(baseband, up, down)
        except Exception:
            return self._result()

        matched = np.convolve(resampled, taps, mode='same').astype(np.complex64)

        # Sample at symbol centres.
        # _sym_offset tracks where in the symbol period we should start so that
        # each chunk's first sample is exactly 1 period after the previous chunk's
        # last sample — avoiding the 1-tribit slip that occurs when resample_poly
        # output length is not a multiple of TARGET_SPS.
        syms   = matched[self._sym_offset::_TARGET_SPS]
        n_out  = len(matched)
        self._sym_offset = (_TARGET_SPS - (n_out - self._sym_offset) % _TARGET_SPS) % _TARGET_SPS

        if len(syms) < 2:
            return self._result()

        # Prepend previous chunk's last symbol for cross-chunk differential decode
        if self._prev_sym is not None:
            syms = np.concatenate([[self._prev_sym], syms])
        self._prev_sym = syms[-1]

        # D8PSK differential decode → scrambled bits → descramble
        raw_bits = d8psk_demod(syms)
        new_bits, self._descramble_ctx = descramble(raw_bits, self._descramble_ctx)
        self._bit_buf.extend(new_bits)

        # HDLC frame detection on accumulated bit buffer
        buf = list(self._bit_buf)
        for payload, crc_ok in hdlc_frames(buf):
            self._n_frames += 1
            if not crc_ok:
                self._n_errors += 1
                continue                    # count errors but don't display them
            key = (len(payload), _crc_key(payload))
            if key in self._seen:
                continue
            self._seen.add(key)
            if len(self._seen) > 512:
                self._seen.clear()
            parsed = parse_avlc(payload)
            ts     = time.strftime('%H:%M:%S')
            self._messages.append({
                'ts':     ts,
                'parsed': parsed,
                'raw':    payload,
            })

        return self._result()

    def _result(self) -> dict:
        return {
            'n_frames':  self._n_frames,
            'n_errors':  self._n_errors,
            'n_msgs':    len(self._messages),
        }

    # ── Keys ──────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self.start(state)
            return True
        return False

    # ── Status line ───────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        n  = result.get('n_frames', 0)
        er = result.get('n_errors', 0)
        if n == 0:
            return '[VDL2 —] '
        return '[VDL2 {} frm{} {}err] '.format(
            n, '' if n == 1 else 's', er)

    # ── Persistence ───────────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:
        pass

    # ── Full-screen display ───────────────────────────────────────────────

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        n_frames = result.get('n_frames', 0)
        n_errors = result.get('n_errors', 0)

        header = 'VDL Mode 2   D8PSK {:,} sym/s   {} frames  {} CRC errors'.format(
            _SYMBOL_RATE, n_frames, n_errors)
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header, curses.A_BOLD)
        except curses.error:
            pass

        # Message list — newest at bottom
        msgs     = list(self._messages)
        max_rows = rows - 5
        visible  = msgs[-max_rows:] if len(msgs) > max_rows else msgs

        try:
            curses.init_pair(3, curses.COLOR_GREEN,  -1)
            curses.init_pair(2, curses.COLOR_RED,    -1)
            curses.init_pair(13, curses.COLOR_YELLOW, -1)
        except Exception:
            pass

        y = 2
        if not visible:
            try:
                screen_obj.addstr(y, 2,
                                  'Waiting for frames — decoding at centre freq; '
                                  'enable peak_marker (k) + follow (t) if signal is off-centre',
                                  curses.A_DIM)
            except curses.error:
                pass
        else:
            for msg in visible:
                if y >= rows - 3:
                    break
                ts     = msg['ts']
                parsed = msg['parsed']

                if parsed:
                    src  = parsed.get('src', '??:??')
                    text = parsed.get('text', '')
                    line = '[{}] {} > {}'.format(ts, src, text)
                    attr = curses.color_pair(3) | curses.A_BOLD
                else:
                    line = '[{}] {} bytes (no AVLC parse)  {}'.format(
                        ts, len(msg['raw']), msg['raw'][:8].hex(' '))
                    attr = curses.color_pair(3)

                try:
                    screen_obj.addstr(y, 2, line[:cols - 4], attr)
                except curses.error:
                    pass
                y += 1

        footer = 'D8PSK  RRC α={:.2f}  {:,} sym/s  31,500 bit/s   r=clear'.format(
            _RRC_ALPHA, _SYMBOL_RATE)
        try:
            screen_obj.addstr(rows - 2, 2, footer[:cols - 4], curses.A_DIM)
        except curses.error:
            pass


# ── helpers ────────────────────────────────────────────────────────────────

def _crc_key(data: bytes) -> int:
    """Fast dedup hash using the last 2 bytes as a fingerprint alongside length."""
    return int.from_bytes(data[-2:], 'little') if len(data) >= 2 else 0
