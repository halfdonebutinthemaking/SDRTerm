# airband_recorder — Fixed-channel VHF airband voice capture

> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

This plugin monitors a user-defined list of VHF airband frequencies at the same time. It applies a squelch for each channel. It writes each transmission to a timestamped WAV file. One capture pipeline runs for each entry in `channels`. All pipelines use the same wideband IQ stream, so no retune is necessary.

## Controls

| Key | Action |
|-----|--------|
| `r` | Reset the per-channel statistics. Stop all open WAVs and set the counters to zero. |

## Pipeline

- `min_sample_rate = 2_000_000`. The plugin needs enough bandwidth to cover the full channel list in one capture.
- `realtime = False`. The downmix, decimation, and squelch for each channel run on a worker thread.
- `full_view = True`. The plugin uses the full tab area when it is active.

For each channel and each chunk, the plugin does these steps:

1. **Complex NCO downmix.** The plugin moves the channel offset to DC. It keeps a continuous phase across chunks (the same anti-click method as the FM DC blocker and the ACARS multi-channel plugin).
2. **64-tap FIR LPF** at 6 kHz. The filter state continues between chunks with `lfilter` `zi`.
3. **Decimate** to a 25 kHz channel rate.
4. **AM envelope.** The plugin computes `|x|` after it removes the DC.
5. **RMS squelch.** The squelch opens at `squelch_dbfs + 3 dB` (hysteresis margin). It closes after `silence_hangover_s` of silence.
6. **Resample** to `audio_rate_hz` (the default is 8 kHz). While the squelch is open, the plugin writes a 16-bit PCM WAV file.
7. **Index row.** When the WAV closes, the plugin adds a row to `<output_dir>/index.csv`. The row contains the timestamp, the frequency, the channel name, the peak dBFS, and the duration.

## Preset keys

These fields go under `plugin_states.airband_recorder` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | Main on/off. The plugin does nothing until this field is `true`. |
| `output_dir` | string | `"airband_recordings"` | Root directory for the WAV files and the `index.csv` file. |
| `squelch_dbfs` | float | `-55.0` | Open threshold on the channel RMS. |
| `silence_hangover_s` | float | `2.0` | Hang-on time after the RMS drops below the threshold. |
| `audio_rate_hz` | int | `8000` | Output WAV sample rate. The plugin resamples from the 25 kHz channel rate. |
| `channels` | list of `{freq, name}` | `[]` | Each entry has `freq` in Hz and `name` as a short string. The plugin uses `name` in the output filename. |

## Web view

None.

## Limitations

- **The channel list is fixed.** The plugin does not do a wideband scan or auto-discovery of channels. You must add each frequency to `channels`. See `ideas.md` for the deferred scan mode.
- The plugin drops transmissions that are shorter than 0.4 s. These are usually squelch false opens.
- The plugin excludes the first ~1% of each FIR output chunk from the RMS. This prevents startup-transient false opens.
- External dependency: `scipy.signal.firwin / lfilter / resample_poly`.
