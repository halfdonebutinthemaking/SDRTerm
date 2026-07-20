# Future Additions

Directions that would give SDRTerm a distinctive identity in the SDR community.
Each is achievable in pure Python, fits the existing plugin architecture, and addresses
a gap that no other terminal-based SDR tool currently fills.

---

## 4. VDL Mode 2 Decoder

**Status:** Postponed — need a real VDL2 recording to develop against  
**Dependencies:** None beyond existing stack (pure NumPy/SciPy)

VDL Mode 2 is the digital datalink used by commercial aviation for ACARS and
ADS-C text messages, transmitted in D8PSK at 31.5 kbps on 25 kHz channels
(primary: 136.900 MHz). No terminal SDR tool currently decodes it.

### Decode chain

1. Mix to baseband (from `peak_marker` frequency)
2. Low-pass filter + decimate to ~4× symbol rate
3. Gardner symbol timing recovery loop
4. Differential 8PSK demodulation (multiply symbol by conjugate of previous)
5. NRZI decoding + descrambler polynomial
6. HDLC frame sync (`0x7E` flag correlation)
7. Bit destuffing (remove zeros after five consecutive ones)
8. CRC-CCITT frame verification
9. AVLC header parse → ACARS message text extraction

### Plugin tab output

Scrolling decoded frame list showing callsign, flight number, and message body.
ASCII constellation of the recovered D8PSK symbols (8 clusters at 22.5° spacing)
as a secondary view to confirm lock.

### Pre-requisites

A real recording of a VDL2 burst in SigMF format is needed to develop and
validate the timing recovery and descrambler. VDL2 is bursty (~20–30 ms packets
with silence between), so the recording will look very different from a continuous
carrier — power envelope will show clear on/off pattern.

---

## 1. Satellite Doppler Auto-Tracking

**Status:** Not started  
**Dependencies:** `sgp4`, `pyorbital` (both pure Python)

Predict upcoming satellite passes, auto-tune the center frequency in real time with
Doppler correction, and optionally trigger the record plugin automatically when a bird
enters view. No other TUI SDR tool does this.

### What it would do

- Fetch and cache TLEs from Celestrak on demand (or from a local file)
- Show a pass schedule for a configured observer position (lat/lon/alt)
- On pass start: hand off center frequency control to the plugin, applying per-frame
  Doppler shift based on the propagated satellite position
- On pass end: return frequency control, optionally stop recording
- The range-scan plugin's stepped-scan infrastructure is a natural companion for
  multi-satellite monitoring between passes

### Key design points

- Observer position stored in `AppState` or plugin save-state (lat/lon/alt)
- TLE cache: `~/.config/sdrterm/tle_cache.json`, refreshed if older than 24 h
- Doppler formula: `f_rx = f_tx * (1 - v_radial / c)`; radial velocity from SGP4
  position+velocity vectors
- Plugin produces a `next_pass` result dict consumed by `render.py` for a pass
  countdown in the status line
- Integrates with `record` plugin via the same `_prev_plugin` injection mechanism
  already used for WAV/SigMF capture

---

## 2. Live Modulation Classifier

**Status:** Design laid out below — see implementation plan  
**Dependencies:** `onnxruntime` (optional; ~10 MB), pre-trained ONNX model (~2 MB)

Feed raw IQ frames into a small neural network and annotate the strongest signal with
its likely modulation type (FM, AM, BPSK, QPSK, QAM16, OOK, …) and a confidence
score. No live TUI SDR tool currently does this.

→ See **Implementation Plan** section below.

---

## 3. RF Environment Monitor / Anomaly Logger

**Status:** Not started  
**Dependencies:** None beyond existing stack; optional `requests` for webhook push

Continuous headless background scan with signal-appeared / signal-disappeared events
logged to a structured file or pushed to a webhook. Replaces the common pattern of
`rtl_power` + custom shell-script glue with a single integrated tool.

### What it would do

- Maintain a per-bin baseline power level (exponential moving average)
- Detect bins where instantaneous power exceeds baseline by a configurable threshold
- Emit events: `{ "type": "signal_appeared", "freq_hz": …, "db": …, "timestamp": … }`
- Write to JSONL log or POST to a webhook URL (Grafana, Home Assistant, custom)
- Optionally trigger range-scan on the detected frequency for a closer look
- Runs as a background plugin — no tab needed, just a status line indicator showing
  event count and last event

### Key design points

- Per-bin EMA updated every N frames; `N` and threshold configurable via save-state
- Event deduplication: a signal must disappear for at least `cooldown_s` seconds
  before a second `signal_appeared` event fires on the same bin
- JSONL format matches SigMF annotations for interoperability
- Webhook: `requests.post` in a daemon thread so it never blocks the UI loop

---

## Implementation Plan — Live Modulation Classifier

### Overview

A `modclass` plugin that sits after `peak_marker` in the pipeline. On each frame it
extracts a fixed-length IQ window centred on the tracked peak, resamples it to the
model's expected sample rate, and runs ONNX inference. The result (label + confidence)
is shown in the status line and optionally overlaid on the spectrum.

### Modulation classes (RadioML 2018.01a)

```
OOK  AM-DSB  AM-SSB  WBFM  BPSK  QPSK  8PSK  QAM16  QAM64
GFSK  CPFSK  PAM4  16APSK  32APSK  OFDM-64  OFDM-72  OFDM-128  ...
```
(24 classes total; can be reduced to a smaller subset for a lighter model)

### Data flow

```
raw IQ (complex64)
  └─ spectrum plugin  →  FFT bins + noise floor
  └─ peak_marker      →  peak_hz, peak_db
  └─ modclass
       ├─ extract_window(samples, peak_hz, state.center_hz, state.bw_hz)
       │    └─ shift to baseband, low-pass filter, decimate to MODEL_SR
       ├─ normalise: zero-mean, unit variance per I and Q
       ├─ reshape to (1, 2, MODEL_SAMPLES)  ← ONNX input
       ├─ session.run(...)                  ← ~1 ms on CPU
       └─ softmax → top-1 label + confidence
```

### Files to create / modify

| File | Change |
|------|--------|
| `plugins/modclass.py` | New plugin |
| `plugins/modclass.md` | Documentation |
| `pyproject.toml` | Add optional `[dependency-groups] ml = ["onnxruntime"]` |
| `README.md` | Add plugin to feature list |
| `plugins/README.md` | Add row to overview table |
| `scripts/train_modclass.py` | Offline training script (RadioML → ONNX export) |
| `plugins/modclass/models/modclass_lite.onnx` | Committed pre-trained model (~2 MB) |

### Model architecture

A lightweight 1-D ResNet is well-established for this task (see "Over the Air Deep
Learning" — O'Shea et al., 2018). Suggested architecture:

```
Input (2, 1024)              ← I and Q as two channels
Conv1d(2→32, k=7) + BN + ReLU
ResBlock(32, k=5)  × 3
GlobalAvgPool
Dense(32 → 24)
Softmax
```

Total parameters: ~120 k. Inference on CPU (Apple Silicon / x86): < 2 ms per frame.
Export via `torch.onnx.export` or `tf2onnx`; training data from RadioML 2018.01a
(free download, ~2 GB).

For a shortcut: the `DeepSig` community has published pre-trained ONNX checkpoints
that can be adapted without running training locally.

### `plugins/modclass.py` — skeleton

```python
class ModClassDecoder(Decoder):
    name            = 'modclass'
    key             = 'm'
    key_help        = '+/-=conf_threshold'
    min_sample_rate = 250_000

    _MODEL_SR      = 200_000   # samples/s the model was trained at
    _MODEL_SAMPLES = 1_024
    _MODEL_PATH    = os.path.join(os.path.dirname(__file__), '..', 'models',
                                  'modclass_lite.onnx')

    def start(self, state):
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(self._MODEL_PATH)
        except Exception as e:
            self._session = None
            self._error = str(e)
        self._label      = None
        self._confidence = 0.0
        self._threshold  = 0.6   # min confidence to display

    def process(self, samples, state, results=None, sdr=None):
        if self._session is None:
            return {'error': self._error}

        peak = (results or {}).get('peak_marker')
        if peak is None or peak.get('peak_hz') is None:
            return {'label': None}

        window = _extract_and_resample(
            samples, peak['peak_hz'], state.center_hz,
            state.bw_hz, self._MODEL_SR, self._MODEL_SAMPLES,
        )
        if window is None:
            return {'label': None}

        x = np.stack([window.real, window.imag]).astype(np.float32)
        x = (x - x.mean()) / (x.std() + 1e-8)
        probs = self._session.run(None, {'input': x[None]})[0][0]
        idx   = int(np.argmax(probs))
        self._label      = _LABELS[idx]
        self._confidence = float(probs[idx])
        return {'label': self._label, 'confidence': self._confidence,
                'freq_hz': peak['peak_hz']}
```

### `_extract_and_resample` helper

```python
def _extract_and_resample(samples, peak_hz, center_hz, bw_hz,
                           target_sr, target_len):
    """Shift peak to baseband, decimate, return complex window or None."""
    offset_hz = peak_hz - center_hz
    t = np.arange(len(samples)) / bw_hz
    shifted = samples * np.exp(-2j * np.pi * offset_hz * t)

    # Low-pass + decimate to target sample rate
    decim = max(1, int(bw_hz / target_sr))
    from scipy.signal import decimate as sp_decimate
    down = sp_decimate(shifted, decim, ftype='fir', zero_phase=True)

    if len(down) < target_len:
        return None
    mid = len(down) // 2
    half = target_len // 2
    return down[mid - half: mid + half]
```

### Status line

```
[MOD BPSK 94%]    ← when confident
[MOD ?    41%]    ← below threshold
[MOD off]         ← session failed (onnxruntime not installed)
```

### Minimum viable first iteration

1. Ship the plugin skeleton with graceful degradation when `onnxruntime` is absent
2. Add a `scripts/download_model.py` that fetches a pre-trained checkpoint and
   converts it to ONNX (avoids committing large binaries)
3. Wire up the pipeline: `spectrum → peak_marker → modclass → record`
4. Show label in status line only; no spectrum overlay yet

### Stretch goals

- Per-modulation colour coding in the spectrum overlay
- Confidence time-series shown as a small bar in the plugin tab
- User-adjustable confidence threshold via `+`/`-` keys
- Fine-tuning mode: `record` captures labelled IQ snippets for later re-training
