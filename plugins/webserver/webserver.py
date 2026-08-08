"""
SDRTerm web-view server plugin.

Auto-discovers other plugins that implement a small web contract and
publishes each as a browser-visible tab at ``http://<host>:<port>/``.

Web contract (all duck-typed and opt-in):

    plugin.web_json()      -> dict          # snapshot for the browser (required)
    plugin.web_title       = str            # tab label     (default: plugin.name)
    plugin.web_slug        = str            # URL segment   (default: plugin.name)
    plugin.web_static_dir  = 'web'          # folder next to plugin.py w/ index.html
    plugin.web_poll_ms     = int            # JS poll interval hint (default: 2000)

Any plugin exposing at least ``web_json`` becomes a tab.  If the plugin
has a ``web_static_dir/index.html`` that page is served for ``/tab/<slug>``;
otherwise a tiny auto-generated shell polls ``/api/<slug>`` and dumps the
JSON as text (useful during development).

Threading: the HTTP server runs on its own daemon thread so it never
blocks SDRTerm's main loop.  Because plugin ``process()`` methods run on
the SDRTerm worker thread while ``web_json()`` is called on the request
thread, every plugin's ``web_json()`` MUST return a defensive copy
(or hold an internal lock) --- see plugins/adsb/adsb.py for the pattern.
"""

import inspect
import json
import os
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from core import Decoder, AppState


_DEFAULT_HOST = '127.0.0.1'
_DEFAULT_PORT = 8080

_MIME = {
    '.html': 'text/html; charset=utf-8',
    '.htm':  'text/html; charset=utf-8',
    '.js':   'application/javascript',
    '.mjs':  'application/javascript',
    '.css':  'text/css',
    '.json': 'application/json',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
    '.txt':  'text/plain; charset=utf-8',
    '.map':  'application/json',
}


# ── HTTP request handler ─────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    # Set by the enclosing WebServer via a class attribute.
    _webserver = None

    # Suppress the default HTTP access log -- otherwise SDRTerm's curses
    # screen scrolls with every request.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = unquote(self.path.split('?', 1)[0])
        parts = [p for p in path.split('/') if p]
        try:
            if not parts:
                return self._serve_index()
            if parts[0] == 'api' and len(parts) == 2:
                return self._serve_api(parts[1])
            if parts[0] == 'tab' and len(parts) == 2:
                return self._serve_tab(parts[1])
            if parts[0] == 'static' and len(parts) >= 3:
                return self._serve_static(parts[1], '/'.join(parts[2:]))
        except Exception as exc:
            self.send_error(500, str(exc))
            return
        self.send_error(404)

    # ── routes ───────────────────────────────────────────────────────────────

    def _serve_index(self):
        entries = self._webserver._web_plugins()
        html = [
            '<!doctype html><meta charset=utf-8><title>SDRTerm</title>',
            '<style>body{font:16px system-ui;padding:2em;background:#111;color:#eee;max-width:40em;margin:auto}'
            'a{color:#8cf;text-decoration:none;display:block;padding:.6em 0;'
            'border-bottom:1px solid #333;font-size:1.1em}'
            'a:hover{color:#fff}h1{border-bottom:2px solid #555;padding-bottom:.3em}</style>',
            '<h1>SDRTerm plugins</h1>',
        ]
        if not entries:
            html.append('<p>No plugin currently exposes a web view. Enable one '
                        '(e.g. adsb) to see it listed here.</p>')
        else:
            for slug, title in entries:
                html.append(f'<a href="/tab/{slug}">{title}</a>')
        self._send(200, 'text/html; charset=utf-8', '\n'.join(html).encode())

    def _serve_api(self, slug: str):
        plugin = self._webserver._plugin_by_slug(slug)
        if plugin is None or not hasattr(plugin, 'web_json'):
            self.send_error(404)
            return
        try:
            data = plugin.web_json()
        except Exception as exc:
            self.send_error(500, f'plugin.web_json() raised: {exc!r}')
            return
        body = json.dumps(data, default=str).encode()
        self._send(200, 'application/json', body)

    def _serve_tab(self, slug: str):
        plugin = self._webserver._plugin_by_slug(slug)
        if plugin is None:
            self.send_error(404)
            return
        static_dir = self._webserver._plugin_static_dir(plugin)
        if static_dir is not None:
            index = os.path.join(static_dir, 'index.html')
            if os.path.isfile(index):
                with open(index, 'rb') as f:
                    self._send(200, 'text/html; charset=utf-8', f.read())
                return
        # Fallback: auto-generated JSON-dump shell so a plugin can ship a
        # useful web tab with zero HTML by just implementing web_json.
        title = getattr(plugin, 'web_title', slug)
        poll = int(getattr(plugin, 'web_poll_ms', 2000))
        html = (
            f'<!doctype html><meta charset=utf-8><title>{title}</title>'
            '<style>body{font:14px monospace;background:#111;color:#eee;padding:1em}'
            'pre{white-space:pre-wrap;line-height:1.4}h1{color:#8cf}</style>'
            f'<h1>{title}</h1><pre id="d">loading…</pre>'
            '<script>'
            'async function tick(){try{'
            f'const r=await fetch("/api/{slug}");'
            'document.getElementById("d").textContent=JSON.stringify(await r.json(),null,2);'
            '}catch(e){document.getElementById("d").textContent="error: "+e;}}'
            f'setInterval(tick,{poll});tick();'
            '</script>'
        )
        self._send(200, 'text/html; charset=utf-8', html.encode())

    def _serve_static(self, slug: str, subpath: str):
        plugin = self._webserver._plugin_by_slug(slug)
        if plugin is None:
            self.send_error(404)
            return
        static_dir = self._webserver._plugin_static_dir(plugin)
        if static_dir is None:
            self.send_error(404)
            return

        # Path-traversal defence: resolve fully then check it stays inside static_dir.
        base = os.path.realpath(static_dir)
        full = os.path.realpath(os.path.join(static_dir, subpath))
        if full != base and not full.startswith(base + os.sep):
            self.send_error(403)
            return

        if not os.path.isfile(full):
            self.send_error(404)
            return

        ext = os.path.splitext(full)[1].lower()
        mime = _MIME.get(ext, 'application/octet-stream')
        with open(full, 'rb') as f:
            self._send(200, mime, f.read())

    # ── helpers ──────────────────────────────────────────────────────────────

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')   # convenience for local tools
        self.end_headers()
        self.wfile.write(body)


# ── plugin class ─────────────────────────────────────────────────────────────

class WebServer(Decoder):
    name            = 'webserver'
    key             = 'w'
    key_help        = 'w=toggle'
    min_sample_rate = 0
    realtime        = False
    bg_queue_depth  = 1
    full_view       = False

    def __init__(self):
        self._registry: dict            = {}
        self._server                    = None       # ThreadingHTTPServer
        self._thread                    = None
        self._enabled                   = True
        self._host                      = _DEFAULT_HOST
        self._port                      = _DEFAULT_PORT

    # main.py calls this once after load_plugins() so we know our siblings.
    def wire(self, registry: dict) -> None:
        self._registry = registry

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        if self._enabled:
            self._start_server()

    def stop(self) -> None:
        self._stop_server()

    # ── HTTP server management ───────────────────────────────────────────────

    def _start_server(self) -> None:
        if self._server is not None:
            return
        _Handler._webserver = self
        try:
            self._server = ThreadingHTTPServer((self._host, self._port), _Handler)
        except OSError:
            # Port already in use — silently disable for this session.
            self._server = None
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name='sdrterm-web',
        )
        self._thread.start()

    def _stop_server(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None

    # ── DSP hook (no-op, but return status snapshot for the status bar) ──────

    def process(self, samples, state: AppState, results=None, sdr=None) -> dict:
        return {
            'enabled': self._enabled and self._server is not None,
            'url':     'http://{}:{}/'.format(self._host, self._port)
                       if self._server is not None else None,
            'plugins': [{'slug': s, 'title': t} for s, t in self._web_plugins()],
        }

    # ── keys ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('w'):
            self._enabled = not self._enabled
            if self._enabled:
                self._start_server()
            else:
                self._stop_server()
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if result and result.get('enabled') and result.get('url'):
            return '[web {}] '.format(result['url'])
        return '[web OFF] '

    def save_state(self) -> dict:
        return {'enabled': self._enabled, 'host': self._host, 'port': self._port}

    def load_state(self, d: dict) -> None:
        self._enabled = bool(d.get('enabled', True))
        self._host    = str(d.get('host', _DEFAULT_HOST))
        self._port    = int(d.get('port', _DEFAULT_PORT))

    # ── plugin discovery helpers (used by the request handler) ───────────────

    def _web_plugins(self):
        """List of (slug, title) tuples for plugins with a web contract."""
        out = []
        for name, p in self._registry.items():
            if p is self:
                continue
            if hasattr(p, 'web_json') and callable(getattr(p, 'web_json')):
                slug  = getattr(p, 'web_slug', name)
                title = getattr(p, 'web_title', name)
                out.append((slug, title))
        return sorted(out)

    def _plugin_by_slug(self, slug: str):
        for name, p in self._registry.items():
            if p is self:
                continue
            if hasattr(p, 'web_json') and getattr(p, 'web_slug', name) == slug:
                return p
        return None

    def _plugin_static_dir(self, plugin):
        """Absolute path of the plugin's static folder, or None."""
        rel = getattr(plugin, 'web_static_dir', None)
        if not rel:
            return None
        mod = inspect.getmodule(plugin)
        if mod is None or not getattr(mod, '__file__', None):
            return None
        base = pathlib.Path(mod.__file__).parent
        candidate = (base / rel).resolve()
        return str(candidate) if candidate.is_dir() else None
