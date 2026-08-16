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


class _ChannelState:
    """Everything the D8PSK demod needs to remember between chunks for
    ONE VDL2 channel.  Instantiated once per configured channel in
    multi-channel mode; also used as a container for the single-channel
    fallback so both paths share the same helpers."""
    __slots__ = ('name', 'freq_hz', 'bit_buf', 'carrier_phase',
                 'prev_sym', 'sym_offset', 'descramble_ctx')

    def __init__(self, name, freq_hz):
        self.name            = name
        self.freq_hz         = float(freq_hz) if freq_hz is not None else None
        self.bit_buf         = deque(maxlen=_MAX_BIT_BUF)
        self.carrier_phase   = 0.0
        self.prev_sym        = None
        self.sym_offset      = 0
        self.descramble_ctx  = [0] * 6

    def reset(self):
        self.bit_buf.clear()
        self.carrier_phase   = 0.0
        self.prev_sym        = None
        self.sym_offset      = 0
        self.descramble_ctx  = [0] * 6


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
        # Shared / global state (applies in both single- and multi-channel modes)
        self._messages        = deque(maxlen=_MAX_MESSAGES)
        self._seen            = set()          # CRC+len dedup cache (across channels)
        self._rrc_cache       = {}             # bw_hz → (up, down, taps) — shared
        self._n_frames        = 0
        self._n_errors        = 0
        # Single-channel mode: one implicit channel that follows the tuned centre
        # (using peak_marker offset when present).  Keeps existing behaviour when
        # no channels are configured — same defaults, same tuning strategy.
        self._default_ch      = _ChannelState(name=None, freq_hz=None)
        # Multi-channel mode: populated by load_state() from preset.  When
        # non-empty, process() dispatches to the multi-channel path and
        # ignores peak_marker / state.center_hz shifts — each configured
        # freq_hz is fixed and downconverted independently.
        self._channels: list  = []

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._seen.clear()
        self._n_frames = 0
        self._n_errors = 0
        self._default_ch.reset()
        for ch in self._channels:
            ch.reset()

    def stop(self) -> None:
        self.start(None)

    # ── Signal processing ──────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        # Multi-channel mode wins when the preset configured a channel
        # list.  Each freq gets its own downconvert + demod + bit buffer,
        # all fed from the same one-chunk IQ.  Channels must lie inside
        # state.bw_hz around state.center_hz (10% guardband).
        if self._channels:
            return self._process_multi(samples, state)
        return self._process_single(samples, state, results)

    # ── single-channel path (original behaviour, unchanged semantics) ────

    def _process_single(self, samples, state, results):
        # Use peak_marker hint if available; otherwise decode at centre frequency.
        # D8PSK is differential so small offsets cancel in the phase differences,
        # but the RRC matched filter still needs the signal near DC (< ~1 kHz off).
        peak      = (results or {}).get('peak_marker', {})
        peak_hz   = peak.get('peak_hz', state.center_hz)
        offset_hz = peak_hz - state.center_hz
        self._downconvert_and_decode(
            samples, offset_hz, int(state.bw_hz),
            ch=self._default_ch, channel_name=None,
        )
        return self._result()

    # ── multi-channel path (preset configured a channel list) ────────────

    def _process_multi(self, samples, state):
        sr = int(state.bw_hz)
        for ch in self._channels:
            offset_hz = ch.freq_hz - state.center_hz
            if abs(offset_hz) > sr / 2 * 0.9:      # 10 % guardband
                continue
            self._downconvert_and_decode(
                samples, offset_hz, sr,
                ch=ch, channel_name=ch.name,
            )
        r = self._result()
        r['channels'] = [ch.name for ch in self._channels]
        return r

    # ── shared per-channel DSP ───────────────────────────────────────────

    def _downconvert_and_decode(self, samples, offset_hz, sr,
                                 ch: '_ChannelState', channel_name):
        """Mix `samples` down by `offset_hz`, resample + RRC-match, slice
        at symbol centres, differential D8PSK decode, descramble, HDLC
        frame parse, dedup by CRC, append new messages tagged with
        `channel_name` (None in single mode).

        All per-channel state (`carrier_phase`, `prev_sym`, `sym_offset`,
        `descramble_ctx`, `bit_buf`) lives on the passed-in `ch`, so
        each channel maintains cross-chunk continuity independently."""

        # NCO downconvert with accumulated phase (no per-chunk boundary steps).
        n           = len(samples)
        t_local     = np.arange(n) / sr
        baseband    = (samples * np.exp(
            -1j * (ch.carrier_phase + 2 * np.pi * offset_hz * t_local)
        )).astype(np.complex128)
        ch.carrier_phase = (
            ch.carrier_phase + 2 * np.pi * offset_hz * n / sr
        ) % (2 * np.pi)

        # Resample to TARGET_SPS × symbol_rate; cache by source rate.
        cache_key = sr
        if cache_key not in self._rrc_cache:
            target_sr = _SYMBOL_RATE * _TARGET_SPS
            g         = gcd(target_sr, cache_key)
            up, down  = target_sr // g, cache_key // g
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
            return

        matched = np.convolve(resampled, taps, mode='same').astype(np.complex64)

        # Sample at symbol centres — sym_offset preserves alignment across
        # chunks (resample_poly output length isn't always a multiple of
        # TARGET_SPS, which would otherwise cause a one-tribit slip per chunk).
        syms  = matched[ch.sym_offset::_TARGET_SPS]
        n_out = len(matched)
        ch.sym_offset = (_TARGET_SPS - (n_out - ch.sym_offset) % _TARGET_SPS) % _TARGET_SPS

        if len(syms) < 2:
            return

        if ch.prev_sym is not None:
            syms = np.concatenate([[ch.prev_sym], syms])
        ch.prev_sym = syms[-1]

        # D8PSK differential demod → scrambled bits → descramble
        raw_bits = d8psk_demod(syms)
        new_bits, ch.descramble_ctx = descramble(raw_bits, ch.descramble_ctx)
        ch.bit_buf.extend(new_bits)

        # HDLC frame detection on the per-channel bit buffer
        buf = list(ch.bit_buf)
        for payload, crc_ok in hdlc_frames(buf):
            self._n_frames += 1
            if not crc_ok:
                self._n_errors += 1
                continue                # count CRC errors but don't display
            key = (len(payload), _crc_key(payload))
            if key in self._seen:
                continue                # dedup across channels — same message
            self._seen.add(key)
            if len(self._seen) > 512:
                self._seen.clear()
            parsed = parse_avlc(payload)
            self._messages.append({
                'ts':      time.strftime('%H:%M:%S'),
                'parsed':  parsed,
                'raw':     payload,
                'channel': channel_name,
            })

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
        chans = result.get('channels')
        ch_suffix = ' · {} ch'.format(len(chans)) if chans else ''
        if n == 0:
            return '[VDL2 —{}] '.format(ch_suffix)
        return '[VDL2 {} frm{} {}err{}] '.format(
            n, '' if n == 1 else 's', er, ch_suffix)

    # ── Persistence ───────────────────────────────────────────────────────

    def save_state(self) -> dict:
        if not self._channels:
            return {}
        return {
            'channels': [{'freq': ch.freq_hz, 'name': ch.name}
                         for ch in self._channels],
        }

    def load_state(self, d: dict) -> None:
        chans = d.get('channels') or []
        self._channels = [
            _ChannelState(spec.get('name') or f'{spec["freq"] / 1e6:.3f}',
                          float(spec['freq']))
            for spec in chans
            if isinstance(spec, dict) and 'freq' in spec
        ]

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

                channel = msg.get('channel')
                ch_tag  = '{:<10s} '.format(channel[:10]) if channel else ''
                if parsed:
                    src  = parsed.get('src', '??:??')
                    text = parsed.get('text', '')
                    line = '[{}] {}{} > {}'.format(ts, ch_tag, src, text)
                    attr = curses.color_pair(3) | curses.A_BOLD
                else:
                    line = '[{}] {}{} bytes (no AVLC parse)  {}'.format(
                        ts, ch_tag, len(msg['raw']), msg['raw'][:8].hex(' '))
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
