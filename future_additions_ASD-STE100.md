> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# Future Additions

These are directions that would give SDRTerm a clear identity in the SDR community.
Each one is possible in pure Python. Each one fits the current plugin architecture. Each one fills a gap that no other terminal-based SDR tool fills now.

---

## 4. VDL Mode 2 Decoder

**Status:** Done (`plugins/vdl2/`)  
**Dependencies:** None more than the current stack (pure NumPy/SciPy)

D8PSK 10 500 sym/s, HDLC/AVLC framing, self-synchronising descrambler
(G(x) = 1 + x + x⁶), CRC-CCITT. Decodes at the centre frequency without
`peak_marker`. See `plugins/vdl2/vdl2.md` for the full documentation.

### Known limitations that stay

- **No symbol timing recovery.** The sampling offset is fixed. Long signals on a
  drifting oscillator will get bit errors after some time. A Gardner or
  Mueller & Müller loop would fix this.
- **No frequency correction.** The carrier offset must stay in about 1 kHz for
  the RRC matched filter to pass the signal cleanly.

---

## 5. Constellation — Mth-power phase correction

**Status:** Not started. Safe to add. Low risk.  
**Dependencies:** None. The change is self-contained in `plugins/constellation/constellation.py`.

### Problem

The carrier phase estimator is hardcoded to the 4th-power law:

```python
powered     = matched ** 4
frame_phase = np.angle(np.mean(powered)) / 4.0
candidates  = [frame_phase + k * np.pi / 2 for k in range(4)]
```

For M=2 (BPSK) and M=4 (QPSK) this works. All symbol phases raised to the
4th power collapse to 1. This gives a stable non-zero mean to estimate from.

For **M=8 (8PSK)** the 4th power of the eight symbol phases gives only
{+1, −1}. Their mean is about 0 for balanced data. So `angle(0)` is undefined.
The constellation spins into a ring in place of showing 8 clusters.

### Fix

Replace the hardcoded `4` with `self._m` in all parts of the estimator:

```python
powered     = matched ** self._m
frame_phase = np.angle(np.mean(powered)) / float(self._m)
candidates  = [frame_phase + k * 2 * np.pi / self._m for k in range(self._m)]
```

For M=4 this is the same in algebra as the current code (no regression).
For M=8 the 8th power of all 8PSK symbols is 1. This gives a stable mean and
correct phase correction.

### Important: do not change the symbol sampling offset

An earlier try combined this fix with a change of `offset` from
`delay % SPS + SPS//2` (= 4) to `(len(taps)//2) % SPS` (= 0). The offset
change caused flower-petal ISI patterns and was reverted. The Mth-power change
by itself is safe. **Do not change the offset formula.**

---

## 1. Satellite Doppler Auto-Tracking

**Status:** Not started  
**Dependencies:** `sgp4`, `pyorbital` (both pure Python)

Predict the next satellite passes. Auto-tune the center frequency in real time with
Doppler correction. As an option, trigger the record plugin by itself when a bird
comes into view. No other TUI SDR tool does this.

### What it would do

- Fetch and cache TLEs from Celestrak on demand (or from a local file)
- Show a pass schedule for a configured observer position (lat/lon/alt)
- On pass start, hand off center frequency control to the plugin. Apply per-frame
  Doppler shift based on the propagated satellite position.
- On pass end, give back frequency control. As an option, stop recording.
- The stepped-scan infrastructure of the range-scan plugin is a good companion for
  multi-satellite monitoring between passes.

### Key design points

- The observer position is kept in `AppState` or in plugin save-state (lat/lon/alt).
- TLE cache: `~/.config/sdrterm/tle_cache.json`. Refreshed if older than 24 h.
- Doppler formula: `f_rx = f_tx * (1 - v_radial / c)`. Radial velocity is from SGP4
  position and velocity vectors.
- The plugin gives a `next_pass` result dict. `render.py` uses this dict for a pass
  countdown in the status line.
- Integrates with the `record` plugin through the same `_prev_plugin` injection method
  already used for WAV and SigMF capture.

---

## 2. Live Modulation Classifier

**Status:** The design is set out below. See the implementation plan.  
**Dependencies:** `onnxruntime` (optional, about 10 MB). A pre-trained ONNX model (about 2 MB).

Feed raw IQ frames into a small neural network. Annotate the strongest signal with
the likely modulation type (FM, AM, BPSK, QPSK, QAM16, OOK, …) and a confidence
score. No live TUI SDR tool does this now.

→ See the **Implementation Plan** section below.

---

## 3. RF Environment Monitor / Anomaly Logger

**Status:** Not started  
**Dependencies:** None more than the current stack. Optional `requests` for webhook push.

Continuous headless background scan. Signal-appeared and signal-disappeared events
are logged to a structured file or pushed to a webhook. Replaces the common pattern of
`rtl_power` and custom shell-script glue with one integrated tool.

### What it would do

- Keep a per-bin baseline power level (exponential moving average)
- Find bins where the instant power is above the baseline by a configurable threshold
- Send events: `{ "type": "signal_appeared", "freq_hz": …, "db": …, "timestamp": … }`
- Write to a JSONL log or POST to a webhook URL (Grafana, Home Assistant, custom)
- As an option, trigger range-scan on the detected frequency for a closer look
- Runs as a background plugin. No tab is needed. It only has a status line indicator that shows
  the event count and the last event.

### Key design points

- Per-bin EMA updated every N frames. `N` and threshold are configurable through save-state.
- Event deduplication: a signal must disappear for at least `cooldown_s` seconds
  before a second `signal_appeared` event fires on the same bin.
- The JSONL format matches SigMF annotations for interoperability.
- Webhook: `requests.post` in a daemon thread so it never blocks the UI loop.

---

## Implementation Plan — Live Modulation Classifier

### Overview

A `modclass` plugin sits after `peak_marker` in the pipeline. On each frame it
gets a fixed-length IQ window centred on the tracked peak. It resamples the window to the
expected sample rate of the model. Then it runs ONNX inference. The result (label + confidence)
is shown in the status line. As an option, it is drawn as an overlay on the spectrum.

### Modulation classes (RadioML 2018.01a)

```
OOK  AM-DSB  AM-SSB  WBFM  BPSK  QPSK  8PSK  QAM16  QAM64
GFSK  CPFSK  PAM4  16APSK  32APSK  OFDM-64  OFDM-72  OFDM-128  ...
```
(24 classes total. You can cut this to a smaller subset for a lighter model.)

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

### Files to create or modify

| File | Change |
|------|--------|
| `plugins/modclass.py` | New plugin |
| `plugins/modclass.md` | Documentation |
| `pyproject.toml` | Add optional `[dependency-groups] ml = ["onnxruntime"]` |
| `README.md` | Add the plugin to the feature list |
| `plugins/README.md` | Add a row to the overview table |
| `scripts/train_modclass.py` | Offline training script (RadioML → ONNX export) |
| `plugins/modclass/models/modclass_lite.onnx` | Committed pre-trained model (about 2 MB) |

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

Total parameters: about 120 k. Inference on CPU (Apple Silicon / x86): less than 2 ms per frame.
Export through `torch.onnx.export` or `tf2onnx`. Training data comes from RadioML 2018.01a
(free download, about 2 GB).

For a shortcut: the `DeepSig` community has published pre-trained ONNX checkpoints
that you can adapt without local training.

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

1. Ship the plugin skeleton with graceful fallback when `onnxruntime` is not there.
2. Add a `scripts/download_model.py` that fetches a pre-trained checkpoint and
   converts it to ONNX. This avoids committing large binaries.
3. Wire up the pipeline: `spectrum → peak_marker → modclass → record`
4. Show the label in the status line only. No spectrum overlay yet.

### Stretch goals

- Per-modulation colour code in the spectrum overlay
- Confidence time-series shown as a small bar in the plugin tab
- User-adjustable confidence threshold with `+`/`-` keys
- Fine-tuning mode: `record` captures labelled IQ snippets for later re-training
