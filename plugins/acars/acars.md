# acars — Classic ACARS Decoder

Decodes classic VHF ACARS (Aircraft Communications Addressing and Reporting
System) — the AM/AFSK data link used by commercial aviation for gate-to-cockpit
messaging, ATIS, fuel reports, and position reports.

This plugin handles the legacy analogue ACARS standard (AM carrier + AFSK
subcarrier). For the newer digital replacement see the **vdl2** plugin.

## Signal parameters

| Parameter | Value |
|---|---|
| Modulation | AM with AFSK subcarrier (non-suppressed carrier) |
| Subcarrier | Continuous-phase FSK |
| Mark (bit 1) | 2 400 Hz |
| Space (bit 0) | 1 200 Hz |
| Bit rate | 2 400 bps |
| Character format | 7-bit ASCII, ODD parity in bit 7, LSB first |
| Minimum bandwidth | 250 000 Hz |
| Primary US frequency | 129.125 MHz |
| Secondary US frequencies | 130.025, 130.450, 131.125, 131.550 MHz |

## Controls

| Key | Action |
|---|---|
| `r` | Clear message buffer |

## Loading the test signal

A synthetic ACARS test signal with four readable messages is included once
generated:

```bash
uv run python scripts/gen_acars_test.py
```

Then replay it:

```bash
uv run python main.py --file samples/acars_test.sigmf-data --bw 250000 --f 129.125M
```

Activate the ACARS tab with `a`. Within a few seconds four messages should
appear in bold:

```
[HH:MM:SS] N12345  AA0123  HELLO FROM ACARS! SDRTERM IS DECODING THIS.
[HH:MM:SS] N67890  DL0456  FL350 FUEL 8.2T ETA 1823Z ENJOYING THE RIDE
[HH:MM:SS] N11111  UA0789  ATIS MIA WINDS 080/10 VIS 10 CLR TEMP 28/22
[HH:MM:SS] N99999  SW0321  ACARS TEST FRAME FOUR  ALL SYSTEMS NOMINAL
```

Lines are styled:

| Style | Meaning |
|---|---|
| Bold | BCS integrity check passed |
| Dim + `[CRC ERR]` prefix | BCS mismatch — frame detected but corrupted |

## How it works

1. **AM demodulation** — `|IQ|` computes the envelope of the AM signal,
   recovering the AFSK audio. Per-chunk DC removal strips the carrier offset.
2. **Resampling** — the audio is resampled from the SDR bandwidth (250 kHz) to
   12 000 Hz using a polyphase FIR filter (`resample_poly`, ratio 6/125).
   At 12 000 Hz there are exactly 5 samples per bit, which simplifies clock
   recovery.
3. **Ring buffer** — 3 seconds of 12 kHz audio are accumulated across processing
   calls. This is necessary because a single IQ chunk (~65 ms at 250 kHz) is
   shorter than a typical ACARS frame (~300 ms), so frame detection runs on the
   accumulated buffer rather than individual chunks.
4. **Non-coherent FSK demodulation** — the audio is multiplied by complex
   sinusoids at the mark (2 400 Hz) and space (1 200 Hz) frequencies. A
   moving-average filter (length = 1 bit period = 5 samples) integrates each
   product. Taking the magnitude difference `|mark| − |space|` gives a
   decision variable: positive → mark (1), negative → space (0).
5. **Clock recovery** — rather than a tracking loop, the decoder tries all 5
   possible sampling phases (0–4 samples offset within a bit period) and runs
   the full frame search on each phase. Duplicate frames from different phases
   are deduplicated by `(reg, flight, text[:20])`.
6. **Sync detection** — the bit stream is searched for the 24-bit pattern
   `SYN SYN SOH` (0x16 0x16 0x01, LSB-first), allowing up to 1 bit error.
7. **Frame parsing** — once a sync is found, the decoder reads the fixed-length
   header (Mode + Reg(7) + Type/Block/Seq(3) + FlightID(6) + STX), then
   accumulates text bytes until ETX is found (max 220 chars), then reads the
   2-char BCS.
8. **BCS check** — the Block Check Sequence is the XOR of all parity-inclusive
   bytes from Mode through ETX, encoded as two ASCII hex nibble chars (+0x30
   offset). Frames with a passing BCS are shown bold; failures are shown dim.

## Decode chain

```
IQ samples (250 kHz)
  → |IQ| envelope  (AM demod)
  → DC removal
  → resample_poly(6, 125)  →  12 000 Hz audio
  → ring buffer (3 s)
  → multiply by e^(j2πf_mark·t) and e^(j2πf_space·t)
  → moving-average integration (5 taps)
  → |mark| − |space|  (decision variable)
  → sample at 5 phases × every 5 samples
  → search for SYN SYN SOH (≤ 1 bit error)
  → parse header + text + BCS
  → display
```

## Limitations

- **No carrier tracking**: the AM demodulator uses `|IQ|` which is
  carrier-frequency agnostic. Frequency offsets have no effect on decoding.
- **No symbol timing loop**: clock recovery is brute-force (5 phases). This
  works reliably for short bursts but would drift on very long transmissions.
- **Parity not validated per character**: bit 7 (ODD parity) is stripped but
  not checked; corrupted characters are passed through rather than flagged.
- **BCS-error frames are shown but not deduplicated**: a later clean decode of
  the same message will appear as a separate entry.
- The dedup cache (512 entries, keyed on confirmed BCS-OK frames) resets when
  the plugin is stopped.
