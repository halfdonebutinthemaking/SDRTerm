> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# pocsag — POCSAG Paging Decoder

The plugin decodes POCSAG (Post Office Code Standardisation Advisory
Group). This is the wireless paging protocol that hospitals, fire
services, industrial telemetry, and amateur radio (DAPNET) still use. It
handles all three standard baud rates (512, 1200, 2400) with automatic
detection.

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

Make the 12-second synthetic test recording:

```bash
uv run python scripts/gen_pocsag_test.py
```

Replay:

```bash
uv run python main.py --file samples/pocsag_test.sigmf-data --bw 250000 --f 439.9875M
```

Start the pocsag plugin from the plugin menu (`p`) and change to its tab.
Three messages must show in bold within a few seconds:

```
[HH:MM:SS] RIC: 100200 F0  1200bps  numeric  12345
[HH:MM:SS] RIC: 200304 F3  1200bps  alpha    TEST 1234
[HH:MM:SS] RIC: 300400 F3  1200bps  alpha    HELLO FROM SDRTERM POCSAG DECODER
```

Line styles:

| Style | Meaning |
|---|---|
| Bold | BCH and parity checks passed |
| Dim | 1 codeword or more needed correction, or parity was wrong |

## How it works

1. **Peak lock** — if `peak_marker` is active, the plugin mixes the
   signal to baseband at the tracked peak frequency. POCSAG bursts are
   ~12.5 kHz channels. `peak_marker` finds them easily as a bright spike.
2. **Decimation** — 250 kHz IQ goes to 50 kHz through
   `scipy.signal.decimate` (5:1, with anti-alias FIR).
3. **FM discriminator** — the instantaneous frequency deviation is
   `angle(x[n] · conj(x[n−1]))`. Positive means space (0). Negative means
   mark (1).
4. **Ring buffer** — the plugin keeps 3 s of discriminator output across
   `process()` calls. A batch at 512 baud is about 1.1 s and cannot fit
   in a single chunk.
5. **Auto-slice** — the plugin subtracts the running mean of the buffer.
   This removes any residual DC or centre-frequency offset.
6. **Multi-hypothesis clock recovery** — the plugin tries all three baud
   rates × 4 clock phases × both polarities (mark = negative or
   positive). That is 24 combinations per pass. This is cheap enough
   because sync failure short-circuits at once.
7. **Sync search** — the plugin checks every bit position against the
   32-bit sync codeword `0x7CD215D8`. It permits up to 2 bit errors.
8. **Batch parse** — for each sync hit, the plugin reads the next 16
   codewords. It skips idle codewords. Address codewords (bit 31 = 0)
   start a new message. Message codewords (bit 31 = 1) append 20 bits of
   payload to the current message.
9. **BCH correction** — each 32-bit codeword goes through BCH(31,21) +
   parity. The plugin corrects up to 2 BCH errors. It verifies the parity
   bit separately. It uses a precomputed 466-entry syndrome lookup table.
10. **Payload decoding** — after the last codeword of a message:
    - If function code = 3 → alphanumeric (7-bit ASCII, LSB first)
    - If function code = 0 → numeric (4-bit BCD through charset
      `0123456789SU -)(`, LSB first per digit)
    - If not → heuristic: pick the decoding that gives ASCII letters

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

DAPNET is the best starting point. It transmits often. Its messages
appear live on the DAPNET website. So you can confirm you decoded the
correct message. Reception also tolerates weak signals.

## Limitations

- **Single batch messages only** — messages that span many 544-bit
  batches are cut at the batch boundary. In practice most pages are
  short enough (≤ 12 codewords ≈ 84 alphanumeric chars) to fit.
- **No symbol timing loop** — clock recovery is brute-force 4-phase.
  This works for short bursts. But you would need a Gardner or M&M loop
  for very long transmissions with clock drift.
- **Function code is treated as a hint** — some transmitters lie about
  numeric against alphanumeric. The decoder falls back to a printability
  heuristic when the code is ambiguous.
- **Sync search is O(n²)** — 24 slice hypotheses × ~3600 bits × 32-bit
  window. This is enough for the 3 s buffer used here but heavy. A
  bitwise rolling XOR would be faster if it ever becomes a bottleneck.
- **No idle-codeword confidence** — the decoder treats an idle codeword
  hit within 2 bits of `0x7A89C197` as an idle marker. In noisy
  conditions this could mask a real address codeword that is Hamming-2
  from idle. This is very unlikely by construction of the BCH code, but
  possible.
