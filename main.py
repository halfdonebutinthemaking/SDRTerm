#!/usr/bin/env python3
import os, time, curses, threading, queue
from collections import deque
import numpy as np

# Homebrew on Apple Silicon installs to /opt/homebrew/lib, which is not in the
# default dyld search path. Set it before rtlsdr triggers dlopen().
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')

from core import (
    AppState, Device, parse_freq,
    FFT_BINS, N_AVG, REFRESH_S, READ_MAX,
    _required_bw, _nearest_bw,
)
from devices import load_devices, open_first_device, open_device_by_name, open_file_device
from plugins import load_plugins
from presets import _PRESET_FIELDS, _save_preset_to, _load_preset, _find_presets, _apply_preset, _migrate_preset
from render import draw
from keys import handle_keys


# ── main curses loop ──────────────────────────────────────────────────────────
def _curses_main(stdscr: curses.window, sdr: Device, state: AppState) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)

    registry = load_plugins()
    # Discard any preset-loaded decoder names that don't exist in this registry
    state.active_decoders = (state.active_decoders & set(registry.keys())) | {'spectrum'}
    registry['spectrum'].start(state)
    # Start any additional decoders loaded from a preset
    for _name in list(state.active_decoders):
        if _name != 'spectrum' and _name in registry:
            registry[_name].start(state)

    # stable ordered list of all toggleable plugins (used for the menu)
    all_plugins = [p for p in registry.values() if p.key]
    if state.plugin_order:
        _om = {name: i for i, name in enumerate(state.plugin_order)}
        all_plugins.sort(key=lambda p: _om.get(p.name, len(state.plugin_order)))

    # Clamp bw_hz to the nearest value the device actually supports.
    # Matters when --bw was given or a preset had a different BW.
    if state.bw_hz not in sdr.supported_bandwidths:
        state.bw_hz = min(sdr.supported_bandwidths,
                          key=lambda b: abs(b - state.bw_hz))
    # Ensure BW is sufficient for all active decoders
    _min_needed = _required_bw(state.active_decoders, registry)
    if state.bw_hz < _min_needed:
        state.bw_hz = _nearest_bw(_min_needed, sdr.supported_bandwidths)

    # For file devices: latch _file_center_hz to the initial display centre
    # if it wasn't already set from metadata or the filename.  This ensures
    # the spectrum-roll mechanism (used by follow mode) has a stable reference
    # even for plain .iq / .wav files without embedded frequency information.
    if hasattr(sdr, '_file_center_hz') and sdr._file_center_hz is None:
        sdr._file_center_hz = state.center_hz

    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = 'auto' if state.gain_auto else state.gain_db

    SPEC_NEED = FFT_BINS * N_AVG
    iq_deque  = deque(maxlen=64)
    wf_rows   = deque(maxlen=256)   # one mags_db array per rendered spectrum frame
    results: dict = {}
    stop_evt  = threading.Event()

    # ── tiered plugin execution ───────────────────────────────────────────────
    # realtime=True  → process() called inline in the SDR callback (same thread,
    #                  zero latency; must be fast — FM, RDS, record).
    # realtime=False → samples pushed to a per-plugin bounded queue; a dedicated
    #                  daemon thread drains it at whatever pace it can sustain.
    #                  If the queue fills the chunk is silently dropped (display
    #                  plugins tolerate this; audio plugins never land here).
    bg_queues = {
        p.name: queue.Queue(maxsize=p.bg_queue_depth)
        for p in all_plugins if not p.realtime
    }

    def _bg_worker(plugin, q):
        while not stop_evt.is_set():
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:       # shutdown sentinel
                break
            r = plugin.process(chunk, state, results, sdr)
            if r is not None:
                results[plugin.name] = r

    bg_workers = [
        threading.Thread(target=_bg_worker,
                         args=(p, bg_queues[p.name]),
                         name='bg-' + p.name, daemon=True)
        for p in all_plugins if not p.realtime
    ]
    for t in bg_workers:
        t.start()

    def _sdr_cb(samples, _ctx):
        if stop_evt.is_set():
            return
        # Real-time pass: run audio/record plugins sorted by descending priority
        # so high-priority plugins (FM=10, RDS=5) always run before record (0),
        # regardless of the user's pipeline order.
        frame_results = {}
        prev_plugin   = None
        rt_active = sorted(
            (p for p in all_plugins
             if p.realtime and p.name in state.active_decoders),
            key=lambda p: -p.priority,
        )
        for plugin in rt_active:
            frame_results['_prev_plugin'] = prev_plugin
            r = plugin.process(samples, state, frame_results, sdr)
            if r is not None:
                frame_results[plugin.name] = r
            prev_plugin = plugin
        results.update(frame_results)

        # Background pass: push samples to each worker queue (non-blocking).
        for plugin in all_plugins:
            if not plugin.realtime and plugin.name in state.active_decoders:
                try:
                    bg_queues[plugin.name].put_nowait(samples)
                except queue.Full:
                    pass   # worker is behind; drop this chunk

        iq_deque.append(samples)

    reader: list = [None]

    def _start_reader():
        if reader[0] and reader[0].is_alive():
            sdr.cancel_read_async()
            reader[0].join(timeout=2.0)
            if reader[0].is_alive():
                # Async read didn't stop in time — skip and let the debounce
                # timer retry on the next loop iteration.
                return
            # Reopen the device to get a clean C-level handle.  On macOS,
            # libusb/IOKit leaves lingering USB transfer state after
            # rtlsdr_read_async() returns; reusing the same handle for the
            # next call causes SIGABRT.  close()+open() is the safe path.
            sdr.reopen()
            sdr.center_freq = state.center_hz
            sdr.gain        = 'auto' if state.gain_auto else state.gain_db
        sdr.sample_rate = state.bw_hz
        def _run():
            sdr.read_samples_async(_sdr_cb, num_samples=READ_MAX)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        reader[0] = t

    _start_reader()

    spec_chunks: list = []
    spec_count  = 0
    last_draw   = 0.0
    last_bw     = state.bw_hz
    bw_change_t = 0.0          # monotonic time of last bw_hz mutation
    BW_DEBOUNCE = 0.15         # seconds to wait after last BW keypress before restarting

    try:
        while not state.quit:
            # derive tab order from currently enabled plugins
            tab_plugins = [p for p in all_plugins
                           if p.name in state.active_decoders]
            state.tab_idx = min(state.tab_idx, len(tab_plugins))

            key = stdscr.getch()
            handle_keys(key, stdscr, state, registry,
                        tab_plugins, all_plugins, sdr, results, wf_rows)

            # Hardware changes queued by active RTL-TCP plugin.
            # Applied here (main loop) not in process() to avoid calling
            # sdr.* from inside the librtlsdr async-read callback, which
            # deadlocks libusb's internal event-loop locks.
            if state.pending_freq is not None:
                state.center_hz    = state.pending_freq
                sdr.center_freq    = state.pending_freq
                state.pending_freq = None
                iq_deque.clear()
                spec_chunks.clear()
                spec_count = 0

            if state.pending_gain is not None:
                if state.pending_gain < 0:
                    state.gain_auto = True
                    sdr.gain        = 'auto'
                else:
                    state.gain_auto = False
                    state.gain_db   = state.pending_gain
                    sdr.gain        = state.pending_gain
                state.pending_gain = None

            if state.pending_sr is not None:
                state.bw_hz      = _nearest_bw(state.pending_sr, sdr.supported_bandwidths)
                state.pending_sr = None
                # bw_hz changed → the check below cancels old reader and restarts
                # with sdr.sample_rate set safely inside _start_reader()

            if state.bw_hz != last_bw:
                # BW changed — flush stale data and arm the debounce timer.
                # Don't restart the reader yet; rapid keypresses keep pushing
                # this forward so the reader only restarts once settled.
                last_bw     = state.bw_hz
                bw_change_t = time.monotonic()
                spec_chunks.clear()
                spec_count = 0
                wf_rows.clear()
                iq_deque.clear()

            if bw_change_t and time.monotonic() - bw_change_t >= BW_DEBOUNCE:
                before = reader[0]
                _start_reader()
                if reader[0] is not before:
                    # new reader was started — debounce satisfied
                    bw_change_t = 0.0
                # else: old reader still alive after timeout; keep bw_change_t
                # set so the main loop retries on the next tick

            while iq_deque:
                try:
                    c = iq_deque.popleft()
                    spec_chunks.append(c)
                    spec_count += len(c)
                except IndexError:
                    break

            now = time.monotonic()
            if now - last_draw >= REFRESH_S and spec_count >= SPEC_NEED:
                samples = np.concatenate(spec_chunks)
                results['spectrum'] = registry['spectrum'].process(
                    samples[:SPEC_NEED], state, results, sdr)
                spec_chunks.clear()
                spec_count = 0
                wf_rows.append(results['spectrum']['mags_db'].copy())
                draw(stdscr, state, results, registry, tab_plugins, all_plugins, sdr, wf_rows)
                last_draw = now
            elif key == -1:
                time.sleep(0.002)

    finally:
        stop_evt.set()
        sdr.cancel_read_async()
        if reader[0]:
            reader[0].join(timeout=2.0)
        # Send shutdown sentinels so workers blocked on q.get() exit immediately.
        for q in bg_queues.values():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass   # stop_evt is set; worker exits on its next Empty timeout
        for t in bg_workers:
            t.join(timeout=1.0)
        for name in list(state.active_decoders):
            registry[name].stop()
        sdr.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog='sdrterm',
        description='SDRTerm — terminal SDR spectrum analyser',
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--d', metavar='NAME',
                        help='device to open, e.g. RTL-SDR-V3\n'
                             'omit to use the first available device')
    parser.add_argument('--file', metavar='PATH',
                        help='replay a .iq (raw complex64) or stereo .wav IQ file '
                             '(selects the localfile device; WAV sample rate is read from the file header)')
    parser.add_argument('--bw', metavar='BW',
                        help='capture bandwidth / sample rate, e.g. 2.4M, 1024k, 250000\n'
                             'for real hardware: sets the initial sample rate\n'
                             'for --file .iq: must match the rate used when recording\n'
                             'for --file .wav: overrides the rate from the file header')
    parser.add_argument('--f', metavar='FREQ',
                        help='centre frequency, e.g. 105.8M, 1420000000')
    parser.add_argument('--g', metavar='GAIN',
                        help='gain in dB (e.g. 30.0) or "auto"')
    parser.add_argument('--i', metavar='on|off', choices=['on', 'off'],
                        help='enable IQ correction at startup')
    parser.add_argument('--preset', metavar='FILE',
                        help='load a .sdrterm preset file (overrides all other settings)')
    args = parser.parse_args()

    # build initial AppState from CLI overrides
    state = AppState()
    if args.f:
        hz = parse_freq(args.f)
        if hz is None:
            parser.error('invalid frequency: {}'.format(args.f))
        state.center_hz = hz
    bw_hz = None
    if args.bw:
        bw_hz = parse_freq(args.bw)
        if bw_hz is None:
            parser.error('invalid --bw value: {}'.format(args.bw))
        bw_hz = int(bw_hz)
        state.bw_hz = bw_hz   # clamped to device's supported list in _curses_main
    if args.g:
        if args.g.lower() == 'auto':
            state.gain_auto = True
        else:
            try:
                state.gain_db = float(args.g)
            except ValueError:
                parser.error('invalid gain value: {}'.format(args.g))
    if args.i:
        state.iq_corr = (args.i == 'on')
    # --preset overrides everything set above
    if args.preset:
        if not _load_preset(args.preset, state):
            parser.error('cannot load preset: {}'.format(args.preset))

    # suppress librtlsdr/libusb noise on stderr for the entire session
    devnull  = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    error_msg = None
    try:
        if args.file:
            sdr = open_file_device(args.file, bw_hz)
            if sdr is None:
                error_msg = 'cannot open file: {}'.format(args.file)
            elif not args.f:
                # Use centre frequency embedded in the file (SigMF meta or
                # filename pattern) as the initial display centre when --f
                # was not supplied explicitly.
                file_center = getattr(sdr, '_file_center_hz', None)
                if file_center is not None:
                    state.center_hz = file_center
        elif args.d:
            sdr = open_device_by_name(args.d)
            if sdr is None:
                known = ', '.join(d.name for d in load_devices()) or 'none'
                error_msg = 'device not found: {}  (known drivers: {})'.format(
                    args.d, known)
        else:
            sdr = open_first_device()
            if sdr is None:
                error_msg = 'no device found'
        if error_msg is None:
            curses.wrapper(lambda stdscr: _curses_main(stdscr, sdr, state))
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
    if error_msg:
        print(error_msg)


if __name__ == '__main__':
    main()
