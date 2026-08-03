"""In-memory hand-off queue between the iridium (Stage 1) capture path and
the iridium_decoder (Stage 3) demodulator.

Design constraints:
  - Zero-cost when the decoder plugin isn't active.  The iridium plugin
    imports this module lazily and only channelises + pushes if
    `has_consumers()` returns True.
  - Bounded + drop-on-full.  Same policy as the spectrum plugin — during
    a satellite pass with heavy activity, we prefer real-time
    responsiveness over completeness.  Dropped bursts are counted so
    the decoder plugin's status line can surface the rate.
  - No hard dependency between plugins.  The iridium plugin catches
    ImportError so users without iridium_decoder installed see no
    behaviour change.

The queue itself is a stdlib `queue.Queue` (thread-safe, single-process).
For true multiprocessing we'd use `multiprocessing.Queue`, but the
iridium plugin runs in the main SDRTerm process alongside the decoder,
so an intra-process queue is sufficient and avoids pickle overhead on
push.  Worker processes get burst payloads via the pool's built-in
task-submission mechanism, not via this queue.
"""
import queue

# Bounded capacity — 256 bursts × ~4 KB narrow-band IQ = ~1 MB worst case.
_MAX = 256

_q = queue.Queue(maxsize=_MAX)
_consumers = 0            # bumped by decoder plugin on start(), decremented on stop()
_drop_count = 0           # cumulative bursts dropped due to full queue


def has_consumers() -> bool:
    """True when at least one decoder plugin is active and listening."""
    return _consumers > 0


def register_consumer() -> None:
    global _consumers
    _consumers += 1


def unregister_consumer() -> None:
    global _consumers
    _consumers = max(0, _consumers - 1)
    # Drain any pending items when the last consumer leaves — otherwise
    # they'd sit in memory until the process exits, and the next
    # register_consumer() would find stale bursts from a previous session.
    if _consumers == 0:
        try:
            while True:
                _q.get_nowait()
        except queue.Empty:
            pass


def push(burst: dict) -> bool:
    """Non-blocking push.  Returns True if enqueued, False if dropped.

    `burst` must be a dict with at least:
      iq         : np.ndarray (complex64), narrow-band centred on channel
      sample_rate: int (Hz after decimation, e.g. 50 000)
      chan_id    : int (0..251)
      chan_freq  : float (Hz — original channel absolute frequency)
      snr_db     : float
      timestamp  : str (ISO)
    """
    global _drop_count
    try:
        _q.put_nowait(burst)
        return True
    except queue.Full:
        _drop_count += 1
        return False


def pop(timeout: float = 0.1):
    """Blocking pop with timeout.  Returns None on timeout."""
    try:
        return _q.get(timeout=timeout)
    except queue.Empty:
        return None


def depth() -> int:
    """Approximate current queue depth."""
    return _q.qsize()


def drop_count() -> int:
    return _drop_count


def reset_drop_count() -> None:
    global _drop_count
    _drop_count = 0
