"""FastAPI entrypoint for the MeTTa Playground sidecar.

Exposes:
    POST /api/playground/run      — evaluate MeTTa source in a sandboxed container
    GET  /api/playground/health   — readiness/liveness
    GET  /metrics                 — Prometheus metrics
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .config import settings
from .models import HealthResponse, RunRequest, RunResponse
from .runner import run_metta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


_started_at = time.monotonic()

# Prometheus counters (plan A-10).
metta_eval_total = Counter(
    "metta_eval_total",
    "MeTTa evaluation outcomes",
    labelnames=("status",),
)
metta_eval_duration_seconds = Histogram(
    "metta_eval_duration_seconds",
    "MeTTa evaluation wall-clock duration",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "magi-playground-wiki starting (image=%s, default_timeout=%.1fs, memory=%s)",
        settings.runtime_image,
        settings.default_timeout_s,
        settings.memory_limit,
    )
    yield
    log.info("magi-playground-wiki shutting down")


app = FastAPI(
    title="magi-playground-wiki",
    description="MeTTa Playground sidecar for the Hyperon Wiki right column",
    version="0.1.0",
    lifespan=lifespan,
)


@app.post("/api/playground/run", response_model=RunResponse)
async def post_run(req: RunRequest) -> RunResponse:
    timeout_s = (req.timeout_ms / 1000.0) if req.timeout_ms is not None else None
    try:
        result = await run_metta(req.code, timeout_s=timeout_s)
    except FileNotFoundError as exc:
        # docker binary not on PATH — bubble up as 503 so the frontend stub fallback kicks in.
        log.error("docker binary not found: %s", exc)
        raise HTTPException(status_code=503, detail="sandbox runtime unavailable") from exc

    metta_eval_total.labels(status=result.status).inc()
    metta_eval_duration_seconds.observe(result.elapsed_ms / 1000.0)

    return RunResponse(
        status=result.status,  # type: ignore[arg-type]
        output=result.output,
        stdout=result.stdout,
        stderr=result.stderr,
        elapsed_ms=result.elapsed_ms,
    )


@app.get("/api/playground/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    uptime_s = int(time.monotonic() - _started_at)
    return HealthResponse(
        ok=True,
        hyperon_version="0.2.10",
        uptime_s=uptime_s,
        runtime_image=settings.runtime_image,
    )


@app.get("/metrics", response_class=PlainTextResponse)
async def get_metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
