"""Runtime configuration loaded from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Process-wide settings; instantiated once at startup."""

    # Image tag of the sandbox container. Must be built locally or pulled from registry.
    runtime_image: str = os.environ.get("MAGI_RUNTIME_IMAGE", "hyperon-runtime:0.2.10")

    # Hard wall-clock timeout (seconds) for a single eval. Per plan I-5.
    default_timeout_s: float = float(os.environ.get("MAGI_DEFAULT_TIMEOUT_S", "5.0"))

    # Hard cap clients can request via timeout_ms (seconds). Clients can request less,
    # never more.
    max_timeout_s: float = float(os.environ.get("MAGI_MAX_TIMEOUT_S", "10.0"))

    # Memory cap passed to `docker run --memory`. Per plan I-5.
    memory_limit: str = os.environ.get("MAGI_MEMORY_LIMIT", "256m")

    # CPU cap passed to `docker run --cpus`.
    cpu_limit: str = os.environ.get("MAGI_CPU_LIMIT", "1.0")

    # Process-count cap passed to `docker run --pids-limit`.
    pids_limit: int = int(os.environ.get("MAGI_PIDS_LIMIT", "64"))

    # Path to the `docker` binary. Defaults to PATH lookup.
    docker_bin: str = os.environ.get("MAGI_DOCKER_BIN", "docker")

    # LRU result cache size (entries). Per plan A-5.
    result_cache_size: int = int(os.environ.get("MAGI_RESULT_CACHE_SIZE", "256"))

    # LRU result cache TTL (seconds).
    result_cache_ttl_s: int = int(os.environ.get("MAGI_RESULT_CACHE_TTL_S", "60"))


settings = Settings()
