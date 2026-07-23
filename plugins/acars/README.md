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
| Minimum bandwidth | 250 000 Hz (wider recordings — e.g. 2 MHz SDRUno WAV — decode too; the plugin auto-resamples) |
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

Enable the ACARS plugin from the plugin menu (`p`) and switch to its tab.
Within a few seconds four messages should appear in bold:

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

1. **Shift to tuned frequency** — for file replay the IQ is fixed at the
   recording's centre frequency while `state.center_hz` follows user tuning;
   a complex mixer shifts the tuned channel to DC so the rest of the pipeline
   only sees the wanted signal. For a live SDR the hardware is already tuned
   and the shift is a no-op.
2. **Two-stage bandwidth reduction** — `resample_poly` down-filters the IQ to
   50 kHz around DC (Stage 1). This anti-aliases and rejects adjacent airband
   channels + any stronger signals elsewhere in a wide-band recording. `|IQ|`
   is then taken on the narrow-band signal (Stage 2) so only the target
   channel's amplitude modulation contributes to the envelope. Finally the
   envelope is resampled to 12 000 Hz (Stage 3) — exactly 5 samples per bit,
   which simplifies clock recovery. The resample ratios are computed at
   runtime from `state.bw_hz`, so 250 kHz, 2 MHz, and 2.048 MHz recordings
   all work.
3. **Ring buffer** — 3 seconds of 12 kHz audio are accumulated across
   processing calls. This is necessary because a single IQ chunk is shorter
   than a typical ACARS frame (~300 ms), so frame detection runs on the
   accumulated buffer rather than individual chunks.
4. **HPF + AGC** — a 300 Hz 4-th order Butterworth high-pass filter strips
   DC and slow burst-envelope shape (otherwise low-frequency energy leaks
   asymmetrically into the FSK correlator's 1200-Hz arm and biases every
   decision). Short-term AGC then normalises the envelope audio by its
   20-ms RMS, gated at 1.5× the running median so quiet stretches are not
   amplified into spurious symbols.
5. **Non-coherent FSK demodulation** — the audio is multiplied by complex
   sinusoids at the mark (2 400 Hz) and space (1 200 Hz) frequencies. A
   moving-average filter (length = 1 bit period = 5 samples) integrates each
   product. Taking the magnitude difference `|mark| − |space|` gives a
   decision variable: positive → mark (1), negative → space (0).
6. **Clock recovery** — rather than a tracking loop, the decoder tries all 5
   possible sampling phases (0–4 samples offset within a bit period) and runs
   the full frame search on each phase. Duplicate frames from different phases
   are deduplicated by `(reg, flight, text[:20])`.
7. **Adaptive-threshold bit slicer** — bits are sliced against a 20-bit
   running mean of the decision signal rather than a hard zero. This tolerates
   any residual DC drift on `|mark| − |space|` (common on weak signals when
   the two arms have unequal noise energies) without forcing every bit to the
   same value.
8. **Sync detection** — the bit stream is searched for the 24-bit pattern
   `SYN SYN SOH` (0x16 0x16 0x01, LSB-first), allowing up to 1 bit error.
9. **Frame parsing** — once a sync is found, the decoder reads the fixed-length
   header (Mode + Reg(7) + Type/Block/Seq(3) + FlightID(6) + STX), then
   accumulates text bytes until ETX is found (max 220 chars), then reads the
   2-char BCS.
10. **BCS check** — the Block Check Sequence is the XOR of all parity-inclusive
    bytes from Mode through ETX, encoded as two ASCII hex nibble chars (+0x30
    offset). Frames with a passing BCS are shown bold; failures are shown dim.

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

- **No symbol timing loop**: clock recovery is brute-force (5 phases). This
  works reliably for short bursts but would drift on very long transmissions.
- **Parity not validated per character**: bit 7 (ODD parity) is stripped but
  not checked; corrupted characters are passed through rather than flagged.
- **BCS-error frames are shown but not deduplicated**: a later clean decode of
  the same message will appear as a separate entry.
- The dedup cache (512 entries, keyed on confirmed BCS-OK frames) resets when
  the plugin is stopped.
- **Low-SNR ACARS**: the envelope + non-coherent-FSK approach starts to fail
  around 6 dB envelope SNR. Signals visibly above the noise on a spectrum
  display can still be undecodable if the burst envelope is only 2–3× the
  quiet-channel RMS — a proper matched filter and clock-recovery loop would
  be needed to close that gap.
