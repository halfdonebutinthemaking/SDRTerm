> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# nrsc5_text — NRSC-5 HD Radio Decoder

This plugin decodes NRSC-5 HD Radio digital sidebands (IBOC). It uses pure Python and NumPy.

![NRSC-5 decoder in action](images/nrsc.gif)

The plugin corrects the carrier frequency offset (CFO) automatically. It also does a two-pass phase correction on each symbol. The overlay highlights the primary digital sideband region (±129–198 kHz) on the spectrum or waterfall.

## How it works

NRSC-5 uses In-Band On-Channel (IBOC) modulation. The digital carriers are in the upper and lower sidebands next to the analogue FM signal. This plugin does these steps:

1. Estimates and corrects the carrier frequency offset (CFO). It uses the reference subcarriers.
2. Synchronises the OFDM symbols.
3. Applies a two-pass phase correction on each symbol. It uses the pilots.
4. Runs a Viterbi decoder on the de-interleaved soft bits.
5. Outputs any decoded text. The text can be the station name, title or artist from PAD or SIS frames.

This plugin has no tab-specific keys.

## Limitations

The pure-Python Viterbi decoder uses a lot of CPU. At 2.4 MHz bandwidth, you can expect 20 to 40 % CPU on one core of a modern laptop. The decode quality depends on the signal strength. It also depends on the deviation of the analogue host station.
