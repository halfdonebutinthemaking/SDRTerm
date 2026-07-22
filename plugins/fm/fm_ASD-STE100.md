> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# fm — FM Broadcast Audio Decoder

This plugin decodes wideband FM broadcast audio. It plays back the audio in real time. It draws a cyan highlight on the spectrum or waterfall to show the channel bandwidth.

![FM plugin in action](images/fm.gif)

## Controls

| Key | Action |
|-----|--------|
| `[` | Make the FM channel bandwidth narrow (−10 kHz, min 30 kHz) |
| `]` | Make the FM channel bandwidth wide (+10 kHz, max 200 kHz) |

## How it works

The plugin decodes the signal with the instantaneous frequency method. This method uses the conjugate product of two samples that come one after the other. A 6th-order Chebyshev IF filter selects the channel around the centre frequency before decoding. The `[` and `]` keys set the filter bandwidth.

The plugin then resamples the audio to 48 kHz. It uses `scipy.signal.resample_poly` with a ratio from `gcd(sample_rate, 48000)`. The decoder can run at any bandwidth preset. You do not need to change the hardware sample rate.

After resampling, the plugin applies a 15 kHz FIR low-pass filter and a 50 µs de-emphasis IIR filter (EU standard).

## Recording

Put the `fm` plugin before the `record` plugin in the pipeline. The audio is then saved as a `.wav` file. See [record.md](record.md).
