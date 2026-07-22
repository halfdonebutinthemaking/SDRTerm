> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# spectrum — FFT Spectrum Display

This built-in plugin is always on. It collects IQ samples and applies a Hann window. It then computes an averaged FFT. The result is the dBFS power spectrum. The plugin draws the spectrum as a bar chart or a waterfall.

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

If you change `FFT_BINS` from 512 to 4096, the mean floor goes down by about 9 dB (`10·log10(4096/512)`).
A smaller bandwidth makes the floor go down more. This is the same as if you make the RBW smaller on a bench spectrum analyser.

## Views

Press `v` on the core tab to change between two views. The first view is the **spectrum** (bar chart). The second view is the **waterfall** (scrolling time-frequency).

The waterfall fills from the top with the newest frame. Older frames move down. Block characters (`░▒▓█`) show the signal strength. Plugin overlays (band highlights, peak markers) work in both views.
