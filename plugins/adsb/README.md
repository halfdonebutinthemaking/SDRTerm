# adsb — ADS-B 1090 MHz aircraft tracker

Decodes Mode-S Extended Squitter (DF17) at 1090 MHz. Tracks aircraft position, altitude, speed, and callsign from live 2 MHz IQ, and serves a live 3D Cesium globe map via the webserver plugin.

## Controls

| Key | Action |
|-----|--------|
| `r` | Clear aircraft table and reset counters (bursts, CRC-OK, messages) |
| `s` | Toggle CSV telemetry logging on/off (file at `plugins/adsb/adsb_logs/adsb.csv`) |

## Pipeline

- `min_sample_rate = 2_000_000` — Mode-S PPM bits are 500 ns wide; 2 MSPS gives 4 samples/bit for reliable preamble correlation.
- `realtime = False` — processed on a background worker thread.
- `full_view = True` — plugin owns the whole tab when active.

Signal flow per chunk:

1. **Preamble correlation** — matched-filter against the 8 µs Mode-S preamble template, adaptive threshold (10× rolling median of |x|²).
2. **PPM bit slice** — 112-bit payload demod, early/late sample pair per bit.
3. **CRC-24** — Mode-S CRC with the DF17 generator polynomial; frames failing CRC are discarded silently.
4. **Type-code dispatch** — identification (callsign), airborne position (CPR encoded), velocity, surface position.
5. **CPR decode** — global decode when a matching even/odd pair arrives within 10 s; local decode (using the last known position) otherwise.
6. **Aircraft table** — one row per ICAO, latest telemetry wins; track points appended to a per-aircraft list for the web map.
7. **CSV log** — one row per `_TRACKED_LOG_FIELDS` change (lat/lon/alt/gs/ias/tas/heading/vr); backs the web view's time-window queries.

## Preset keys

Fields under `plugin_states.adsb` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `logging_enabled` | bool | `true` | Write telemetry rows to `adsb_logs/adsb.csv` |
| `location_lat` | float | — | Receiver latitude (decimal degrees). Both lat and lon must be set to enable distance / max-range features. |
| `location_lon` | float | — | Receiver longitude (decimal degrees) |
| `web_tiles` | string or dict | `"esri-satellite"` | Named basemap (`cartodb`, `cartodb-dark`, `osm`, `osm-de`, `opentopomap`, `wikimedia`, `esri-satellite`) or a full dict with `url` / `subdomains` / `credit` / `max_zoom` for a custom raster XYZ provider |

## Web view

Served by the webserver plugin at:

- `/tab/adsb` — Cesium globe (3D) with live aircraft markers, trails, selection, receiver pin, range rings, and a layer-switcher dropdown.
- `/api/adsb` — JSON snapshot. Query params:
  - `from`, `to` (ISO 8601 UTC) — time-window filter against the CSV log; default is the last 30 min.
  - `q` — case-insensitive substring match against ICAO + callsign.
  - `global=1` — ignore `from` / `to`, scan the entire log (use with `q`).
  - `tiles=<name>` — one-shot server-side basemap switch; the dropdown writes back through this.

The response carries `tile_providers` (the full server-side provider list used to populate the dropdown) and `web_tiles` (the currently resolved provider).

## Limitations

- Only **DF17 Extended Squitter** is decoded; DF11 (short) and other formats are ignored.
- CPR global decode needs both an **even and an odd** position frame within 10 s. If only one parity arrives, the plugin falls back to local decode against the last known position for that ICAO.
- **VersaTiles' public endpoint is vector-only** and cannot be rendered by Cesium's `UrlTemplateImageryProvider` (which is raster-only). A self-hosted raster VersaTiles endpoint can still be used by passing a full dict in `web_tiles`.
- The CSV log appends forever — one row per field change per aircraft. Rotate externally; the plugin provides no auto-pruning.
