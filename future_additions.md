# Future Additions

Directions that would give SDRTerm a distinctive identity in the SDR community.
Each is achievable in pure Python, fits the existing plugin architecture, and addresses
a gap that no other terminal-based SDR tool currently fills.

---

## 4. VDL Mode 2 Decoder

**Status:** Implemented (`plugins/vdl2/`)  
**Dependencies:** None beyond existing stack (pure NumPy/SciPy)

D8PSK 10 500 sym/s, HDLC/AVLC framing, self-synchronising descrambler
(G(x) = 1 + x + x⁶), CRC-CCITT. Decodes at centre frequency without
`peak_marker`. See `plugins/vdl2/vdl2.md` for full documentation.

### Known remaining limitations

- **No symbol timing recovery** — fixed sampling offset; long signals on a
  drifting oscillator will eventually accumulate bit errors. A Gardner or
  Mueller & Müller loop would fix this.
- **No frequency correction** — carrier offset must stay within ~1 kHz for
  the RRC matched filter to pass the signal cleanly.

---

## 5. Constellation — Mth-power phase correction

**Status:** Not started — safe to implement, low risk  
**Dependencies:** None; change is self-contained in `plugins/constellation/constellation.py`

### Problem

The carrier phase estimator is hardcoded to the 4th-power law:

```python
powered     = matched ** 4
frame_phase = np.angle(np.mean(powered)) / 4.0
candidates  = [frame_phase + k * np.pi / 2 for k in range(4)]
```

For M=2 (BPSK) and M=4 (QPSK) this works: all symbol phases raised to the
4th power collapse to 1, giving a stable non-zero mean to estimate from.

For **M=8 (8PSK)** the 4th power of the eight symbol phases produces only
{+1, −1}. Their mean is ≈ 0 for balanced data, so `angle(0)` is undefined
and the constellation spins into a ring instead of showing 8 clusters.

### Fix

Replace the hardcoded `4` with `self._m` throughout the estimator:

```python
powered     = matched ** self._m
frame_phase = np.angle(np.mean(powered)) / float(self._m)
candidates  = [frame_phase + k * 2 * np.pi / self._m for k in range(self._m)]
```

For M=4 this is algebraically identical to the current code (no regression).
For M=8 the 8th power of all 8PSK symbols equals 1, giving a stable mean and
correct phase correction.

### Important: do not change the symbol sampling offset

A previous attempt combined this fix with changing `offset` from
`delay % SPS + SPS//2` (= 4) to `(len(taps)//2) % SPS` (= 0). The offset
change caused flower-petal ISI patterns and was reverted. The Mth-power change
alone is safe — **leave the offset formula untouched**.

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
