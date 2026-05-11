"""Docker-exec wrapper around the hyperon-runtime image.

Per plan A-2 / A-7: each invocation spawns a fresh container. No pooling
or instance reuse in V1. The container is locked down with seccomp/no-net/
read-only-rootfs/memory-cap/pid-limit/cap-drop/no-new-privileges so the
worst a malicious MeTTa program can do is consume its own CPU/memory budget
until killed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Final

from .config import settings

log = logging.getLogger(__name__)

# Sentinel separator between captured-output JSON envelope and any stray container output.
# entrypoint.py writes its envelope on sys.__stdout__ in a single json.dumps call,
# so we can parse the LAST line of stdout as JSON.

DEFAULT_DOCKER_ARGS: Final[tuple[str, ...]] = (
    "run",
    "--rm",
    "--interactive",          # stdin → container
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
)


@dataclass(slots=True)
class RunResult:
    """Outcome of a single eval invocation."""

    status: str  # "ok" | "error" | "timeout"
    output: str
    stdout: str
    stderr: str
    elapsed_ms: int


def _build_docker_command(timeout_s: float) -> list[str]:
    """Build the docker-run argv for a single eval invocation."""
    args: list[str] = [settings.docker_bin]
    args.extend(DEFAULT_DOCKER_ARGS)
    # Resource caps.
    args.extend(["--memory", settings.memory_limit])
    args.extend(["--memory-swap", settings.memory_limit])  # disable swap by matching memory
    args.extend(["--cpus", settings.cpu_limit])
    args.extend(["--pids-limit", str(settings.pids_limit)])
    # Writable tmpfs at /tmp (size 64m, mounted noexec).
    args.extend(["--tmpfs", "/tmp:size=64m,mode=1777,noexec,nosuid,nodev"])
    # SIGTERM grace period — give the container 1s after the wall clock before SIGKILL.
    # `--stop-timeout` is a soft hint; the asyncio-side timeout enforces the hard kill.
    args.extend(["--stop-timeout", str(max(1, int(timeout_s) + 1))])
    # The image.
    args.append(settings.runtime_image)
    return args


async def run_metta(code: str, timeout_s: float | None = None) -> RunResult:
    """Spawn a one-shot container, feed `code` on stdin, parse the JSON envelope."""

    effective_timeout_s = min(
        timeout_s if timeout_s is not None else settings.default_timeout_s,
        settings.max_timeout_s,
    )

    argv = _build_docker_command(effective_timeout_s)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(code.encode("utf-8")),
            timeout=effective_timeout_s,
        )
    except asyncio.TimeoutError:
        # Hard kill — proc.kill() sends SIGKILL to the docker-run client, which
        # propagates to the container engine and the container is force-stopped.
        proc.kill()
        # Drain to release the pipes; ignore output.
        with _suppress_async_errors():
            await proc.communicate()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return RunResult(
            status="timeout",
            output="",
            stdout="",
            stderr=f"Evaluation exceeded {effective_timeout_s:.1f}s and was terminated.",
            elapsed_ms=elapsed_ms,
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr_raw = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        # Container exited non-zero (sandbox kill, OOM, docker-run failure).
        # Distinguish OOM from generic error if we can tell from stderr/exit code.
        oom = "killed" in stderr_raw.lower() or proc.returncode == 137
        return RunResult(
            status="error",
            output="",
            stdout="",
            stderr=(
                "Container terminated (likely memory limit reached)."
                if oom
                else f"Container exited with code {proc.returncode}.\n{stderr_raw}".strip()
            ),
            elapsed_ms=elapsed_ms,
        )

    # entrypoint.py emits a single JSON line on stdout. Be tolerant of trailing
    # whitespace or other lines (shouldn't happen, but defensive).
    envelope = _parse_envelope(stdout)
    if envelope is None:
        return RunResult(
            status="error",
            output="",
            stdout=stdout,
            stderr=f"Unable to parse runner envelope.\nstderr: {stderr_raw}".strip(),
            elapsed_ms=elapsed_ms,
        )

    status = "error" if envelope.get("stderr") else "ok"
    return RunResult(
        status=status,
        output=envelope.get("output", ""),
        stdout=envelope.get("stdout", ""),
        stderr=envelope.get("stderr", ""),
        elapsed_ms=elapsed_ms,
    )


def _parse_envelope(stdout: str) -> dict | None:
    """Extract the JSON envelope emitted by runtime/entrypoint.py.

    Tries the trailing line first (the common case), falls back to whole-buffer parse.
    """
    stripped = stdout.strip()
    if not stripped:
        return None
    # Try last line first.
    last_line = stripped.rsplit("\n", 1)[-1]
    for candidate in (last_line, stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class _suppress_async_errors:
    """async-friendly suppress for use in `with` blocks within async functions."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True
