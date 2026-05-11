"""Pydantic request/response models for the /api/playground/* endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """POST /api/playground/run request body."""

    code: str = Field(..., min_length=1, max_length=64 * 1024, description="MeTTa source")
    timeout_ms: int | None = Field(
        None, ge=100, description="Optional per-request timeout override, capped by MAGI_MAX_TIMEOUT_S."
    )


class RunResponse(BaseModel):
    """POST /api/playground/run response body."""

    status: Literal["ok", "error", "timeout"]
    output: str = Field("", description="Newline-separated `!` query results")
    stdout: str = Field("", description="Captured program stdout (prints, traces)")
    stderr: str = Field("", description="Exception text or runtime errors")
    elapsed_ms: int = Field(..., ge=0)


class HealthResponse(BaseModel):
    """GET /api/playground/health response body."""

    ok: bool
    hyperon_version: str
    uptime_s: int
    runtime_image: str
