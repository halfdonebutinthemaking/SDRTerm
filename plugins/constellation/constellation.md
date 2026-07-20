# constellation — IQ Constellation Display

Shows the phase-space scatter plot of the strongest signal. Use it to visually
identify the modulation order of a digital carrier and to tune the symbol rate
until the clusters click into focus.

Requires `peak_marker` to be active before it in the pipeline.

## Controls

| Key | Action |
|-----|--------|
| `+` / `=` | Increase symbol rate (+500 sym/s) |
| `-` | Decrease symbol rate (−500 sym/s) |
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

For the QPSK test signal: four compact blobs on the diagonals, empty space
between them, clear crosshair at the origin:

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

## Limitations

- The 4th-power carrier correction is optimised for QPSK. For 8PSK or higher
  orders the correction is approximate; the constellation may show slight
  rotation between frames, causing blobs to widen slightly.
- The RRC matched filter assumes α = 0.35. Real signals with different roll-off
  factors will produce slightly wider clusters but remain readable.
- The display accumulates the last 4 000 symbols. Press `r` to clear when
  switching signals or after changing the symbol rate.
