> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# range-scan — Frequency Range Scanner

This plugin does a stepped scan across a frequency range that you set.

![Range scan in action](images/range.gif)

The plugin tunes the SDR through the range step by step. It collects FFT power at each position. It finds signal peaks by SNR. It shows a live list of the signals that it finds. You can move through the list and press return to tune to any signal.

## Controls

| Key | Action |
|-----|--------|
| `e` | Start or stop the scan |
| `↑` / `↓` | Move the cursor up or down in the signal list |
| `ret` | Tune to the selected signal and stop the scan |
| `[` | Make the dwell time per step shorter (−0.1 s, min 0.2 s) |
| `]` | Make the dwell time per step longer (+0.1 s, max 5.0 s) |
| `-` | Make the SNR detection threshold lower (−0.5 dB, min 3 dB) |
| `+` / `=` | Make the SNR detection threshold higher (+0.5 dB, max 30 dB) |
| `s` | Change the sort order: frequency ↔ SNR |
| `m` | Set the scan minimum frequency (opens frequency entry) |
| `n` | Set the scan maximum frequency (opens frequency entry) |

## Scan range

If you do not set `m` or `n`, the plugin gets the scan range on its own:

1. Device `freq_min` and `freq_max` if available (RTL-SDR V3: 25 MHz – 1766 MHz)
2. If not: centre frequency ± 20 × current bandwidth

The plugin saves the scan range, dwell time, SNR threshold and sort order in presets. These stay set across sessions.

## Step size and dwell

Each step covers `bandwidth × 0.85` Hz. Two steps that come one after the other overlap by 15 %. This makes sure that the plugin does not miss signals near the step boundaries.

At each step, the plugin waits 150 ms for the hardware to settle after the retune. Then it collects FFT frames for the dwell time that you set. Then it moves to the next step.

## Peak detection

The plugin estimates the noise floor as the median power of the lower 70 % of FFT bins in the current step window. The plugin groups all bins that are above the SNR threshold into contiguous runs. Each run gives one signal entry at the bin with the maximum SNR.

The plugin counts only the bins in the centre ± half-step-size zone. It does not count the overlap region from the last step. This prevents double-counting.

## Signal list

The plugin shows each signal with its frequency, estimated bandwidth, SNR and age. Signals that are older than 30 s are dim. The plugin removes a signal after 2 full sweeps with no new detection (minimum age 10 s).
