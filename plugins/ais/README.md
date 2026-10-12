# ais — AIS 162 MHz maritime vessel tracker

Decodes AIS (Automatic Identification System) GMSK 9600 bps bursts on both marine VHF channels in parallel from a single 1.024 MHz capture. Tracks vessel position, speed, course, and heading per MMSI; appends a telemetry CSV per field change — the maritime equivalent of the ADS-B plugin.

## Controls

| Key | Action |
|-----|--------|
| `r` | Clear the vessel table and reset counters (bursts, CRC-OK, messages) |
| `s` | Toggle CSV telemetry logging on/off (file at `plugins/ais/ais_logs/ais.csv`) |

## Pipeline

- `min_sample_rate = 1_024_000` — spans both AIS 1 (161.975 MHz) and AIS 2 (162.025 MHz) with headroom.
- `realtime = False` — per-channel demod runs on a background worker thread.
- `full_view = True` — plugin owns the whole tab when active.

Per channel, per chunk:

1. **NCO downmix** — complex multiplication by `exp(-j·2π·offset·t)` with phase accumulated across chunks (same anti-click pattern as the FM DC blocker and the ACARS / VDL2 multi-channel plugins).
2. **Resample** to 76.8 kHz baseband (8 samples/bit) via `scipy.signal.resample_poly`.
3. **GMSK demod** — one-sample frequency discriminator (`angle(x[n]·conj(x[n-1]))`) followed by a one-bit boxcar lowpass and centre-sample slicing.
4. **NRZI decode** — `d[n] = 1 if raw[n] == raw[n-1] else 0`, with cross-chunk continuity via a per-channel `nrzi_last` state.
5. **HDLC frame scan** — opening `0x7E` flag, bit-destuff, closing flag, 16-bit FCS (CRC-16-CCITT reflected, poly 0x8408).
6. **Byte-order flip** — HDLC transmits LSB-first within bytes but AIS fields are MSB-first, so each payload byte is bit-reversed before field decoding.
7. **Dispatch** — Type 1/2/3 (Class A position report) and Type 18 (Class B) are field-decoded. Other types are counted but surface only `type` + `mmsi`.
8. **Vessel table** — one row per MMSI, latest fields win. Positional fields (`lat`/`lon`/`sog`/`cog`/`heading`) are only overwritten when the new message carries them, so static-data messages don't clear a vessel's last known fix.
9. **Dedup** — the same transmission heard on both AIS 1 and AIS 2 is merged by a `(len, last-two-bytes)` key with a 2 s expiry.
10. **CSV log** — one row per change in `_TRACKED_LOG_FIELDS` (lat/lon/sog/cog/heading/nav_status).

## Preset keys

Fields under `plugin_states.ais` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `logging_enabled` | bool | `true` | Write telemetry rows to `ais_logs/ais.csv` |
| `channels` | list of `{freq, name}` | `[AIS 1 @ 161.975M, AIS 2 @ 162.025M]` | Override the channel list; useful for the Chinese AIS 3/4 channels or a one-channel capture. Each entry has `freq` in Hz and a short display `name`. |

## Web view

None in this scaffold. A Cesium ship-track map following the ADS-B pattern is the obvious follow-up — the vessel table already carries `lat` / `lon` / `cog` / `heading` and the CSV log is window-queryable the same way.

## Limitations

- **Scaffold scope.** Only Class A position reports (types 1/2/3) and Class B position reports (type 18) are field-decoded. Static/voyage data (type 5), base-station reports (type 4), aids-to-navigation (type 21), and the other 20+ types are counted but not parsed. Follow-up PRs will add them per demand.
- **Reception is line-of-sight.** AIS is VHF marine band — practical range is tens of nautical miles from an elevated coastal antenna, nearly zero inland. Expect dead silence away from the coast or a major river.
- **Simple discriminator demod.** The GMSK demodulator is a frequency discriminator + one-bit boxcar, no Gaussian matched filter and no timing recovery beyond preamble-driven HDLC flag alignment. Fine at ≥10 dB SNR (coastal reception) but will drop more fringe traffic than a reference decoder like `gr-ais` or `aisdeco2`.
- **No NMEA output yet.** The CSV log is the only persistent artefact. An AIVDM/AIVDO file writer would be a short follow-up.
- The CSV log appends forever (one row per field change per MMSI). Rotate externally; the plugin provides no auto-pruning.
