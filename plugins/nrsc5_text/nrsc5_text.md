# nrsc5_text — NRSC-5 HD Radio Decoder

Decodes NRSC-5 HD Radio digital sidebands (IBOC) in pure Python/NumPy.

![NRSC-5 decoder in action](images/nrsc.gif)

Performs automatic carrier frequency offset (CFO) correction and two-pass per-symbol phase correction. The overlay highlights the primary digital sideband region (±129–198 kHz) on the spectrum or waterfall.

## How it works

NRSC-5 uses In-Band On-Channel (IBOC) modulation: digital carriers are placed in the upper and lower sidebands adjacent to the analogue FM signal. This plugin:

1. Estimates and corrects the carrier frequency offset (CFO) using the reference subcarriers
2. Performs OFDM symbol synchronisation
3. Applies a two-pass per-symbol phase correction using pilots
4. Runs a Viterbi decoder on the de-interleaved soft bits
5. Outputs any decoded text (station name, title, artist from PAD/SIS frames)

No tab-specific keys.

## Limitations

Pure-Python Viterbi is CPU-intensive. At 2.4 MHz bandwidth, expect ~20–40% CPU on a modern laptop core. Decoding reliability depends on signal strength and the analogue host station's deviation.
