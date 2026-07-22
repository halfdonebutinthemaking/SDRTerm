# pocsag — POCSAG Paging Decoder

Decodes POCSAG (Post Office Code Standardisation Advisory Group) — the
wireless paging protocol still used by hospitals, fire services, industrial
telemetry, and amateur radio (DAPNET).  Handles all three standard baud
rates (512, 1200, 2400) with automatic detection.

## Signal parameters

| Parameter | Value |
|---|---|
| Modulation | Direct 2-FSK (no subcarrier) |
| Deviation | ±4.5 kHz |
| Baud rates | 512, 1200, 2400 (auto-detected) |
| Encoding | NRZ — mark (1) = negative deviation, space (0) = positive |
| Bit order | MSB first over the wire (character payload is LSB first) |
| Sync codeword | `0x7CD215D8` |
| Idle codeword | `0x7A89C197` |
| Error correction | BCH(31,21) + even parity, corrects ≤ 2 bit errors per codeword |
| Batch layout | 1 sync + 8 frames × 2 codewords (16 codewords, 544 bits) |
| Address | 21-bit RIC (upper 18 bits in codeword + lower 3 bits = frame position) |
| Function | 2-bit code (0 = numeric, 3 = alphanumeric, 1/2 = beep) |
| Minimum bandwidth | 250 000 Hz |

## Controls

| Key | Action |
|---|---|
| `r` | Clear message buffer |

## Loading the test signal

Generate the 12-second synthetic test recording:

```bash
uv run python scripts/gen_pocsag_test.py
```

Replay:

```bash
uv run python main.py --file samples/pocsag_test.sigmf-data --bw 250000 --f 439.9875M
```

Enable the pocsag plugin from the plugin menu (`p`) and switch to its tab.
Within a few seconds three messages should appear in bold:

```
[HH:MM:SS] RIC: 100200 F0  1200bps  numeric  12345
[HH:MM:SS] RIC: 200304 F3  1200bps  alpha    TEST 1234
[HH:MM:SS] RIC: 300400 F3  1200bps  alpha    HELLO FROM SDRTERM POCSAG DECODER
```

Lines are styled:

| Style | Meaning |
|---|---|
| Bold | BCH and parity checks passed |
| Dim | ≥ 1 codeword needed correction or parity was wrong |

## How it works

1. **Peak lock** — if `peak_marker` is active, the signal is mixed to
   baseband at the tracked peak frequency.  POCSAG bursts are ~12.5 kHz
   channels, easy for `peak_marker` to spot as a bright spike.
2. **Decimation** — 250 kHz IQ → 50 kHz via `scipy.signal.decimate` (5:1,
   with anti-alias FIR).
3. **FM discriminator** — instantaneous frequency deviation is
   `angle(x[n] · conj(x[n−1]))`.  Positive → space (0), negative → mark (1).
4. **Ring buffer** — 3 s of discriminator output accumulated across
   `process()` calls, since a batch at 512 baud is ~1.1 s and cannot fit
   in a single chunk.
5. **Auto-slice** — subtract the running mean of the buffer to remove
   any residual DC / centre-frequency offset.
6. **Multi-hypothesis clock recovery** — brute-force try all three baud
   rates × 4 clock phases × both polarities (mark = negative or positive).
   That's 24 combinations per pass.  Cheap enough because sync failure
   short-circuits immediately.
7. **Sync search** — every bit position is checked against the 32-bit
   sync codeword `0x7CD215D8`, allowing up to 2 bit errors.
8. **Batch parse** — for each sync hit, read the next 16 codewords.
   Idle codewords are skipped.  Address codewords (bit 31 = 0) begin a
   new message; message codewords (bit 31 = 1) append 20 bits of payload
   to the current message.
9. **BCH correction** — each 32-bit codeword goes through BCH(31,21) +
   parity.  Up to 2 BCH errors are corrected; the parity bit is verified
   separately.  Uses a precomputed 466-entry syndrome lookup table.
10. **Payload decoding** — after the last codeword of a message:
    - If function code = 3 → alphanumeric (7-bit ASCII, LSB first)
    - If function code = 0 → numeric (4-bit BCD via charset
      `0123456789SU -)(`, LSB first per digit)
    - Otherwise → heuristic: pick whichever decoding yields ASCII letters

## Decode chain

```
IQ samples (250 kHz)
  → shift to peak_hz (if peak_marker active)
  → decimate 5:1  →  50 kHz baseband
  → FM discriminate: angle(x[n]·conj(x[n−1]))
  → 3 s ring buffer
  → subtract mean (auto-slice)
  → for each (baud, phase, polarity):
      → slice bits
      → search for sync 0x7CD215D8 (≤ 2 errors)
      → parse 16-codeword batch
      → BCH(31,21) + parity correct each codeword
      → assemble address + message → decode text
      → dedupe by (RIC, first-30-chars)
```

## Test frequencies

Real POCSAG traffic to try after the synthetic test passes:

| Region | Frequency | Network / notes |
|---|---|---|
| EU (ham) | 439.9875 MHz | DAPNET — amateur pager network, decodable and legal, cross-check at [hampager.de](https://hampager.de) |
| Germany | 448.475 MHz | e*Message / Cityruf legacy |
| UK | 153.075, 153.500 MHz | Multitone / on-site paging |
| US | 152.480, 157.740 MHz | Commercial paging |

DAPNET is the recommended starting point: it transmits regularly, its
messages are shown live on the DAPNET website so you can confirm you
decoded the correct message, and reception is tolerant of weak signals.

## Limitations

- **Single batch messages only** — messages that span multiple 544-bit
  batches are truncated at the batch boundary.  In practice most pages
  are short enough (≤ 12 codewords ≈ 84 alphanumeric chars) to fit.
- **No symbol timing loop** — clock recovery is brute-force 4-phase.
  Works reliably for short bursts but would need a Gardner or M&M loop
  for very long transmissions with clock drift.
- **Function code is treated as a hint** — some transmitters lie about
  numeric vs alphanumeric.  The decoder falls back to a printability
  heuristic when the code is ambiguous.
- **Sync search is O(n²)** — 24 slice hypotheses × ~3600 bits × 32-bit
  window.  Adequate for the 3 s buffer used here but heavy; a bitwise
  rolling XOR would be faster if it ever becomes a bottleneck.
- **No idle-codeword confidence** — an idle codeword hit within 2 bits
  of `0x7A89C197` is treated as an idle marker.  In noisy conditions
  this could mask a real address codeword that happens to be Hamming-2
  from idle (very unlikely by construction of the BCH code, but possible).
