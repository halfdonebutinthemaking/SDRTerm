# iridium_decoder — Native in-process Iridium DQPSK demodulator (Stage 3)

The plugin consumes narrow-band burst IQ from the
[iridium (Stage 1)](../iridium/) plugin through an in-memory queue.
It demodulates each burst with a matched-filter DQPSK pipeline. It
shows the resulting bits in real time.

## Status

**Phase 1** — bit extraction only. The output is one raw 2-bits-per-
symbol string per burst. Frame parsing (unique-word search, IRA / IIQ
/ MSG classification, field decoding) is Phase 2. It is on the roadmap
but not implemented yet.

Phase 1 output is useful as a start. You can compare the raw bits
against iridium-toolkit's output on the same captures. This validates
the demodulation chain end to end. Then you can build the parser on
top.

## Design

The plugin runs on a **background thread** in the SDRTerm process.
It is not a worker pool yet. A `multiprocessing.Pool` is on the
roadmap. See the
[HeavyPlugin architecture memo](../../future_additions.md#heavy-plugin-architecture-stage-3-pattern-for-cpu-heavy-decoding).
The current threaded implementation is a smaller starting point. It
already shows the end-to-end pipeline. It can move to the process
pool pattern when per-burst CPU cost requires it.

**Zero cost when the plugin is not active.** The iridium (Stage 1)
plugin imports `plugins.iridium_decoder.burst_queue` as an optional
dependency. It channelises and pushes bursts only when
`has_consumers()` is True. If this plugin is not enabled, the iridium
plugin does no extra work.

**Drop-on-full backpressure.** The shared queue has a limit of 256
bursts. If the decoder falls behind during a satellite pass, new
bursts from the iridium plugin are dropped. A counter shows in the
status line (`drop=N`). This is the same rule as the spectrum plugin.
It keeps real-time responsiveness.

**Cooperative CPU scheduling.** The worker thread calls `os.nice(10)`
at startup. This nudges the OS scheduler to yield CPU to SDR
callbacks, UI redraw, and the detector when they need it. Between
bursts, it also calls `time.sleep(0)` to release the GIL.

## Signal chain (per burst)

```
narrow-band burst IQ (50 kHz, complex64, from the iridium plugin)
  → resample to 200 kHz (8 samples per symbol at 25 ksym/s)
  → matched RRC filter (α = 0.4)
  → auto symbol timing: pick the sampling phase with the maximum
                        mean magnitude
  → DQPSK bit extraction (differential phase → 2 bits per symbol,
                          Gray-coded)
  → append to the display deque
```

## Controls

| Key | Action |
|---|---|
| `j` | Enable or disable the plugin (SDRTerm plugin menu rule) |
| `r` | Clear the decoded-burst list and reset the counters |

## What you see

The header shows total bursts decoded since the plugin started, the
current queue depth, and the dropped count.

For each burst, one line:

```
  <ts>    <freq/MHz>  <ch>  <SNR>  <n_syms>  <first 48 bits>
  15:42:17  1622.146   147  20.3dB    2500   0100010101101100110111011101...
```

The bits are the raw DQPSK output. There is no unique-word alignment
and no framing. The same burst decoded by iridium-toolkit would have
its unique word recognised and the bit stream framed into an
IRA / IIQ / MSG line.

## Prerequisites

None more than SDRTerm's baseline (numpy, scipy). There is no
iridium-toolkit dependency. This plugin is a native replacement for
the shell-out approach in
[`../iridium/decode_bursts.sh`](../iridium/decode_bursts.sh).

## Loading with a preset

Enable both plugins together with `presets/iridium_decode.sdrterm`:

```
uv run python main.py --preset presets/iridium_decode.sdrterm
```

The preset tunes to 1621.25 MHz on a HackRF at 2 MHz BW. It enables
iridium (with capture on). It enables this decoder plugin.

## Compared with the shell-script pipeline

|                     | `iridium/decode_bursts.sh`     | `iridium_decoder` (this plugin) |
|---------------------|--------------------------------|---------------------------------|
| Runtime             | Second terminal, iridium-toolkit | In the SDRTerm process |
| Disk usage          | ~1 MB per burst (wide IQ)      | Zero |
| Memory per burst    | n/a                            | ~4 KB (narrow-band) |
| Latency             | Batch cycle (~seconds)         | ~ms per burst |
| Frame parsing       | Full iridium-toolkit parser    | Bits only (Phase 1) |
| Setup               | crcmod + parser paths          | None |
| Best for            | Validated decodes, offline replay | Live monitoring, iteration on demod |

Both can run at the same time if you want that.

## Roadmap

- **Phase 2** — Unique-word correlation to align frame start. Burst
  type classification (IRA / IIQ / IBC / IIP / IU3 / MSG / VOC /
  VDA). BCH error correction where the frame format specifies it.
- **Phase 3** — Full field parsing. RIC extraction from IRA. Message
  body decoding from MSG. Satellite and beam identification from IIQ.
  And more.
- **HeavyPlugin migration** — When Phase 2 or Phase 3 work per burst
  exceeds what a threaded worker can sustain on a burst-flood, move
  to the `multiprocessing.Pool` pattern in the memo at
  [`../../future_additions.md`](../../future_additions.md#heavy-plugin-architecture-stage-3-pattern-for-cpu-heavy-decoding).
