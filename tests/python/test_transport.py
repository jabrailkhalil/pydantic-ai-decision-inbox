import http.client
import json
import threading
from pathlib import Path

import pytest

from pydantic_ai_decision_inbox.server import make_server


@pytest.fixture
def server(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Fixture</title>")
    httpd = make_server(static, tmp_path / "work", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "origin,ctype,path,body,expected",
    [
        (
            "https://example.invalid",
            "application/json",
            "/api/scenarios/happy",
            "{}",
            403,
        ),
        ("local", "text/plain", "/api/scenarios/happy", "{}", 403),
        ("local", "application/json", "/api/scenarios/arbitrary", "{}", 404),
        ("local", "application/json", "/api/scenarios/happy", '{"command":"bad"}', 400),
        ("local", "application/json", "/api/scenarios/happy", "{", 400),
    ],
)
def test_local_transport_rejects_cross_origin_or_arbitrary_requests(
    server,
    origin: str,
    ctype: str,
    path: str,
    body: str,
    expected: int,
) -> None:
    port = server.server_port
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request(
            "POST",
            path,
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{port}" if origin == "local" else origin,
                "Content-Type": ctype,
            },
        )
        response = client.getresponse()
        assert response.status == expected
        assert "error" in json.loads(response.read())
    finally:
        client.close()


def test_missing_static_build_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Build the browser UI"):
        make_server(tmp_path, tmp_path / "work", 0)
