"""Loopback-only developer lab. This is not an authentication/approval server."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer
from urllib.parse import urlsplit

from .lab import SCENARIOS, run_scenario


def make_server(
    directory: Path, workspace: Path, port: int = 8000
) -> ThreadingHTTPServer:
    root = directory.resolve()
    workspace = workspace.resolve()
    if not (root / "index.html").is_file():
        raise ValueError("Build the browser UI first (npm run build)")
    workspace.mkdir(parents=True, exist_ok=True)
    gate = threading.BoundedSemaphore(1)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(
            self,
            request: socket.socket,
            client_address: tuple[str, int],
            server: BaseServer,
        ) -> None:
            super().__init__(request, client_address, server, directory=str(root))

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: object) -> None:
            pass

        def json_response(self, status: int, value: object) -> None:
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if (
                not Path(self.translate_path(urlsplit(self.path).path))
                .resolve()
                .is_relative_to(root)
            ):
                self.send_error(403)
                return
            super().do_GET()

        def do_POST(self) -> None:
            host = f"127.0.0.1:{httpd.server_port}"
            if (
                self.headers.get("Host") != host
                or self.headers.get("Origin") != "http://" + host
                or self.headers.get("Content-Type") != "application/json"
                or self.headers.get("Transfer-Encoding") is not None
            ):
                self.json_response(
                    403, {"error": "Only same-origin local lab requests are allowed"}
                )
                return
            path = urlsplit(self.path).path
            prefix = "/api/scenarios/"
            identifier = path.removeprefix(prefix)
            if not path.startswith(prefix) or identifier not in SCENARIOS:
                self.json_response(404, {"error": "Unknown scenario"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 128:
                    raise ValueError("Invalid length")
                if json.loads(self.rfile.read(length)) != {}:
                    raise ValueError("Scenario parameters are fixed fixtures")
            except (ValueError, UnicodeError):
                self.json_response(400, {"error": "Expected a small empty JSON object"})
                return
            if not gate.acquire(blocking=False):
                self.json_response(
                    409, {"error": "A local scenario is already running"}
                )
                return
            try:
                with tempfile.TemporaryDirectory(
                    prefix=identifier + "-", dir=workspace
                ) as tmp:
                    work = Path(tmp).resolve()
                    if not work.is_relative_to(workspace):
                        raise RuntimeError("Unexpected temporary path")
                    report = run_scenario(identifier, work)
                    self.json_response(200, report.model_dump(mode="json"))
            except Exception:
                self.json_response(
                    500,
                    {"error": "Local scenario failed; rerun the CLI for diagnostics"},
                )
            finally:
                gate.release()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd
