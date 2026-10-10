# meteor — Weather-satellite pass planner + LRPT/APT capture

> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

This plugin predicts passes for six LEO polar weather satellites at ~137 MHz. For each pass, the plugin tunes the SDR to the correct downlink frequency and starts an external `satdump` subprocess. The subprocess captures and decodes the downlink to an image. The plugin serves a live pass-schedule table and a capture gallery through the webserver plugin.

The plugin name is "meteor", but the plugin handles both digital **LRPT** (METEOR-M2 2 / 3 / 4) and analog **APT** (NOAA 15 / 18 / 19) satellites. All six use the 137 MHz band, so one QFH antenna and one SAWbird filter can receive all of them.

## Controls

| Key | Action |
|-----|--------|
| `r` | Refresh the pass cache. The plugin invalidates the TLE cache, downloads fresh TLEs from CelesTrak, and recomputes the next `horizon_hours` of passes. |
| `a` | Start or stop auto-tune on pass rise. |
| `c` | Start or stop auto-capture on pass rise. The plugin starts `satdump` on rise and stops it on fall. |

## Pipeline

- `min_sample_rate = 200_000`. LRPT needs this rate after satdump decimates the signal internally.
- `realtime = False`. The plugin does no DSP in process. `satdump` reads IQ data directly from the `rtltcp_passive` server.
- `full_view = False`. The plugin draws into the plugin tab, not into the full terminal.

The signal flow is mostly out-of-process:

1. **TLE cache.** `plugins/meteor/passes.py` downloads TLEs from CelesTrak when necessary. The cache is in `plugins/meteor/tle_cache.txt` and is valid for 12 h.
2. **Pass predictor.** `pyorbital` computes the rise, peak, and fall times, and the maximum elevation, for each satellite in the next `horizon_hours`. The plugin filters by `min_elevation_deg`.
3. **Current-pass detector.** At each refresh, the plugin checks if `now` is between a rise and a fall time. If `auto_tune` is on, the plugin queues a frequency change through `state.pending_freq`.
4. **Capture subprocess.** If `auto_capture` is on and `satdump` is on the PATH, the plugin starts `satdump <pipeline> rtltcp -source_host <rtltcp_host> -source_port <rtltcp_port>` for the pass duration. The output goes to `plugins/meteor/web/captures/`.

## Preset keys

These fields go under `plugin_states.meteor` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `location_lat` | float | — | The latitude of the receiver (decimal degrees). You must set both `location_lat` and `location_lon` to get pass predictions. |
| `location_lon` | float | — | The longitude of the receiver (decimal degrees). |
| `location_alt_km` | float | `0.0` | The altitude of the receiver above the WGS-84 ellipsoid, in kilometres. |
| `min_elevation_deg` | float | `15.0` | The minimum peak elevation for the plugin to include a pass. |
| `horizon_hours` | float | `24.0` | The prediction window from now. |
| `auto_tune` | bool | `true` | Tune the SDR to the satellite downlink on pass rise. |
| `auto_capture` | bool | `true` | Start `satdump` for the pass. |
| `rtltcp_host` | string | `"127.0.0.1"` | The host for the rtl-tcp-passive bridge that satdump uses. |
| `rtltcp_port` | int | `1234` | The port for the same bridge. |

## Web view

The webserver plugin serves these endpoints:

- `/tab/meteor` — a pass-schedule table with the rise, peak, and fall times, the maximum elevation, and the azimuth. The page also shows a thumbnail gallery for the images already in `web/captures/`.
- `/api/meteor` — a JSON snapshot. It contains the upcoming passes, the receiver location, the status of the satdump binary, the state of the auto-tune and auto-capture toggles, and the list of saved image filenames.

`web_poll_ms = 60000`. The dashboard polls once each minute. Passes change on a scale of minutes, not seconds.

## Limitations

- **External dependency: `satdump`.** If the binary is not on the PATH, the plugin disables auto-capture without an error. The pass schedule continues to work.
- **The `rtltcp_passive` plugin must be active** for captures to work. `satdump` talks to it through TCP. The plugin has no direct SDR access path.
- **NOAA-15** has had intermittent transmitter faults since 2019. It can be silent even during a predicted pass.
- `pyorbital` is a requirement for TLE parsing and orbital propagation. If a CelesTrak download fails (network issues, 503 errors), the plugin returns an empty pass list instead of an error.
