# modclass — Live Modulation Classifier

Identifies the modulation type of the strongest signal in the spectrum using a small
neural network running entirely on-device.  Requires the `peak_marker` plugin to be
active in the pipeline before it.

## Quick start

Install the runtime inference library (needed once):

```bash
uv add --group ml onnxruntime
```

A pre-trained synthetic model ships in `models/modclass_lite.onnx` and works
out of the box.  For higher real-world accuracy, train on RadioML 2018.01a data
(see [Training the model](#training-the-model) below).

## Controls

| Key | Action |
|-----|--------|
| `+` / `=` | Raise confidence threshold (fewer, surer labels) |
| `-` | Lower confidence threshold (more labels, less certain) |

## Pipeline order

`peak_marker` must appear **before** `modclass` in the plugin pipeline.  The
classifier extracts a window of IQ samples centred on the tracked peak frequency;
without a tracked peak it produces no output.

## How it works

1. `peak_marker` reports the frequency and power of the strongest signal.
2. That frequency is shifted to baseband and the samples are decimated to the
   model's native 200 kHz sample rate.
3. A window of 1 024 samples (~5 ms) is normalised and fed to a 1-D residual CNN
   (~150 k parameters).
4. The network outputs a probability over all classes.  Probabilities are smoothed
   with an exponential moving average (α = 0.20, ~465 ms half-life) to prevent
   the label from flickering between frames.
5. The top-1 class is shown once its smoothed confidence exceeds the threshold.

The plugin tab shows a side-by-side view of the smoothed probabilities (what drives
the label) and the raw per-frame probabilities so you can see the effect of smoothing
live.

---

## Training the model

The model needs to be trained once to produce `models/modclass_lite.onnx`.  Two
data modes are supported: **RadioML 2018.01a** (recommended) and **synthetic**
(fast fallback).  After training, `models/modclass_labels.json` is written with
the class list; the plugin picks it up automatically on the next start.

### Option A — RadioML 2018.01a (recommended)

RadioML 2018.01a is a publicly available dataset of real-channel-impaired IQ
recordings covering 24 modulation types at SNRs from −20 to +30 dB.  Training on
it gives significantly better real-world accuracy than synthetic data.

**Step 1 — Download the dataset (~3.5 GB)**

```bash
# Install the Kaggle CLI and put your API token at ~/.kaggle/kaggle.json
# (create a free account at kaggle.com → Account → Create New API Token)
pip install kaggle
uv run scripts/download_radioml.py
```

The script tries the Kaggle API first, then falls back to the DeepSig direct URL,
then prints manual download instructions if both fail.

Manual alternative: download `GOLD_XYZ_OSC.0001_1024.hdf5` from  
[kaggle.com/datasets/pinxau1000/radioml2018](https://www.kaggle.com/datasets/pinxau1000/radioml2018)  
and place it in the `data/` directory.

**Step 2 — Train and export**

```bash
uv run --with torch --with onnxscript scripts/train_modclass.py \
    --data data/GOLD_XYZ_OSC.0001_1024.hdf5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--data PATH` | *(none)* | Path to the RadioML HDF5 file |
| `--snr-min DB` | `0` | Discard examples below this SNR (dB) |
| `--max-per-class N` | `6000` | Max examples per class (reduce for speed) |

Typical runtimes on Apple Silicon MPS:

| `--max-per-class` | Examples | Approx. time | Val accuracy |
|-------------------|----------|--------------|--------------|
| 1 000 | ~24 k | ~5 min | ~85 % |
| 3 000 | ~72 k | ~15 min | ~90 % |
| 6 000 *(default)* | ~144 k | ~30 min | ~93 %+ |

---

### Option B — Synthetic data (fast, lower real-world accuracy)

Generates IQ signals in Python — no download required.  The training script
synthesises OOK, AM-DSB, WBFM, BPSK, QPSK, 8PSK, QAM16, and FSK signals
across −5 to +25 dB SNR.

```bash
uv run --with torch --with onnxscript scripts/train_modclass.py
```

Typical runtime: ~5 minutes on CPU, ~2 minutes on MPS.  Val accuracy ~92 % on
synthetic test data.  Real-world accuracy is lower because synthetic signals do
not include hardware impairments (phase noise, IQ imbalance, multipath).

---

### What training produces

| File | Description |
|------|-------------|
| `models/modclass_lite.onnx` | Trained model weights (single-file ONNX, ~600 KB) |
| `models/modclass_labels.json` | Ordered class list matching the model output indices |

The plugin reloads both files each time it starts, so a retrain takes effect
immediately without restarting SDRTerm.

---

## Recognised modulation types

### Synthetic model (8 classes)

| Label | Modulation |
|-------|------------|
| OOK | On-off keying |
| AM-DSB | Amplitude modulation — double sideband |
| WBFM | Wideband FM |
| BPSK | Binary phase-shift keying |
| QPSK | Quadrature PSK |
| 8PSK | 8-ary PSK |
| QAM16 | 16-QAM |
| FSK | Frequency-shift keying (2-FSK) |

### RadioML 2018.01a model (24 classes)

32PSK · 16APSK · 32QAM · FM · GMSK · 32APSK ·
OFDM-64 · OFDM-72 · OFDM-128 · OFDM-256 · OFDM-512 · OFDM-1024 · OFDM-2048 ·
256QAM · 64QAM · 16QAM · 32QAM ·
AM-SSB-SC · AM-SSB-WC · AM-DSB-SC · AM-DSB-WC ·
BPSK · QPSK · 8PSK · 16PSK · 32PSK

---

## Limitations

- Accuracy degrades below ~5 dB SNR regardless of training data.
- The model always sees a 200 kHz window centred on the tracked peak.  Signals
  narrower than ~5 kHz may be under-represented after decimation.  Signals wider
  than 200 kHz (e.g. a full FM broadcast band at 250 kHz deviation) will be clipped.
- One signal at a time: the classifier assumes a single modulated carrier at the
  peak frequency.  Composite or multiplexed signals are not reliably identified.
- The 200 kHz window is determined by the sample rate the model was trained at
  (`_MODEL_SR = 200_000` in `train_modclass.py`).  To handle wider signals, raise
  this constant and retrain.
