# iridium_decoder — Native in-process Iridium DQPSK demodulator (Stage 3)

Consumes narrow-band burst IQ from the [iridium (Stage 1)](../iridium/) plugin
via an in-memory queue, demodulates each burst with a matched-filter DQPSK
pipeline, and displays the resulting bits in real time.

## Status

**Phase 2a** — bit extraction + unique-word correlation.  Produces a raw
2-bits-per-symbol string per burst AND detects Iridium's fixed UW
patterns (downlink / uplink) inside the stream, giving:

- Frame direction (DL vs UL)
- Frame-start offset (needed for Phase 2b LCW parsing)
- A live "UW lock" match-rate stat as a demod-quality indicator

What still needs doing:

- **Phase 2b** — LCW field parsing after the UW to classify burst type
  (IRA / IIQ / IBC / IIP / IU3 / MSG / VOC / VDA)
- **Phase 3** — Full field decoding (RIC extraction from IRA, message
  body decoding from MSG, satellite/beam ID from IIQ, etc.)

### Reading the "UW lock" percentage

Random bits give Hamming distance ≈ 12 on a single 24-bit UW comparison.
Our search covers ~5000 bit positions × 4 UW variants (~20 000 candidates
per burst), so HD ≤ 3 shows up ~once per burst by pure chance — that's
the false-positive floor.

A real correctly-demodulated Iridium burst produces HD 0 or 1, at most
2 under moderate noise.  So the honest lock criterion is **HD ≤ 2**,
and the match-rate stat means:

| Match rate | Interpretation |
|---|---|
| ~100 % | Demod chain working correctly on real Iridium |
| 30-80 % | Demod partially working — bit ordering / timing off on some bursts |
| < 30 % | Demod is wrong somewhere (bit order, phase mapping, sample rate) |
| ~0 % | No real Iridium in the bursts — antenna / gain / tuning issue |

## Design

The plugin runs on a **background thread** in the SDRTerm process
(not a worker pool — yet).  A `multiprocessing.Pool` is on the roadmap
under the [HeavyPlugin architecture memo](../../future_additions.md#heavy-plugin-architecture-stage-3-pattern-for-cpu-bound-decoding);
the current threaded implementation is a smaller starting point that
already demonstrates the end-to-end pipeline and can migrate to the
process-pool pattern when per-burst CPU cost warrants it.

**Zero-cost when the plugin isn't active.**  The iridium (Stage 1)
plugin conditionally imports `plugins.iridium_decoder.burst_queue` and
only channelises + pushes bursts when `has_consumers()` is True.  If
this plugin isn't enabled, the iridium plugin does no extra work.

**Drop-on-full backpressure.**  The shared queue is bounded to 256
bursts.  If the decoder falls behind during a satellite pass, new
bursts from the iridium plugin are dropped and a counter is exposed
in the status line (`drop=N`).  Same policy as the spectrum plugin —
preserves real-time responsiveness over completeness.

**Cooperative CPU scheduling.**  The worker thread calls `os.nice(10)`
at startup to nudge the OS scheduler toward yielding CPU to SDR
callbacks, UI redraw, and the detector when they need it.  Between
bursts it also `time.sleep(0)` to release the GIL explicitly.

## Signal chain (per burst)

```
narrow-band burst IQ (50 kHz, complex64, from iridium plugin)
  → resample to 200 kHz (8 samples per symbol at 25 ksym/s)
  → matched RRC filter (α = 0.4)
  → auto-symbol-timing: pick sampling phase with max mean magnitude
  → DQPSK bit extraction (differential phase → 2 bits per symbol,
                          Gray-coded)
  → append to display deque
```

## Controls

| Key | Action |
|---|---|
| `j` | Enable / disable the plugin (SDRTerm plugin menu convention) |
| `r` | Clear the decoded-burst list + reset counters |

## What you see

Header: total bursts decoded since enable, current queue depth, dropped
count.

For each burst, one line:

```
  <ts>    <freq/MHz>  <ch>  <SNR>  <n_syms>  <first 48 bits>
  15:42:17  1622.146   147  20.3dB    2500   0100010101101100110111011101...
```

The bits are the raw DQPSK output — no unique-word alignment, no framing.
Same burst decoded by iridium-toolkit would have its unique word
recognised and the bit stream framed into an IRA/IIQ/MSG line.

## Prerequisites

None beyond SDRTerm's baseline (numpy, scipy).  No iridium-toolkit
dependency — this plugin is a native replacement for the shell-out
approach in [`../iridium/decode_bursts.sh`](../iridium/decode_bursts.sh).

## Loading via preset

Enable both plugins together with `presets/iridium_decode.sdrterm`:

```
uv run python main.py --preset presets/iridium_decode.sdrterm
```

The preset tunes to 1621.25 MHz on a HackRF at 2 MHz BW, enables
iridium (with capture on), and enables this decoder plugin.

## Compared with the shell-script pipeline

| | `iridium/decode_bursts.sh` | `iridium_decoder` (this plugin) |
|---|---|---|
| Runtime | Second terminal, iridium-toolkit | In SDRTerm process |
| Disk usage | ~1 MB per burst (wide IQ) | Zero |
| Memory per burst | n/a | ~4 KB (narrow-band) |
| Latency | Batch cycle (~seconds) | ~ms per burst |
| Frame parsing | Full iridium-toolkit parser | Bits only (Phase 1) |
| Setup | crcmod + parser paths | None |
| Best for | Validated decodes, offline replay | Live monitoring, iterating on demod |

Both can run simultaneously if you want.

## Roadmap

- **Phase 2** — Unique-word correlation to align frame start, burst-type
  classification (IRA / IIQ / IBC / IIP / IU3 / MSG / VOC / VDA), and
  BCH error-correction where the frame format specifies it.
- **Phase 3** — Full field parsing: RIC extraction from IRA, message
  body decoding from MSG, satellite/beam identification from IIQ, etc.
- **HeavyPlugin migration** — When Phase 2/3 work per burst exceeds
  what a threaded worker can sustain on a burst-flood, move to the
  `multiprocessing.Pool` pattern described in
  [`../../future_additions.md`](../../future_additions.md#heavy-plugin-architecture-stage-3-pattern-for-cpu-bound-decoding).
