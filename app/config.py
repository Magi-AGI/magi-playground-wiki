"""Runtime configuration loaded from environment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The stale-container TTL must clear the longest possible eval window by at least
# this margin (seconds), so the sweep can never reap a container that is still
# legitimately running. Covers daemon create/teardown slop on top of max_timeout_s.
_STALE_TTL_SAFETY_MARGIN_S = 30


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

    # ── Concurrency / cleanup (2026-06-30 OOM incident hardening) ─────────────
    # App-level cap on simultaneous sandbox evals. Each container can use up to
    # `memory_limit`, so this bounds peak container memory at roughly
    # max_concurrent_evals * memory_limit. Sized conservatively for the ~3.7GiB,
    # no-swap EC2 host that also runs Decko/Ruby/Nginx. The 2026-06-30 outage saw
    # ~12 concurrent 256m containers (~3GiB) trigger a global OOM cascade.
    max_concurrent_evals: int = int(os.environ.get("MAGI_MAX_CONCURRENT_EVALS", "4"))

    # Best-effort stale-container sweep: containers labelled as owned by THIS
    # service and older than this TTL are force-removed. Backstop for leaks from
    # a crashed/restarted worker (where `docker run --rm` client-side cleanup
    # never fires). Must comfortably exceed max_timeout_s so in-flight evals are
    # never reaped mid-run.
    stale_container_ttl_s: int = int(os.environ.get("MAGI_STALE_CONTAINER_TTL_S", "300"))

    # How often the background stale-container sweep runs (seconds). A sweep also
    # runs once at startup to reclaim leaks from a previous process.
    cleanup_interval_s: int = int(os.environ.get("MAGI_CLEANUP_INTERVAL_S", "60"))

    # Wall-clock timeout for an individual cleanup docker call (`rm`/`ps`), so a
    # wedged docker daemon can't hang the cleanup path indefinitely.
    cleanup_op_timeout_s: float = float(os.environ.get("MAGI_CLEANUP_OP_TIMEOUT_S", "5.0"))

    def __post_init__(self) -> None:
        """Clamp env-supplied values that would otherwise be unsafe.

        Defaults are safe; this guards against bad operator overrides (zero/
        negative intervals, a stale TTL at/below the eval window that would let
        the sweep reap live containers, etc.). Frozen dataclass → assign via
        object.__setattr__.
        """
        if self.max_concurrent_evals < 1:
            log.warning(
                "MAGI_MAX_CONCURRENT_EVALS=%s is < 1; clamping to 1",
                self.max_concurrent_evals,
            )
            object.__setattr__(self, "max_concurrent_evals", 1)

        if self.cleanup_interval_s < 1:
            log.warning(
                "MAGI_CLEANUP_INTERVAL_S=%s is < 1; clamping to 1", self.cleanup_interval_s
            )
            object.__setattr__(self, "cleanup_interval_s", 1)

        if self.cleanup_op_timeout_s < 0.5:
            log.warning(
                "MAGI_CLEANUP_OP_TIMEOUT_S=%s is < 0.5; clamping to 0.5",
                self.cleanup_op_timeout_s,
            )
            object.__setattr__(self, "cleanup_op_timeout_s", 0.5)

        # Stale TTL must exceed the longest eval window plus a safety margin, or
        # the background sweep could force-remove a container that is still
        # running a legitimate eval.
        min_ttl = int(self.max_timeout_s) + _STALE_TTL_SAFETY_MARGIN_S
        if self.stale_container_ttl_s < min_ttl:
            log.warning(
                "MAGI_STALE_CONTAINER_TTL_S=%s is <= the eval window; raising to %s "
                "so the sweep can't reap live evals",
                self.stale_container_ttl_s,
                min_ttl,
            )
            object.__setattr__(self, "stale_container_ttl_s", min_ttl)


settings = Settings()
