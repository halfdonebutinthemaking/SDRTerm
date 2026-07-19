# spectrum — FFT Spectrum Display

Always-active built-in plugin. Accumulates IQ samples, applies a Hann window, and computes an averaged FFT to produce the dBFS power spectrum rendered as the bar chart or waterfall.

## Signal processing pipeline

```
RTL-SDR IQ samples
  → reshape into N_AVG frames of FFT_BINS samples each
  → (optional) IQ correction per frame
  → Hann window × frame
  → FFT (FFT_BINS points) → fftshift
  → |FFT|² accumulated across N_AVG frames
  → 10·log10(mean power / FFT_BINS²)   [dBFS]
```

| Constant | Value | Effect |
|----------|-------|--------|
| `FFT_BINS` | 4096 | Bin count — larger = lower mean noise floor |
| `N_AVG` | 8 | Frames averaged per display update — reduces variance |
| `REFRESH_S` | 0.15 s | Target frame period (~7 fps) |
| `DB_MAX` / `DB_MIN` | 0 / −110 dBFS | Vertical axis range |

## Noise floor

```
bin_width = sample_rate / FFT_BINS
```

Increasing `FFT_BINS` from 512 → 4096 lowers the mean floor by ~9 dB (`10·log10(4096/512)`).
Reducing bandwidth lowers the floor further — identical to reducing the RBW on a bench spectrum analyser.

## Views

Press `v` on the core tab to toggle between **spectrum** (bar chart) and **waterfall** (scrolling time-frequency) views.

The waterfall fills from the top with the newest frame; older frames scroll downward. Signal strength is encoded in block characters (`░▒▓█`). Plugin overlays (band highlights, peak markers) apply in both views.
