# Future Additions

Directions that would give SDRTerm a distinctive identity in the SDR community.
Each is achievable in pure Python, fits the existing plugin architecture, and addresses
a gap that no other terminal-based SDR tool currently fills.

---

## 4. VDL Mode 2 Decoder

**Status:** Implemented (`plugins/vdl2/`)  
**Dependencies:** None beyond existing stack (pure NumPy/SciPy)

D8PSK 10 500 sym/s, HDLC/AVLC framing, self-synchronising descrambler
(G(x) = 1 + x + x⁶), CRC-CCITT. Decodes at centre frequency without
`peak_marker`. See `plugins/vdl2/vdl2.md` for full documentation.

### Known remaining limitations

- **No symbol timing recovery** — fixed sampling offset; long signals on a
  drifting oscillator will eventually accumulate bit errors. A Gardner or
  Mueller & Müller loop would fix this.
- **No frequency correction** — carrier offset must stay within ~1 kHz for
  the RRC matched filter to pass the signal cleanly.

---

## 5. Constellation — Mth-power phase correction

**Status:** Not started — safe to implement, low risk  
**Dependencies:** None; change is self-contained in `plugins/constellation/constellation.py`

### Problem

The carrier phase estimator is hardcoded to the 4th-power law:

```python
powered     = matched ** 4
frame_phase = np.angle(np.mean(powered)) / 4.0
candidates  = [frame_phase + k * np.pi / 2 for k in range(4)]
```

For M=2 (BPSK) and M=4 (QPSK) this works: all symbol phases raised to the
4th power collapse to 1, giving a stable non-zero mean to estimate from.

For **M=8 (8PSK)** the 4th power of the eight symbol phases produces only
{+1, −1}. Their mean is ≈ 0 for balanced data, so `angle(0)` is undefined
and the constellation spins into a ring instead of showing 8 clusters.

### Fix

Replace the hardcoded `4` with `self._m` throughout the estimator:

```python
powered     = matched ** self._m
frame_phase = np.angle(np.mean(powered)) / float(self._m)
candidates  = [frame_phase + k * 2 * np.pi / self._m for k in range(self._m)]
```

For M=4 this is algebraically identical to the current code (no regression).
For M=8 the 8th power of all 8PSK symbols equals 1, giving a stable mean and
correct phase correction.

### Important: do not change the symbol sampling offset

A previous attempt combined this fix with changing `offset` from
`delay % SPS + SPS//2` (= 4) to `(len(taps)//2) % SPS` (= 0). The offset
change caused flower-petal ISI patterns and was reverted. The Mth-power change
alone is safe — **leave the offset formula untouched**.

---

## 1. Satellite Doppler Auto-Tracking

**Status:** Not started  
**Dependencies:** `sgp4`, `pyorbital` (both pure Python)

Predict upcoming satellite passes, auto-tune the center frequency in real time with
Doppler correction, and optionally trigger the record plugin automatically when a bird
enters view. No other TUI SDR tool does this.

### What it would do

- Fetch and cache TLEs from Celestrak on demand (or from a local file)
- Show a pass schedule for a configured observer position (lat/lon/alt)
- On pass start: hand off center frequency control to the plugin, applying per-frame
  Doppler shift based on the propagated satellite position
- On pass end: return frequency control, optionally stop recording
- The range-scan plugin's stepped-scan infrastructure is a natural companion for
  multi-satellite monitoring between passes

### Key design points

- Observer position stored in `AppState` or plugin save-state (lat/lon/alt)
- TLE cache: `~/.config/sdrterm/tle_cache.json`, refreshed if older than 24 h
- Doppler formula: `f_rx = f_tx * (1 - v_radial / c)`; radial velocity from SGP4
  position+velocity vectors
- Plugin produces a `next_pass` result dict consumed by `render.py` for a pass
  countdown in the status line
- Integrates with `record` plugin via the same `_prev_plugin` injection mechanism
  already used for WAV/SigMF capture

---

## 2. Live Modulation Classifier

**Status:** Design laid out below — see implementation plan  
**Dependencies:** `onnxruntime` (optional; ~10 MB), pre-trained ONNX model (~2 MB)

Feed raw IQ frames into a small neural network and annotate the strongest signal with
its likely modulation type (FM, AM, BPSK, QPSK, QAM16, OOK, …) and a confidence
score. No live TUI SDR tool currently does this.

→ See **Implementation Plan** section below.

---

## 6. xPSK Signal Generator (HackRF TX plugin)

**Status:** Not started
**Dependencies:** Existing HackRF device driver (`devices/hackrf.py`). HackRF
runs as full-duplex-capable RX/TX hardware but SDRTerm currently only uses
its RX path.

A plugin that turns SDRTerm into a configurable digital signal generator.
Takes a payload (text, hex, or file) and transmits it live through the
HackRF using a chosen modulation, symbol rate, and carrier frequency. The
offline `scripts/gen_*_test.py` helpers already prove the DSP works —
this plugin would move that DSP inline and feed a HackRF instead of a
SigMF file. A key use case is validating other SDRTerm decoders end-to-end
on real RF without needing an active real-world transmitter (e.g.
transmit a synthetic ACARS burst on 129.125 MHz into a dummy load,
receive it on an RTL-SDR next to the HackRF, confirm the ACARS decoder
recovers the message).

### What it would do

- Modulations supported: OOK, 2-FSK, MSK/GMSK, BPSK, QPSK, 8PSK, DBPSK,
  DQPSK, D8PSK, 16-QAM. All the modulation classes SDRTerm can already
  identify and decode.
- Payload sources: literal text, hex string, file bytes, or a repeating
  pattern (useful for BER testing and receiver tuning).
- Configurable parameters per transmission:
  - Modulation type + differential/absolute
  - Symbol rate (100 sym/s – 5 Msym/s)
  - Carrier frequency (offset from HackRF centre)
  - Pulse shape (rectangular, raised cosine, RRC) + roll-off α
  - Samples per symbol (upsampling ratio for pulse shaping)
  - TX gain (HackRF IF gain 0–47 dB in 1 dB steps)
  - Preamble / sync word / trailer bits
  - Continuous loop vs single-shot
- Full-view tab shows current TX state, payload preview, and a live
  waveform / constellation of what's being transmitted.

### Key design points

- HackRF `read_samples_async` currently pulls RX samples in a background
  thread. Add a matching TX path using `hackrf_start_tx_async` (already
  in libhackrf; needs a Python wrapper).
- RX and TX cannot run simultaneously on HackRF — the plugin must
  disable RX-based decoders (or the SDR loop temporarily) while
  transmitting. State machine: `idle → prepare → transmitting → cool_down → idle`.
- Modulation DSP is straightforward — same pattern as the existing test
  signal generators. Precompute the full IQ buffer for short payloads,
  or use a rolling generator for long/looping transmissions.
- **Legal / safety notice must be prominent.** Transmitting on most
  frequencies requires a licence in most jurisdictions. Plugin should:
  - Show a bright warning on first activation
  - Refuse to transmit above a configurable maximum TX gain (default: low)
  - Require an explicit confirm keystroke before each transmission
  - Save "acknowledged legal notice" flag in the preset so returning
    users are not spammed
- File output as a fallback: same DSP, write to SigMF instead of TX. Useful
  when the HackRF is not present, and makes the plugin usable as a
  general-purpose test-vector generator (obsoleting most of the current
  `scripts/gen_*_test.py` files).
- Loopback test mode: if both a HackRF (TX) and an RTL-SDR (RX) are
  attached, the plugin could offer a self-test that transmits a known
  payload and asks the paired decoder plugin (ACARS, POCSAG, VDL2, etc.)
  to verify recovery — a real end-to-end regression test.

---

## 3. RF Environment Monitor / Anomaly Logger

**Status:** Not started  
**Dependencies:** None beyond existing stack; optional `requests` for webhook push

Continuous headless background scan with signal-appeared / signal-disappeared events
logged to a structured file or pushed to a webhook. Replaces the common pattern of
`rtl_power` + custom shell-script glue with a single integrated tool.

### What it would do

- Maintain a per-bin baseline power level (exponential moving average)
- Detect bins where instantaneous power exceeds baseline by a configurable threshold
- Emit events: `{ "type": "signal_appeared", "freq_hz": …, "db": …, "timestamp": … }`
- Write to JSONL log or POST to a webhook URL (Grafana, Home Assistant, custom)
- Optionally trigger range-scan on the detected frequency for a closer look
- Runs as a background plugin — no tab needed, just a status line indicator showing
  event count and last event

### Key design points

- Per-bin EMA updated every N frames; `N` and threshold configurable via save-state
- Event deduplication: a signal must disappear for at least `cooldown_s` seconds
  before a second `signal_appeared` event fires on the same bin
- JSONL format matches SigMF annotations for interoperability
- Webhook: `requests.post` in a daemon thread so it never blocks the UI loop

---

## Implementation Plan — Live Modulation Classifier

### Overview

A `modclass` plugin that sits after `peak_marker` in the pipeline. On each frame it
extracts a fixed-length IQ window centred on the tracked peak, resamples it to the
model's expected sample rate, and runs ONNX inference. The result (label + confidence)
is shown in the status line and optionally overlaid on the spectrum.

### Modulation classes (RadioML 2018.01a)

```
OOK  AM-DSB  AM-SSB  WBFM  BPSK  QPSK  8PSK  QAM16  QAM64
GFSK  CPFSK  PAM4  16APSK  32APSK  OFDM-64  OFDM-72  OFDM-128  ...
```
(24 classes total; can be reduced to a smaller subset for a lighter model)

### Data flow

```
raw IQ (complex64)
  └─ spectrum plugin  →  FFT bins + noise floor
  └─ peak_marker      →  peak_hz, peak_db
  └─ modclass
       ├─ extract_window(samples, peak_hz, state.center_hz, state.bw_hz)
       │    └─ shift to baseband, low-pass filter, decimate to MODEL_SR
       ├─ normalise: zero-mean, unit variance per I and Q
       ├─ reshape to (1, 2, MODEL_SAMPLES)  ← ONNX input
       ├─ session.run(...)                  ← ~1 ms on CPU
       └─ softmax → top-1 label + confidence
```

### Files to create / modify

| File | Change |
|------|--------|
| `plugins/modclass.py` | New plugin |
| `plugins/modclass.md` | Documentation |
| `pyproject.toml` | Add optional `[dependency-groups] ml = ["onnxruntime"]` |
| `README.md` | Add plugin to feature list |
| `plugins/README.md` | Add row to overview table |
| `scripts/train_modclass.py` | Offline training script (RadioML → ONNX export) |
| `plugins/modclass/models/modclass_lite.onnx` | Committed pre-trained model (~2 MB) |

### Model architecture

A lightweight 1-D ResNet is well-established for this task (see "Over the Air Deep
Learning" — O'Shea et al., 2018). Suggested architecture:

```
Input (2, 1024)              ← I and Q as two channels
Conv1d(2→32, k=7) + BN + ReLU
ResBlock(32, k=5)  × 3
GlobalAvgPool
Dense(32 → 24)
Softmax
```

Total parameters: ~120 k. Inference on CPU (Apple Silicon / x86): < 2 ms per frame.
Export via `torch.onnx.export` or `tf2onnx`; training data from RadioML 2018.01a
(free download, ~2 GB).

For a shortcut: the `DeepSig` community has published pre-trained ONNX checkpoints
that can be adapted without running training locally.

### `plugins/modclass.py` — skeleton

```python
class ModClassDecoder(Decoder):
    name            = 'modclass'
    key             = 'm'
    key_help        = '+/-=conf_threshold'
    min_sample_rate = 250_000

    _MODEL_SR      = 200_000   # samples/s the model was trained at
    _MODEL_SAMPLES = 1_024
    _MODEL_PATH    = os.path.join(os.path.dirname(__file__), '..', 'models',
                                  'modclass_lite.onnx')

    def start(self, state):
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(self._MODEL_PATH)
        except Exception as e:
            self._session = None
            self._error = str(e)
        self._label      = None
        self._confidence = 0.0
        self._threshold  = 0.6   # min confidence to display

    def process(self, samples, state, results=None, sdr=None):
        if self._session is None:
            return {'error': self._error}

        peak = (results or {}).get('peak_marker')
        if peak is None or peak.get('peak_hz') is None:
            return {'label': None}

        window = _extract_and_resample(
            samples, peak['peak_hz'], state.center_hz,
            state.bw_hz, self._MODEL_SR, self._MODEL_SAMPLES,
        )
        if window is None:
            return {'label': None}

        x = np.stack([window.real, window.imag]).astype(np.float32)
        x = (x - x.mean()) / (x.std() + 1e-8)
        probs = self._session.run(None, {'input': x[None]})[0][0]
        idx   = int(np.argmax(probs))
        self._label      = _LABELS[idx]
        self._confidence = float(probs[idx])
        return {'label': self._label, 'confidence': self._confidence,
                'freq_hz': peak['peak_hz']}
```

### `_extract_and_resample` helper

```python
def _extract_and_resample(samples, peak_hz, center_hz, bw_hz,
                           target_sr, target_len):
    """Shift peak to baseband, decimate, return complex window or None."""
    offset_hz = peak_hz - center_hz
    t = np.arange(len(samples)) / bw_hz
    shifted = samples * np.exp(-2j * np.pi * offset_hz * t)

    # Low-pass + decimate to target sample rate
    decim = max(1, int(bw_hz / target_sr))
    from scipy.signal import decimate as sp_decimate
    down = sp_decimate(shifted, decim, ftype='fir', zero_phase=True)

    if len(down) < target_len:
        return None
    mid = len(down) // 2
    half = target_len // 2
    return down[mid - half: mid + half]
```

### Status line

```
[MOD BPSK 94%]    ← when confident
[MOD ?    41%]    ← below threshold
[MOD off]         ← session failed (onnxruntime not installed)
```

### Minimum viable first iteration

1. Ship the plugin skeleton with graceful degradation when `onnxruntime` is absent
2. Add a `scripts/download_model.py` that fetches a pre-trained checkpoint and
   converts it to ONNX (avoids committing large binaries)
3. Wire up the pipeline: `spectrum → peak_marker → modclass → record`
4. Show label in status line only; no spectrum overlay yet

### Stretch goals

- Per-modulation colour coding in the spectrum overlay
- Confidence time-series shown as a small bar in the plugin tab
- User-adjustable confidence threshold via `+`/`-` keys
- Fine-tuning mode: `record` captures labelled IQ snippets for later re-training

---

## Heavy-Plugin Architecture (Stage 3 pattern for CPU-bound decoding)

### The problem

Several planned or in-progress plugins do work that's too heavy for the
main SDR callback thread and too GIL-bound for a background *thread*:

- **Iridium Stage 3** — full DQPSK demod + frame parsing per burst
- **ADS-B** — DF17 Manchester decoding + Reed-Solomon + CRC per frame
- **ACARS matched-filter** — replace the current envelope decoder with
  a proper matched filter + Gardner timing loop
- **NRSC-5 HD Radio** — deinterleaver + Viterbi + Reed-Solomon
- **Heavier `modclass` models** — full CNN instead of the shipped lite
  ONNX

The existing plugin runner has two tiers:

- `realtime=True` plugins run **inline in the SDR callback thread**
  (FM, RDS, record — must be fast; if they block, samples pile up in
  libusb and eventually drop)
- `realtime=False` plugins run on a **per-plugin background daemon thread**
  fed by a bounded queue (spectrum, waterfall, iridium detector,
  peak_marker, POCSAG, ACARS, VDL2, constellation)

The background-thread tier works for anything CPU-cheap enough that a
single GIL-holding worker keeps up.  Once the work per chunk exceeds
that (matched filtering, Viterbi, FFT-per-burst), threads stop helping
and the plugin either drops chunks or slows down the whole UI redraw.

### The concept: `HeavyPlugin` base class

A third plugin tier that owns a **`multiprocessing.Pool` of worker
processes** and pushes CPU-bound work off the main process entirely.

**Base class contract (`HeavyPlugin` extends `Decoder`)**:

```python
class HeavyPlugin(Decoder):
    realtime = False
    n_workers = 2                  # override in subclass
    max_pending = 4                # backpressure threshold

    # Subclass overrides:
    @staticmethod
    def worker_task(iq_chunk, meta) -> dict:
        """Runs in a WORKER process.  No shared state with main.
        `iq_chunk` is a numpy array; `meta` is a picklable dict.
        Return a small dict of results (bits, decoded frames, whatever)."""
        raise NotImplementedError

    def merge_result(self, result: dict) -> None:
        """Runs in the MAIN process.  Fold `result` into the plugin's
        display state (a deque of decoded frames, a counter, etc.).
        Called from process() as results become available."""
        raise NotImplementedError

    # Base class provides:
    #   start(state)  → spawn pool
    #   stop()        → terminate + join workers
    #   process(...)  → submit chunk (or drop on backpressure), poll for
    #                   results, call merge_result for each
```

### Design points

**Backpressure via drop-on-full.**  Chunks arrive at the SDR sample
rate; workers process at whatever pace they can.  When the pool has
`max_pending` in flight, incoming chunks are dropped (with a counter
exposed in `status_text` so the user sees "3% dropped").  Same policy
the spectrum plugin uses today — preserves real-time responsiveness
over completeness.

**Zero shared state.**  Workers are cold processes.  Work items are
self-contained: an IQ chunk plus a plain-dict meta.  Results are small
(decoded frame lists, not raw arrays).  This makes the workers
trivially crash-safe — a rogue exception in one doesn't touch the main
plugin's display state.

**Shared-memory fast path for large IQ chunks** (optional).  Pickling
16k complex samples through a queue costs ~15 µs per chunk which is
fine.  For bigger stitched batches (Iridium-style multi-burst
concatenation, ~2 s of 2 MHz IQ = 32 MB), use
`multiprocessing.shared_memory.SharedMemory` to hand the buffer over
by name, then reclaim.  Only worth adding when profiling shows pickle
cost dominates.

**Result polling.**  Two options:
1. Main-thread poll in `process()` — simple, no extra threads, but
   result latency is one process() cycle
2. Dedicated result-consumer thread — pushes results into `merge_result`
   as they arrive, at the cost of a lock around the display deque

Option 1 first; option 2 if latency becomes visible.

**Worker crash recovery.**  Pool workers that raise unhandled
exceptions get restarted transparently by the pool.  Base class logs
the exception via `state.flash_msg` so the user can see something went
wrong without the plugin silently going dark.

**Lifecycle discipline.**  `start()` spawns workers; `stop()` sends a
sentinel + `pool.terminate() → pool.join(timeout=2)` before returning.
If a worker hangs, the timeout kicks in and we move on — the plugin
tab goes dead but the app stays responsive.

### How Iridium Stage 3 uses it

Concrete example — replaces the current shell-out to iridium-toolkit:

```python
class IridiumDecoderPlugin(HeavyPlugin):
    name        = 'iridium_decode'
    key         = 'D'
    n_workers   = 2         # DSP is CPU-heavy, HackRF sample rate low
    max_pending = 4

    @staticmethod
    def worker_task(iq_chunk, meta):
        # Port the iridium-toolkit pipeline as a pure Python function:
        #   fft_burst_tagger → cut_and_downmix → demod → bitsparser
        bursts = detect_bursts(iq_chunk, meta['sample_rate'])
        frames = []
        for b in bursts:
            bits = dqpsk_demod(b, meta['sample_rate'])
            frame = parse_iridium_frame(bits)
            if frame is not None:
                frames.append(frame)
        return {'frames': frames, 'chunk_ts': meta['ts']}

    def merge_result(self, result):
        for frame in result['frames']:
            self._messages.appendleft(frame)
            if len(self._messages) > _MAX_MSGS:
                self._messages.pop()

    def draw_full(self, screen_obj, state, result, rows, cols):
        # Same shape as the acars/pocsag plugins' message-list view
```

The `iridium` (Stage 1) plugin still runs unchanged as the detector.
Stage 3 subscribes to the same IQ stream in parallel, does the
demod in workers, and shows decoded frames in its own tab.

### Migration path for existing heavy plugins

Rather than rewrite everything at once, land the base class + one
adopter (Iridium Stage 3), then migrate opportunistically when a
plugin runs into CPU headroom limits:

1. **Ship `HeavyPlugin` base class** in `core.py` — a few dozen lines
2. **First adopter: Iridium Stage 3** — replaces the `decode_bursts.sh`
   shell-out with in-process demod.  Batching logic (same-slot grouping)
   moves into the plugin itself, no external file dance
3. **Second adopter: ACARS matched-filter mode** (optional) — the
   current AGC + adaptive slicer works for strong signals, matched
   filter would help weak-signal reception at the cost of more CPU
4. **Third: any future ADS-B / NRSC-5 / heavy modclass plugin**

### Non-goals

- **No IPC framework replacement.**  Standard `multiprocessing.Pool` +
  queue is enough; we're not building an actor system.
- **No cross-plugin worker sharing.**  Each `HeavyPlugin` owns its own
  pool.  Shared pools would introduce coupling that isn't worth the
  memory savings for a hobbyist app.
- **No GPU offload.**  Real-time DSP on the CPU with SIMD-heavy numpy
  is fine at these sample rates; GPU adds an install-complexity burden
  that hurts more than it helps.

### Rough cost

- `HeavyPlugin` base class: ~150 LOC + a small test that spawns
  workers, submits a fake task, verifies result arrival, terminates
- Iridium Stage 3 as first adopter: multi-day effort (the demod itself
  is the hard part, not the plugin plumbing) — port + validate against
  iridium-toolkit's output on the same input files
- Doc pass: one section in `plugins/README.md` explaining the three
  tiers so future contributors know which one to pick

---

## 7. Beast Output Plugin — feed ADS-B decoded frames to aggregators

**Status:** Not started
**Dependencies:** None (stdlib TCP + bytes)

### Rationale

The `plugins/adsb` decoder already produces valid 112-bit Mode-S frames
that pass CRC-24.  Every open ADS-B aggregator (ADSBExchange, adsb.fi,
airplanes.live, RadarBox, OpenSky) ingests the same standard
**Beast binary format** used by dump1090 and readsb.  A Beast TCP server
plugin lets SDRTerm feed all of them via one interface, without
reinventing per-aggregator upload logic or the MLAT/registration
choreography.

### Architecture

```
SDRTerm/adsb → decoded Mode-S frames
                     ↓
             plugins/beast_out → Beast TCP :30005
                                      ↓
                    readsb / adsbexchange-feeder / dump1090-fa client
                                      ↓
                            feed.adsbexchange.com
                            feed.adsb.fi
                            feed.airplanes.live
                            feed.radarbox.com
                            opensky
```

Users install the aggregator's official feeder software (a Docker
container or a `.deb` — every aggregator ships one).  That software
connects to `localhost:30005` as a Beast client and handles the actual
upload, MLAT coordination, health-checking, and account registration
with the aggregator.

We stay firmly on our side of the line: we produce a standards-compliant
local Beast stream, they handle everything else.

### Wire format (per frame)

```
0x1a 0x33 <6-byte 12MHz timestamp> <1-byte RSSI> <14-byte long-Mode-S>
```

- Prefix `0x1a 0x33` marks a long-format frame; `0x1a 0x32` for short (56-bit)
- Timestamp: 6 bytes, big-endian, 12 MHz tick counter
- RSSI: 1 byte, 0-255, proxied from the correlator's peak strength
  divided into the current chunk's noise floor
- Payload: the raw 14 bytes we already have in `_parse_df17`
- Any `0x1a` in the timestamp, RSSI or payload is escaped by doubling
  (`0x1a 0x1a` per occurrence)

### Plugin skeleton

Same pattern as `rtl-tcp-passive`:

- `name = 'beast-out'`, `key = ...` (something free)
- `save_state` / `load_state` for `port` (default 30005), `enabled`
- `start()`: bind TCP server on 0.0.0.0:30005, spawn accept thread
- Cross-plugin hook: adsb plugin calls `beast_out.publish(msg_bytes, rssi)`
  for every CRC-passing DF17 frame.  Duck-typed via
  `getattr(registry['beast-out'], 'publish', None)`.
- Broadcast: on `publish`, format Beast bytes, `sendall` to every
  connected client (drop on error, remove from client set)

Small enough that no LaTeX walkthrough is warranted — the wire format
comment above is the whole spec.

### Rough cost

- Beast formatter: ~30 LOC (byte packing + escape loop)
- TCP server + accept thread + client set: ~60 LOC (mostly matching
  `rtl-tcp-passive`'s socket handling)
- adsb-plugin hook: 3 lines in `_parse_df17` — `if beast_out: beast_out.publish(msg, rssi)`
- Tests: publish → connect → receive → assert bytes match spec.  ~50 LOC.
- Preset addition: put `beast-out` in `active_decoders`.

Total: ~150 LOC + ~50 LOC tests.

### Non-goals

- **No direct upload to aggregators.**  Each has its own auth flow,
  MLAT sync, retry semantics.  Their official feeder software solves
  that correctly; we don't need to.
- **No Beast client mode.**  If a user has an existing Beast source
  (external dump1090, a KiwiSDR, whatever) they can already point
  their feeder at that; we don't need to broker.
- **No web view for this plugin.**  Status is "connected clients: N"
  — display in the SDRTerm status bar is enough.

### Related — client-side inputs

adsbdb + hexdb + planespotters (see the enrichment discussion) are all
INPUT sources — they read.  This plugin is the corresponding OUTPUT:
it publishes.  Together they close the ADS-B loop end-to-end.
