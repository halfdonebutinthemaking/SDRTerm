# adsb — ADS-B 1090 MHz aircraft tracker

> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

This plugin decodes Mode-S Extended Squitter (DF17) at 1090 MHz. It tracks the position, altitude, speed, and callsign of each aircraft from live 2 MHz IQ data. It serves a live 3D Cesium globe map through the webserver plugin.

## Controls

| Key | Action |
|-----|--------|
| `r` | Clear the aircraft table. Set the counters (bursts, CRC-OK, messages) to zero. |
| `s` | Start or stop the CSV telemetry log. The file is `plugins/adsb/adsb_logs/adsb.csv`. |

## Pipeline

- `min_sample_rate = 2_000_000`. Mode-S PPM bits are 500 ns long. 2 MSPS gives 4 samples for each bit, which is sufficient for the preamble match.
- `realtime = False`. The plugin runs on a background worker thread.
- `full_view = True`. The plugin uses the full tab area when it is active.

The signal flow for each chunk is:

1. **Preamble match.** A matched filter compares the signal to the 8 µs Mode-S preamble template. The threshold is 10 times the rolling median of |x|².
2. **PPM bit slice.** The plugin reads 112 payload bits. For each bit, it compares an early and a late sample.
3. **CRC-24.** The plugin uses the Mode-S CRC with the DF17 generator polynomial. If the CRC does not agree, the plugin discards the frame.
4. **Type-code dispatch.** The plugin reads the identification (callsign), the airborne position (CPR encoded), the velocity, or the surface position.
5. **CPR decode.** When an even and an odd position frame arrive within 10 s, the plugin does a global decode. If not, it does a local decode from the last known position.
6. **Aircraft table.** The plugin keeps one row for each ICAO. The newest data replaces the older data. The plugin adds each track point to a per-aircraft list for the web map.
7. **CSV log.** The plugin writes one row for each change in `_TRACKED_LOG_FIELDS` (lat/lon/alt/gs/ias/tas/heading/vr). The web view uses this file for time-window queries.

## Preset keys

These fields go under `plugin_states.adsb` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `logging_enabled` | bool | `true` | Write telemetry rows to `adsb_logs/adsb.csv`. |
| `location_lat` | float | — | The latitude of the receiver (decimal degrees). You must set both `location_lat` and `location_lon` to get the distance and max-range features. |
| `location_lon` | float | — | The longitude of the receiver (decimal degrees). |
| `web_tiles` | string or dict | `"esri-satellite"` | A named basemap (`cartodb`, `cartodb-dark`, `osm`, `osm-de`, `opentopomap`, `wikimedia`, `esri-satellite`), or a full dict with `url` / `subdomains` / `credit` / `max_zoom` for a custom raster XYZ provider. |

## Web view

The webserver plugin serves these endpoints:

- `/tab/adsb` — a 3D Cesium globe with live aircraft markers, trails, selection, a receiver pin, range rings, and a layer-switcher dropdown.
- `/api/adsb` — a JSON snapshot. Query parameters:
  - `from`, `to` (ISO 8601 UTC) — a time-window filter for the CSV log. The default is the last 30 min.
  - `q` — a case-insensitive substring match on the ICAO and the callsign.
  - `global=1` — causes the plugin to ignore `from` and `to` and to scan the full log. Use with `q`.
  - `tiles=<name>` — a one-shot server-side change of the basemap. The dropdown writes back through this parameter.

The response contains the `tile_providers` field (the full server-side list of providers for the dropdown) and the `web_tiles` field (the resolved provider now in use).

## Limitations

- The plugin decodes only **DF17 Extended Squitter**. It does not decode DF11 (short) or other formats.
- A CPR global decode needs **both an even and an odd** position frame within 10 s. If only one parity arrives, the plugin uses a local decode from the last known position for that ICAO.
- The **public VersaTiles endpoint uses vector tiles only**. Cesium's `UrlTemplateImageryProvider` does not render vector tiles — it renders raster tiles only. To use VersaTiles, run a local raster version and give the plugin a full dict in `web_tiles`.
- The CSV log grows for ever. It writes one row for each field change for each aircraft. You must rotate the file externally. The plugin does not prune automatically.
