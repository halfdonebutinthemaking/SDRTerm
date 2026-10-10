# airband_recorder — Fixed-channel VHF airband voice capture

Monitors a user-defined list of VHF airband frequencies in parallel, applies per-channel squelch, and writes each over-the-air transmission to its own timestamped WAV. One capture pipeline runs for every entry in `channels` from the same wideband IQ stream — no retuning.

## Controls

| Key | Action |
|-----|--------|
| `r` | Reset per-channel statistics (abort any open WAVs, zero recorded/dropped counters) |

## Pipeline

- `min_sample_rate = 2_000_000` — needs enough bandwidth to span the whole channel list in one capture.
- `realtime = False` — per-channel downconvert/decimate/squelch runs on a worker thread.
- `full_view = True` — plugin owns the tab when active.

Per channel, per chunk:

1. **Complex NCO downmix** — shifts the channel offset to DC with continuous phase accumulation across chunks (same anti-click pattern as the FM DC blocker and ACARS multi-channel plugins).
2. **64-tap FIR LPF** at 6 kHz, state preserved via `lfilter` `zi`.
3. **Decimate to 25 kHz** channel sample rate.
4. **AM envelope** — `|x|` after DC detrending.
5. **RMS squelch** — opens at `squelch_dbfs + 3 dB` hysteresis margin, closes after `silence_hangover_s` of silence.
6. **Resample to `audio_rate_hz`** (default 8 kHz) and write 16-bit PCM WAV while squelch is open.
7. **Index row** appended to `<output_dir>/index.csv` on close with timestamp, frequency, channel name, peak dBFS, and duration.

## Preset keys

Fields under `plugin_states.airband_recorder` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | Master on/off. The plugin is a no-op until this is set true. |
| `output_dir` | string | `"airband_recordings"` | Root directory for WAVs and `index.csv` |
| `squelch_dbfs` | float | `-55.0` | Open threshold on channel-RMS |
| `silence_hangover_s` | float | `2.0` | Tail hang-on after RMS drops below the threshold |
| `audio_rate_hz` | int | `8000` | Output WAV sample rate (resampled from the 25 kHz channel rate) |
| `channels` | list of `{freq, name}` | `[]` | Each entry: `freq` in Hz, `name` is a short string used in the output filename |

## Web view

None.

## Limitations

- **Fixed channel list.** No wideband scan or on-the-fly channel discovery; add every frequency you want to monitor to `channels`. See `ideas.md` for the deferred scan mode.
- Transmissions shorter than 0.4 s are dropped as spurious squelch openings.
- The first ~1% of each FIR output chunk is excluded from the RMS to avoid startup-transient false opens.
- External dependency: `scipy.signal.firwin / lfilter / resample_poly`.
