"""Tests for the webserver plugin: discovery, routes, sandbox, ADS-B payload."""

import json
import socket
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from plugins.webserver.webserver import WebServer, _Handler


# ── fake plugin fixtures ─────────────────────────────────────────────────────

class _FakeDecoderNoWeb:
    """A plugin without any web contract — should be ignored by discovery."""
    name = 'nope'


class _FakeDecoderJsonOnly:
    """A plugin that exposes web_json but no static dir."""
    name       = 'json_only'
    web_slug   = 'json_only'
    web_title  = 'JSON-only tab'
    def web_json(self):
        return {'hello': 'world', 'n': 42}


class _FakeDecoderRaising:
    """A plugin whose web_json blows up — server must return 500, not crash."""
    name = 'boom'
    web_slug = 'boom'
    def web_json(self):
        raise RuntimeError('kaboom')


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """Start a real WebServer on a random free port, tear down after test."""
    ws = WebServer()
    ws._port = _free_port()
    yield ws
    ws.stop()


def _get(url, expect_status=200):
    try:
        resp = urllib.request.urlopen(url, timeout=2)
        assert resp.status == expect_status, \
            f'unexpected status {resp.status} for {url}'
        return resp.read(), resp.status, dict(resp.headers)
    except HTTPError as e:
        assert e.code == expect_status, \
            f'unexpected error status {e.code} (wanted {expect_status}) for {url}'
        return e.read(), e.code, dict(e.headers)


def _base(ws):
    return f'http://127.0.0.1:{ws._port}'


# ── discovery ────────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_ignores_plugins_without_web_contract(self, server):
        server.wire({'nope': _FakeDecoderNoWeb(), 'webserver': server})
        assert server._web_plugins() == []

    def test_picks_up_web_json_plugins(self, server):
        server.wire({'a': _FakeDecoderJsonOnly(), 'webserver': server})
        assert server._web_plugins() == [('json_only', 'JSON-only tab')]

    def test_never_includes_itself(self, server):
        server.wire({'webserver': server})
        assert server._web_plugins() == []


# ── routes ───────────────────────────────────────────────────────────────────

class TestRoutes:
    @pytest.fixture(autouse=True)
    def _wire(self, server):
        server.wire({'json_only': _FakeDecoderJsonOnly(), 'webserver': server})
        server.start(None)
        # Small settle for the daemon thread to bind
        time.sleep(0.05)
        self.s = server

    def test_index_lists_tabs(self):
        body, _, _ = _get(_base(self.s) + '/')
        html = body.decode()
        assert '/tab/json_only' in html
        assert 'JSON-only tab' in html

    def test_api_returns_plugin_snapshot(self):
        body, _, _ = _get(_base(self.s) + '/api/json_only')
        data = json.loads(body)
        assert data == {'hello': 'world', 'n': 42}

    def test_api_content_type_is_json(self):
        _, _, headers = _get(_base(self.s) + '/api/json_only')
        assert headers.get('Content-Type', '').startswith('application/json')

    def test_unknown_slug_returns_404(self):
        _, code, _ = _get(_base(self.s) + '/api/does-not-exist', expect_status=404)
        assert code == 404

    def test_tab_fallback_html_when_no_static_dir(self):
        body, _, _ = _get(_base(self.s) + '/tab/json_only')
        html = body.decode()
        # Fallback shell polls /api/json_only
        assert '/api/json_only' in html
        assert 'JSON-only tab' in html

    def test_plugin_raising_returns_500(self):
        # Replace registry to include the exploding plugin.
        self.s.wire({'boom': _FakeDecoderRaising(), 'webserver': self.s})
        _, code, _ = _get(_base(self.s) + '/api/boom', expect_status=500)
        assert code == 500


# ── ADS-B integration ────────────────────────────────────────────────────────

class TestAdsbIntegration:
    """Real ADS-B plugin exposes a valid web_json payload + serves its map."""

    def test_adsb_appears_in_discovery(self, server, tmp_path):
        from plugins.adsb.adsb import AdsbDecoder
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        server.wire({'adsb': adsb, 'webserver': server})
        assert server._web_plugins() == [('adsb', 'ADS-B live map')]

    def test_adsb_web_json_shape(self, tmp_path):
        from plugins.adsb.adsb import AdsbDecoder
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        payload = adsb.web_json()
        assert set(payload.keys()) == {'aircraft', 'n_bursts', 'n_crc_ok',
                                       'logging', 'window',
                                       'receiver', 'max_range_km', 'farthest'}
        assert isinstance(payload['aircraft'], list)
        assert payload['n_bursts'] == 0
        assert payload['logging'] in (True, False)
        assert 'from' in payload['window']

    def test_adsb_map_html_is_served(self, server, tmp_path):
        from plugins.adsb.adsb import AdsbDecoder
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        server.wire({'adsb': adsb, 'webserver': server})
        server.start(None)
        time.sleep(0.05)
        body, _, _ = _get(_base(server) + '/tab/adsb')
        html = body.decode()
        # 3D globe renderer (CesiumJS) with the standard base-URL trick.
        assert 'cesium' in html.lower()
        assert 'CESIUM_BASE_URL' in html
        assert '/api/adsb' in html
        # Enrichment wiring: adsbdb + attribution + localStorage cache
        assert 'adsbdb.com' in html
        assert '/v0/aircraft/' in html
        assert '/v0/callsign/' in html
        assert 'localStorage' in html
        # Dual-unit display + camera fit + selection restore.
        assert 'km/h' in html
        assert 'm/s'  in html
        assert 'flyTo' in html
        assert 'adsb:selected' in html
        # Time-window selector + manual refresh (no auto-reload / no polling)
        assert 'window-select' in html
        assert 'refresh-btn'   in html
        assert 'last 30 minutes' in html
        assert 'custom range'   in html
        # No polling / auto-reload lingers in the page
        assert 'setInterval' not in html
        assert 'location.reload' not in html
        # Receiver + farthest-signal wiring
        assert 'drawReceiver' in html
        assert 'farthest'     in html
        # Plane silhouette billboard (not just a Cesium point)
        assert 'PLANE_SVG'    in html
        assert 'billboard'    in html

    def test_adsb_static_dir_resolves(self, server, tmp_path):
        from plugins.adsb.adsb import AdsbDecoder
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        static = server._plugin_static_dir(adsb)
        assert static is not None
        assert static.endswith('/plugins/adsb/web')

    def test_time_window_query_reads_csv_log(self, server, tmp_path):
        """End-to-end: /api/adsb?from=&to= filters aircraft from the CSV log."""
        from plugins.adsb.adsb import AdsbDecoder
        # Seed a small log the plugin can read
        (tmp_path / 'adsb.csv').write_text(
            'timestamp,icao,callsign,lat,lon,alt,gs,ias,tas,heading,vr\n'
            '2026-08-09T10:00:00.000Z,4CA1F2,RYR1,51.5,-0.1,35000,480,,,90,0\n'
            '2026-08-09T14:00:00.000Z,4CA1F2,RYR1,51.6,-0.1,35000,480,,,91,0\n'
        )
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        server.wire({'adsb': adsb, 'webserver': server})
        server.start(None)
        time.sleep(0.05)

        # Window that only covers the 14:00 row
        body, _, _ = _get(_base(server) +
            '/api/adsb?from=2026-08-09T13:00:00.000Z&to=2026-08-09T15:00:00.000Z')
        data = json.loads(body)
        assert data['window']['from'] == '2026-08-09T13:00:00.000Z'
        assert data['window']['to']   == '2026-08-09T15:00:00.000Z'
        assert len(data['aircraft']) == 1
        ac = data['aircraft'][0]
        assert ac['icao']     == '4CA1F2'
        assert ac['callsign'] == 'RYR1'
        assert len(ac['track']) == 1                    # only the 14:00 row is in-window
        assert ac['track'][0]['lat'] == 51.6


# ── security ─────────────────────────────────────────────────────────────────

class TestPathTraversalDefence:
    def test_dotdot_is_rejected(self, server, tmp_path):
        from plugins.adsb.adsb import AdsbDecoder
        adsb = AdsbDecoder()
        adsb._log_dir = str(tmp_path)
        server.wire({'adsb': adsb, 'webserver': server})
        server.start(None)
        time.sleep(0.05)
        # Try to escape out of plugins/adsb/web to read something outside.
        _, code, _ = _get(_base(server) + '/static/adsb/../../../etc/passwd',
                          expect_status=403)
        assert code in (403, 404)   # some servers normalize before dispatch


# ── lifecycle ────────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_toggle_off_stops_server(self, server):
        server.wire({'webserver': server})
        server.start(None)
        time.sleep(0.05)
        assert server._server is not None
        server.handle_key(ord('w'), None, None)
        assert server._server is None

    def test_toggle_on_starts_server(self, server):
        server.wire({'webserver': server})
        server._enabled = False
        server.start(None)     # noop because disabled
        assert server._server is None
        server.handle_key(ord('w'), None, None)
        assert server._server is not None

    def test_save_load_state(self, server):
        server._enabled = False
        server._port = 9999
        server._host = '0.0.0.0'
        d = server.save_state()
        assert d == {'enabled': False, 'host': '0.0.0.0', 'port': 9999}

        server2 = WebServer()
        server2.load_state(d)
        assert server2._enabled is False
        assert server2._port == 9999
        assert server2._host == '0.0.0.0'
