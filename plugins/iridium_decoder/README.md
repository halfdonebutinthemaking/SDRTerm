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
second plus O(N × 401) FIR filtering per burst.  On modest CPUs (M-series
Mac, mid-range x86) this is ~2× realtime — should handle live streams
comfortably.  During dense satellite passes the queue can fill; the plugin
drops chunks rather than blocking the SDR reader.  If drops become
persistent, disable both-bin mode (`b`) to halve per-burst CPU cost.
