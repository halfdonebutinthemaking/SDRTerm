# meteor — Weather-satellite pass planner + LRPT/APT capture

Predicts passes for six LEO polar weather satellites at ~137 MHz and, on each pass, auto-retunes the SDR and launches an external `satdump` subprocess to capture + decode the downlink to an image. Serves a live pass schedule and capture gallery via the webserver plugin.

Despite the plugin name, both digital **LRPT** (METEOR-M2 2 / 3 / 4) and analog **APT** (NOAA 15 / 18 / 19) satellites are handled — all share the 137 MHz band and the same QFH + SAWbird reception chain.

## Controls

| Key | Action |
|-----|--------|
| `r` | Refresh pass cache: invalidate TLEs, re-fetch from CelesTrak, recompute the next `horizon_hours` of passes |
| `a` | Toggle auto-tune on pass rise |
| `c` | Toggle auto-capture on pass rise (spawns `satdump`; killed on fall) |

## Pipeline

- `min_sample_rate = 200_000` — needed by LRPT after satdump's internal decimation.
- `realtime = False` — no DSP in-process; `satdump` reads IQ directly from the `rtltcp_passive` server.
- `full_view = False` — plugin draws into the plugin tab, not the whole terminal.

Signal flow is largely out-of-process:

1. **TLE cache** — `plugins/meteor/passes.py` fetches CelesTrak TLEs on demand, cached 12 h in `plugins/meteor/tle_cache.txt`.
2. **Pass predictor** — pyorbital computes rise/peak/fall and max elevation for each satellite over the next `horizon_hours`, filtered by `min_elevation_deg`.
3. **Current-pass detector** — on every refresh, checks whether `now` falls between any rise and fall. If so and `auto_tune` is on, queues a frequency change via `state.pending_freq`.
4. **Capture subprocess** — if `auto_capture` is on and `satdump` is on PATH, spawns `satdump <pipeline> rtltcp -source_host <rtltcp_host> -source_port <rtltcp_port>` for the pass duration. Output lands in `plugins/meteor/web/captures/`.

## Preset keys

Fields under `plugin_states.meteor` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `location_lat` | float | — | Receiver latitude (decimal degrees). Both lat+lon required to compute passes. |
| `location_lon` | float | — | Receiver longitude (decimal degrees) |
| `location_alt_km` | float | `0.0` | Receiver altitude above WGS-84 ellipsoid, kilometres |
| `min_elevation_deg` | float | `15.0` | Minimum peak elevation to include a pass in the schedule |
| `horizon_hours` | float | `24.0` | Prediction window ahead of now |
| `auto_tune` | bool | `true` | Retune the SDR to the satellite's downlink on pass rise |
| `auto_capture` | bool | `true` | Spawn `satdump` for the pass window |
| `rtltcp_host` | string | `"127.0.0.1"` | Host for the rtl-tcp-passive bridge that satdump consumes |
| `rtltcp_port` | int | `1234` | Port for the same bridge |

## Web view

Served by the webserver plugin at:

- `/tab/meteor` — pass-schedule table with rise/peak/fall times, max elevation, azimuth; thumbnail gallery of captures already in `web/captures/`.
- `/api/meteor` — JSON snapshot: upcoming passes, receiver location, satdump binary status, auto-tune/capture toggle state, and the list of saved image filenames.

`web_poll_ms = 60000` — the dashboard polls once a minute; passes change on the scale of minutes, not seconds.

## Limitations

- **External dependency on `satdump`.** Auto-capture is silently disabled if the binary isn't on PATH. The pass schedule still works.
- **Requires the `rtltcp_passive` plugin to be active** for captures — satdump talks to it over TCP; there is no direct SDR access path.
- **NOAA-15** has had intermittent transmitter issues since 2019 and may not downlink even during a predicted pass.
- `pyorbital` is required for TLE parsing + propagation. CelesTrak failures (network down, 503s) degrade gracefully to an empty pass list rather than an error.
