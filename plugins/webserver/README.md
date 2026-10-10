# webserver — HTTP server for plugin web tabs

Auto-discovers every sibling plugin that implements the small `web_json` contract and publishes each as a browser-visible tab at `http://<host>:<port>/`. Runs on its own daemon thread, so DSP in the main loop is never blocked.

## Controls

| Key | Action |
|-----|--------|
| `w` | Toggle the HTTP server on/off (press on the webserver's own plugin tab) |

## Pipeline

- `min_sample_rate = 0` — the plugin doesn't touch IQ.
- `realtime = False` — a `ThreadingHTTPServer` on a background daemon thread handles every request.
- `full_view = False` — the plugin tab shows a status line only (`[web http://host:port]` or `[web OFF]`).

Request routing:

| Path | Response |
|------|----------|
| `/` | Auto-generated index listing every tab exposed by sibling plugins |
| `/tab/<slug>` | The plugin's `web_static_dir/index.html` if present, otherwise an auto-generated JSON-dump shell |
| `/api/<slug>` | `plugin.web_json(query=…)` serialised as JSON |
| `/static/<slug>/<path>` | Static assets from the plugin's `web_static_dir`, with a path-traversal guard |

## Web contract (how other plugins opt in)

Any plugin that defines `web_json(query=None) -> dict` becomes a tab. Optional attributes refine the surface:

| Attribute | Purpose | Default |
|-----------|---------|---------|
| `web_json(query)` | JSON snapshot for the browser (**required**) | — |
| `web_title` | Tab label | `plugin.name` |
| `web_slug` | URL segment under `/tab/` and `/api/` | `plugin.name` |
| `web_static_dir` | Folder next to `<plugin>.py` holding `index.html` + assets | — |
| `web_poll_ms` | JS poll-interval hint for the fallback shell | `2000` |

**Threading rule:** `process()` runs on the SDRTerm worker thread; `web_json()` runs on the HTTP request thread. Every plugin's `web_json()` must return a defensive copy (or hold an internal lock) to avoid data races. See `plugins/adsb/adsb.py` for the canonical pattern.

## Preset keys

Fields under `plugin_states.webserver` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | Start the HTTP server when the plugin starts |
| `host` | string | `"127.0.0.1"` | Bind address |
| `port` | int | `8080` | Listening port |

## Web view

This plugin **is** the web view. Its own `/tab/webserver` isn't particularly useful — the index page at `/` is the entry point.

## Limitations

- A port-binding failure (`OSError`, port already in use) **silently disables** the server for that session. There's no retry and no visible error in the HUD — check the plugin tab's status line (`[web OFF]`) to notice.
- The default bind address is `127.0.0.1`. Change it to `0.0.0.0` via preset to expose the server on the LAN **only if** you've thought about whom that reaches — there's no authentication layer.
- All plugin `web_json()` implementations must be defensively concurrent. A plugin that returns a reference to internal state will race its own worker thread; this plugin can't catch that for you.
