"""Docker-exec wrapper around the hyperon-runtime image.

Per plan A-2 / A-7: each invocation spawns a fresh container. No pooling
or instance reuse in V1. The container is locked down with seccomp/no-net/
read-only-rootfs/memory-cap/pid-limit/cap-drop/no-new-privileges so the
worst a malicious MeTTa program can do is consume its own CPU/memory budget
until killed.

Lifecycle / cleanup (hardened after the 2026-06-30 OOM incident):
    * Every container gets a unique ``--name`` and ownership ``--label``s.
    * ``docker run --rm`` handles the happy path, but ``--rm`` cleanup is done
      by the *client* process — if we SIGKILL the client on timeout (or the
      worker crashes) the container can be ORPHANED and keep its 256m. So
      timeout/cancel/error paths explicitly ``docker rm -f <name>`` the
      underlying container rather than trusting ``--rm``.
    * A background sweep (`cleanup_stale_containers`) force-removes any
      service-labelled container older than a short TTL, as a backstop for
      leaks from a crashed worker.
    * An ``asyncio.Semaphore`` caps how many evals run at once so a burst of
      requests can't exhaust host RAM (the incident root cause).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Final

from .config import settings

log = logging.getLogger(__name__)

# Sentinel separator between captured-output JSON envelope and any stray container output.
# entrypoint.py writes its envelope on sys.__stdout__ in a single json.dumps call,
# so we can parse the LAST line of stdout as JSON.

# Ownership labels. Cleanup ONLY ever touches containers carrying these labels,
# so the sweep can never reap a container this service did not create.
LABEL_MANAGED: Final[str] = "magi-playground.managed"
LABEL_CREATED: Final[str] = "magi-playground.created"  # int epoch seconds, set at run
CONTAINER_NAME_PREFIX: Final[str] = "magi-pg-"

# `docker rm -f` on the timeout/cancel path may race the daemon still creating
# the container: bound how many times we retry a "No such container" before
# concluding it never materialised. Module-level so tests can shrink the delay.
_RM_RETRY_ATTEMPTS: Final[int] = 4
_RM_RETRY_DELAY_S: float = 0.25

DEFAULT_DOCKER_ARGS: Final[tuple[str, ...]] = (
    "run",
    "--rm",
    "--interactive",          # stdin → container
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
)

# Lazily-constructed concurrency gate. Built on first use so it binds to the
# running event loop (and so `settings` is read at call time, not import time).
_eval_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Return the process-wide eval concurrency gate, creating it on first use."""
    global _eval_semaphore
    if _eval_semaphore is None:
        _eval_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_evals))
    return _eval_semaphore


@dataclass(slots=True)
class RunResult:
    """Outcome of a single eval invocation."""

    status: str  # "ok" | "error" | "timeout"
    output: str
    stdout: str
    stderr: str
    elapsed_ms: int


def _container_name() -> str:
    """A unique, service-prefixed container name (also used for `docker rm -f`)."""
    return f"{CONTAINER_NAME_PREFIX}{uuid.uuid4().hex}"


def _build_docker_command(name: str, timeout_s: float) -> list[str]:
    """Build the docker-run argv for a single eval invocation."""
    args: list[str] = [settings.docker_bin]
    args.extend(DEFAULT_DOCKER_ARGS)
    # Identity: unique name (for targeted removal) + ownership labels (for the
    # stale sweep filter). The created-epoch label lets the sweep compute age
    # without parsing docker's locale/timezone-dependent CreatedAt string.
    args.extend(["--name", name])
    args.extend(["--label", f"{LABEL_MANAGED}=true"])
    args.extend(["--label", f"{LABEL_CREATED}={int(time.time())}"])
    # Resource caps.
    args.extend(["--memory", settings.memory_limit])
    args.extend(["--memory-swap", settings.memory_limit])  # disable swap by matching memory
    args.extend(["--cpus", settings.cpu_limit])
    args.extend(["--pids-limit", str(settings.pids_limit)])
    # Writable tmpfs at /tmp (size 64m, mounted noexec).
    args.extend(["--tmpfs", "/tmp:size=64m,mode=1777,noexec,nosuid,nodev"])
    # Hyperon writes a config dir under $HOME at startup (environment.rs:289 reads/creates
    # ~/.metta). With --read-only rootfs we need a separate writable tmpfs for HOME.
    # 16m is plenty for the runner's small bookkeeping files.
    args.extend(["--tmpfs", "/home/metta:size=16m,mode=0700,uid=1001,gid=1001,nosuid,nodev"])
    # SIGTERM grace period — give the container 1s after the wall clock before SIGKILL.
    # `--stop-timeout` is a soft hint; the asyncio-side timeout enforces the hard kill.
    args.extend(["--stop-timeout", str(max(1, int(timeout_s) + 1))])
    # The image.
    args.append(settings.runtime_image)
    return args


async def run_metta(code: str, timeout_s: float | None = None) -> RunResult:
    """Spawn a one-shot container, feed `code` on stdin, parse the JSON envelope.

    Concurrency is bounded by `MAGI_MAX_CONCURRENT_EVALS`; excess requests queue
    on the semaphore rather than over-committing host memory.
    """

    effective_timeout_s = min(
        timeout_s if timeout_s is not None else settings.default_timeout_s,
        settings.max_timeout_s,
    )

    async with _get_semaphore():
        # Build the container identity AFTER admission through the semaphore, not
        # before. If we stamped LABEL_CREATED at queue-entry time, a request that
        # waited behind the gate for longer than the stale TTL would launch a
        # brand-new container already carrying an "old" created label — and the
        # background sweep could reap a live eval. Stamping here means the label
        # reflects actual container start time.
        name = _container_name()
        argv = _build_docker_command(name, effective_timeout_s)
        started = time.monotonic()

        # `proc` must be None-initialised BEFORE the spawn await: if cancellation
        # arrives *during* create_subprocess_exec, the Docker daemon may already
        # have created the named container even though Python never received the
        # `proc` handle. The spawn is therefore inside the try so that path still
        # force-removes the container by name (live smoke 006 leaked a `Created`
        # container exactly here).
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(code.encode("utf-8")),
                timeout=effective_timeout_s,
            )
        except asyncio.TimeoutError:
            # Wall-clock exceeded (only reachable from wait_for, so `proc` exists).
            # Killing the `docker run` CLIENT does not reliably stop the container,
            # and with the client dead `--rm` never fires — so explicitly
            # force-remove the named container. Shielded so a second cancellation
            # mid-cleanup can't orphan the container.
            await _cleanup_shielded(_terminate_container(proc, name))
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return RunResult(
                status="timeout",
                output="",
                stdout="",
                stderr=f"Evaluation exceeded {effective_timeout_s:.1f}s and was terminated.",
                elapsed_ms=elapsed_ms,
            )
        except BaseException:
            # Request cancelled (client disconnect → CancelledError) or any other
            # failure. Clean up before propagating so we never leak a container,
            # shielded so cleanup completes even if another cancellation arrives.
            if proc is not None:
                # Client handle exists: kill it AND force-remove the container.
                await _cleanup_shielded(_terminate_container(proc, name))
            else:
                # Cancelled/failed inside create_subprocess_exec — no client handle,
                # but the daemon may have created the container. Reap it by name
                # (with not-found retry, since it may still be materialising).
                await _cleanup_shielded(
                    _force_remove_container(name, retry_not_found=True)
                )
            raise

    elapsed_ms = int((time.monotonic() - started) * 1000)

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr_raw = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        # Container exited non-zero (sandbox kill, OOM, docker-run failure). The
        # client exited normally here, so `--rm` already removed the container.
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


async def _cleanup_shielded(coro) -> None:
    """Run `coro` (a cleanup coroutine) to completion even under cancellation.

    Cleanup must not be interrupted by a *second* cancellation (e.g. server
    shutdown racing a client disconnect) — that would orphan the very container
    we are trying to remove. We shield the cleanup task and keep waiting for it
    across any cancellations of this caller, re-raising CancelledError only once
    the cleanup has finished.
    """
    task = asyncio.ensure_future(coro)
    caller_cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            caller_cancelled = True
            if task.done():
                break
            # Shielded cleanup is still running — absorb this cancellation and
            # keep waiting for it to finish.
        except Exception:  # noqa: BLE001 — cleanup is best-effort; it shouldn't raise
            break
    if caller_cancelled:
        raise asyncio.CancelledError


async def _terminate_container(proc: asyncio.subprocess.Process, name: str) -> None:
    """Kill the docker-run client AND force-remove the underlying container.

    Called on the timeout / cancellation / error paths. `proc.kill()` alone only
    targets the CLI client; `docker rm -f <name>` is what actually reclaims the
    container's memory. The removal retries on "No such container" because the
    daemon may still be CREATING the named container when we kill the client —
    a single early not-found is not proof the container will never exist.
    """
    with _suppress_async_errors():
        proc.kill()
    # Drain the client's pipes so it can exit and we don't leak fds. Bounded so a
    # wedged client can't hang us; the force-remove below is the real cleanup.
    with _suppress_async_errors():
        await asyncio.wait_for(proc.communicate(), timeout=settings.cleanup_op_timeout_s)
    await _force_remove_container(name, retry_not_found=True)


def _is_not_found(stderr: str) -> bool:
    """True if a docker stderr indicates the target container does not exist."""
    return "no such container" in stderr.lower()


async def _force_remove_container(name: str, *, retry_not_found: bool = False) -> None:
    """Best-effort `docker rm -f <name>`. Never raises.

    `retry_not_found` is for the timeout/cancel path, where the container may not
    have materialised yet when we issue the removal: we retry a bounded number of
    times so that a container which appears slightly later still gets reaped. The
    stale sweep (which already listed the container via `docker ps`) leaves it
    False — a not-found there just means a concurrent removal won the race.

    A non-zero exit that is NOT a benign "No such container" is logged as a real
    failure rather than silently swallowed.
    """
    attempts = _RM_RETRY_ATTEMPTS if retry_not_found else 1
    for attempt in range(attempts):
        try:
            rm = await asyncio.create_subprocess_exec(
                settings.docker_bin,
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err_bytes = await asyncio.wait_for(
                rm.communicate(), timeout=settings.cleanup_op_timeout_s
            )
        except FileNotFoundError:
            # docker binary not on PATH — nothing we can do, nothing to leak locally.
            log.warning("docker binary not found; cannot remove container %s", name)
            return
        except Exception as exc:  # noqa: BLE001 — cleanup must never raise into callers
            log.warning("force-remove of container %s failed: %s", name, exc)
            return

        err = err_bytes.decode("utf-8", errors="replace").strip()
        rc = rm.returncode
        if rc == 0:
            return  # removed
        if rc == 1 and _is_not_found(err):
            # Container absent. On the terminate path it may simply not have been
            # created yet — retry briefly in case the daemon is mid-create.
            if retry_not_found and attempt + 1 < attempts:
                await asyncio.sleep(_RM_RETRY_DELAY_S)
                continue
            return  # benign: already gone, or never materialised
        # Any other non-zero exit (incl. rc==1 that is NOT "No such container",
        # e.g. a daemon/permission error) is a genuine failure worth surfacing.
        log.warning("docker rm -f %s exited %s: %s", name, rc, err)
        return
    # Exhausted retries and the container never appeared — benign.
    log.debug("container %s not found after %d removal attempt(s)", name, attempts)


async def cleanup_stale_containers(ttl_s: float | None = None) -> int:
    """Force-remove service-owned containers older than `ttl_s`. Returns the count.

    Safety: the `docker ps` query filters on this service's ownership label, so
    only containers THIS service created are ever candidates; each candidate is
    additionally gated on age >= TTL. Best-effort — never raises.
    """
    ttl = ttl_s if ttl_s is not None else settings.stale_container_ttl_s
    fmt = '{{.Names}}\t{{.Label "' + LABEL_CREATED + '"}}'
    try:
        ps = await asyncio.create_subprocess_exec(
            settings.docker_bin,
            "ps",
            "-a",
            "--filter",
            f"label={LABEL_MANAGED}=true",
            "--format",
            fmt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            ps.communicate(), timeout=settings.cleanup_op_timeout_s
        )
    except FileNotFoundError:
        # docker not installed (e.g. local dev / CI) — nothing to sweep.
        return 0
    except Exception as exc:  # noqa: BLE001 — sweep must never raise into the loop
        log.warning("stale-container sweep: docker ps failed: %s", exc)
        return 0

    if ps.returncode != 0:
        log.warning(
            "stale-container sweep: docker ps exited %s: %s",
            ps.returncode,
            err.decode("utf-8", errors="replace").strip(),
        )
        return 0

    now = time.time()
    removed = 0
    for line in out.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, created_raw = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        try:
            created = int(created_raw.strip())
        except ValueError:
            # Managed container with no/garbage created label: anomalous. Skip
            # rather than risk removing something mid-creation; the worst case is
            # one leaked container, which the next sweep with a valid label catches.
            log.debug("stale-container sweep: skipping %s with unparseable age", name)
            continue
        if now - created >= ttl:
            await _force_remove_container(name)
            removed += 1

    if removed:
        log.info(
            "stale-container sweep removed %d container(s) older than %ss", removed, ttl
        )
    return removed


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
