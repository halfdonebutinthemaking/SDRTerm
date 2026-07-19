# fm — FM Broadcast Audio Decoder

Demodulates wideband FM broadcast audio and plays it back in real time. Draws a cyan channel-bandwidth highlight on the spectrum or waterfall.

![FM plugin tab with channel highlight](images/02_plugin_fm.png)

## Controls

| Key | Action |
|-----|--------|
| `[` | Narrow FM channel bandwidth (−10 kHz, min 30 kHz) |
| `]` | Widen FM channel bandwidth (+10 kHz, max 200 kHz) |

## How it works

Demodulation uses the instantaneous frequency method (conjugate product of successive samples). A 6th-order Chebyshev IF filter selects the channel around the centre frequency before demodulation; its bandwidth is set by `[`/`]`. The demodulated audio is resampled to 48 kHz using `scipy.signal.resample_poly` with a ratio derived from `gcd(sample_rate, 48000)`, so the decoder runs at any bandwidth preset without forcing a hardware sample-rate change.

A 15 kHz FIR low-pass filter and a 50 µs de-emphasis IIR (EU standard) are applied after resampling.

## Recording

When FM precedes the `record` plugin in the pipeline, audio is saved as a `.wav` file. See [record.md](record.md).
