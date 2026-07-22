> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# peak_marker — Peak Frequency Marker

This plugin marks the strongest signal peak in the visible spectrum. It can also follow or track the peak.

![Peak marker in action](images/peak.gif)

The plugin has two modes.

## Controls

| Key | Action |
|-----|--------|
| `-` | Make the hold time shorter (−0.5 s, min 0.5 s) — hold-off mode only |
| `+` / `=` | Make the hold time longer (+0.5 s, max 10 s) — hold-off mode only |
| `c` | Retune the SDR centre to the current peak frequency (one-shot) |
| `t` | Turn follow mode on or off |
| `r` | Turn alpha-beta tracking mode on or off |

## Hold-off mode (default)

The marker locks onto the strongest peak. It holds the position for a set dwell time before it updates. It snaps to a new peak if a signal that is 6 dB stronger appears somewhere else. Use this mode to find and centre on a stable or slow signal.

## Alpha-beta tracking mode (`r`)

At each frame, the plugin predicts the next frequency of the signal. It uses the current drift-rate estimate. Then it searches only in a range of ±10 kHz around that prediction. An alpha-beta filter updates the frequency estimate and the drift rate. It uses the measurement residual:

```
prediction  = freq_est + rate_est × dt
error       = measured_freq − prediction
freq_est   += α × error
rate_est   += β × error / dt
```

The plugin bypasses the hold-off timer. The estimate updates on each frame. The marker turns green. The status line shows the estimated drift rate (for example, `TRACK −320 Hz/s`). Use this mode for Doppler signals such as satellites and aircraft. The signal moves in a continuous and predictable way.

## Follow mode (`t`)

You can use follow mode in both modes. When follow mode is on, the plugin retunes the hardware. It does this when the tracked frequency drifts more than 500 Hz (tracking mode) or 1 kHz (hold-off mode) from the current SDR centre frequency. This keeps the signal in the centre of the display.

Use `r` and `t` together for Doppler tracking. The alpha-beta filter smooths the frequency estimate and rejects noise peaks. Follow mode keeps the signal on-screen.
