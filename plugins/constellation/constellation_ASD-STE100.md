> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# constellation — IQ Constellation Display

The plugin shows the phase-space scatter plot of the strongest signal. Use it
to see the modulation order of a digital carrier. Use it also to tune the
symbol rate until the clusters become sharp.

The plugin needs `peak_marker` to be active before it in the pipeline. You
must set `peak_marker` to tracking mode (press `r` in the peak_marker tab).
In hold-off mode, the peak frequency updates only on detections. Between
updates the frequency drifts. This drift adds a carrier offset that the
plugin cannot fully correct. The clusters then smear into a ring.

## Controls

| Key | Action |
|-----|--------|
| `+` / `=` | Raise symbol rate (coarse, +500 sym/s) |
| `-` | Lower symbol rate (coarse, −500 sym/s) |
| `]` | Raise symbol rate (fine, +50 sym/s) |
| `[` | Lower symbol rate (fine, −50 sym/s) |
| `,` | Turn reference markers counter-clockwise |
| `.` | Turn reference markers clockwise |
| `z` | Change between absolute and differential display mode |
| `r` | Clear the scatter buffer |

## How to read the display

Each dot is one recovered symbol. The plugin plots it at its (I, Q)
coordinates after normalisation. For a correct PSK signal, the dots group
at fixed angles on the unit circle:

| Modulation | Clusters | Angles |
|------------|----------|--------|
| BPSK | 2 | 0°, 180° |
| QPSK | 4 | 45°, 135°, 225°, 315° |
| 8PSK | 8 | 22.5°, 67.5°, … |

If the symbol rate is **too low**, the clusters smear into arcs. You sample
in the middle of symbol transitions. If the symbol rate is **too high**, you
get many overlapping rings. At the correct rate, the clusters snap into
tight blobs.

## How it works

1. `peak_marker` gives the carrier frequency.
2. The plugin mixes the carrier to DC. It then resamples the result to 8
   samples per symbol with rational resampling (`resample_poly`).
3. A matched root-raised-cosine filter (α = 0.35) removes inter-symbol
   interference. Most real digital links use this filter shape on the
   transmit side.
4. A 4th-power batch phase estimate removes the residual carrier phase
   offset per frame. This stops the constellation from spinning.
5. The plugin takes one sample at the centre of each symbol period. It adds
   this sample to a rolling buffer of 4 000 symbols.

## Tuning procedure

1. Start `peak_marker` and make sure the peak is locked onto the signal.
2. Change to the constellation tab.
3. Press `+` or `-` to sweep the symbol rate. Look for the scattered ring
   to become distinct blobs.
4. When the blobs are tight, read the symbol rate from the footer. This
   value and the number of visible clusters together show the modulation.
   For example: 4 clusters at 10 500 sym/s means QPSK at VDL Mode 2 rate.

## Verified test signal

`samples/constellation_test.sigmf-data` is a synthetic QPSK signal at
10 500 sym/s with 20 dB SNR. The script
`scripts/gen_constellation_test.py` makes it.

Replay:

```bash
uv run python main.py \
  --file samples/constellation_test.sigmf-data \
  --bw 250000 \
  --f 120M
```

Start `peak_marker`. Change to the constellation tab. Then set the symbol
rate to **10 500 sym/s**.

### What "right" looks like

The examples below are for the **QPSK test signal**. For other modulations
the number of blobs changes (2 for BPSK, 8 for 8PSK, and so on). But the
diagnostic logic — tight blobs against smeared ring — is the same for all
PSK.

For the QPSK test signal you see four compact blobs. There is empty space
between them. You also see a clear crosshair at the origin. The blobs may
land on the axes (0°/90°/180°/270°) or on the diagonals
(45°/135°/225°/315°). Both are valid QPSK. There is a 90° rotational
ambiguity, and the display does not resolve it:

![BPSK constellation — tight clusters at correct symbol rate](images/constellation_bpsk.gif)

### What "wrong" looks like

**Symbol rate too low** — you sample in the middle of the transition
between symbols. The blobs smear outward into arcs. The arcs then join
into a continuous ring:

```
                    +Q

      . . ─ ─ ─ . .
    .               .
    .               .
    .       +       .
    .               .
    .               .
      . . ─ ─ ─ . .

                    -Q
```

**Symbol rate too high** — you sample each symbol many times. Each cluster
then splits into an inner and outer ring:

```
                    +Q

      * *  │  * *
     *   * │ *   *
─────────────+──────────
     *   * │ *   *
      * *  │  * *

                    -Q
```

**Carrier not locked or wrong signal** — you see a flat cloud of noise with
no structure. This means `peak_marker` does not track the signal, or the
signal is not PSK, or the SNR is too low (< 5 dB).

If you move the symbol rate away from 10 500 sym/s in either direction, the
clusters smear. This shows the tuning sensitivity.

## Display modes

Press `z` to change between **absolute** and **differential** phase
display.

- **Absolute** (default) — each dot is the raw recovered symbol position.
  This mode needs the 4th-power carrier estimator to remove the unknown
  phase offset. It works well for QPSK. It is not reliable for 8PSK and
  higher.
- **Differential** — each dot is the phase difference between two
  consecutive symbols (`sym[n] × conj(sym[n−1])`). The carrier phase
  cancels. So the display is stable for any PSK order. It also works
  correctly for differential encodings like D8PSK (VDL Mode 2).

![Absolute vs differential constellation mode](images/constellation_phases.gif)

## What the constellation can identify

### Modulation family and order

| Pattern on screen | Modulation |
|---|---|
| 2 clusters on real axis | BPSK |
| 4 clusters at 90° intervals, same radius | QPSK |
| 8 or 16 clusters on a single ring | 8PSK / 16PSK |
| Grid of clusters at many amplitudes | 16QAM, 64QAM, 256QAM |
| Two or more concentric rings with clusters | APSK (e.g. DVB-S2) |
| Smeared arcs, no discrete clusters | GMSK / MSK (continuous phase) |

The first key question is **ring or grid**. PSK and APSK put all points at
equal or quantised radii. QAM puts them on a rectangular grid with many
distinct amplitude levels. The current reference overlay (red `o` markers)
assumes a single ring. This is helpful for PSK/APSK. But you need a grid
overlay to align exactly with QAM.

### Signal quality and impairments

| Cluster shape | Cause |
|---|---|
| Whole constellation turned | carrier phase error |
| Clusters stretched along the radius | amplitude noise or AGC instability |
| Clusters stretched along the arc | phase noise |
| Not symmetric left/right against up/down | IQ imbalance |
| Whole constellation moved from origin | DC offset |

### What the constellation cannot show

- **Differential against absolute encoding** — same cluster positions with
  different bit mapping. You cannot tell them apart by sight.
- **Scrambling / LFSR whitening** — this randomises which cluster each
  symbol lands in. But it does not move the clusters.
- **FEC / channel coding rate** — coding is above the symbol layer.
- **OFDM** — the signal is the sum of many subcarriers. The total IQ looks
  like a uniform disc. You must demodulate individual subcarriers first.
- **Spread spectrum (DSSS)** — chips spread the energy. The constellation
  looks like noise for any symbol rate tuning.

## EVM measurement

The header shows **EVM** (Error Vector Magnitude) and an estimated **SNR**:

```
Constellation  10,500 sym/s  QPSK  21,000 bit/s  EVM 3.2%  ~30dB  4000/4000 pts
```

EVM is the RMS distance from each symbol to its nearest reference marker.
It is a percentage of the nominal symbol amplitude. Lower is better. The
SNR estimate comes from `SNR ≈ −20 log₁₀(EVM)`.

| EVM | ~SNR | Signal quality |
|---|---|---|
| < 5 % | > 26 dB | Excellent — you can use it for higher-order modulations |
| 5–10 % | 20–26 dB | Good — enough for QPSK/8PSK |
| 10–25 % | 12–20 dB | Marginal — BPSK/QPSK may still decode |
| > 25 % | < 12 dB | Poor — frame errors are likely |

### Rotation changes EVM accuracy

Each symbol is put with its **nearest** reference marker before the plugin
measures the error distance. If the markers are off by more than half the
angular slot spacing (> 45° for QPSK, > 22.5° for 8PSK), symbols go to the
wrong reference. Then EVM is falsely too high.

**Rule**: before you trust the EVM readout, use `,` or `.` to align the
red `o` markers to the visual centre of the actual clusters. After
alignment the EVM number is a true signal quality measurement. Small
misalignments (< half a slot) correct themselves through nearest-neighbour
assignment.

### Radius of the reference markers

Symbols are normalised to **median magnitude ≈ 1** before the plotting. So
for PSK signals (constant envelope), the cluster centres land very close to
the unit circle. The reference markers at radius = 1 are then accurate.

For **QAM** (many amplitude levels), the median of all symbols falls
between the inner and outer rings. This puts the unit-circle references in
the wrong place for every cluster. EVM will be too high for QAM signals
until the plugin supports per-ring reference radii. The current
implementation is accurate for any PSK/DPSK modulation order.



### Phase correction only works reliably for QPSK

The plugin uses a 4th-power carrier recovery to remove the unknown carrier
phase:

```
frame_phase = angle(mean(symbols⁴)) / 4
```

For QPSK this works well. The 4th power of the four symbol phases
{1, j, −1, −j} all collapse to 1. This gives a stable non-zero mean to
measure.

For **8PSK** the 4th power of the eight symbol phases gives only {+1, −1}.
For balanced data their mean is about zero. So `angle(0)` is undefined and
the correction gives garbage. The constellation spins continuously. It
shows a ring instead of 8 clusters.

For **16PSK and higher orders** the situation is the same or worse.

To recover carrier phase for M-PSK, you need an M-th power estimator
(`symbols^M`, then divide by M). That would add an M-way phase ambiguity.
You would then need a separate ambiguity resolver.

### D8PSK (VDL Mode 2) specifically does not work

VDL Mode 2 uses **differential** 8PSK. It has two extra problems apart
from the phase correction issue above:

1. **peak_marker cannot find the carrier.** D8PSK is wideband. The RRC
   pulse-shaping filter spreads its power across ~17 kHz. Each FFT bin
   carries only 1/140th of the total power. This puts individual bins at
   or below the noise floor. `peak_marker` finds nothing to lock onto. So
   no symbols reach the constellation.

2. **The 4th-power correction fails for 8PSK.** See the text above.

For VDL Mode 2, use the dedicated **VDL2 plugin** instead. That plugin
does wideband detection and differential decoding internally.

### Other limitations

- The RRC matched filter assumes α = 0.35. Real signals with different
  roll-off factors will give slightly wider clusters. But they stay
  readable.
- The display keeps the last 4 000 symbols. Press `r` to clear when you
  change signals or after you change the symbol rate.
