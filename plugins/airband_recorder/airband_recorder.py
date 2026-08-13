"""
Airband voice recorder — captures AM voice transmissions on a
user-defined list of frequencies within the VHF airband (nominally
118–137 MHz).  MVP scope: fixed channel list configured in the preset.
Wideband channel discovery (scan the whole slice, record whatever pops
up) is left for a follow-up PR — see ideas.md.

Signal path per channel (one instance of the pipeline per configured
frequency):

  IQ @ state.bw_hz
    → complex NCO downconvert  (target freq → DC, continuous phase across chunks)
    → FIR LPF at 6 kHz
    → decimate to 25 kHz  (fits a legacy 25 kHz AM airband channel)
    → AM envelope |x|
    → detrend (subtract mean = remove carrier level, keep audio)
    → squelch decision (rolling RMS + hysteresis + hangover)
    → resample to _audio_rate_hz
    → append to per-channel WAV file when squelch is OPEN
    → on close: append row to CSV index

Config lives in the preset under plugin_states.airband_recorder:

  "airband_recorder": {
    "enabled":            true,
    "output_dir":         "airband_recordings",
    "squelch_dbfs":       -55.0,
    "silence_hangover_s": 2.0,
    "audio_rate_hz":      8000,
    "channels": [
      {"freq": 118500000, "name": "Tower"},
      {"freq": 121900000, "name": "Ground"},
      ...
    ]
  }

Output structure:

  airband_recordings/
    index.csv       ─ one row per completed transmission
    2026-08-13_12-34-56_118.500MHz_Tower.wav
    ...
"""

import csv
import curses
import os
import time
import wave
from datetime import datetime, timezone

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi, resample_poly

from core import Decoder, AppState


# ── configuration defaults ─────────────────────────────────────────────────────
_DEFAULT_OUTPUT_DIR    = 'airband_recordings'
_INDEX_FILE_NAME       = 'index.csv'
_INDEX_HEADER          = 'timestamp_utc,freq_hz,name,duration_s,peak_dbfs,filename\n'

_CHANNEL_SR            = 25_000       # sample rate inside per-channel demod
_CHANNEL_LPF_HZ        = 6_000        # LPF cutoff before decimation
_DEFAULT_AUDIO_HZ      = 8_000        # WAV sample rate for output

_DEFAULT_SQUELCH_DBFS  = -55.0
_DEFAULT_HANGOVER_S    = 2.0
_SQUELCH_OPEN_MARGIN   = 3.0          # dB above threshold to open (hysteresis)
_MIN_DURATION_S        = 0.4          # discard transmissions shorter than this


class _ChannelState:
    """Everything the plugin needs to remember between chunks for one channel."""

    def __init__(self, freq_hz: float, name: str):
        self.freq_hz         = float(freq_hz)
        self.name            = name or _mhz_str(freq_hz)
        # Squelch
        self.rms_db          = -120.0
        self.is_open         = False
        self.opened_at_ts    = 0.0
        self.last_active_ts  = 0.0
        self.peak_dbfs       = -120.0
        # WAV writer
        self.wav_file        = None      # wave.Wave_write | None
        self.wav_path        = None      # str | None
        # NCO / filter state — carries across chunks so we don't get
        # per-chunk boundary artefacts.
        self._nco_phase      = 0.0
        self._lp_taps        = None
        self._lp_zi_i        = None
        self._lp_zi_q        = None
        self._decim_factor   = None
        self._configured_sr  = None


class AirbandRecorderDecoder(Decoder):
    name            = 'airband_recorder'
    key             = 'A'
    key_help        = 'r=reset stats'
    min_sample_rate = 2_000_000       # need enough BW to span the channel list
    realtime        = False
    bg_queue_depth  = 4
    full_view       = True

    def __init__(self):
        self._channels: list = []
        self._output_dir      = _DEFAULT_OUTPUT_DIR
        self._squelch_dbfs    = _DEFAULT_SQUELCH_DBFS
        self._hangover_s      = _DEFAULT_HANGOVER_S
        self._audio_rate_hz   = _DEFAULT_AUDIO_HZ
        self._enabled         = False
        self._n_recorded      = 0
        self._n_dropped_short = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        # Reset per-channel WAV state; keep configured channel list.
        for c in self._channels:
            self._abort_wav(c)

    def stop(self) -> None:
        for c in self._channels:
            self._abort_wav(c)

    # ── preset persistence ────────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {
            'enabled':            self._enabled,
            'output_dir':         self._output_dir,
            'squelch_dbfs':       self._squelch_dbfs,
            'silence_hangover_s': self._hangover_s,
            'audio_rate_hz':      self._audio_rate_hz,
            'channels':           [{'freq': c.freq_hz, 'name': c.name}
                                   for c in self._channels],
        }

    def load_state(self, d: dict) -> None:
        self._enabled       = bool(d.get('enabled', False))
        self._output_dir    = str(d.get('output_dir', _DEFAULT_OUTPUT_DIR))
        self._squelch_dbfs  = float(d.get('squelch_dbfs',       _DEFAULT_SQUELCH_DBFS))
        self._hangover_s    = float(d.get('silence_hangover_s', _DEFAULT_HANGOVER_S))
        self._audio_rate_hz = int(d.get('audio_rate_hz',        _DEFAULT_AUDIO_HZ))
        # Rebuild channel list — close any prior WAVs first
        for c in self._channels:
            self._abort_wav(c)
        self._channels = [
            _ChannelState(float(spec['freq']), spec.get('name', ''))
            for spec in d.get('channels', [])
            if isinstance(spec, dict) and 'freq' in spec
        ]

    # ── main processing ───────────────────────────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if not self._enabled or not self._channels:
            return {'enabled': False, 'active': 0,
                    'channels': [], 'recorded_total': self._n_recorded}

        sr = int(state.bw_hz)
        now_ts = time.time()

        # File-replay handling: mirror what acars/vdl2 do — samples' true
        # centre is the file's recorded centre, not state.center_hz (which
        # follows user tuning during playback).
        file_center = getattr(sdr, '_file_center_hz', None)
        rf_center_hz = file_center if file_center is not None else state.center_hz

        chan_status = []
        n_active = 0
        for c in self._channels:
            offset = c.freq_hz - rf_center_hz
            in_range = abs(offset) < sr / 2 * 0.9     # keep 10% guardband
            if not in_range:
                chan_status.append(self._status_dict(c, in_range=False))
                continue

            env = self._demod_channel(samples, sr, offset, c)
            if env is None or len(env) == 0:
                chan_status.append(self._status_dict(c, in_range=True))
                continue

            # Squelch on the envelope.  Skip the first ~1% of samples
            # when computing RMS — the LPF's state carried across from
            # the previous chunk produces a big spike in env[0..N-taps]
            # (up to full-scale) that would inflate the RMS of an
            # otherwise-silent chunk.
            skip   = max(1, len(env) // 100)
            body   = env[skip:]
            rms    = float(np.sqrt(float(np.mean(body * body)) + 1e-20))
            rms_db = 20.0 * float(np.log10(rms + 1e-20))
            # Use per-chunk RMS directly for the squelch decision — no
            # smoothing.  Rationale: the hangover already absorbs brief
            # dips, and _MIN_DURATION_S filters out spurious triggers
            # by deleting sub-threshold WAVs after close.  Smoothing
            # would make the close-side slow to react on the natural
            # rms drop from the previous chunk's smoothed value.
            c.rms_db = rms_db

            if c.is_open:
                if c.rms_db > self._squelch_dbfs:
                    c.last_active_ts = now_ts
                if c.rms_db > c.peak_dbfs:
                    c.peak_dbfs = c.rms_db
                self._append_wav(c, env)
                if (now_ts - c.last_active_ts) > self._hangover_s:
                    self._finalise_wav(c, now_ts)
                    c.is_open = False
            else:
                if c.rms_db > self._squelch_dbfs + _SQUELCH_OPEN_MARGIN:
                    self._open_wav(c, now_ts)
                    if c.wav_file is not None:
                        c.is_open = True
                        c.opened_at_ts = now_ts
                        c.last_active_ts = now_ts
                        c.peak_dbfs = c.rms_db
                        self._append_wav(c, env)   # capture the opening block too

            if c.is_open:
                n_active += 1
            chan_status.append(self._status_dict(c, in_range=True))

        return {
            'enabled':        True,
            'active':         n_active,
            'channels':       chan_status,
            'recorded_total': self._n_recorded,
            'dropped_short':  self._n_dropped_short,
        }

    def _status_dict(self, c: '_ChannelState', in_range: bool) -> dict:
        return {
            'freq_hz':   c.freq_hz,
            'name':      c.name,
            'in_range':  in_range,
            'is_open':   c.is_open,
            'rms_db':    c.rms_db,
            'peak_dbfs': c.peak_dbfs if c.is_open else None,
            'wav_path':  c.wav_path if c.is_open else None,
        }

    # ── per-channel DSP ───────────────────────────────────────────────────────

    def _demod_channel(self, samples, sr, offset_hz, c: '_ChannelState'):
        """Complex-mix samples down by offset_hz, LPF, decimate to
        _CHANNEL_SR, return AM envelope samples (float32, DC-detrended)."""

        # Rebuild filter + decimation ratio if sample rate changed.
        if c._configured_sr != sr:
            c._decim_factor = max(1, int(round(sr / _CHANNEL_SR)))
            c._lp_taps      = firwin(64, _CHANNEL_LPF_HZ / (sr / 2)).astype(np.float64)
            c._lp_zi_i      = None
            c._lp_zi_q      = None
            c._configured_sr = sr

        n = len(samples)
        # Continuous-phase NCO — no per-chunk boundary steps (same reason
        # the FM DC-blocker uses a stateful IIR instead of per-chunk mean).
        t = np.arange(n, dtype=np.float64)
        phase = c._nco_phase + 2.0 * np.pi * offset_hz * t / sr
        nco = np.exp(-1j * phase).astype(np.complex64)
        c._nco_phase = (c._nco_phase + 2.0 * np.pi * offset_hz * n / sr) \
                       % (2.0 * np.pi)
        mixed = samples * nco

        i_in = mixed.real.astype(np.float64)
        q_in = mixed.imag.astype(np.float64)
        if c._lp_zi_i is None:
            c._lp_zi_i = lfilter_zi(c._lp_taps, [1.0]) * i_in[0]
            c._lp_zi_q = lfilter_zi(c._lp_taps, [1.0]) * q_in[0]
        i_f, c._lp_zi_i = lfilter(c._lp_taps, [1.0], i_in, zi=c._lp_zi_i)
        q_f, c._lp_zi_q = lfilter(c._lp_taps, [1.0], q_in, zi=c._lp_zi_q)

        i_dec = i_f[::c._decim_factor]
        q_dec = q_f[::c._decim_factor]

        env = np.sqrt(i_dec * i_dec + q_dec * q_dec).astype(np.float32)
        env -= float(np.mean(env))          # detrend: remove carrier level
        return env

    # ── WAV writer ────────────────────────────────────────────────────────────

    def _open_wav(self, c: '_ChannelState', now_ts: float) -> None:
        try:
            os.makedirs(self._output_dir, exist_ok=True)
        except OSError:
            return
        ts_str    = datetime.fromtimestamp(now_ts, timezone.utc) \
                            .strftime('%Y-%m-%d_%H-%M-%S')
        name_safe = _safe_name(c.name)
        fname     = f'{ts_str}_{_mhz_str(c.freq_hz)}_{name_safe}.wav'
        path      = os.path.join(self._output_dir, fname)
        try:
            wf = wave.open(path, 'wb')
            wf.setnchannels(1)
            wf.setsampwidth(2)                # 16-bit PCM
            wf.setframerate(self._audio_rate_hz)
            c.wav_file = wf
            c.wav_path = path
        except (OSError, wave.Error):
            c.wav_file = None
            c.wav_path = None

    def _append_wav(self, c: '_ChannelState', env: np.ndarray) -> None:
        if c.wav_file is None:
            return
        # Resample envelope from _CHANNEL_SR to audio_rate_hz.
        if _CHANNEL_SR != self._audio_rate_hz:
            g = np.gcd(_CHANNEL_SR, self._audio_rate_hz)
            audio = resample_poly(env,
                                  self._audio_rate_hz // g,
                                  _CHANNEL_SR // g)
        else:
            audio = env
        # Normalise to int16 with a bit of headroom.  Envelope was
        # DC-detrended so it's centred at 0.
        peak = float(np.max(np.abs(audio)))
        if peak > 1e-9:
            audio_i16 = np.clip(audio / peak * 32767 * 0.9,
                                -32768, 32767).astype(np.int16)
        else:
            audio_i16 = np.zeros(len(audio), dtype=np.int16)
        try:
            c.wav_file.writeframes(audio_i16.tobytes())
        except (OSError, wave.Error):
            pass

    def _finalise_wav(self, c: '_ChannelState', now_ts: float) -> None:
        """Close the WAV, log to the CSV index — or delete it if it turned
        out to be a sub-_MIN_DURATION_S false trigger."""
        if c.wav_file is None:
            return
        try:
            c.wav_file.close()
        except (OSError, wave.Error):
            pass
        duration = now_ts - c.opened_at_ts
        path     = c.wav_path
        c.wav_file = None
        c.wav_path = None
        if duration < _MIN_DURATION_S:
            self._n_dropped_short += 1
            try:
                if path:
                    os.remove(path)
            except OSError:
                pass
            return
        self._append_index(c, duration, now_ts, path)
        self._n_recorded += 1

    def _abort_wav(self, c: '_ChannelState') -> None:
        """Called on stop() / channel-list reload — close WAV without
        indexing (partial recording, no known duration)."""
        if c.wav_file is None:
            return
        try:
            c.wav_file.close()
        except (OSError, wave.Error):
            pass
        c.wav_file = None
        c.wav_path = None
        c.is_open  = False

    def _append_index(self, c: '_ChannelState', duration: float,
                      ts: float, path: str) -> None:
        idx_path = os.path.join(self._output_dir, _INDEX_FILE_NAME)
        need_header = not os.path.isfile(idx_path)
        try:
            with open(idx_path, 'a', newline='') as f:
                if need_header:
                    f.write(_INDEX_HEADER)
                w = csv.writer(f)
                w.writerow([
                    datetime.fromtimestamp(ts, timezone.utc)
                            .strftime('%Y-%m-%dT%H:%M:%SZ'),
                    int(c.freq_hz),
                    c.name,
                    f'{duration:.2f}',
                    f'{c.peak_dbfs:.1f}',
                    os.path.basename(path or ''),
                ])
        except OSError:
            pass

    # ── keys ──────────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('r'):
            for c in self._channels:
                self._abort_wav(c)
                c.rms_db    = -120.0
                c.peak_dbfs = -120.0
            self._n_recorded      = 0
            self._n_dropped_short = 0
            return True
        return False

    # ── status bar + tab view ────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        if not result.get('enabled'):
            return '[AIR off] '
        return '[AIR {}/{} act · {} rec] '.format(
            result.get('active', 0),
            len(result.get('channels', [])),
            result.get('recorded_total', 0),
        )

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        header = 'AIRBAND VOICE RECORDER  [r=reset stats  channels from preset]'
        try:
            screen_obj.addstr(1, max(0, (cols - len(header)) // 2),
                              header[:cols - 2], curses.A_BOLD)
        except curses.error:
            pass

        if not result or not result.get('enabled'):
            try:
                screen_obj.addstr(4, 2,
                    'plugin idle — set enabled: true in preset '
                    'plugin_states.airband_recorder')
            except curses.error:
                pass
            return

        col_hdr = ' {:<12} {:<12} {:>9} {:>8} {:>7}  {}'.format(
            'FREQ [MHz]', 'NAME', 'RMS [dB]', 'PEAK', 'STATE', 'FILE')
        try:
            screen_obj.addstr(3, 2, col_hdr[:cols - 4],
                              curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass

        y = 4
        for cs in result.get('channels', []):
            if y >= rows - 2:
                break
            if not cs.get('in_range'):
                state_str = 'off-band'
            elif cs.get('is_open'):
                state_str = 'OPEN'
            else:
                state_str = 'idle'
            rms_val = cs.get('rms_db')
            rms_str = f'{rms_val:.1f}' if rms_val is not None else '?'
            pk_val  = cs.get('peak_dbfs')
            pk_str  = f'{pk_val:.1f}' if pk_val is not None else ''
            fname   = os.path.basename(cs.get('wav_path') or '') \
                      if cs.get('is_open') else ''
            room    = max(1, cols - 60)
            if len(fname) > room:
                fname = '…' + fname[-(room - 1):]
            line = ' {:<12} {:<12} {:>9} {:>8} {:>7}  {}'.format(
                f'{cs["freq_hz"] / 1e6:.3f}',
                (cs['name'] or '')[:12],
                rms_str, pk_str, state_str, fname,
            )
            try:
                attr = curses.A_BOLD if cs.get('is_open') else 0
                screen_obj.addstr(y, 2, line[:cols - 4], attr)
            except curses.error:
                pass
            y += 1

        if y < rows - 1:
            summary = ('total recorded: {}   dropped (too short): {}   '
                       'output: {}').format(
                result.get('recorded_total', 0),
                result.get('dropped_short',  0),
                self._output_dir)
            try:
                screen_obj.addstr(rows - 2, 2, summary[:cols - 4])
            except curses.error:
                pass


# ── module helpers ───────────────────────────────────────────────────────────

def _mhz_str(hz: float) -> str:
    return f'{hz / 1e6:.3f}MHz'


def _safe_name(s: str) -> str:
    """Make a filesystem-safe channel-name suffix.  ASCII-alnum plus a few
    common punctuation chars only; everything else becomes '_'."""
    cleaned = ''.join(c if (c.isalnum() or c in '-_.') else '_' for c in s)
    return cleaned[:40] or 'ch'
