# constellation — IQ Constellation Display

Shows the phase-space scatter plot of the strongest signal. Use it to visually
identify the modulation order of a digital carrier and to tune the symbol rate
until the clusters click into focus.

Requires `peak_marker` to be active before it in the pipeline, **with tracking
mode enabled** (`r` in the peak_marker tab). In hold-off mode the peak
frequency is only updated on detections; between updates it drifts, which
introduces a carrier offset the plugin cannot fully correct, causing the
clusters to smear into a ring.

## Controls

| Key | Action |
|-----|--------|
| `+` / `=` | Increase symbol rate (coarse, +500 sym/s) |
| `-` | Decrease symbol rate (coarse, −500 sym/s) |
| `]` | Increase symbol rate (fine, +50 sym/s) |
| `[` | Decrease symbol rate (fine, −50 sym/s) |
| `r` | Clear the scatter buffer |

## How to read the display

Each dot is one recovered symbol plotted at its (I, Q) coordinates after
normalisation. For a correctly demodulated PSK signal the dots cluster at fixed
angles on the unit circle:

| Modulation | Clusters | Angles |
|------------|----------|--------|
| BPSK | 2 | 0°, 180° |
| QPSK | 4 | 45°, 135°, 225°, 315° |
| 8PSK | 8 | 22.5°, 67.5°, … |

When the symbol rate is **too low** the clusters smear into arcs — you are
sampling in the middle of symbol transitions.  When it is **too high** you get
multiple overlapping rings.  At the correct rate the clusters visibly snap into
tight blobs.

## How it works

1. `peak_marker` supplies the carrier frequency.
2. The carrier is mixed to DC and the result is resampled to exactly
   8 samples per symbol using rational resampling (`resample_poly`).
3. A matched root-raised-cosine filter (α = 0.35) removes inter-symbol
   interference — the same filter shape used on the transmit side of most
   real digital links.
4. A 4th-power batch phase estimate removes the residual carrier phase offset
   per frame, preventing the constellation from spinning.
5. One sample is taken at the centre of each symbol period and added to a
   rolling buffer of 4 000 symbols.

## Tuning procedure

1. Enable `peak_marker` and confirm the peak is locked onto the signal.
2. Switch to the constellation tab (`c`).
3. Press `+` / `-` to sweep the symbol rate.  Watch for the scattered ring to
   resolve into distinct blobs.
4. Once tight, read the symbol rate from the footer.  Combined with the number
   of visible clusters this identifies the modulation (e.g. 4 clusters at
   10 500 sym/s → QPSK at VDL Mode 2 rate).

## Verified test signal

`samples/constellation_test.sigmf-data` is a synthetic QPSK signal at
10 500 sym/s with 20 dB SNR, generated with `scripts/gen_constellation_test.py`.

Replay:

```bash
uv run python main.py \
  --file samples/constellation_test.sigmf-data \
  --bw 250000 \
  --f 120M
```

Enable `peak_marker` (`k`), switch to constellation (`c`), then set symbol rate
to **10 500 sym/s**.

### What "right" looks like

The examples below are specific to the **QPSK test signal**.  For other
modulations the number of blobs changes (2 for BPSK, 8 for 8PSK, etc.) but
the diagnostic logic — tight blobs vs. smeared ring — is the same for all PSK.

For the QPSK test signal: four compact blobs, empty space between them, clear
crosshair at the origin. The blobs may land on the axes (0°/90°/180°/270°)
or on the diagonals (45°/135°/225°/315°) — both are valid QPSK; there is an
inherent 90° rotational ambiguity and the display does not resolve it:

```
                    +Q

        *    │    *
       ***   │   ***
        *    │    *
─────────────+──────────
        *    │    *
       ***   │   ***
        *    │    *

                    -Q
```

### What "wrong" looks like

**Symbol rate too low** — you are sampling mid-transition between symbols.
The blobs smear outward into arcs that eventually merge into a continuous ring:

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

**Symbol rate too high** — you sample each symbol multiple times, so every
cluster duplicates into an inner and outer ring:

```
                    +Q

      * *  │  * *
     *   * │ *   *
─────────────+──────────
     *   * │ *   *
      * *  │  * *

                    -Q
```

**Carrier not locked / wrong signal** — a flat cloud of noise with no
structure at all means `peak_marker` is not tracking the signal, the signal
is not PSK, or the SNR is too low (<5 dB).

Rotating the symbol rate away from 10 500 sym/s in either direction causes the
clusters to smear, confirming the tuning sensitivity.

## What the constellation can identify

### Modulation family and order

| Pattern on screen | Modulation |
|---|---|
| 2 clusters on real axis | BPSK |
| 4 clusters at 90° intervals, same radius | QPSK |
| 8 or 16 clusters on a single ring | 8PSK / 16PSK |
| Grid of clusters at multiple amplitudes | 16QAM, 64QAM, 256QAM |
| Two or more concentric rings with clusters | APSK (e.g. DVB-S2) |
| Smeared arcs, no discrete clusters | GMSK / MSK (continuous phase) |

The key first question is **ring or grid**: PSK and APSK put all points at equal
or quantised radii; QAM places them on a rectangular grid with multiple distinct
amplitude levels.  The current reference overlay (red `o` markers) assumes a
single ring — useful for PSK/APSK, but a grid overlay would be needed to
precisely align with QAM.

### Signal quality and impairments

| Cluster shape | Cause |
|---|---|
| Whole constellation rotated | carrier phase error |
| Clusters elongated radially | amplitude noise or AGC instability |
| Clusters elongated in the arc direction | phase noise |
| Asymmetric left/right vs. up/down | IQ imbalance |
| Whole constellation shifted from origin | DC offset |

### What the constellation cannot reveal

- **Differential vs. absolute encoding** — same cluster positions, different bit
  mapping; indistinguishable visually.
- **Scrambling / LFSR whitening** — randomises which cluster each symbol lands
  in but does not move the clusters.
- **FEC / channel coding rate** — coding happens above the symbol layer.
- **OFDM** — the signal is a sum of many subcarriers; the aggregate IQ looks
  like a uniform disc.  Individual subcarriers must be demodulated first.
- **Spread spectrum (DSSS)** — chips spread the energy; the constellation looks
  like noise regardless of symbol rate tuning.

## Limitations

### Phase correction only works reliably for QPSK

The plugin uses a 4th-power carrier recovery to remove the unknown carrier phase:

```
frame_phase = angle(mean(symbols⁴)) / 4
```

For QPSK this works perfectly: the 4th power of the four symbol phases {1, j, −1, −j}
all collapse to 1, giving a stable non-zero mean to measure.

For **8PSK** the 4th power of the eight symbol phases produces only {+1, −1}.
For balanced data their mean is approximately zero, so `angle(0)` is undefined and
the correction produces garbage. The constellation spins continuously and shows a
ring instead of 8 clusters.

For **16PSK and higher orders** the situation is similar or worse.

To properly recover carrier phase for M-PSK, an M-th power estimator is needed
(`symbols^M`, then divide by M). That would introduce an M-way phase ambiguity,
requiring a separate ambiguity resolver.

### D8PSK (VDL Mode 2) specifically does not work

VDL Mode 2 uses **differential** 8PSK, which has two additional problems beyond
the phase correction issue above:

1. **peak_marker cannot find the carrier.** D8PSK is wideband — its power is
   spread across ~17 kHz by the RRC pulse-shaping filter.  Each FFT bin carries
   only 1/140th of the total power, putting individual bins at or below the noise
   floor.  `peak_marker` finds nothing to lock onto, so no symbols reach the
   constellation at all.

2. **The 4th-power correction fails for 8PSK** as described above.

For VDL Mode 2 use the dedicated **VDL2 plugin** (`v`) instead, which handles
wideband detection and differential decoding internally.

### Other limitations

- The RRC matched filter assumes α = 0.35. Real signals with different roll-off
  factors will produce slightly wider clusters but remain readable.
- The display accumulates the last 4 000 symbols. Press `r` to clear when
  switching signals or after changing the symbol rate.
