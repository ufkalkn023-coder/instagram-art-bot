"""Loopback-only manual refresh of guarded, read-only dashboard snapshots."""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
CONFIG_MARKER = '<script id="dashboard-config" type="application/json">null</script>'
MAX_PAGE_BYTES = 16_000_000
MAX_REFRESH_BYTES = 64
MIN_REFRESH_SECONDS = 5


def dashboard_server(load_page, *, port: int = 38143, clock=time.monotonic) -> HTTPServer:
    def snapshot():
        page = load_page()
        if not isinstance(page, str) or len(page.encode('utf-8')) > MAX_PAGE_BYTES:
            raise ValueError('Invalid dashboard snapshot')
        return page

    cached = snapshot()
    token = secrets.token_urlsafe(32)
    last_attempt = None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            # Do not log request paths, session headers or credential diagnostics.
            pass

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, body):
            self._send(status, json.dumps(body, ensure_ascii=False).encode(),
                       'application/json; charset=utf-8')

        def _host_allowed(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                               f'localhost:{self.server.server_port}'}

        def do_GET(self):
            if not self._host_allowed():
                self._json(403, {'error': 'Yerel panel adresini kullanın.'})
                return
            if urlsplit(self.path).path not in {'/', '/index.html'}:
                self._json(404, {'error': 'Sayfa bulunamadı.'})
                return
            config = '<script id="dashboard-config" type="application/json">' + json.dumps({'token': token}) + '</script>'
            self._send(200, cached.replace(CONFIG_MARKER, config, 1).encode(),
                       'text/html; charset=utf-8')

        def do_POST(self):
            nonlocal cached, last_attempt
            if self.path != '/api/refresh':
                self._json(404, {'error': 'İşlem bulunamadı.'})
                return
            host = self.headers.get('Host', '')
            supplied = self.headers.get('X-Artfolio-Dashboard', '')
            if (not self._host_allowed() or self.headers.get('Origin') != f'http://{host}'
                    or not supplied.isascii() or not hmac.compare_digest(supplied, token)):
                self._json(403, {'error': 'Yerel paneli yeniden açın.'})
                return
            length = self.headers.get('Content-Length', '')
            if (self.headers.get('Transfer-Encoding')
                    or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json'
                    or not length.isdecimal() or not 1 <= int(length) <= MAX_REFRESH_BYTES):
                self._json(400, {'error': 'Geçersiz yenileme isteği.'})
                return
            try:
                if json.loads(self.rfile.read(int(length))) != {}:
                    raise ValueError('Expected empty object')
            except (ValueError, UnicodeError, OSError):
                self._json(400, {'error': 'Geçersiz yenileme isteği.'})
                return
            now = clock()
            if last_attempt is not None and now - last_attempt < MIN_REFRESH_SECONDS:
                self._json(429, {'error': 'Yeniden denemeden önce 5 saniye bekleyin.'})
                return
            last_attempt = now
            try:
                updated = snapshot()
            except Exception as error:
                logger.warning('dashboard_refresh_failed error_type=%s', type(error).__name__)
                self._json(503, {'error': 'Yeni veriler alınamadı. Son başarılı rapor korunuyor; yeniden deneyin.'})
                return
            cached = updated
            self._json(200, {'ok': True})

    return HTTPServer(('127.0.0.1', port), Handler)
