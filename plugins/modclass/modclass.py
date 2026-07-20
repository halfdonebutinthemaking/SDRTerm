import json
import os
import numpy as np
from scipy.signal import decimate as sp_decimate
from core import Decoder, AppState

_MODEL_SR      = 200_000   # Hz — sample rate the model was trained at
_MODEL_SAMPLES = 1_024
_MODELS_DIR    = os.path.join(os.path.dirname(__file__), 'models')
_MODEL_PATH    = os.path.join(_MODELS_DIR, 'modclass_lite.onnx')
_LABELS_PATH   = os.path.join(_MODELS_DIR, 'modclass_labels.json')

# Fallback label list used when no JSON sidecar exists (synthetic-trained model)
_FALLBACK_LABELS = ['OOK', 'AM-DSB', 'WBFM', 'BPSK', 'QPSK', '8PSK', 'QAM16', 'FSK']


def _load_labels() -> list[str]:
    try:
        with open(_LABELS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _FALLBACK_LABELS


_LABELS = _load_labels()
_N_CLS  = len(_LABELS)

# Minimum peak power (dBFS) and confidence to display a result
_MIN_DB   = -70.0
_MIN_CONF =  0.55

# EMA smoothing: weight on the newest frame's probabilities.
# 0.2 → ~3 frames half-life (~450 ms at 150 ms/frame) before a label shift.
_EMA_ALPHA = 0.20

# If the tracked peak jumps more than this between frames, reset smoothing
# (signal was lost and reacquired, or peak_marker jumped to a different signal).
_RESET_HZ  = 50_000


class ModClassDecoder(Decoder):
    name            = 'modclass'
    key             = 'm'
    key_help        = '+/-=threshold'
    min_sample_rate = 250_000
    realtime        = False      # runs in background worker — inference is ~1 ms but
    bg_queue_depth  = 2          # we don't want it blocking the display loop

    def __init__(self):
        self._session        = None
        self._error          = None
        self._threshold      = _MIN_CONF
        self._label          = None
        self._conf           = 0.0
        self._peak_hz        = None
        self._smoothed_probs = None   # EMA over probability vectors
        self._raw_probs      = None   # last raw frame (shown alongside smoothed in tab)

    def start(self, state: AppState) -> None:
        self._label          = None
        self._conf           = 0.0
        self._peak_hz        = None
        self._smoothed_probs = None
        self._raw_probs      = None
        self._error          = None
        # Reload labels in case the model was retrained since import
        self._labels = _load_labels()
        self._n_cls  = len(self._labels)
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 2
            opts.log_severity_level   = 3   # suppress INFO noise
            self._session = ort.InferenceSession(_MODEL_PATH, sess_options=opts)
            self._dbg('onnxruntime session ready')
        except ImportError:
            self._error = 'onnxruntime not installed (uv add onnxruntime)'
        except FileNotFoundError:
            self._error = 'model not found — run scripts/train_modclass.py'
        except Exception as e:
            self._error = str(e)
        if self._error:
            self._dbg(f'modclass init error: {self._error}')

    def stop(self) -> None:
        self._session        = None
        self._label          = None
        self._conf           = 0.0
        self._peak_hz        = None
        self._smoothed_probs = None
        self._raw_probs      = None

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if self._session is None:
            return {'error': self._error or 'not ready'}

        # Need a tracked peak to know what frequency to classify.
        peak = (results or {}).get('peak_marker', {})
        peak_hz = peak.get('peak_hz')
        peak_db = peak.get('peak_db', -999.0)

        if peak_hz is None or peak_db < _MIN_DB:
            return {'label': None, 'conf': 0.0, 'peak_hz': None}

        window = _extract_window(samples, peak_hz,
                                 state.center_hz, state.bw_hz)
        if window is None:
            return {'label': None, 'conf': 0.0, 'peak_hz': peak_hz}

        # Normalise: zero-mean, unit std per channel
        x      = np.stack([window.real, window.imag]).astype(np.float32)
        x     -= x.mean(axis=1, keepdims=True)
        std    = x.std(axis=1, keepdims=True) + 1e-8
        x     /= std
        x      = x[np.newaxis]    # (1, 2, N)

        logits         = self._session.run(None, {'input': x})[0][0]
        raw_probs      = _softmax(logits)
        self._raw_probs = raw_probs

        # Reset EMA if the peak jumped to a different signal or on first frame.
        prev_hz = self._peak_hz
        if (self._smoothed_probs is None
                or prev_hz is None
                or abs(peak_hz - prev_hz) > _RESET_HZ
                or len(self._smoothed_probs) != self._n_cls):
            self._smoothed_probs = raw_probs.copy()
        else:
            self._smoothed_probs = (_EMA_ALPHA * raw_probs
                                    + (1.0 - _EMA_ALPHA) * self._smoothed_probs)

        self._peak_hz = peak_hz

        idx  = int(np.argmax(self._smoothed_probs))
        conf = float(self._smoothed_probs[idx])

        self._label = self._labels[idx] if conf >= self._threshold else None
        self._conf  = conf
        self._dbg(f'{peak_hz/1e6:.3f} MHz → {self._labels[idx]} {conf:.2f} '
                  f'(raw {self._labels[int(np.argmax(raw_probs))]} '
                  f'{float(raw_probs.max()):.2f})')

        return {
            'label':      self._label,
            'conf':       self._conf,
            'peak_hz':    self._peak_hz,
            'smoothed':   {self._labels[i]: float(self._smoothed_probs[i])
                           for i in range(self._n_cls)},
            'raw':        {self._labels[i]: float(raw_probs[i])
                           for i in range(self._n_cls)},
        }

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('+') or key == ord('='):
            self._threshold = min(0.95, round(self._threshold + 0.05, 2))
            return True
        if key == ord('-'):
            self._threshold = max(0.10, round(self._threshold - 0.05, 2))
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        if 'error' in result:
            return '[MOD err] '
        label = result.get('label')
        conf  = result.get('conf', 0.0)
        if label is None:
            return '[MOD ···] '
        return '[MOD {} {:.0f}%] '.format(label, conf * 100)

    def save_state(self) -> dict:
        return {'threshold': self._threshold}

    def load_state(self, d: dict) -> None:
        self._threshold = float(d.get('threshold', _MIN_CONF))

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        """Simple text panel shown when the modclass tab is active."""
        import curses
        if not result:
            return
        y = 2
        if 'error' in result:
            screen_obj.addstr(y, 2, result['error'], curses.A_BOLD)
            return

        label    = result.get('label')
        conf     = result.get('conf', 0.0)
        peak_hz  = result.get('peak_hz')
        smoothed = result.get('smoothed', {})
        raw      = result.get('raw', {})

        header = 'Modulation Classifier'
        screen_obj.addstr(y, (cols - len(header)) // 2, header, curses.A_BOLD)
        y += 2

        if peak_hz is not None:
            from core import fmt_freq
            screen_obj.addstr(y, 2, 'Peak:  {}'.format(fmt_freq(peak_hz)))
            y += 1
        if label is not None:
            screen_obj.addstr(y, 2, 'Class: {}  ({:.0f}%)'.format(label, conf * 100),
                              curses.A_BOLD)
        else:
            screen_obj.addstr(y, 2, 'Class: — (below {:.0f}% threshold)'.format(
                              self._threshold * 100))
        y += 2

        # Two-column table: smoothed (stable) vs raw (per-frame)
        if smoothed:
            col2 = max(26, cols // 2)
            hdr  = '  {:<8}  {:>6}  {:<20}   {:>6}'.format(
                   'Label', 'smooth', '', 'raw')
            try:
                screen_obj.addstr(y, 2, hdr[:cols - 4], curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            bar_w = max(4, min(20, (cols - 36) // 2))
            order = sorted(smoothed.keys(), key=lambda k: -smoothed[k])
            for lbl in order:
                sp   = smoothed.get(lbl, 0.0)
                rp   = raw.get(lbl, 0.0)
                sbar = '█' * int(sp * bar_w)
                rbar = '█' * int(rp * bar_w)
                line = '  {:<8} {:5.1f}%  {:<{bw}}   {:5.1f}%  {}'.format(
                       lbl, sp * 100, sbar, rp * 100, rbar, bw=bar_w)
                attr = curses.A_BOLD if lbl == label else 0
                try:
                    screen_obj.addstr(y, 2, line[:cols - 4], attr)
                except curses.error:
                    pass
                y += 1
                if y >= rows - 3:
                    break

        y += 1
        import math
        half_life_ms = -150 * math.log(2) / math.log(1 - _EMA_ALPHA)
        footer = ('+/- confidence threshold ({:.0f}%)   '
                  'smoothing α={:.2f}  half-life ~{:.0f} ms').format(
                  self._threshold * 100, _EMA_ALPHA, half_life_ms)
        try:
            screen_obj.addstr(min(y, rows - 2), 2, footer[:cols - 4])
        except curses.error:
            pass


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_window(samples: np.ndarray, peak_hz: float,
                    center_hz: float, bw_hz: float):
    """Shift peak to baseband, decimate to MODEL_SR, return 1024-sample window."""
    if len(samples) < _MODEL_SAMPLES:
        return None

    offset_hz = peak_hz - center_hz
    t         = np.arange(len(samples)) / bw_hz
    shifted   = samples * np.exp(-2j * np.pi * offset_hz * t)

    decim = max(1, int(round(bw_hz / _MODEL_SR)))
    if decim > 1:
        # sp_decimate operates on real arrays; split and rejoin
        try:
            re = sp_decimate(shifted.real.astype(np.float64), decim,
                             ftype='fir', zero_phase=True).astype(np.float32)
            im = sp_decimate(shifted.imag.astype(np.float64), decim,
                             ftype='fir', zero_phase=True).astype(np.float32)
            down = re + 1j * im
        except Exception:
            return None
    else:
        down = shifted.astype(np.complex64)

    if len(down) < _MODEL_SAMPLES:
        return None

    mid  = len(down) // 2
    half = _MODEL_SAMPLES // 2
    return down[mid - half: mid + half].astype(np.complex64)


def _softmax(x):
    e = np.exp(x - x.max())
    return e / e.sum()
