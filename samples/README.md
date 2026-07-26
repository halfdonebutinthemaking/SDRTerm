# Test signals

Synthetic SigMF recordings shipped with the repo for end-to-end verification
of each decoder plugin.  Each pair (`*.sigmf-data` + `*.sigmf-meta`) is
self-contained — the sample rate and centre frequency are stored in the meta
file so the `--bw` / `--f` flags below are redundant, but included for
clarity.

## Quick reference

| Signal file | Plugin | Symbol / bit rate | Center | SNR | Purpose |
|---|---|---|---|---|---|
| `acars_test.sigmf-data`              | acars         | 2 400 baud AM/AFSK        | 129.125 MHz | 20 dB | Verify ACARS frame decode + BCS |
| `constellation_test.sigmf-data`      | constellation | 10 500 sym/s QPSK π/4     | 120.000 MHz | 20 dB | Realistic QPSK constellation reference |
| `constellation_test_clean.sigmf-data`| constellation | 10 500 sym/s QPSK π/4     | 120.000 MHz | 35 dB | High-SNR reference — should give green EVM (~3 %) |
| `doppler_test.sigmf-data`            | spectrum / fm | FM narrow, 1 kHz tone     | 1.000 GHz   | –     | LEO Doppler ±20 kHz over 10 s |
| `pocsag_test.sigmf-data`             | pocsag        | 1 200 baud 2-FSK ±4.5 kHz | 439.9875 MHz | 20 dB | Verify POCSAG numeric + alphanumeric decode |
| `vdl2_test.sigmf-data`               | vdl2          | 10 500 sym/s D8PSK        | 136.900 MHz | 20 dB | Verify VDL Mode 2 AVLC frame decode |

## Replay commands

Each command opens SDRTerm against the recording. After the app starts, open
the plugin menu (`p`), enable the target plugin, and switch to its tab (`Tab`).

### acars

```bash
uv run python main.py --file samples/acars_test.sigmf-data --bw 250000 --f 129.125M
```

Enable **acars** plugin. Four messages should appear in bold within a few
seconds — `HELLO FROM ACARS!`, `FL350 FUEL 8.2T`, `ATIS MIA`, `TEST FRAME FOUR`.

### constellation (20 dB reference)

```bash
uv run python main.py --file samples/constellation_test.sigmf-data --bw 250000 --f 120M
```

Enable **constellation**. Defaults are correct (10 500 sym/s, QPSK, m=4, gate
off, lock on). Expect **green EVM around 3–4 %** and four tight clusters on
the ±1, ±j reference markers.

### constellation (35 dB clean reference)

```bash
uv run python main.py --file samples/constellation_test_clean.sigmf-data --bw 250000 --f 120M
```

Same settings as the 20 dB signal. Expect **green EVM around 3 %** and even
tighter clusters — this is the "known good" baseline for confirming the
display and decode chain are working end-to-end.

### doppler

```bash
uv run python main.py --file samples/doppler_test.sigmf-data --bw 250000 --f 1000M
```

Watch the **spectrum / waterfall** tab. A narrow-band FM signal drifts from
about +20 kHz to −20 kHz across the 10-second recording — the diagonal streak
in the waterfall is a realistic LEO Doppler sweep. Loops on replay.
Useful for testing `peak_marker` tracking and any Doppler-compensation
experiments; no dedicated decoder plugin.

### pocsag

```bash
uv run python main.py --file samples/pocsag_test.sigmf-data --bw 250000 --f 439.9875M
```

Enable **pocsag**. Numeric and alphanumeric messages should decode within a
few seconds with correct RICs.

### vdl2

```bash
uv run python main.py --file samples/vdl2_test.sigmf-data --bw 250000 --f 136.9M
```

Enable **vdl2** and switch to its tab. AVLC frames should decode showing
aircraft addresses and message payloads. The constellation plugin will *not*
show anything useful on this signal — see the note in `plugins/vdl2/README.md`
under "Why the constellation plugin shows nothing for VDL2."

## Regenerating

Each test signal has a matching generator under `scripts/`.  Rerun to
reproduce the tracked file byte-for-byte (fixed RNG seed) or to make a
variant.

```bash
uv run python scripts/gen_acars_test.py         # → acars_test
uv run python scripts/gen_constellation_test.py # → constellation_test (20 dB)
uv run python scripts/gen_constellation_test.py --snr 35 --duration 5 \
    --output-base samples/constellation_test_clean   # → the clean 35 dB variant
uv run python scripts/gen_doppler_test.py       # → doppler_test
uv run python scripts/gen_pocsag_test.py        # → pocsag_test
uv run python scripts/gen_vdl2_test.py          # → vdl2_test
```

## Not in this table

`sdrterm_20-07-2026_073045.sigmf-data` — a real user recording made by the
`record` plugin, kept as an incidental sample rather than a documented test
signal.
