# ais — AIS 162 MHz maritime vessel tracker

> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

This plugin decodes AIS (Automatic Identification System) GMSK 9600 bps bursts. It uses both marine VHF channels at the same time, from one 1.024 MHz capture. It tracks the position, speed, course, and heading of each vessel by MMSI. It appends a telemetry CSV for each field change. It is the maritime equivalent of the ADS-B plugin.

## Controls

| Key | Action |
|-----|--------|
| `r` | Clear the vessel table. Set the counters (bursts, CRC-OK, messages) to zero. |
| `s` | Start or stop the CSV telemetry log. The file is `plugins/ais/ais_logs/ais.csv`. |

## Pipeline

- `min_sample_rate = 1_024_000`. This rate covers both AIS 1 (161.975 MHz) and AIS 2 (162.025 MHz) with headroom.
- `realtime = False`. The demodulator for each channel runs on a background worker thread.
- `full_view = True`. The plugin uses the full tab area when it is active.

For each channel and each chunk, the plugin does these steps:

1. **NCO downmix.** The plugin multiplies the signal by `exp(-j·2π·offset·t)`. It keeps a continuous phase across chunks (the same anti-click method as the FM DC blocker and the ACARS and VDL2 multi-channel plugins).
2. **Resample** to a 76.8 kHz baseband rate (8 samples for each bit) with `scipy.signal.resample_poly`.
3. **GMSK demodulator.** The plugin uses a one-sample frequency discriminator (`angle(x[n]·conj(x[n-1]))`), then a one-bit boxcar low-pass filter, then centre-sample slicing.
4. **NRZI decode.** The rule is `d[n] = 1 if raw[n] == raw[n-1] else 0`. The plugin keeps a per-channel `nrzi_last` field for continuity across chunks.
5. **HDLC frame scan.** The plugin finds an opening `0x7E` flag, removes stuffed bits, finds the closing flag, and verifies the 16-bit FCS (CRC-16-CCITT reflected, poly 0x8408).
6. **Byte-order flip.** HDLC transmits LSB-first within each byte, but AIS fields use MSB-first order. For this reason, the plugin reverses the bits in each payload byte before field decoding.
7. **Dispatch.** The plugin decodes Type 1/2/3 (Class A position report) and Type 18 (Class B). For other types, the plugin shows only `type` and `mmsi`.
8. **Vessel table.** The plugin keeps one row for each MMSI. The newest data replaces the older data. The plugin replaces positional fields (`lat`/`lon`/`sog`/`cog`/`heading`) only if the new message contains them. Thus a static-data message does not clear a known position.
9. **Dedup.** The plugin merges the same transmission from AIS 1 and AIS 2 with a `(len, last-two-bytes)` key. The key expires after 2 s.
10. **CSV log.** The plugin writes one row for each change in `_TRACKED_LOG_FIELDS` (lat/lon/sog/cog/heading/nav_status).

## Preset keys

These fields go under `plugin_states.ais` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `logging_enabled` | bool | `true` | Write telemetry rows to `ais_logs/ais.csv`. |
| `channels` | list of `{freq, name}` | `[AIS 1 @ 161.975M, AIS 2 @ 162.025M]` | Override the channel list. Useful for the Chinese AIS 3/4 channels, or for a one-channel capture. Each entry has `freq` in Hz and a short display `name`. |

## Web view

The scaffold does not have a web view. A Cesium ship-track map in the pattern of the ADS-B plugin is the next step. The vessel table already carries `lat`, `lon`, `cog`, and `heading`. The CSV log supports time-window queries in the same way as ADS-B.

## Limitations

- **The scaffold is a minimum.** The plugin decodes the fields of only Class A position reports (types 1/2/3) and Class B position reports (type 18). Static/voyage data (type 5), base-station reports (type 4), aids-to-navigation (type 21), and more than 20 other types are counted but not parsed. More PRs can add them later.
- **Reception needs line of sight.** AIS is VHF marine band. The practical range is tens of nautical miles from a high coastal antenna. The range is almost zero inland. Away from the coast or a large river, you must expect silence.
- **The demodulator is simple.** The GMSK demodulator is a frequency discriminator with a one-bit boxcar. It is not a Gaussian matched filter. It has no symbol timing recovery except for the HDLC flag detection. It is sufficient at ≥10 dB SNR (coastal reception), but it drops more fringe traffic than a reference decoder (for example `gr-ais` or `aisdeco2`).
- **No NMEA output.** The only persistent artefact is the CSV log. A writer for AIVDM/AIVDO sentences is a short follow-up.
- The CSV log grows for ever (one row per field change per MMSI). You must rotate the file externally. The plugin does not prune.
