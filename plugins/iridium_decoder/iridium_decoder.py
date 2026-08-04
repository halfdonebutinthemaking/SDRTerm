"""iridium_decoder — real Iridium demodulator (gr-iridium toolkit port).

Runs the complete gr-iridium DSP chain in-process:

    fft_burst_tagger  →  burst_downmix  →  qpsk_demod  →  bits + UW check

on the raw IQ stream from the SDR.  Emits gr-iridium-compatible RAW:
lines suitable for feeding into iridium-toolkit's iridium-parser.py
for frame classification.

Optionally spawns iridium-parser.py as a subprocess, pipes each
decoded RAW: line into it, and displays the parsed message types
(VOC / IRI / ISY / IU3 / IBC / IME / ...) via the `m` view toggle.

Runs independently of the iridium (Stage 1) plugin — the two plugins
serve different purposes:
  - iridium              : burst detection & channel-hit statistics
                           (display of what's on-air)
  - iridium_decoder      : actual message decoding (this plugin)
"""
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path

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

# View modes for the full-view tab
_VIEW_BITS     = 0   # raw demoded bits per burst (default)
_VIEW_MESSAGES = 1   # parsed message types via iridium-parser subprocess


def _find_iridium_parser() -> tuple:
    """Locate iridium-parser.py.  Returns (parser_path, cwd) or (None, None).
    Search order: $IRIDIUM_PARSER env var, then known user location,
    then `iridium-parser.py` in PATH."""
    env = os.environ.get('IRIDIUM_PARSER')
    if env and Path(env).is_file():
        return env, str(Path(env).parent)
    candidates = [
        Path.home() / 'Projects/Hardware/sdr/iridium_decode/iridium-toolkit/iridium-parser.py',
        Path.home() / 'iridium-toolkit/iridium-parser.py',
    ]
    for p in candidates:
        if p.is_file():
            return str(p), str(p.parent)
    which = shutil.which('iridium-parser.py')
    if which:
        return which, str(Path(which).parent)
    return None, None


class IridiumDecoderPlugin(Decoder):
    name            = 'iridium_decode'
    key             = 'j'
    key_help        = 'r=clear  b=both-bin  m=view'
    min_sample_rate = 2_000_000
    realtime        = False      # runs in bg worker; process() just enqueues
    bg_queue_depth  = 8
    full_view       = True

    def __init__(self):
        self._messages    = deque(maxlen=_MAX_MESSAGES)
        # Parsed messages from iridium-parser (view mode 1).  Same
        # maxlen so both views scroll at similar rates.
        self._parsed      = deque(maxlen=_MAX_MESSAGES)
        self._view        = _VIEW_BITS
        self._n_bursts    = 0     # detector output count
        self._n_a_ok      = 0     # unique bursts that passed UW check
        self._n_dropped   = 0     # IQ chunks dropped due to backpressure
        self._try_both    = _DEFAULT_TRY_BOTH_BINS
        self._iq_queue: deque = deque()
        self._iq_lock     = threading.Lock()
        self._worker      = None
        self._stop_evt    = threading.Event()
        # iridium-parser subprocess (lazy-started on first view toggle)
        self._parser_proc = None
        self._parser_path: str = None
        self._parser_cwd:  str = None
        self._parser_reader_thread = None
        self._parser_available = None   # None=untested, True/False=cached
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
        self._stop_parser()

    # ── iridium-parser subprocess (view mode 1) ─────────────────────────

    def _ensure_parser(self) -> bool:
        """Start iridium-parser.py subprocess on demand.  Returns True if
        parsing is available."""
        if self._parser_proc is not None and self._parser_proc.poll() is None:
            return True
        if self._parser_available is False:
            return False
        self._parser_path, self._parser_cwd = _find_iridium_parser()
        if self._parser_path is None:
            self._parser_available = False
            return False
        try:
            self._parser_proc = subprocess.Popen(
                ['python3', '-u', self._parser_path, '--uw-ec', '--harder', '-'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self._parser_cwd,
                text=True, bufsize=1,
            )
        except Exception:
            self._parser_available = False
            return False
        self._parser_available = True
        # Backfill: feed any already-decoded bursts so the parsed view
        # shows history as soon as it's toggled on.
        for m in list(self._messages)[::-1]:   # oldest first
            self._feed_parser(m)
        # Reader thread
        self._parser_reader_thread = threading.Thread(
            target=self._parser_reader_loop,
            name='iridium-parser-reader', daemon=True)
        self._parser_reader_thread.start()
        return True

    def _stop_parser(self):
        if self._parser_proc is not None:
            try:
                self._parser_proc.stdin.close()
                self._parser_proc.terminate()
                self._parser_proc.wait(timeout=1.0)
            except Exception:
                pass
            self._parser_proc = None

    def _feed_parser(self, msg: dict):
        """Write one RAW: line to iridium-parser's stdin."""
        if self._parser_proc is None or self._parser_proc.stdin is None:
            return
        if self._parser_proc.poll() is not None:
            self._parser_proc = None
            return
        bits_str = ''.join(str(b) for b in msg['bits'])
        line = ("RAW: live {ts:012.4f} {freq:010d} "
                "N:{mag:05.2f}{noise:+06.2f} I:{id:011d} "
                "{conf:3d}% {level:.5f} {nsyms:3d} {bits}\n").format(
            ts=msg['ts_ms'], freq=int(msg['freq_hz']),
            mag=msg['magnitude'], noise=msg['noise'],
            id=msg['id'], conf=msg['confidence'],
            level=0.02, nsyms=msg['n_symbols'] - iridium.UW_LENGTH,
            bits=bits_str)
        try:
            self._parser_proc.stdin.write(line)
            self._parser_proc.stdin.flush()
        except Exception:
            self._parser_proc = None

    def _parser_reader_loop(self):
        """Read parsed lines from iridium-parser stdout and cache them."""
        while (self._parser_proc is not None
               and self._parser_proc.poll() is None
               and not self._stop_evt.is_set()):
            try:
                line = self._parser_proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.rstrip()
            if not line:
                continue
            # Skip parser's own error / warning lines
            if line.startswith(('ERR:', 'Warning:')):
                continue
            self._parsed.appendleft(line)

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
        msg = {
            'ts_ms':      best_pdu['timestamp'] / 1e6,
            'freq_hz':    best_pdu['center_frequency'],
            'direction':  best_pdu['direction'],
            'confidence': best_pdu['confidence'],
            'n_symbols':  best_pdu['n_symbols'],
            'magnitude':  best_pdu['magnitude'],
            'noise':      best_pdu['noise'],
            'bits':       best_pdu['bits'],
            'id':         best_pdu['id'],
        }
        self._messages.appendleft(msg)
        # Also feed the parser if it's running (so message view stays
        # live even when we're currently viewing bits).
        if self._parser_proc is not None:
            self._feed_parser(msg)

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
            'parsed':     list(self._parsed),
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            self._messages.clear()
            self._parsed.clear()
            self._n_bursts = 0
            self._n_a_ok = 0
            self._n_dropped = 0
            return True
        if key == ord('b'):
            self._try_both = not self._try_both
            return True
        if key == ord('m'):
            # Toggle bits ↔ messages view.  Start iridium-parser lazily
            # on first switch to message view.
            self._view = _VIEW_MESSAGES if self._view == _VIEW_BITS else _VIEW_BITS
            if self._view == _VIEW_MESSAGES:
                self._ensure_parser()
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

        view_label = 'messages' if self._view == _VIEW_MESSAGES else 'bits'
        header = 'Iridium Decoder (gr-iridium port · in-process)  [view: {}]'.format(view_label)
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

        if self._view == _VIEW_MESSAGES:
            self._draw_messages_view(screen_obj, result, rows, cols)
        else:
            self._draw_bits_view(screen_obj, result, rows, cols)

    def _draw_bits_view(self, screen_obj, result: dict, rows: int, cols: int):
        import curses
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

    def _draw_messages_view(self, screen_obj, result: dict, rows: int, cols: int):
        import curses
        if self._parser_available is False:
            msg = ('iridium-parser.py not found.  Set $IRIDIUM_PARSER '
                   'to its full path, or clone iridium-toolkit under '
                   '~/iridium-toolkit/ and press m again.')
            try:
                screen_obj.addstr(5, 2, msg[:cols - 4])
            except curses.error:
                pass
            return

        parsed = result.get('parsed', [])
        header = ('Parsed messages via {} ({} shown)'.format(
            self._parser_path or 'iridium-parser', len(parsed)))
        try:
            screen_obj.addstr(5, 2, header[:cols - 4], curses.A_UNDERLINE)
        except curses.error:
            pass

        if not parsed:
            try:
                screen_obj.addstr(7, 2,
                    'Waiting for parsed output.  If you just toggled to '
                    'this view, iridium-parser is chewing through the '
                    'backlog; results should stream in shortly.')
            except curses.error:
                pass
            return

        # Type-code first-3-chars → curses colour, if we can init
        colors = {}
        try:
            curses.init_pair(31, curses.COLOR_GREEN,   -1)   # IRI/IU3/IIU
            curses.init_pair(32, curses.COLOR_CYAN,    -1)   # VOC voice
            curses.init_pair(33, curses.COLOR_YELLOW,  -1)   # ISY/IBC broadcast
            curses.init_pair(34, curses.COLOR_MAGENTA, -1)   # IME/other
            colors = {
                'IRI': 31, 'IU3': 31, 'IIU': 31, 'IIQ': 31,
                'VOC': 32, 'VO':  32,
                'ISY': 33, 'IBC': 33, 'IRA': 33,
                'IME': 34, 'DAQ': 34, 'IAQ': 34,
            }
        except Exception:
            pass

        y = 6
        for line in parsed:
            if y >= rows - 2:
                break
            code = line[:3]
            attr = curses.color_pair(colors.get(code, 0)) if colors else 0
            try:
                screen_obj.addstr(y, 2, line[:cols - 4], attr)
            except curses.error:
                pass
            y += 1

    # ── state persistence ───────────────────────────────────────────────

    def save_state(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:
        pass
