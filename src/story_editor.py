"""Loopback-only story editor; immutable sources, guarded and revisioned writes."""

from __future__ import annotations

import hmac
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.story_project import (
    QualityBlocked,
    RevisionConflict,
    load_project,
    update_project,
)

from src.story_quality import ISSUE_MESSAGES

MAX_EDIT_BYTES = 128_000


def validate_editor_request(
    *,
    host: str,
    origin: str | None,
    token: str | None,
    expected_token: str,
    port: int,
    content_type: str,
    length: str | None,
) -> int:
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in allowed or origin != f"http://{host}":
        raise PermissionError("Open the editor from its local address")
    if (
        not token
        or not token.isascii()
        or not hmac.compare_digest(token, expected_token)
    ):
        raise PermissionError("Editor session expired; reload the page")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("Expected JSON")
    if length is None or not length.isdecimal():
        raise ValueError("Invalid content length")
    size = int(length)
    if not 1 <= size <= MAX_EDIT_BYTES:
        raise ValueError("Edit exceeds byte limit")
    return size


def editor_server(directory: Path, *, port: int = 38129) -> HTTPServer:
    directory = directory.resolve()
    load_project(directory)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict):
            self._send(
                status,
                json.dumps(data, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _host_allowed(self):
            actual_port = self.server.server_port
            return self.headers.get("Host") in {
                f"127.0.0.1:{actual_port}",
                f"localhost:{actual_port}",
            }

        def do_GET(self):
            if not self._host_allowed():
                self._json(403, {"error": "Invalid editor host"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                # Resolve once: a revision commit cannot mix a plan and gallery.
                snapshot = (directory / "current").resolve()
                page = (snapshot / "index.html").read_text()
                page = page.replace(
                    "window.ARTFOLIO_EDITOR=null;",
                    "window.ARTFOLIO_EDITOR=" + json.dumps({"token": token}) + ";",
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
                return
            if re.fullmatch(r"/pages/[a-z][a-z0-9_-]{0,63}-[0-9a-f]{20}\.jpg", path):
                asset = (directory / path.lstrip("/")).resolve()
                if asset.is_relative_to(directory / "pages") and asset.is_file():
                    self._send(200, asset.read_bytes(), "image/jpeg")
                    return
            self._json(404, {"error": "Page not found"})

        def do_POST(self):
            if self.path != "/api/plan":
                self._json(404, {"error": "Page not found"})
                return
            try:
                size = validate_editor_request(
                    host=self.headers.get("Host", ""),
                    origin=self.headers.get("Origin"),
                    token=self.headers.get("X-Artfolio-Editor"),
                    expected_token=token,
                    port=self.server.server_port,
                    content_type=self.headers.get("Content-Type", ""),
                    length=self.headers.get("Content-Length"),
                )
                data = json.loads(self.rfile.read(size))
                if (
                    not isinstance(data, dict)
                    or set(data) != {"expected_revision", "plan"}
                    or not isinstance(data["plan"], dict)
                ):
                    raise ValueError("Expected plan and revision")
                report = update_project(
                    directory, data["plan"], expected_revision=data["expected_revision"]
                )
                self._json(
                    200,
                    {
                        "revision": report["revision"],
                        "rendered_count": report["rendered_count"],
                        "reused_count": report["reused_count"],
                    },
                )
            except PermissionError as error:
                self._json(403, {"error": str(error)})
            except RevisionConflict:
                self._json(
                    409,
                    {"error": "Taslak başka bir işlemde değişti. Sayfayı yenileyin."},
                )
            except QualityBlocked as error:
                codes = ", ".join(
                    dict.fromkeys(
                        ISSUE_MESSAGES.get(i.code, "Taslağı kontrol edin")
                        for i in error.report.issues
                        if i.severity == "critical"
                    )
                )
                self._json(
                    422,
                    {
                        "error": "Kalite kontrolü: " + codes,
                        "quality": error.report.model_dump(mode="json"),
                    },
                )
            except ValidationError:
                self._json(
                    422,
                    {
                        "error": "Başlık, sayfa sırası veya detay alanı geçersiz. Son taslak korundu."
                    },
                )
            except (ValueError, UnicodeError):
                self._json(
                    400, {"error": "Geçersiz düzenleme isteği. Son taslak korundu."}
                )
            except OSError:
                self._json(
                    500,
                    {
                        "error": "Taslak diske kaydedilemedi. Son sürümü yeniden yükleyin."
                    },
                )

    return HTTPServer(("127.0.0.1", port), Handler)
