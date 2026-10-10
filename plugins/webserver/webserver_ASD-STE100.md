# webserver — HTTP server for plugin web tabs

> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

This plugin finds each sibling plugin that implements the small `web_json` contract. For each one, it publishes a browser tab at `http://<host>:<port>/`. The plugin runs on a daemon thread. Thus, it does not block the DSP in the main loop.

## Controls

| Key | Action |
|-----|--------|
| `w` | Start or stop the HTTP server. You must be on the webserver plugin tab to use this key. |

## Pipeline

- `min_sample_rate = 0`. The plugin does not use IQ data.
- `realtime = False`. A `ThreadingHTTPServer` on a background daemon thread handles each request.
- `full_view = False`. The plugin tab shows one status line only (`[web http://host:port]` or `[web OFF]`).

The request routing is:

| Path | Response |
|------|----------|
| `/` | An auto-generated index that lists all the tabs from the sibling plugins. |
| `/tab/<slug>` | The `web_static_dir/index.html` of the plugin, if the file is present. If not, an auto-generated shell that shows the JSON output. |
| `/api/<slug>` | The output of `plugin.web_json(query=…)` as JSON. |
| `/static/<slug>/<path>` | Static assets from the `web_static_dir` of the plugin. The server has a path-traversal defence. |

## Web contract (how other plugins opt in)

Each plugin that defines `web_json(query=None) -> dict` becomes a tab. Optional attributes refine the surface:

| Attribute | Purpose | Default |
|-----------|---------|---------|
| `web_json(query)` | JSON snapshot for the browser (**required**) | — |
| `web_title` | Tab label | `plugin.name` |
| `web_slug` | URL segment below `/tab/` and `/api/` | `plugin.name` |
| `web_static_dir` | Folder next to `<plugin>.py` with `index.html` and assets | — |
| `web_poll_ms` | JS poll-interval hint for the fallback shell | `2000` |

**Thread safety rule:** The `process()` method runs on the SDRTerm worker thread. The `web_json()` method runs on the HTTP request thread. Each plugin's `web_json()` must return a defensive copy, or must hold an internal lock, to prevent data races. See `plugins/adsb/adsb.py` for the standard pattern.

## Preset keys

These fields go under `plugin_states.webserver` in a `.sdrterm` preset.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | Start the HTTP server when the plugin starts. |
| `host` | string | `"127.0.0.1"` | Bind address. |
| `port` | int | `8080` | Listening port. |

## Web view

This plugin **is** the web view. The `/tab/webserver` page is not useful. The entry point is the index page at `/`.

## Limitations

- If the port is in use (`OSError`), the server **disables itself silently** for the session. The plugin does not retry and does not show an error in the HUD. The status line of the plugin tab shows `[web OFF]`.
- The default bind address is `127.0.0.1`. You can change it to `0.0.0.0` through a preset to make the server available on the LAN, but **only if you have decided who can reach it**. The plugin has no authentication.
- Each plugin's `web_json()` must be safe for concurrent calls. A plugin that returns a reference to its internal state can race against its own worker thread. This plugin cannot prevent that.
