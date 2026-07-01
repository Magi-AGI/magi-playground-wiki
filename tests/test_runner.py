"""Unit tests for runner cleanup / concurrency / labelling, without Docker.

These mock `asyncio.create_subprocess_exec` so they exercise the real control
flow in `app.runner` (timeout handling, force-removal, the stale sweep, the
concurrency gate) against fake `docker` invocations. They run in the default
suite (no `live` marker, no Docker daemon required).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import runner
from app.config import _STALE_TTL_SAFETY_MARGIN_S, Settings

# `asyncio_mode = "auto"` (pyproject) marks async tests automatically, so the
# sync settings-validation tests below run as plain functions without warnings.


class FakeProc:
    """Stand-in for an asyncio subprocess.

    `mode` controls how `communicate` behaves:
        "exit"  — return (stdout, stderr) immediately with `returncode`.
        "hang"  — block until kill() is called (simulates a runaway container).
        "gate"  — signal `started`, then block until `release` is set
                  (used to observe in-flight concurrency).
    """

    def __init__(
        self,
        *,
        mode: str = "exit",
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._mode = mode
        self._stdout = stdout
        self._stderr = stderr
        self._final_returncode = returncode
        self._started = started
        self._release = release
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        if self.killed:
            self.returncode = self._final_returncode
            return (self._stdout, self._stderr)
        if self._mode == "hang":
            while not self.killed:
                await asyncio.sleep(0.005)
            self.returncode = self._final_returncode
            return (self._stdout, self._stderr)
        if self._mode == "gate":
            if self._started is not None:
                self._started.set()
            assert self._release is not None
            await self._release.wait()
        self.returncode = self._final_returncode
        return (self._stdout, self._stderr)

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode


class SpawnRecorder:
    """Patches create_subprocess_exec and dispatches by docker subcommand.

    `rm_responses` optionally supplies a per-call sequence of (returncode, stderr)
    for `docker rm`, so tests can model the create/remove race (not-found then
    success) and real failures. Once exhausted, `rm` defaults to (0, b"").
    """

    def __init__(
        self,
        *,
        run_proc_factory,
        ps_output: bytes = b"",
        rm_responses: list[tuple[int, bytes]] | None = None,
        run_raises: BaseException | None = None,
        run_started: "asyncio.Event | None" = None,
        run_release: "asyncio.Event | None" = None,
    ) -> None:
        self._run_proc_factory = run_proc_factory
        self._ps_output = ps_output
        self._rm_responses = list(rm_responses or [])
        # If set, the `docker run` spawn records its argv (so the name is
        # observable) and then RAISES — simulating cancellation/error arriving
        # mid create_subprocess_exec after the daemon created the container.
        self._run_raises = run_raises
        # `run_started` is set when the spawn coroutine begins; `run_release`, if
        # provided, is awaited before the spawn returns — letting a test cancel
        # the caller while the spawn is still in flight, then release it so the
        # spawn returns a handle LATE (the live-smoke 008 scenario).
        self._run_started = run_started
        self._run_release = run_release
        self.calls: list[list[str]] = []
        self.removed: list[str] = []

    async def __call__(self, *argv, **kwargs):
        argv_list = [str(a) for a in argv]
        self.calls.append(argv_list)
        # argv[0] is the docker binary; argv[1] is the subcommand.
        sub = argv_list[1] if len(argv_list) > 1 else ""
        if sub == "run":
            if self._run_started is not None:
                self._run_started.set()
            if self._run_release is not None:
                await self._run_release.wait()
            if self._run_raises is not None:
                raise self._run_raises
            return self._run_proc_factory()
        if sub == "rm":
            # docker rm -f <name>  → record the target name.
            self.removed.append(argv_list[-1])
            if self._rm_responses:
                rc, err = self._rm_responses.pop(0)
            else:
                rc, err = 0, b""
            return FakeProc(mode="exit", returncode=rc, stderr=err)
        if sub == "ps":
            return FakeProc(mode="exit", stdout=self._ps_output, returncode=0)
        return FakeProc(mode="exit", returncode=0)


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Each test gets a fresh semaphore bound to the test's event loop."""
    runner._eval_semaphore = None
    yield
    runner._eval_semaphore = None


def _run_argv(recorder: SpawnRecorder) -> list[str]:
    for call in recorder.calls:
        if len(call) > 1 and call[1] == "run":
            return call
    raise AssertionError("no `docker run` call was recorded")


async def test_build_command_has_name_labels_and_security(monkeypatch) -> None:
    """The run argv carries a unique name, ownership labels, and all sandbox flags."""
    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(mode="exit", stdout=b'{"output":"[3]"}', returncode=0)
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(+ 1 2)")
    assert result.status == "ok"

    argv = _run_argv(recorder)
    # Unique, service-prefixed name.
    name_idx = argv.index("--name")
    name = argv[name_idx + 1]
    assert name.startswith(runner.CONTAINER_NAME_PREFIX)
    # Ownership labels.
    assert f"{runner.LABEL_MANAGED}=true" in argv
    assert any(a.startswith(f"{runner.LABEL_CREATED}=") for a in argv)
    # Security constraints preserved.
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
    ):
        assert flag in argv, f"missing security flag {flag}"
    assert "--memory" in argv and "--pids-limit" in argv and "--cpus" in argv
    # Two tmpfs mounts (/tmp and HOME).
    assert argv.count("--tmpfs") == 2


async def test_timeout_force_removes_container(monkeypatch) -> None:
    """On wall-clock timeout the underlying container is `docker rm -f`'d by name."""
    run_proc = FakeProc(mode="hang")
    recorder = SpawnRecorder(run_proc_factory=lambda: run_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(loop-forever)", timeout_s=0.05)

    assert result.status == "timeout"
    # The docker-run client was killed AND the named container force-removed.
    assert run_proc.killed is True
    argv = _run_argv(recorder)
    name = argv[argv.index("--name") + 1]
    assert recorder.removed == [name]


async def test_cancellation_cleans_up_and_propagates(monkeypatch) -> None:
    """A cancelled request (client disconnect) still removes the container, then re-raises."""
    run_proc = FakeProc(mode="hang")
    recorder = SpawnRecorder(run_proc_factory=lambda: run_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    task = asyncio.create_task(runner.run_metta("!(loop-forever)", timeout_s=30))
    # Let it reach the awaiting-communicate point, then cancel.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert run_proc.killed is True
    argv = _run_argv(recorder)
    name = argv[argv.index("--name") + 1]
    assert recorder.removed == [name]


async def test_cancellation_during_spawn_recovers_handle_and_terminates(monkeypatch) -> None:
    """HOTFIX 008: cancellation while the docker-run spawn is STILL IN FLIGHT must
    not abandon it. The shielded spawn returns its client handle LATE; cleanup
    recovers that handle, kills/drains the client, and force-removes the
    container — then re-raises CancelledError."""
    run_started = asyncio.Event()
    run_release = asyncio.Event()
    fake = FakeProc(mode="hang")
    recorder = SpawnRecorder(
        run_proc_factory=lambda: fake,
        run_started=run_started,
        run_release=run_release,
        rm_responses=[(0, b"")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    task = asyncio.create_task(runner.run_metta("!(x)"))
    await run_started.wait()  # spawn is in flight (proc handle not yet returned)
    task.cancel()
    # Let the cancellation be delivered at the shielded-spawn await while proc is
    # still None, THEN let the spawn complete and hand back the late handle.
    await asyncio.sleep(0.02)
    run_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The recovered client handle was killed, and the container force-removed.
    assert fake.killed is True
    argv = _run_argv(recorder)
    name = argv[argv.index("--name") + 1]
    assert name in recorder.removed


async def test_cancellation_during_spawn_no_handle_reaps_by_name(monkeypatch) -> None:
    """HOTFIX 006/008 fallback: if the spawn never yields a usable handle (it
    fails outright), cleanup still force-removes the container by name, retrying
    past an initial not-found in case the daemon materialised it late."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        run_proc_factory=lambda: None,  # unused: the run spawn raises instead
        run_raises=asyncio.CancelledError(),
        # Container materialised late: first rm not-found, retry succeeds.
        rm_responses=[
            (1, b"Error: No such container: magi-pg-spawn"),
            (0, b""),
        ],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    with pytest.raises(asyncio.CancelledError):
        await runner.run_metta("!(x)")

    # The run argv was recorded before the raise, so we know the intended name.
    argv = _run_argv(recorder)
    name = argv[argv.index("--name") + 1]
    # Force-removed by name, retrying past the initial not-found → two attempts.
    assert recorder.removed == [name, name]


async def test_clean_exit_does_not_force_remove(monkeypatch) -> None:
    """Happy path relies on `--rm`; we don't spawn a redundant `docker rm`."""
    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(
            mode="exit", stdout=b'{"output":"[3]","stdout":"","stderr":""}', returncode=0
        )
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(+ 1 2)")
    assert result.status == "ok"
    assert result.output == "[3]"
    assert recorder.removed == []


async def test_concurrency_cap_is_enforced(monkeypatch) -> None:
    """No more than `max_concurrent_evals` containers run simultaneously."""
    # Settings is frozen; install the gate directly (run_metta uses it as-is).
    runner._eval_semaphore = asyncio.Semaphore(2)

    inflight = 0
    peak = 0
    release = asyncio.Event()

    def make_run_proc() -> FakeProc:
        nonlocal inflight, peak

        started = asyncio.Event()
        proc = FakeProc(mode="gate", started=started, release=release, returncode=0,
                        stdout=b'{"output":"[]","stdout":"","stderr":""}')

        # Wrap communicate to track in-flight count around the gated section.
        orig = proc.communicate

        async def tracked(_input=None):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            try:
                return await orig(_input)
            finally:
                inflight -= 1

        proc.communicate = tracked  # type: ignore[method-assign]
        return proc

    recorder = SpawnRecorder(run_proc_factory=make_run_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    tasks = [asyncio.create_task(runner.run_metta("!(+ 1 1)")) for _ in range(5)]
    # Give the scheduler time to admit as many as the gate allows.
    await asyncio.sleep(0.1)
    assert peak <= 2, f"peak in-flight {peak} exceeded cap of 2"
    assert inflight <= 2

    release.set()
    results = await asyncio.gather(*tasks)
    assert all(r.status == "ok" for r in results)
    assert peak == 2, "expected the gate to admit exactly the cap concurrently"


async def test_stale_cleanup_only_removes_old_labelled_containers(monkeypatch) -> None:
    """The sweep filters by ownership label and removes only containers past the TTL."""
    now = int(time.time())
    # One old (well past TTL), one fresh (just created).
    old_name = "magi-pg-old"
    fresh_name = "magi-pg-fresh"
    ps_output = (
        f"{old_name}\t{now - 9999}\n"
        f"{fresh_name}\t{now - 1}\n"
    ).encode("utf-8")

    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(mode="exit"), ps_output=ps_output
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    removed = await runner.cleanup_stale_containers(ttl_s=300)

    assert removed == 1
    assert recorder.removed == [old_name]

    # The ps query must be scoped to this service's ownership label.
    ps_call = next(c for c in recorder.calls if len(c) > 1 and c[1] == "ps")
    assert f"label={runner.LABEL_MANAGED}=true" in ps_call


async def test_stale_cleanup_no_docker_is_noop(monkeypatch) -> None:
    """If docker isn't installed the sweep returns 0 and doesn't raise."""

    async def boom(*_argv, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", boom)
    assert await runner.cleanup_stale_containers() == 0


def _count_runs(recorder: SpawnRecorder) -> int:
    return sum(1 for c in recorder.calls if len(c) > 1 and c[1] == "run")


def _count_rms(recorder: SpawnRecorder) -> int:
    return sum(1 for c in recorder.calls if len(c) > 1 and c[1] == "rm")


async def test_identity_is_built_after_semaphore_admission(monkeypatch) -> None:
    """A request queued behind the gate does NOT spawn (or stamp its label) until
    admitted — so LABEL_CREATED reflects start time, not queue-entry time."""
    runner._eval_semaphore = asyncio.Semaphore(1)
    release = asyncio.Event()
    first_started = asyncio.Event()

    def make_proc() -> FakeProc:
        return FakeProc(
            mode="gate",
            started=first_started,
            release=release,
            returncode=0,
            stdout=b'{"output":"[]","stdout":"","stderr":""}',
        )

    recorder = SpawnRecorder(run_proc_factory=make_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    t1 = asyncio.create_task(runner.run_metta("!(a)"))
    await first_started.wait()
    assert _count_runs(recorder) == 1  # first admitted and spawned

    t2 = asyncio.create_task(runner.run_metta("!(b)"))
    await asyncio.sleep(0.05)
    # t2 is queued on the semaphore: it must NOT have built argv / spawned a
    # container yet (that's the whole point — no pre-stamped stale label).
    assert _count_runs(recorder) == 1, "queued request spawned before admission"

    release.set()
    await asyncio.gather(t1, t2)
    assert _count_runs(recorder) == 2


async def test_force_remove_retries_not_found_then_succeeds(monkeypatch) -> None:
    """On the terminate path a transient 'No such container' (daemon still
    creating) is retried until the container appears and is removed."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(mode="exit"),
        rm_responses=[
            (1, b"Error: No such container: magi-pg-x"),
            (0, b""),
        ],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    await runner._force_remove_container("magi-pg-x", retry_not_found=True)
    # Retried once, then succeeded → exactly two rm attempts.
    assert _count_rms(recorder) == 2


async def test_force_remove_no_retry_for_stale_sweep(monkeypatch) -> None:
    """Stale-sweep removals (container known to exist via ps) do not retry on
    not-found — a single benign attempt suffices."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(mode="exit"),
        rm_responses=[(1, b"Error: No such container: magi-pg-x")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    await runner._force_remove_container("magi-pg-x")  # retry_not_found=False
    assert _count_rms(recorder) == 1


async def test_force_remove_real_failure_is_logged(monkeypatch, caplog) -> None:
    """A non-'No such container' failure (e.g. permission/daemon error) is NOT
    treated as benign: it is surfaced as a warning and not retried."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        run_proc_factory=lambda: FakeProc(mode="exit"),
        rm_responses=[(1, b"Error response from daemon: permission denied")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    with caplog.at_level("WARNING", logger="app.runner"):
        await runner._force_remove_container("magi-pg-x", retry_not_found=True)

    assert _count_rms(recorder) == 1, "non-not-found rc=1 must not be retried"
    assert any("permission denied" in r.getMessage() for r in caplog.records)


async def test_timeout_reaps_container_that_appears_late(monkeypatch) -> None:
    """Integration: a timeout whose first rm races container creation still
    removes the container once it appears."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    run_proc = FakeProc(mode="hang")
    recorder = SpawnRecorder(
        run_proc_factory=lambda: run_proc,
        rm_responses=[
            (1, b"Error: No such container: magi-pg-late"),
            (0, b""),
        ],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(loop)", timeout_s=0.05)
    assert result.status == "timeout"
    argv = _run_argv(recorder)
    name = argv[argv.index("--name") + 1]
    # Two attempts, both targeting the same container name.
    assert recorder.removed == [name, name]


async def test_cleanup_shielded_completes_under_cancellation() -> None:
    """A second cancellation while cleanup is running must not abort it; cleanup
    runs to completion and the cancellation is re-raised afterward."""
    done: list[bool] = []

    async def cleanup() -> None:
        await asyncio.sleep(0.05)
        done.append(True)

    async def caller() -> None:
        await runner._cleanup_shielded(cleanup())

    task = asyncio.create_task(caller())
    await asyncio.sleep(0.01)  # let cleanup start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert done == [True], "cleanup was aborted by the second cancellation"


def test_settings_clamp_unsafe_overrides() -> None:
    """Bad operator overrides are clamped to safe values."""
    s = Settings(
        max_timeout_s=10.0,
        max_concurrent_evals=0,
        cleanup_interval_s=0,
        cleanup_op_timeout_s=0.1,
        stale_container_ttl_s=1,
    )
    assert s.max_concurrent_evals == 1
    assert s.cleanup_interval_s == 1
    assert s.cleanup_op_timeout_s == 0.5
    # TTL raised above the eval window so the sweep can't reap a live eval.
    assert s.stale_container_ttl_s == int(s.max_timeout_s) + _STALE_TTL_SAFETY_MARGIN_S
    assert s.stale_container_ttl_s > s.max_timeout_s


def test_settings_safe_defaults_unchanged() -> None:
    """Sane explicit values pass through untouched."""
    s = Settings(
        max_timeout_s=10.0,
        max_concurrent_evals=4,
        cleanup_interval_s=60,
        cleanup_op_timeout_s=5.0,
        stale_container_ttl_s=300,
    )
    assert s.max_concurrent_evals == 4
    assert s.cleanup_interval_s == 60
    assert s.cleanup_op_timeout_s == 5.0
    assert s.stale_container_ttl_s == 300
