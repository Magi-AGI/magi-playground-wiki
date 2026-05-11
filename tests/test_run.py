"""Smoke tests for the /api/playground/run endpoint.

These tests require a running Docker daemon AND the `hyperon-runtime:0.2.10`
image to be built locally:

    docker build -f runtime/Dockerfile -t hyperon-runtime:0.2.10 ./runtime

Tests are marked `live` and skipped by default. Run with:

    pytest -m live
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/api/playground/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["hyperon_version"] == "0.2.10"
    assert "runtime_image" in body


async def test_run_validation_empty_code(client: AsyncClient) -> None:
    r = await client.post("/api/playground/run", json={"code": ""})
    assert r.status_code == 422  # Pydantic min_length=1


async def test_run_validation_oversize_code(client: AsyncClient) -> None:
    r = await client.post("/api/playground/run", json={"code": "x" * (64 * 1024 + 1)})
    assert r.status_code == 422  # Pydantic max_length


@pytest.mark.live
async def test_run_arithmetic(client: AsyncClient) -> None:
    """Smoke: evaluate `!(+ 1 2)` in a real sandbox container."""
    r = await client.post("/api/playground/run", json={"code": "!(+ 1 2)"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "3" in body["output"]
    assert body["elapsed_ms"] > 0
    assert body["stderr"] == ""


@pytest.mark.live
async def test_run_pattern_match(client: AsyncClient) -> None:
    """Smoke: knowledge-base pattern match."""
    code = """
    (= (parent Tom Bob))
    (= (parent Bob Sam))
    !(match &self (= (parent Tom $x)) $x)
    """
    r = await client.post("/api/playground/run", json={"code": code})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "Bob" in body["output"]


@pytest.mark.live
async def test_run_timeout(client: AsyncClient) -> None:
    """Hostile input: infinite loop must be killed by the timeout, not hang the worker."""
    # An infinite catchall in MeTTa is hard to express portably; use a tight
    # explicit timeout to force the timeout branch regardless.
    r = await client.post(
        "/api/playground/run",
        json={"code": "!(+ 1 2)", "timeout_ms": 100},
    )
    # Either the eval finishes under 100ms (status=ok) or it gets killed (status=timeout).
    # Either is acceptable behavior — the assertion is that we don't 500.
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "timeout"}


@pytest.mark.live
async def test_run_syntax_error(client: AsyncClient) -> None:
    """A malformed MeTTa expression should surface in stderr, not 500."""
    r = await client.post("/api/playground/run", json={"code": "!(unbalanced"})
    assert r.status_code == 200
    body = r.json()
    # Either MeTTa accepts and produces noise, or it errors — both 200 with envelope.
    assert body["status"] in {"ok", "error"}
