> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# acars — Classic ACARS Decoder

The plugin decodes classic VHF ACARS (Aircraft Communications Addressing
and Reporting System). This is the AM/AFSK data link that commercial
aviation uses for gate-to-cockpit messaging, ATIS, fuel reports, and
position reports.

This plugin handles the legacy analogue ACARS standard (AM carrier + AFSK
subcarrier). For the newer digital replacement, see the **vdl2** plugin.

## Signal parameters

| Parameter | Value |
|---|---|
| Modulation | AM with AFSK subcarrier (non-suppressed carrier) |
| Subcarrier | Continuous-phase FSK |
| Mark (bit 1) | 2 400 Hz |
| Space (bit 0) | 1 200 Hz |
| Bit rate | 2 400 bps |
| Character format | 7-bit ASCII, ODD parity in bit 7, LSB first |
| Minimum bandwidth | 250 000 Hz (wider recordings — for example a 2 MHz SDRUno WAV — also decode; the plugin resamples them automatically) |
| Primary US frequency | 129.125 MHz |
| Secondary US frequencies | 130.025, 130.450, 131.125, 131.550 MHz |

## Controls

| Key | Action |
|---|---|
| `r` | Clear message buffer |

## Loading the test signal

A synthetic ACARS test signal with four readable messages is included
after you make it:

```bash
uv run python scripts/gen_acars_test.py
```

Then replay it:

```bash
uv run python main.py --file samples/acars_test.sigmf-data --bw 250000 --f 129.125M
```

Start the ACARS plugin from the plugin menu (`p`) and change to its tab.
Four messages must show in bold within a few seconds:

```
[HH:MM:SS] N12345  AA0123  HELLO FROM ACARS! SDRTERM IS DECODING THIS.
[HH:MM:SS] N67890  DL0456  FL350 FUEL 8.2T ETA 1823Z ENJOYING THE RIDE
[HH:MM:SS] N11111  UA0789  ATIS MIA WINDS 080/10 VIS 10 CLR TEMP 28/22
[HH:MM:SS] N99999  SW0321  ACARS TEST FRAME FOUR  ALL SYSTEMS NOMINAL
```

Line styles:

| Style | Meaning |
|---|---|
| Bold | BCS integrity check passed |
| Dim + `[CRC ERR]` prefix | BCS mismatch — frame found but corrupted |

## How it works

1. **Shift to tuned frequency** — for file replay the IQ is fixed at
   the recording's centre frequency. But `state.center_hz` follows user
   tuning. A complex mixer shifts the tuned channel to DC. This makes
   the rest of the pipeline see only the wanted signal. For a live SDR
   the hardware is already tuned, so the shift does nothing.
2. **Two-stage bandwidth reduction** — `resample_poly` down-filters the
   IQ to 50 kHz around DC (Stage 1). This anti-aliases and rejects
   adjacent airband channels and any stronger signals elsewhere in a
   wide-band recording. `|IQ|` is then taken on the narrow-band signal
   (Stage 2). This makes sure that only the target channel's amplitude
   modulation contributes to the envelope. Then the envelope is
   resampled to 12 000 Hz (Stage 3). At 12 000 Hz there are exactly 5
   samples per bit. This makes clock recovery simpler. The resample
   ratios are computed at run time from `state.bw_hz`. So recordings
   at 250 kHz, 2 MHz, and 2.048 MHz all decode.
3. **Ring buffer** — the plugin keeps 3 seconds of 12 kHz audio across
   processing calls. This is needed because a single IQ chunk is
   shorter than a typical ACARS frame (~300 ms). So frame detection
   runs on the buffer rather than individual chunks.
4. **HPF + AGC** — a 300 Hz 4-th order Butterworth high-pass filter
   removes DC and slow burst-envelope shape. Without this, low-frequency
   energy leaks asymmetrically into the FSK correlator's 1200-Hz arm
   and biases every decision. Short-term AGC then normalises the
   envelope audio by its 20-ms RMS. The AGC is gated at 1.5× the running
   median. This makes sure that quiet stretches do not get amplified
   into spurious symbols.
5. **Non-coherent FSK demodulation** — the plugin multiplies the audio
   by complex sinusoids at the mark (2 400 Hz) and space (1 200 Hz)
   frequencies. A moving-average filter (length = 1 bit period = 5
   samples) integrates each product. The magnitude difference
   `|mark| − |space|` gives a decision variable. Positive means mark
   (1). Negative means space (0).
6. **Clock recovery** — the decoder does not use a tracking loop. It
   tries all 5 possible sampling phases (0–4 samples offset within a bit
   period). It runs the full frame search on each phase. The plugin
   deduplicates duplicate frames from different phases by
   `(reg, flight, text[:20])`.
7. **Adaptive-threshold bit slicer** — bits are sliced against a 20-bit
   running mean of the decision signal rather than a hard zero. This
   tolerates any residual DC drift on `|mark| − |space|`. Such drift
   is common on weak signals when the two arms have unequal noise
   energies. Without this the plugin forces every bit to the same value.
8. **Sync detection** — the plugin searches the bit stream for the
   24-bit pattern `SYN SYN SOH` (0x16 0x16 0x01, LSB-first). It permits
   up to 1 bit error.
9. **Frame parsing** — after a sync is found, the decoder reads the
   fixed-length header (Mode + Reg(7) + Type/Block/Seq(3) + FlightID(6)
   + STX). It then adds text bytes until ETX is found (max 220 chars).
   Then it reads the 2-char BCS.
10. **BCS check** — the Block Check Sequence is the XOR of all
    parity-inclusive bytes from Mode through ETX. It is encoded as two
    ASCII hex nibble chars (+0x30 offset). Frames with a passing BCS
    show in bold. Failures show dim.

## Decode chain

```
IQ samples (any rate: 250 kHz, 2 MHz, …)
  → shift to state.center_hz  (file replay only)
  → resample_poly → 50 kHz IQ  (Stage 1: anti-alias + adjacent-channel reject)
  → |IQ| envelope + DC removal  (Stage 2: AM demod on the narrow channel)
  → resample_poly → 12 000 Hz audio  (Stage 3)
  → ring buffer (3 s)
  → HPF 300 Hz  (Butterworth, 4th order)
  → AGC (20 ms RMS normalisation, gated at 1.5× median)
  → multiply by e^(j2πf_mark·t) and e^(j2πf_space·t)
  → moving-average integration (5 taps)
  → |mark| − |space|  (decision variable)
  → adaptive threshold: 20-bit running mean
  → sample at 5 phases × every 5 samples
  → search for SYN SYN SOH (≤ 1 bit error)
  → parse header + text + BCS
  → display
```

## Limitations

- **No symbol timing loop**: clock recovery is brute-force (5 phases).
  This works for short bursts. But it would drift on very long
  transmissions.
- **Parity not validated per character**: the plugin strips bit 7 (ODD
  parity) but does not check it. Corrupted characters pass through
  rather than get flagged.
- **BCS-error frames show but are not deduplicated**: a later clean
  decode of the same message will show as a separate entry.
- The dedup cache (512 entries, keyed on confirmed BCS-OK frames) resets
  when the plugin stops.
- **Low-SNR ACARS**: the envelope + non-coherent-FSK approach starts
  to fail around 6 dB envelope SNR. Signals visible above the noise
  on a spectrum display can still fail to decode if the burst envelope
  is only 2–3× the quiet-channel RMS. A proper matched filter and
  clock-recovery loop would be needed to close that gap.
