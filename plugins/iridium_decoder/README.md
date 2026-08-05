# iridium_decoder — Native in-process Iridium demodulator

Runs the complete [gr-iridium](https://github.com/muccc/gr-iridium) DSP chain
in-process on raw SDR samples, producing decoded bit streams in gr-iridium's
`RAW:` format that can be fed directly into
[iridium-toolkit](https://github.com/muccc/iridium-toolkit)'s
`iridium-parser.py` for message classification (VOC, ISY, IRI, IU3, IBC, IME,
etc.).

## Architecture

Fully independent from the [iridium (Stage 1)](../iridium/) plugin.  Both
run on the same raw SDR sample stream but each does its own detection and
serves a different purpose:

- **iridium**            : burst-detection statistics, per-channel
                           activity display, optional `c`-toggle
                           capture-to-disk (`.cf32` + JSON sidecar for
                           external tools).  Does NOT decode.
- **iridium_decoder**    : end-to-end message decoding (this plugin).
                           Runs its own port of gr-iridium's
                           `fft_burst_tagger` — no dependency on the
                           iridium plugin being enabled.

Any combination is valid:
- Decoder alone → live message decodes, no on-disk captures
- Detector alone → burst display + optional `.cf32` capture, no decoding
- Both enabled → decoder emits messages while detector shows channel
                 activity and (optionally) captures wide IQ to disk

The two plugins share no state; each maintains its own tagger, worker
thread, and result deque.

## DSP chain

```
raw IQ (2 MHz, complex64 from SDR)
   │
   ▼
FftBurstTagger        ← toolkit port of gr-iridium/lib/fft_burst_tagger_impl.cc
   │                    per-bin adaptive noise floor + Kalman-like state,
   │                    matches gr-iridium detection count exactly (1170/1170
   │                    on a 30 s indoor reference capture)
   ▼
per-burst wide IQ slice
   │
   ▼
BurstDownmix          ← toolkit port of gr-iridium/lib/burst_downmix_impl.cc
   │                    - rough CFO shift + low-pass + decimate to 500 kHz
   │                    - envelope-based burst start
   │                    - squared-signal FFT for fine CFO
   │                    - RRC matched filter
   │                    - FFT-correlation sync-word alignment (DL vs UL)
   ▼
QpskDemod             ← toolkit port of gr-iridium/lib/iridium_qpsk_demod_impl.cc
   │                    - decimate to symbol rate (sps=20)
   │                    - first-order QPSK PLL (α = 1/5)
   │                    - quadrant slicer + confidence
   │                    - UW check (HD ≤ 2)
   │                    - differential Gray decode → bits
   ▼
{ timestamp, freq, direction, confidence, bits, … }
```

The port lives under [`toolkit/`](toolkit/) and is testable independently
of the SDRTerm plugin.  See `toolkit/*.py` for the full port.

## Validation

Against gr-iridium's C++ `iridium-extractor` on the same 30 s RTL-SDR indoor
capture:

|                                                | Bursts | A:OK | Fully-parsed |
|------------------------------------------------|--------|------|--------------|
| gr-iridium C++                                 |  1170  | ~35  | 9            |
| our port (default)                             |  1170  |  ~19 | 7            |
| our port (both-bin mode, `b` key)              |  1170  |  35  | 13           |

Detection count matches gr-iridium **exactly**.  Decode count is
comparable, and in both-bin mode we exceed gr-iridium's parse rate
(catches bursts on both spectrum halves — there's a subtle FFT convention
difference between numpy and FFTW that mirrors bin indices; both-bin
mode is a pragmatic fix while the root cause is investigated).

## Controls

| Key | Action |
|---|---|
| `j` | Enable / disable the plugin |
| `r` | Clear the decoded-burst list + reset counters |
| `b` | Toggle both-bin mode (2× decodes but 2× CPU) |
| `m` | Toggle view: raw bits ↔ parsed messages (VOC / IRI / ISY / ...) |
| `s` | Save session to a `.raw` log for offline analysis |
| `+` / `-` | Detection threshold up / down by 2 dB (see below) |

### Detection threshold tuning

`+` / `-` adjust the tagger's SNR threshold (range 6-30 dB, default 14).
This is the **single biggest CPU-vs-completeness knob** — every detected
burst costs the same downstream DSP work whether or not it eventually
decodes, so raising the threshold trades marginal weak-burst decodes
for headroom against `Dropped:`.

Benchmark on a fixed 30 s indoor RTL-SDR capture (2 MHz sample rate,
both-bin off):

  | threshold | detections | A:OK | wall time | realtime factor |
  |-----------|-----------:|-----:|----------:|----------------:|
  | 14 dB     |       1170 |   19 |    10.1 s |   2.9× realtime |
  | 18 dB     |          2 |    0 |     2.0 s |  14.6× realtime |
  | 22 dB     |          0 |    0 |     2.0 s |  14.6× realtime |

Indoor / weak-signal captures need low thresholds (marginal bursts
decode).  Outdoor / strong-signal captures work with much higher
thresholds and reclaim large amounts of CPU headroom.  If `Dropped:`
is climbing during a satellite pass, **push threshold up 2 dB at a
time until it stabilises**.  You can then decide whether to back
off if you're missing decodes you care about.

The threshold is persisted across sessions via SDRTerm's preset system.

### Message view

The `m` key switches the full-view tab between two layouts:

- **bits view** (default) — one line per demodulated burst: timestamp,
  frequency, direction, symbol count, confidence, SNR, and the first
  48 bits.
- **messages view** — one line per burst, parsed into typed messages:
  - `IRI` — generic Iridium radio frame
  - `VOC` — voice frame (with LCW handoff / access info)
  - `ISY` — system information (maintenance, LQI, power)
  - `IU3` — uplink telemetry
  - `IBC` — broadcast channel
  - `IME` — Iridium Message Extended (maintenance)
  - `IRA` — ring alert
  - `RAW` — unparsed frame (correct UW but unrecognised LCW)

Parsing is done **in-process** using a vendored copy of iridium-
toolkit's parser (see [`parser/`](parser/)).  No subprocess, no
external tool install, no PATH gymnastics.  The only external
dependency is `crcmod` (added to `pyproject.toml`); everything else
is bundled.

If `crcmod` isn't installed, messages view shows a clear error and
suggests `uv add crcmod`.  Bits view always works without any
external dep.

## What you see

Header shows: detected bursts, A:OK count (UW recognised), queue length,
dropped chunks, both-bin toggle state.

Per burst, one line:
```
  <ts/ms>  <freq/MHz>  <DL/UL>  <syms>  <conf%>  <SNR>  <first 48 bits>
```

The bits are in gr-iridium's on-air convention — starting with the
demodulated unique word (`001100000011000011110011` = DL,
`110011000011110011111100` = UL) followed by the payload.  You can pipe
these bits to `iridium-parser.py --uw-ec --harder -` to classify them
into message types (VOC/ISY/IRI/IU3/IBC/IME/…).

## Loading via preset

```
uv run python main.py --preset presets/iridium_decode.sdrterm
```

Enables both the iridium plugin (burst display) and iridium_decoder
(message decoding) at 2 MHz, HackRF or RTL-SDR.

## Offline pattern analysis (analyze_raw)

`analyze_raw.py` reads one or more saved `.raw` session logs (produced
via the `s` shortcut) and groups the still-unclassified `RAW:` bursts
by common bit patterns.  Recurring patterns are candidate new message
types — signals that iridium-toolkit's parser doesn't yet know how to
classify but which appear repeatedly, so are likely real message
classes rather than noise.

### Usage

```bash
# Default: quality-filtered analysis of all logs in iridium_logs/
uv run python -m plugins.iridium_decoder.analyze_raw iridium_logs

# Same, explicit glob (works with shell or without)
uv run python -m plugins.iridium_decoder.analyze_raw iridium_logs/*.raw

# Multiple explicit files
uv run python -m plugins.iridium_decoder.analyze_raw file1.raw file2.raw
```

### CLI flags

| Flag | Default | Effect |
|---|---|---|
| `--top N` | 15 | How many top patterns to show |
| `--bits N` | 32 | Bits-after-UW used as the pattern key.  32 covers the LCW header (24 bits) plus 8 more.  Smaller `N` groups more bursts together, larger separates them. |
| `--min-conf N` | 40 | Iridium-quality filter: reject bursts with QpskDemod confidence below N % (real bursts land tightly at QPSK quadrant centres) |
| `--min-nsyms N` | 40 | Iridium-quality filter: reject bursts with fewer than N symbols (truncated / spur) |
| `--no-filter` | off | Skip the quality filter — analyse ALL RAW frames including obvious noise triggers |

### Iridium-quality filter

Not every `RAW:` line represents a real Iridium burst.  Some are spurs
or noise that accidentally passed the UW check via bit-error correction
(the demod allows Hamming distance ≤ 2 in the 12-symbol UW, so random
noise occasionally satisfies it).  The default filter uses three
signals to focus pattern analysis on **plausibly real** bursts:

1. **Frequency alignment** — the burst frequency must fall within
   ±5 kHz of an Iridium channel centre (channels are on a fixed
   41.667 kHz grid starting at 1616 MHz).  Real satellite bursts land
   on the grid; interferers and spurs typically don't.
2. **QpskDemod confidence ≥ 40 %** — real bursts produce tightly-
   clustered symbol constellations; noise-triggered UW matches have
   scattered symbols and low confidence.
3. **Symbol count ≥ 40** — below this is almost certainly a truncated
   burst (too short to carry an LCW header).

Rejected counts are shown in the report so you can see why bursts
were dropped:

```
RAW frames: 984 total, 437 kept after Iridium-quality filter
  filter rejected:
    480  nsyms < 40
     67  conf < 40%
```

Use `--no-filter` to see the pre-filter counts, or tune with
`--min-conf` / `--min-nsyms` for stricter or looser rejection.

### Report sections

The analyzer prints, in order:

1. **Classification breakdown** — how many bursts became each parsed
   type (IRI / VOC / ISY / IU3 / IBC / IME / IRA), plus how many
   stayed as `RAW`.  Percentages let you see the parser's hit rate.
2. **RAW-frame filter stats** — after the quality filter, how many
   RAW bursts survive and why any were rejected.
3. **RAW by direction** — DL vs UL split of the filtered RAW bursts.
   Satellite passes are usually 5-10× more DL than UL.
4. **RAW by symbol-length bucket** — Iridium has several frame length
   families (~131-191 syms for normal duplex, 80-444 for simplex).
   A big cluster at one specific length is a strong hint of a
   recurring message class.
5. **RAW by frequency band** — duplex (1616-1626 MHz) vs simplex
   (>1626 MHz) split.  These use different message families.
6. **Top-N recurring N-bit patterns after the UW** — this is the
   payoff.  Each pattern is annotated with:
   - `count`: how many bursts match this exact bit prefix
   - `freq/MHz`: median frequency of matching bursts, ± spread
   - `sample ts,freq`: 1-2 example bursts you can grep in the log

### Reading the top-N patterns

A pattern is a **candidate new message type** when it satisfies TWO
conditions:

- **count ≥ 3** — it recurs, so it's not random noise
- **frequency spread ≤ 5 MHz** — it clusters on specific channels or
  channel groups, not scattered across the band

If both hold, the burst's first 24 bits are likely the LCW field of
an Iridium message class the parser doesn't recognise.  Compare its
bit pattern to iridium-toolkit's [FORMAT.md](https://github.com/muccc/iridium-toolkit/blob/master/FORMAT.md)
and the `IridiumLCWMessage.upgrade()` branches in
[bitsparser.py](parser/bitsparser.py) to see if it maps to a known LCW
type-code that just isn't fully decoded (e.g. `T:rsrvd` variants).

Adding a new classifier is a small patch to `bitsparser.py`'s
`upgrade()` method + a new `IridiumXYZMessage` subclass — a natural
[upstream contribution](https://github.com/muccc/iridium-toolkit) to
iridium-toolkit if you find something new.

### Workflow

1. **Capture** a long session: enable the plugin, press `s` to start
   saving, let it run for 15-30 minutes during a good satellite pass.
2. **Verify** the log contains messages: `head iridium_logs/*.raw` —
   should see many `RAW:` lines with the gr-iridium format.
3. **Analyse** with default filter:
   ```bash
   uv run python -m plugins.iridium_decoder.analyze_raw iridium_logs
   ```
4. **Iterate** on the filter if too many rejects:
   - Too many `nsyms < 40` rejections → your bursts are getting cut
     off; either lower `--min-nsyms 20` or investigate why the demod
     terminates early (SNR / burst-end threshold)
   - Too many `off_channel` rejections → your tuner frequency is
     off; check `state.center_hz` matches Iridium band alignment
5. **Cross-reference** the top-3 patterns with FORMAT.md to see if
   they correspond to known-but-unparsed LCW types.

## Comparison with the earlier shell-out pipeline

| | External `iridium-extractor` + `iridium-parser` | `iridium_decoder` (this plugin) |
|---|---|---|
| Runtime | Second terminal, C++ binary | In-process, pure Python |
| Setup | gr-iridium install + PATH | Nothing beyond SDRTerm's deps |
| Latency | Real-time pipe | Real-time, in-TUI |
| Frame parsing | Full iridium-toolkit parser | Bits only — pipe to iridium-parser |
| CPU | Native C++ (very fast) | Python (2× realtime on modest CPU) |
| Runtime backpressure | Handled by gr-iridium | 128-chunk queue, drops on overflow |

Both can run simultaneously if you want the parsed output alongside the
plugin's raw display.

## CPU tuning

At 2 MHz sample rate the plugin needs to keep up with ~977 FFT frames per
second plus O(N × 113) FIR filtering per burst.  On modest CPUs (M-series
Mac, mid-range x86) this is ~2× realtime — should handle live streams
comfortably.  During dense satellite passes the queue can fill; the plugin
drops chunks rather than blocking the SDR reader.  If drops become
persistent, disable both-bin mode (`b`) to halve per-burst CPU cost.

### What "Dropped" means

The header shows `Detected: N   A:OK: M   Queue: Q   Dropped: D`.
These are three different numbers and easily confused:

- **Dropped** = raw IQ chunks (blocks of ~100 ms of samples straight
  from the SDR) that the plugin threw away **before ever inspecting
  them for bursts**.  This happens when the worker thread's input
  queue is full — the SDR keeps delivering samples faster than the
  Python DSP chain can process them, so we drop the newest chunk to
  keep the SDR reader unblocked.  Every dropped chunk is 100 ms of
  potential Iridium activity that got neither detected nor decoded.
- **Detected** = bursts the tagger found in the chunks we DID process.
  These are candidates that at least made it past the FFT detector.
- **A:OK** = detected bursts that also passed downmix + demod and
  produced a recognisable unique word (i.e., real Iridium messages,
  potentially further classifiable by iridium-parser).

So a healthy live session might read `Detected: 1170  A:OK: 35
Queue: 12  Dropped: 0` — every SDR chunk got processed, ~35 of the
1170 candidates were valid.

An overloaded session reads `Detected: 293  A:OK: 7  Queue: 128
Dropped: 1353` — 1353 chunks × 100 ms = 135 s of IQ we never saw
because the worker was busy.  If you'd processed all of it, both
"Detected" and "A:OK" would be roughly 5× larger.  **Watch the
Dropped counter — if it climbs continuously the plugin is CPU-bound;
disable both-bin (`b`), lower the sample rate, or accept some data
loss.**

### Why some bursts stay `RAW` in the messages view

The `m`-toggle messages view shows type-classified messages: `IRI`,
`VOC`, `ISY`, `IU3`, `IBC`, `IME`, `IRA`, and — sometimes — `RAW`.

`RAW:` means the burst was demodulated (unique word passed at HD ≤ 2)
but the vendored iridium-toolkit parser couldn't identify the frame
type.  Three main causes:

1. **LCW BCH decode failure** — the 3-symbol Link Control Word
   encodes the frame type.  It's BCH-protected but can only correct
   ~3-4 bit errors.  More errors → we can't tell what kind of frame
   it is even though the UW was fine.
2. **Unknown frame type** — Iridium has many message formats; the
   parser only classifies the common ones.  Anything else stays RAW.
3. **Frame truncated** — some types (e.g. IRA ring-alert) need a
   minimum bit count.  If the burst was truncated (weak trailing
   signal), it stays RAW.

The bits after `<uw>` on a RAW line are the recovered payload — you
can save the session with `s` and run `analyze_raw.py` to look for
recurring patterns that might be new message types worth adding to
the parser.
