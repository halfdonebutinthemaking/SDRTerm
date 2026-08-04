"""iridium_decoder — real Iridium demodulator (gr-iridium toolkit port).

Runs the complete gr-iridium DSP chain in-process:

    fft_burst_tagger  →  burst_downmix  →  qpsk_demod  →  bits + UW check

on the raw IQ stream from the SDR.  Emits gr-iridium-compatible RAW:
lines suitable for feeding into iridium-toolkit's iridium-parser.py
for frame classification.

Runs independently of the iridium (Stage 1) plugin — the two plugins
serve different purposes:
  - iridium              : burst detection & channel-hit statistics
                           (display of what's on-air)
  - iridium_decoder      : actual message decoding (this plugin)
"""
import threading
from collections import deque

import numpy as np

from core import Decoder, AppState
from .toolkit.fft_burst_tagger import FftBurstTagger
from .toolkit.burst_downmix import BurstDownmix
from .toolkit.qpsk_demod import QpskDemod
from .toolkit import iridium


_MAX_MESSAGES  = 128
_BITS_SHOWN    = 48
# ~100 ms rolling buffer of raw IQ so burst_downmix has enough runway
# to grab a burst plus its post-length trailer.  At 2 MHz this is
# ~800 KB and is copied at each frame — cheap.
_BUFFER_MS     = 150
# Max IQ chunks queued for the worker before dropping (backpressure).
# Each chunk is typically 100 ms (200 KB at 2 MHz complex64).  128
# chunks = ~12 s of buffer / ~25 MB — enough to smooth over CPU
# spikes during dense burst passes without blowing out RAM.
_MAX_IQ_QUEUE  = 128

# Trying BOTH bin conventions per burst catches ~2× more decodes but
# doubles per-burst CPU cost.  Off by default so realtime can keep up;
# users can toggle with 'b' if their CPU has headroom.
_DEFAULT_TRY_BOTH_BINS = False


class IridiumDecoderPlugin(Decoder):
    name            = 'iridium_decode'
    key             = 'j'
    key_help        = 'r=clear  b=toggle both-bin'
    min_sample_rate = 2_000_000
    realtime        = False      # runs in bg worker; process() just enqueues
    bg_queue_depth  = 8
    full_view       = True

    def __init__(self):
        self._messages    = deque(maxlen=_MAX_MESSAGES)
        self._n_bursts    = 0     # detector output count
        self._n_a_ok      = 0     # unique bursts that passed UW check
        self._n_dropped   = 0     # IQ chunks dropped due to backpressure
        self._try_both    = _DEFAULT_TRY_BOTH_BINS
        self._iq_queue: deque = deque()
        self._iq_lock     = threading.Lock()
        self._worker      = None
        self._stop_evt    = threading.Event()
        # DSP objects — instantiated lazily in start() when we know SR
        self._tagger:  FftBurstTagger = None
        self._downmix: BurstDownmix   = None
        self._demod:   QpskDemod      = None
        self._sample_rate = 0
        self._center_hz   = 0

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._messages.clear()
        self._n_bursts = 0
        self._n_a_ok = 0
        self._n_dropped = 0
        with self._iq_lock:
            self._iq_queue.clear()
        self._sample_rate = int(state.bw_hz)
        self._center_hz   = int(state.center_hz)
        self._tagger  = FftBurstTagger(sample_rate=self._sample_rate,
                                       threshold_db=14.0,
                                       center_frequency=float(self._center_hz))
        self._downmix = BurstDownmix(output_sample_rate=500_000)
        self._demod   = QpskDemod()
        self._stop_evt.clear()
        self._worker  = threading.Thread(
            target=self._worker_loop, name='iridium-decode', daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._worker is not None:
            self._worker.join(timeout=1.5)
            self._worker = None
        with self._iq_lock:
            self._iq_queue.clear()

    # ── worker: tagger → downmix → demod ────────────────────────────────

    def _worker_loop(self) -> None:
        """Feed IQ from the queue through the toolkit chain.  Maintains
        a rolling ~150 ms IQ buffer so burst_downmix can look back at
        detected bursts once they've ended."""
        fft_size    = self._tagger.fft_size
        max_buf_len = int(self._sample_rate * _BUFFER_MS / 1000)
        # Rolling raw IQ buffer + the absolute sample index of buffer[0]
        iq_buf      = np.zeros(0, dtype=np.complex64)
        buf_start_index = 0

        # Leftover samples that didn't fit into a full FFT frame yet
        frame_pending = np.zeros(0, dtype=np.complex64)
        # Sample index at the START of the tagger's current position.
        # Advances by fft_size every time we process a frame.

        while not self._stop_evt.is_set():
            with self._iq_lock:
                chunk = self._iq_queue.popleft() if self._iq_queue else None
            if chunk is None:
                self._stop_evt.wait(0.02)
                continue

            # Append to rolling buffer (NOT trimmed yet — we need old IQ
            # around while frames are being processed so that bursts which
            # end mid-chunk can still find their start samples in iq_buf).
            iq_buf = np.concatenate([iq_buf, chunk])

            # Drain frame-sized chunks into the tagger.  Each processed
            # frame may cause bursts to be marked "gone" — we extract
            # their IQ from iq_buf immediately.
            frame_pending = np.concatenate([frame_pending, chunk])
            n_frames = len(frame_pending) // fft_size
            for k in range(n_frames):
                frame = frame_pending[k * fft_size:(k + 1) * fft_size]
                self._tagger.process_frame(frame)
                _, gone = self._tagger.drain()
                for b in gone:
                    self._process_burst(b, iq_buf, buf_start_index)
            frame_pending = frame_pending[n_frames * fft_size:]

            # NOW trim the rolling buffer.  Keep at least max_buf_len
            # samples plus any tail needed for bursts still in progress
            # (their last_active could be up to burst_post_len samples
            # behind, and burst_downmix needs even more history).
            tail_reserve = self._tagger.burst_post_len + int(
                self._sample_rate * _BUFFER_MS / 1000)
            if len(iq_buf) > max_buf_len + tail_reserve:
                trim = len(iq_buf) - (max_buf_len + tail_reserve)
                iq_buf = iq_buf[trim:]
                buf_start_index += trim

    def _process_burst(self, burst, iq_buf: np.ndarray, buf_start_index: int) -> None:
        """Extract wide IQ for a detected burst, run through downmix+demod,
        emit PDU to display deque."""
        self._n_bursts += 1
        # b.start / b.stop are absolute sample indices in the input stream.
        # Convert to buffer offsets.
        start_off = burst.start - buf_start_index
        end_off   = burst.stop + self._tagger.burst_post_len - buf_start_index
        if start_off < 0:
            return   # burst extends before what we still have in the buffer
        if end_off > len(iq_buf):
            end_off = len(iq_buf)
        if end_off - start_off < 1000:
            return
        burst_iq = iq_buf[max(0, start_off):end_off].copy()

        # See FftBurstTagger.alternate_center_bin.  In "both" mode try
        # both conventions per burst (higher decode rate but 2× CPU);
        # in default mode try only the native convention.
        bins_to_try = (burst.center_bin,)
        if self._try_both:
            bins_to_try = (burst.center_bin,
                           self._tagger.alternate_center_bin(burst.center_bin))
        best_pdu = None
        for bin_use in bins_to_try:
            rel_freq = (bin_use - self._tagger.fft_size / 2) / float(self._tagger.fft_size)
            try:
                frames = self._downmix.process(
                    burst_samples=burst_iq,
                    relative_frequency=rel_freq,
                    center_frequency=float(self._center_hz),
                    input_sample_rate=float(self._sample_rate),
                    timestamp=int(burst.start * 1e9 / self._sample_rate),
                    burst_id=burst.id,
                    noise=burst.noise,
                    magnitude=burst.magnitude,
                )
            except Exception:
                continue
            for frame in frames:
                pdu = self._demod.process(frame)
                if pdu is not None:
                    best_pdu = pdu
                    break
            if best_pdu is not None:
                break

        if best_pdu is None:
            return
        self._n_a_ok += 1
        self._messages.appendleft({
            'ts_ms':      best_pdu['timestamp'] / 1e6,
            'freq_hz':    best_pdu['center_frequency'],
            'direction':  best_pdu['direction'],
            'confidence': best_pdu['confidence'],
            'n_symbols':  best_pdu['n_symbols'],
            'magnitude':  best_pdu['magnitude'],
            'noise':      best_pdu['noise'],
            'bits':       best_pdu['bits'],
            'id':         best_pdu['id'],
        })

    # ── SDRTerm plugin API ──────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        # Enqueue raw IQ for the worker (bg queue).  Backpressure: if
        # the queue is already full, drop this chunk.
        with self._iq_lock:
            if len(self._iq_queue) >= _MAX_IQ_QUEUE:
                self._n_dropped += 1
            else:
                self._iq_queue.append(samples.astype(np.complex64, copy=False))
        return {
            'n_bursts':   self._n_bursts,
            'n_a_ok':     self._n_a_ok,
            'n_dropped':  self._n_dropped,
            'queue_len':  len(self._iq_queue),
            'messages':   list(self._messages),
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._messages.clear()
            self._n_bursts = 0
            self._n_a_ok = 0
            self._n_dropped = 0
            return True
        if key == ord('b'):
            self._try_both = not self._try_both
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        return '[IR-decode {}/{} A:OK, q={}] '.format(
            result.get('n_a_ok', 0),
            result.get('n_bursts', 0),
            result.get('queue_len', 0))

    # ── full-view tab ───────────────────────────────────────────────────

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        import curses
        if not result:
            return

        header = 'Iridium Decoder (gr-iridium port · in-process)'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        nb = result.get('n_bursts', 0)
        na = result.get('n_a_ok', 0)
        pct = (100.0 * na / nb) if nb else 0.0
        both = 'ON' if self._try_both else 'off'
        stats = ('Detected: {}   A:OK (UW): {} ({:.0f}%)   '
                 'Queue: {}   Dropped: {}   both-bin: {}').format(
            nb, na, pct,
            result.get('queue_len', 0),
            result.get('n_dropped', 0),
            both)
        try:
            screen_obj.addstr(3, 2, stats[:cols - 4], curses.A_BOLD)
        except curses.error:
            pass

        if nb == 0 and na == 0:
            try:
                screen_obj.addstr(5, 2,
                    'Waiting for bursts.  This plugin runs the full gr-'
                    'iridium DSP chain in-process on raw SDR samples — '
                    'no need to enable the iridium plugin separately.')
            except curses.error:
                pass
            return

        col_hdr = ('  {:>10s}  {:>10s}  {:>3s}  {:>4s}  {:>4s}  '
                   '{:>6s}  {}').format(
            'time/ms', 'freq/MHz', 'dir', 'syms', 'conf', 'SNR/dB',
            'first bits (2b/sym, gr-iridium format)')
        try:
            screen_obj.addstr(5, 2, col_hdr[:cols - 4], curses.A_UNDERLINE)
        except curses.error:
            pass

        y = 6
        for m in result.get('messages', []):
            if y >= rows - 2:
                break
            direction = 'DL' if m['direction'] == iridium.DOWNLINK else 'UL'
            snr = m['magnitude']
            bits_shown = ''.join(str(b_) for b_ in m['bits'][:_BITS_SHOWN])
            line = ('  {:10.2f}  {:10.4f}  {:>3s}  {:>4d}  '
                    '{:>3d}%  {:+5.1f}   {}').format(
                m['ts_ms'],
                m['freq_hz'] / 1e6,
                direction,
                m['n_symbols'],
                m['confidence'],
                snr,
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
