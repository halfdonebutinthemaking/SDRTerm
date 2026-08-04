"""iridium_decoder — Stage 3 native DQPSK demod for Iridium bursts.

Consumes narrow-band burst IQ from the iridium (Stage 1) plugin via an
in-memory queue, demodulates each burst with a matched-filter DQPSK
pipeline, and displays raw decoded bits in real time.

This is Phase 1 of a larger effort:
  - Phase 1 (this):    bit extraction only (no frame parsing)
  - Phase 2 (future):  unique-word correlation → frame classification
  - Phase 3 (future):  full frame-body parsing (IRA / IIQ / MSG / …)

The plugin is a passive consumer — the iridium (Stage 1) plugin runs
unchanged and continues to write .cf32 files to disk if `c`-capture is
on.  The queue between them is zero-cost when this plugin isn't active
(iridium plugin checks `burst_queue.has_consumers()` before doing any
extra work).
"""
import threading
from collections import deque

import numpy as np

from core import Decoder, AppState
from . import burst_queue
from . import demod


_MAX_MESSAGES = 128
_BITS_SHOWN   = 48       # first N bits shown per burst in the list


class IridiumDecoderPlugin(Decoder):
    name            = 'iridium_decode'
    key             = 'j'
    key_help        = 'r=clear'
    min_sample_rate = 2_000_000
    realtime        = False
    bg_queue_depth  = 1    # tiny — this plugin ignores samples entirely,
                           # it reads bursts from the shared queue instead
    full_view       = True

    def __init__(self):
        self._messages   = deque(maxlen=_MAX_MESSAGES)
        self._n_decoded  = 0
        self._n_dropped  = 0
        self._n_uw_lock  = 0     # bursts with Hamming distance ≤ 2 to a UW
        # Per-variant lock counts, so it's obvious whether one particular
        # phase rotation (DL_r0/DL_r1/…) is systematically winning — a
        # strong single-variant bias means we should hardcode that
        # rotation upstream and drop the extra search cost.
        self._lock_by_variant: dict = {}
        self._worker     = None
        self._stop_evt   = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._n_decoded = 0
        self._n_uw_lock = 0
        self._lock_by_variant.clear()
        burst_queue.reset_drop_count()
        burst_queue.register_consumer()
        self._stop_evt.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name='iridium-decode', daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_evt.set()
        burst_queue.unregister_consumer()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        self._messages.clear()

    # ── worker thread ───────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """Consume bursts from the shared queue and demodulate them.

        Runs on a normal-priority background thread.  Numpy operations
        release the GIL, so the SDR callback thread and UI redraw
        continue interleaving cleanly with demod work.  No forced sleep
        between bursts — that only caps throughput without helping
        anyone (numpy already yields the GIL).  If real burst rate ever
        exceeds this single-thread ceiling on a machine, the answer is
        the HeavyPlugin multiprocessing migration described in
        future_additions.md, not throttling this loop."""
        while not self._stop_evt.is_set():
            burst = burst_queue.pop(timeout=0.2)
            if burst is None:
                continue
            try:
                result = demod.demod_burst(burst['iq'], int(burst['sample_rate']))
            except Exception as e:
                # Never let a bad burst kill the worker.
                self._messages.appendleft({
                    'ts':       burst.get('timestamp', '??'),
                    'chan_id':  burst.get('chan_id', -1),
                    'snr_db':   burst.get('snr_db', 0.0),
                    'bits':     '',
                    'error':    '{}: {}'.format(type(e).__name__, e),
                    'n_symbols': 0,
                })
                continue

            uw = result.get('uw', {'name': 'none', 'pos': -1, 'hd': -1})
            self._messages.appendleft({
                'ts':         burst.get('timestamp', '??'),
                'chan_id':    burst.get('chan_id', -1),
                'chan_freq':  burst.get('chan_freq', 0.0),
                'snr_db':     burst.get('snr_db', 0.0),
                'n_symbols':  result['n_symbols'],
                'bits':       result['bits'],
                'snr_rough':  result['snr_rough_db'],
                'uw':         uw,
                'error':      None,
            })
            self._n_decoded += 1
            # DSP-quality metric.  Random bits give HD ≈ 12 on a single
            # 24-bit comparison, but our ~5000-bit × 4-UW search space
            # (~20 000 positions) hits HD ≤ 3 by luck about once per
            # burst — that's the false-positive floor.  A real correctly-
            # demodulated Iridium burst produces HD 0 or 1, occasionally
            # 2 under moderate noise.  So HD ≤ 2 is the honest "lock"
            # criterion: match rate ≈ 100% means demod is working;
            # < 30% means bit ordering / timing / phase mapping is off.
            if uw['name'] != 'none' and uw['hd'] <= 2:
                self._n_uw_lock += 1
                self._lock_by_variant[uw['name']] = \
                    self._lock_by_variant.get(uw['name'], 0) + 1

    # ── SDRTerm plugin API ──────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        # This plugin doesn't look at raw samples at all — it feeds off
        # the shared burst queue populated by the iridium plugin.  We
        # only implement process() so SDRTerm's background scheduler
        # invokes us and can refresh our result payload for the UI.
        self._n_dropped = burst_queue.drop_count()
        return {
            'n_decoded':       self._n_decoded,
            'n_uw_lock':       self._n_uw_lock,
            'n_dropped':       self._n_dropped,
            'queue_depth':     burst_queue.depth(),
            'messages':        list(self._messages),
            'lock_by_variant': dict(self._lock_by_variant),
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._messages.clear()
            self._n_decoded = 0
            self._n_uw_lock = 0
            self._lock_by_variant.clear()
            burst_queue.reset_drop_count()
            self._n_dropped = 0
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        return '[IR-decode {} bursts, q={}, drop={}] '.format(
            result.get('n_decoded', 0),
            result.get('queue_depth', 0),
            result.get('n_dropped', 0))

    # ── full-view tab ───────────────────────────────────────────────────

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        try:
            curses.init_pair(2, curses.COLOR_RED,    -1)
            curses.init_pair(3, curses.COLOR_GREEN,  -1)
            curses.init_pair(13, curses.COLOR_YELLOW, -1)
        except Exception:
            pass

        header = 'Iridium Decoder (Stage 3 · DQPSK bit extraction)'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        nd = result.get('n_decoded', 0)
        nl = result.get('n_uw_lock', 0)
        lock_pct = (100.0 * nl / nd) if nd else 0.0
        stats = ('Bursts decoded: {}   UW lock: {} ({:.0f}%)   '
                 'Queue: {}   Dropped: {}').format(
            nd, nl, lock_pct,
            result.get('queue_depth', 0),
            result.get('n_dropped', 0))
        try:
            screen_obj.addstr(3, 2, stats[:cols - 4], curses.A_BOLD)
        except curses.error:
            pass

        # Per-variant breakdown (diagnostic).  If one rotation dominates
        # by a large margin we know that's the true mapping and the
        # others are pure false positives.
        lbv = result.get('lock_by_variant') or {}
        if lbv:
            parts = ['{}:{}'.format(n, c)
                     for n, c in sorted(lbv.items(),
                                        key=lambda x: -x[1])]
            variant_line = 'By variant: ' + '  '.join(parts)
            try:
                screen_obj.addstr(4, 2, variant_line[:cols - 4])
            except curses.error:
                pass

        if result.get('queue_depth', 0) == 0 and result.get('n_decoded', 0) == 0:
            try:
                screen_obj.addstr(5, 2,
                    'Waiting for bursts.  Enable the iridium plugin and '
                    'toggle capture (c) — narrow-band bursts will be '
                    'pushed to this decoder in parallel with the disk '
                    'capture path.')
            except curses.error:
                pass
            return

        col_hdr = '  {:8s} {:8s} {:>6s} {:>6s} {:>4s}  {:>13s}  {}'.format(
            'time', 'freq/MHz', 'ch', 'SNR', 'syms',
            'UW/HD/pos', 'first bits (2b/sym, MSB-first, differential)')
        try:
            screen_obj.addstr(5, 2, col_hdr[:cols - 4], curses.A_UNDERLINE)
        except curses.error:
            pass

        y = 6
        for m in result.get('messages', []):
            if y >= rows - 2:
                break
            bits_shown = (m.get('bits') or '')[:_BITS_SHOWN]
            if m.get('error'):
                line = '  {:8s} {:8.4f} {:>6d} {:>5.1f}dB  ERR {}'.format(
                    m['ts'][-8:] if m['ts'] != '??' else '??:??:??',
                    m.get('chan_freq', 0.0) / 1e6,
                    m.get('chan_id', -1),
                    m.get('snr_db', 0.0),
                    m['error'])
            else:
                uw = m.get('uw', {'name': 'none', 'pos': -1, 'hd': -1})
                if uw['name'] == 'none' or uw['hd'] > 6:
                    uw_str = '        ---'
                else:
                    uw_str = '{:>5s} {:>2d}/{:>4d}'.format(
                        uw['name'][:5], uw['hd'], uw['pos'])
                line = '  {:8s} {:8.4f} {:>6d} {:>5.1f}dB {:>4d}  {:>13s}  {}'.format(
                    m['ts'][-8:] if m['ts'] != '??' else '??:??:??',
                    m.get('chan_freq', 0.0) / 1e6,
                    m.get('chan_id', -1),
                    m.get('snr_db', 0.0),
                    m.get('n_symbols', 0),
                    uw_str,
                    bits_shown)
            try:
                screen_obj.addstr(y, 2, line[:cols - 4])
            except curses.error:
                pass
            y += 1

    # ── state persistence ───────────────────────────────────────────────

    def save_state(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:
        pass
