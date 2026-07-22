> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# vdl2 — VDL Mode 2 Decoder

The plugin decodes VHF Digital Link Mode 2 (VDL2). This is the digital
data link that commercial aviation uses. It replaces ACARS-over-VHF and
carries ATC data messages.

The plugin does not need `peak_marker`. VDL2 uses differential phase
encoding (D8PSK). So the decoder works at the centre frequency without
carrier tracking. If `peak_marker` is active and has locked onto a peak,
the plugin uses its frequency hint. For narrowband interference this can
help. But for the normal case of tuning directly to a VDL2 channel, it is
not needed.

## Signal parameters

| Parameter | Value |
|---|---|
| Modulation | D8PSK (differential 8PSK, Gray-coded) |
| Symbol rate | 10 500 sym/s |
| Bit rate | 31 500 bit/s |
| Pulse shape | Root-raised cosine, α = 0.60 |
| Minimum bandwidth | 250 000 Hz |
| Primary frequency | 136.900 MHz |
| Framing | HDLC with bit stuffing, CRC-CCITT |
| Payload | AVLC / ACARS |

## Controls

| Key | Action |
|---|---|
| `r` | Clear message buffer |

## Loading the test signal

A synthetic VDL2 test signal with four readable ACARS messages is included
in `samples/vdl2_test.sigmf-data`. Make it first if it does not exist:

```bash
uv run python scripts/gen_vdl2_test.py
```

Then replay it:

```bash
uv run python main.py --file samples/vdl2_test.sigmf-data --bw 250000 --f 136.9M
```

![VDL2 decoder decoding live aircraft messages](images/vdl2.gif)

## Decoding steps

1. Start the VDL2 plugin from the plugin menu (`p`) and change to its
   tab. Decoding starts at the centre frequency right away. No other
   plugin is needed.

For live hardware, tune the SDR to the VDL2 channel (for example
136.900 MHz) with at least 250 kHz bandwidth. Then open the VDL2 tab. The
decoder tolerates carrier offsets up to about ±1 kHz before the matched
filter starts to attenuate the signal.

The four messages must show in green within a few seconds:

```
[HH:MM:SS] ac:01 > N-SDR01 B737 EGLL-KLAX: HELLO FROM VDL2! SDRTERM IS DECODING THIS
[HH:MM:SS] ac:01 > N-SDR01 FL350 FUEL 8.2T ETA 1823Z: ENJOYING THE RIDE AT 35000FT
[HH:MM:SS] ac:01 > N-SDR01 ACARS MSG: IF YOU CAN READ THIS YOUR DECODER IS WORKING GREAT
[HH:MM:SS] ac:01 > N-SDR01 VDL MODE 2 TEST: SQUAWK 7700  JK JK  ALL SYSTEMS NOMINAL
```

Lines have colour codes:

| Colour | Meaning |
|---|---|
| Green (bold) | CRC OK, AVLC parsed, text shown |
| Green | CRC OK, raw bytes (AVLC parse skipped) |
| Red | CRC error — frame found but corrupted |

## How it works

1. `peak_marker` gives the carrier frequency.
2. The plugin mixes the signal to baseband with a per-chunk phase
   accumulator. This keeps phase continuity across chunk boundaries.
3. The plugin resamples the baseband IQ to 8 samples per symbol
   (84 000 Hz) with rational resampling.
4. An RRC matched filter (α = 0.60) removes inter-symbol interference.
5. The plugin takes one sample per symbol period. It carries the last
   symbol of each chunk into the next chunk. This lets differential
   decoding span chunk boundaries.
6. D8PSK differential demodulation: the plugin quantises the phase
   difference between two consecutive symbols to the nearest multiple of
   π/4. It then does Gray decoding to a tribit. It appends the tribit to
   a rolling bit buffer (about 2 s of bits at 31.5 kbps).
7. The plugin applies a self-synchronising descrambler
   (G(x) = 1 + x + x⁶) to the bit stream: d[n] = r[n] ⊕ r[n−1] ⊕ r[n−6].
   After 6 bits of received data, the descrambler is in sync with the
   transmitter for any initial state. A 6-bit context carried between
   processing calls keeps cross-chunk continuity.
8. HDLC frame detection scans the descrambled bit buffer for 0x7E flags.
   It removes bit stuffing. It verifies the CRC-CCITT FCS.
9. The plugin parses valid frames as AVLC: destination (2 B), source
   (2 B), control (1 B), protocol discriminator (1 B), payload text.

## Decode chain

```
IQ samples
  → mix to baseband (carrier phase accumulator)
  → resample_poly to 84 000 Hz (8 × 10 500)
  → RRC matched filter α = 0.60
  → sample at symbol centres
  → D8PSK differential demod  → scrambled bit stream
  → descramble  G(x) = 1 + x + x⁶
  → HDLC flag search + destuff + CRC check
  → AVLC parse
  → message display
```

## Why the constellation plugin cannot identify this signal

The constellation plugin will show an empty ring or nothing at all for
VDL2. Two reasons:

1. **peak_marker cannot detect it.** VDL2 is a wideband signal. The RRC
   pulse shaper spreads its power across ~17 kHz. Each FFT bin holds only
   about 1/140th of the total power. This puts it at or below the
   per-bin noise floor. peak_marker finds no bright spike. So the
   constellation gets no symbols.

2. **The phase correction fails for 8PSK.** The constellation uses a
   4th-power estimator (`mean(symbols⁴)`). For 8PSK the 4th power gives
   only {+1, −1}. Their mean is about 0 for balanced data. So the
   estimator is undefined and the display spins into a ring instead of 8
   clusters. You need an 8th-power estimator.

Use this plugin to decode VDL2. The constellation is suited to
narrowband signals (FM carriers, BPSK, QPSK). For those signals,
peak_marker can find a single bright bin.

## Limitations
- **No symbol timing recovery**: the decoder samples at fixed offsets.
  Slight timing drift will cause bit errors on long signals. A Mueller &
  Müller or Gardner loop would fix this.
- **No frequency correction**: carrier offset must be small enough for
  the differential decoder to absorb. Follow mode keeps this below about
  500 Hz.
- The dedup cache (512 entries) resets automatically. The plugin
  silently drops duplicate frames within a short window.
