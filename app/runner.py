"""Docker-exec wrapper around the hyperon-runtime image.

Per plan A-2 / A-7: each invocation spawns a fresh container. No pooling
or instance reuse in V1. The container is locked down with seccomp/no-net/
read-only-rootfs/memory-cap/pid-limit/cap-drop/no-new-privileges so the
worst a malicious MeTTa program can do is consume its own CPU/memory budget
until killed.

Lifecycle / cleanup (hardened after the 2026-06-30 OOM incident):
    * We use an EXPLICIT ``create`` → ``start --attach`` → ``rm -f`` lifecycle
      rather than ``docker run --rm``. The lesson from repeated live-cancellation
      leaks (hotfixes 006/008/010) is that ``docker run`` couples container
      creation to the client's lifecycle: cancel/kill the client and the daemon
      can still create+leave an orphaned ``Created`` container that name-based
      retry cleanup races and misses.
    * ``docker create`` establishes the container up front and returns only once
      it definitively exists (by our unique ``--name``). From that point every
      exit path — success, nonzero exit, parse error, timeout, cancellation — is
      guaranteed to reach a ``docker rm -f <name>`` in a ``finally``. Because the
      container already exists, removal by name is deterministic, not a heuristic.
    * ``docker start --attach --interactive`` runs the user code, feeding stdin
      and capturing stdout (the JSON envelope) / stderr exactly as before.
    * Cancellation *during* ``docker create`` is the only remaining create race;
      it is shielded and, if it leaves a container, cleaned up by name.
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

# `docker rm -f` may race the daemon still finishing a `docker create` on the
# cancellation path: bound how many times we retry a "No such container" before
# concluding it never materialised. Module-level so tests can shrink the delay.
_RM_RETRY_ATTEMPTS: Final[int] = 4
_RM_RETRY_DELAY_S: float = 0.25

# `docker create` args (NOT `run`, and NO `--rm`): we manage the container's
# whole lifecycle ourselves so cleanup never depends on the client process.
DEFAULT_DOCKER_ARGS: Final[tuple[str, ...]] = (
    "create",
    "--interactive",          # keep stdin open for `start --attach --interactive`
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


def _build_docker_create_command(name: str, timeout_s: float) -> list[str]:
    """Build the `docker create` argv for a single eval's sandbox container."""
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
    """Create a one-shot container, start it feeding `code` on stdin, parse the
    JSON envelope, and ALWAYS remove the container afterwards.

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
        # waited behind the gate for longer than the stale TTL would create a
        # brand-new container already carrying an "old" created label — and the
        # background sweep could reap a live eval. Stamping here means the label
        # reflects actual container creation time.
        name = _container_name()
        create_argv = _build_docker_create_command(name, effective_timeout_s)
        started = time.monotonic()

        # Phase 1 — create the container. Until this succeeds there is nothing to
        # clean up; once it succeeds the container definitively exists by `name`.
        create_error = await _create_container(name, create_argv)
        if create_error is not None:
            return RunResult(
                status="error",
                output="",
                stdout="",
                stderr=f"Failed to create sandbox container.\n{create_error}".strip(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        # Phase 2 — start + attach. The container now exists, so EVERY path below
        # (return, raise, timeout) removes it via the `finally`. Removal by name
        # is deterministic here: no daemon-create race can leave an orphan.
        try:
            return await _start_and_collect(code, name, effective_timeout_s, started)
        finally:
            await _cleanup_shielded(_force_remove_container(name))


async def _start_and_collect(
    code: str, name: str, effective_timeout_s: float, started: float
) -> RunResult:
    """`docker start --attach` the (already-created) container and build a result.

    The caller's `finally` guarantees the container is removed regardless of how
    this returns/raises, so here we only kill/drain the start CLIENT on the
    timeout/cancel paths (killing the client stops the attached run; the force
    kill of any still-running container is handled by the caller's `rm -f`).
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.docker_bin,
            "start",
            "--attach",
            "--interactive",
            name,
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
        await _kill_and_drain(proc)
        return RunResult(
            status="timeout",
            output="",
            stdout="",
            stderr=f"Evaluation exceeded {effective_timeout_s:.1f}s and was terminated.",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except BaseException:
        # Cancelled (client disconnect) or unexpected error. Kill/drain the start
        # client if we have it; the caller's `finally` removes the container.
        if proc is not None:
            await _kill_and_drain(proc)
        raise

    elapsed_ms = int((time.monotonic() - started) * 1000)
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr_raw = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        # Container exited non-zero (sandbox kill, OOM, start failure).
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


async def _create_container(name: str, create_argv: list[str]) -> str | None:
    """Run `docker create`, shielded against cancellation. Returns None on success
    (the container now exists by `name`), or an error string on failure.

    If cancellation races the create, the shielded create is allowed to settle so
    we can tell whether it produced a container; either way we force-remove by
    name (with not-found retry, covering the create tail) before re-raising.
    """
    create_task = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *create_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.shield(create_task)
        _out_bytes, err_bytes = await proc.communicate()
    except BaseException:
        # Cancelled/failed during create. Recover the shielded create client if
        # it materialised, stop it, then force-remove by name in case the daemon
        # did create the container. Then propagate.
        if proc is None:
            proc = await _await_spawn_after_cancellation(create_task)
        if proc is not None:
            with _suppress_async_errors():
                proc.kill()
            with _suppress_async_errors():
                await asyncio.wait_for(
                    proc.communicate(), timeout=settings.cleanup_op_timeout_s
                )
        await _cleanup_shielded(_force_remove_container(name, retry_not_found=True))
        raise

    if proc.returncode != 0:
        return (
            err_bytes.decode("utf-8", errors="replace").strip()
            or f"docker create exited with code {proc.returncode}"
        )
    return None


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


async def _await_spawn_after_cancellation(
    spawn_task: asyncio.Future,
) -> asyncio.subprocess.Process | None:
    """Recover a docker client handle after cancellation raced its spawn.

    The caller was cancelled while `create_subprocess_exec` (for `docker create`)
    was still in flight; because that spawn was shielded, it keeps running and
    will hand back a real Process handle shortly. We wait for it — bounded by
    `cleanup_op_timeout_s`, absorbing any repeat cancellations — so cleanup can
    kill/drain the client, rather than acting on a handle that never arrives.
    Returns the Process, or None if the spawn never yields a usable handle
    (failed, was itself cancelled, or exceeded the bound). Never raises.
    """
    deadline = time.monotonic() + settings.cleanup_op_timeout_s
    while not spawn_task.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Shield again: neither our timeout nor a repeat cancellation may
            # cancel the underlying spawn.
            await asyncio.wait_for(asyncio.shield(spawn_task), timeout=remaining)
        except asyncio.TimeoutError:
            break
        except asyncio.CancelledError:
            # Either our wait was cancelled (spawn still running → loop) or the
            # spawn task itself finished as cancelled (loop guard then exits).
            continue
        except Exception:  # noqa: BLE001 — spawn failed; state is inspected below
            break

    if not spawn_task.done():
        log.warning(
            "spawn did not return a process handle within %.1fs; "
            "falling back to force-remove by name",
            settings.cleanup_op_timeout_s,
        )
        return None
    if spawn_task.cancelled():
        log.debug("spawn task was cancelled; no process handle to terminate")
        return None
    exc = spawn_task.exception()
    if exc is not None:
        log.debug("spawn task failed during cancellation cleanup: %s", exc)
        return None
    return spawn_task.result()


async def _kill_and_drain(proc: asyncio.subprocess.Process) -> None:
    """Kill a docker-start client and drain its pipes (bounded). Never raises.

    This stops the attached run; the container itself is removed separately by
    the caller's `docker rm -f <name>` (which force-kills it if still running).
    """
    with _suppress_async_errors():
        proc.kill()
    # Drain the client's pipes so it can exit and we don't leak fds. Bounded so a
    # wedged client can't hang us.
    with _suppress_async_errors():
        await asyncio.wait_for(proc.communicate(), timeout=settings.cleanup_op_timeout_s)


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
