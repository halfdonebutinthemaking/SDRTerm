# vdl2 — VDL Mode 2 Decoder

Decodes VHF Digital Link Mode 2 (VDL2) — the digital data link used by
commercial aviation for ACARS-over-VHF replacement and ATC data messages.

Does not require `peak_marker`. VDL2 uses differential phase encoding (D8PSK),
so the decoder works at the centre frequency without carrier tracking. If
`peak_marker` is active and has locked onto a peak, its frequency hint is used
instead; for narrowband interference this can help, but for the normal case of
tuning directly to a VDL2 channel it is not needed.

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

A synthetic VDL2 test signal with four readable ACARS messages is included in
`samples/vdl2_test.sigmf-data`. Generate it first if it does not exist:

```bash
uv run python scripts/gen_vdl2_test.py
```

Then replay it:

```bash
uv run python main.py --file samples/vdl2_test.sigmf-data --bw 250000 --f 136.9M
```

## Decoding steps

1. **`v`** — switch to the VDL2 tab. Decoding starts immediately at the centre
   frequency; no other plugin is required.

For live hardware, tune the SDR to the VDL2 channel (e.g. 136.900 MHz) with at
least 250 kHz bandwidth, then press `v`. The decoder tolerates carrier offsets
up to roughly ±1 kHz before the matched filter starts attenuating the signal.

Within a few seconds the four messages should appear in green:

```
[HH:MM:SS] ac:01 > N-SDR01 B737 EGLL-KLAX: HELLO FROM VDL2! SDRTERM IS DECODING THIS
[HH:MM:SS] ac:01 > N-SDR01 FL350 FUEL 8.2T ETA 1823Z: ENJOYING THE RIDE AT 35000FT
[HH:MM:SS] ac:01 > N-SDR01 ACARS MSG: IF YOU CAN READ THIS YOUR DECODER IS WORKING GREAT
[HH:MM:SS] ac:01 > N-SDR01 VDL MODE 2 TEST: SQUAWK 7700  JK JK  ALL SYSTEMS NOMINAL
```

Lines are colour-coded:

| Colour | Meaning |
|---|---|
| Green (bold) | CRC OK, AVLC parsed, text shown |
| Green | CRC OK, raw bytes (AVLC parse skipped) |
| Red | CRC error — frame detected but corrupted |

## How it works

1. `peak_marker` supplies the carrier frequency.
2. The signal is mixed to baseband with a per-chunk phase accumulator that
   maintains phase continuity across chunk boundaries.
3. The baseband IQ is resampled to 8 samples per symbol (84 000 Hz) using
   rational resampling.
4. An RRC matched filter (α = 0.60) removes inter-symbol interference.
5. One sample per symbol period is taken and the last symbol of each chunk is
   carried into the next chunk so differential decoding spans chunk boundaries.
6. D8PSK differential demodulation: the phase difference between consecutive
   symbols is quantised to the nearest multiple of π/4, Gray-decoded to a
   tribit, and appended to a rolling bit buffer (≈ 2 s of bits at 31.5 kbps).
7. Self-synchronising descrambler (G(x) = 1 + x + x⁶) is applied to the bit
   stream: d[n] = r[n] ⊕ r[n−1] ⊕ r[n−6].  After 6 bits of received data
   the descrambler is in sync with the transmitter regardless of initial state.
   Cross-chunk continuity is maintained via a 6-bit context carried between
   processing calls.
8. HDLC frame detection scans the descrambled bit buffer for 0x7E flags,
   removes bit stuffing, and verifies the CRC-CCITT FCS.
9. Valid frames are parsed as AVLC: destination (2 B), source (2 B), control
   (1 B), protocol discriminator (1 B), payload text.

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

The constellation plugin (`c`) will show an empty ring or nothing at all for VDL2.
Two reasons:

1. **peak_marker cannot detect it.** VDL2 is a wideband signal — its power is
   spread across ~17 kHz by the RRC pulse shaper.  Each FFT bin holds only
   ~1/140th of the total power, putting it at or below the per-bin noise floor.
   peak_marker finds no bright spike, so the constellation receives no symbols.

2. **The phase correction fails for 8PSK.** The constellation uses a 4th-power
   estimator (`mean(symbols⁴)`).  For 8PSK the 4th power produces only {+1, −1};
   their mean is ~0 for balanced data, so the estimator is undefined and the
   display spins into a ring instead of 8 clusters.  An 8th-power estimator
   would be needed.

Use this plugin (`v`) to decode VDL2. The constellation is suited to narrowband
signals (FM carriers, BPSK, QPSK) where peak_marker can find a single bright bin.

## Limitations
- **No symbol timing recovery**: the decoder samples at fixed offsets. Slight
  timing drift will eventually cause bit errors on long signals; a Mueller &
  Müller or Gardner loop would fix this.
- **No frequency correction**: carrier offset must be small enough for the
  differential decoder to absorb. Follow mode keeps this below ≈ 500 Hz.
- The dedup cache (512 entries) resets automatically; duplicate frames within
  a short window are silently dropped.
