"""Live mock providers (tokenbench's mockserver) and a live gateway, all uvicorn threads."""

import contextlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from tokenbench.mockserver import MockConfig
from tokenbench.mockserver import create_app as create_mock


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def live(app):
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            if httpx.get(f"{url}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.02)
    try:
        yield url
    finally:
        server.should_exit = True
        th.join(timeout=5)


@pytest.fixture
def providers():
    """healthy, slow (TTFT 400 ms), flaky (50% 500s)."""
    with live(create_mock(MockConfig(model="m", ttft_ms=5, itl_ms=1))) as healthy, \
         live(create_mock(MockConfig(model="m", ttft_ms=400, itl_ms=1))) as slow, \
         live(create_mock(MockConfig(model="m", ttft_ms=5, itl_ms=1, error_rate=0.5, seed=1))) as flaky:
        yield {"healthy": healthy, "slow": slow, "flaky": flaky}


@pytest.fixture
def live_gateway():
    return live
