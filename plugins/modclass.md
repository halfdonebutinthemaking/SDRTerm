# modclass — Live Modulation Classifier

Identifies the modulation type of the strongest signal in the spectrum using a small
neural network running entirely on-device.  Requires the `peak_marker` plugin to be
active in the pipeline before it.

## Setup

Train the model once before first use (no GPU required, ~5 min on CPU):

```bash
uv run --with torch scripts/train_modclass.py
```

Install the runtime inference library:

```bash
uv add --group ml onnxruntime
```

## Controls

| Key | Action |
|-----|--------|
| `+` / `=` | Raise confidence threshold (fewer, surer labels) |
| `-` | Lower confidence threshold (more labels, less certain) |

## Recognised modulation types

| Label | Modulation |
|-------|------------|
| OOK | On-off keying |
| AM-DSB | Amplitude modulation — double sideband |
| WBFM | Wideband FM |
| BPSK | Binary phase-shift keying |
| QPSK | Quadrature PSK |
| 8PSK | 8-ary PSK |
| QAM16 | 16-QAM |
| FSK | Frequency-shift keying |

## Pipeline order

`peak_marker` must appear before `modclass`.  The classifier extracts a window of IQ
samples centred on the tracked peak frequency, so without a tracked peak it produces
no output.

## How it works

1. The peak frequency reported by `peak_marker` is shifted to baseband.
2. The signal is decimated to the model's native 200 kHz sample rate.
3. A 1-D residual CNN (~150 k parameters) produces a probability over the 8 classes.
4. The top-1 class is shown if its confidence exceeds the current threshold.

The model was trained on synthetically generated IQ data across SNRs of −5 to +25 dB.
Accuracy on in-distribution signals is typically > 85 % above 5 dB SNR.  Real-world
accuracy varies — synthetic training data does not capture every hardware impairment.

## Limitations

- Accuracy degrades below ~5 dB SNR.
- Signals occupying less than ~5 % of the current bandwidth may be cut off when
  decimating, reducing classification quality.
- Wideband composite signals (e.g. a full FM broadcast band) will not be classified
  correctly — the classifier expects a single narrowband signal centred at the peak.
