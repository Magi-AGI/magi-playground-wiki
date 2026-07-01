"""Unit tests for the runner's create/start/remove lifecycle, cleanup,
concurrency, and labelling — without Docker.

These mock `asyncio.create_subprocess_exec` so they exercise the real control
flow in `app.runner` (explicit `docker create` → `docker start --attach` →
`docker rm -f`, timeout handling, the stale sweep, the concurrency gate) against
fake `docker` invocations. They run in the default suite (no `live` marker, no
Docker daemon required).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import runner
from app.config import _STALE_TTL_SAFETY_MARGIN_S, Settings

# `asyncio_mode = "auto"` (pyproject) marks async tests automatically, so the
# sync settings-validation tests below run as plain functions without warnings.

# A well-formed envelope on stdout, as entrypoint.py would emit.
_ENVELOPE = b'{"output":"[3]","stdout":"","stderr":""}'


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


def _ok_start_proc() -> FakeProc:
    return FakeProc(mode="exit", stdout=_ENVELOPE, returncode=0)


class SpawnRecorder:
    """Patches create_subprocess_exec and dispatches by docker subcommand.

    Each of `create`/`start` can optionally: signal a `*_started` event when its
    spawn coroutine begins, block on a `*_release` event before returning (to let
    a test cancel the caller mid-spawn), or raise `*_raises`. `rm_responses`
    supplies a per-call sequence of (returncode, stderr) for `docker rm`, so tests
    can model the create/remove race and real failures. `ps_output` feeds the
    stale sweep.
    """

    def __init__(
        self,
        *,
        start_proc_factory=_ok_start_proc,
        create_proc_factory=None,
        ps_output: bytes = b"",
        rm_responses: list[tuple[int, bytes]] | None = None,
        create_raises: BaseException | None = None,
        create_started: "asyncio.Event | None" = None,
        create_release: "asyncio.Event | None" = None,
        start_raises: BaseException | None = None,
        start_started: "asyncio.Event | None" = None,
        start_release: "asyncio.Event | None" = None,
    ) -> None:
        self._start_proc_factory = start_proc_factory
        self._create_proc_factory = create_proc_factory or (
            lambda: FakeProc(mode="exit", stdout=b"deadbeef\n", returncode=0)
        )
        self._ps_output = ps_output
        self._rm_responses = list(rm_responses or [])
        self._create_raises = create_raises
        self._create_started = create_started
        self._create_release = create_release
        self._start_raises = start_raises
        self._start_started = start_started
        self._start_release = start_release
        self.calls: list[list[str]] = []
        self.removed: list[str] = []

    async def __call__(self, *argv, **kwargs):
        argv_list = [str(a) for a in argv]
        self.calls.append(argv_list)
        # argv[0] is the docker binary; argv[1] is the subcommand.
        sub = argv_list[1] if len(argv_list) > 1 else ""
        if sub == "create":
            if self._create_started is not None:
                self._create_started.set()
            if self._create_release is not None:
                await self._create_release.wait()
            if self._create_raises is not None:
                raise self._create_raises
            return self._create_proc_factory()
        if sub == "start":
            if self._start_started is not None:
                self._start_started.set()
            if self._start_release is not None:
                await self._start_release.wait()
            if self._start_raises is not None:
                raise self._start_raises
            return self._start_proc_factory()
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


def _calls_for(recorder: SpawnRecorder, sub: str) -> list[list[str]]:
    return [c for c in recorder.calls if len(c) > 1 and c[1] == sub]


def _count(recorder: SpawnRecorder, sub: str) -> int:
    return len(_calls_for(recorder, sub))


def _create_argv(recorder: SpawnRecorder) -> list[str]:
    calls = _calls_for(recorder, "create")
    if not calls:
        raise AssertionError("no `docker create` call was recorded")
    return calls[0]


def _name(recorder: SpawnRecorder) -> str:
    argv = _create_argv(recorder)
    return argv[argv.index("--name") + 1]


async def test_create_command_has_name_labels_and_security(monkeypatch) -> None:
    """The `docker create` argv carries a unique name, ownership labels, all
    sandbox flags — and is `create` without `--rm`."""
    recorder = SpawnRecorder()
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(+ 1 2)")
    assert result.status == "ok"

    argv = _create_argv(recorder)
    assert argv[1] == "create"
    assert "--rm" not in argv, "lifecycle is explicit; --rm must not be used"
    # Unique, service-prefixed name.
    name = argv[argv.index("--name") + 1]
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


async def test_normal_lifecycle_create_start_remove(monkeypatch) -> None:
    """Happy path issues create → start → rm (in that order) and parses output."""
    recorder = SpawnRecorder()
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(+ 1 2)")
    assert result.status == "ok"
    assert result.output == "[3]"

    subs = [c[1] for c in recorder.calls if len(c) > 1 and c[1] in {"create", "start", "rm"}]
    assert subs == ["create", "start", "rm"]
    # start targets the created container by name; rm removes that same name.
    name = _name(recorder)
    start_argv = _calls_for(recorder, "start")[0]
    assert start_argv[:4] == [runner.settings.docker_bin, "start", "--attach", "--interactive"]
    assert start_argv[-1] == name
    assert recorder.removed == [name]


async def test_create_failure_returns_error_and_no_leak(monkeypatch) -> None:
    """If `docker create` fails, surface an error, never start, and still issue a
    defensive rm-by-name in case the daemon partially created the container."""
    recorder = SpawnRecorder(
        create_proc_factory=lambda: FakeProc(
            mode="exit", stderr=b"docker: bad flag", returncode=125
        )
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(+ 1 2)")
    assert result.status == "error"
    assert "bad flag" in result.stderr
    assert _count(recorder, "start") == 0
    assert recorder.removed == [_name(recorder)]


async def test_timeout_kills_start_and_removes(monkeypatch) -> None:
    """On wall-clock timeout the start client is killed and the container removed."""
    start_proc = FakeProc(mode="hang")
    recorder = SpawnRecorder(start_proc_factory=lambda: start_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    result = await runner.run_metta("!(loop-forever)", timeout_s=0.05)

    assert result.status == "timeout"
    assert start_proc.killed is True
    assert recorder.removed == [_name(recorder)]


async def test_cancellation_during_start_cleans_up_and_propagates(monkeypatch) -> None:
    """A cancelled request whose start is running kills the client, removes the
    container, and re-raises CancelledError."""
    start_proc = FakeProc(mode="hang")
    recorder = SpawnRecorder(start_proc_factory=lambda: start_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    task = asyncio.create_task(runner.run_metta("!(loop-forever)", timeout_s=30))
    await asyncio.sleep(0.05)  # reach the start communicate await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert start_proc.killed is True
    assert recorder.removed == [_name(recorder)]


async def test_cancellation_during_start_spawn_still_removes(monkeypatch) -> None:
    """Even if cancellation lands while the START spawn is in flight (no proc
    handle yet), the container already exists (created) so the `finally` still
    removes it by name."""
    start_started = asyncio.Event()
    start_release = asyncio.Event()
    recorder = SpawnRecorder(
        start_started=start_started,
        start_release=start_release,
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    task = asyncio.create_task(runner.run_metta("!(x)"))
    await start_started.wait()  # create done; start spawn in flight
    task.cancel()
    await asyncio.sleep(0.02)
    start_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert _name(recorder) in recorder.removed


async def test_cancellation_during_create_recovers_and_removes(monkeypatch) -> None:
    """Cancellation while `docker create` is in flight: the shielded create is
    allowed to settle, then the container is force-removed by name and the
    cancellation re-raised."""
    create_started = asyncio.Event()
    create_release = asyncio.Event()
    recorder = SpawnRecorder(
        create_started=create_started,
        create_release=create_release,
        rm_responses=[(0, b"")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    task = asyncio.create_task(runner.run_metta("!(x)"))
    await create_started.wait()
    task.cancel()
    await asyncio.sleep(0.02)  # deliver cancellation while create is shielded
    create_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert _name(recorder) in recorder.removed
    # Never got to start.
    assert _count(recorder, "start") == 0


async def test_cancellation_during_create_no_handle_reaps_by_name(monkeypatch) -> None:
    """If the create spawn fails outright (no usable handle), cleanup still
    force-removes by name, retrying past an initial not-found."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        create_raises=asyncio.CancelledError(),
        rm_responses=[
            (1, b"Error: No such container: magi-pg-x"),
            (0, b""),
        ],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    with pytest.raises(asyncio.CancelledError):
        await runner.run_metta("!(x)")

    name = _name(recorder)
    # Retried past the not-found → two rm attempts against the same name.
    assert recorder.removed == [name, name]
    assert _count(recorder, "start") == 0


async def test_concurrency_cap_is_enforced(monkeypatch) -> None:
    """No more than `max_concurrent_evals` containers run simultaneously."""
    # Settings is frozen; install the gate directly (run_metta uses it as-is).
    runner._eval_semaphore = asyncio.Semaphore(2)

    inflight = 0
    peak = 0
    release = asyncio.Event()

    def make_start_proc() -> FakeProc:
        started = asyncio.Event()
        proc = FakeProc(mode="gate", started=started, release=release, returncode=0,
                        stdout=_ENVELOPE)
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

    recorder = SpawnRecorder(start_proc_factory=make_start_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    tasks = [asyncio.create_task(runner.run_metta("!(+ 1 1)")) for _ in range(5)]
    await asyncio.sleep(0.1)
    assert peak <= 2, f"peak in-flight {peak} exceeded cap of 2"
    assert inflight <= 2

    release.set()
    results = await asyncio.gather(*tasks)
    assert all(r.status == "ok" for r in results)
    assert peak == 2, "expected the gate to admit exactly the cap concurrently"


async def test_identity_built_after_semaphore_admission(monkeypatch) -> None:
    """A request queued behind the gate does NOT create its container (or stamp
    LABEL_CREATED) until admitted — so the label reflects creation time, not
    queue-entry time."""
    runner._eval_semaphore = asyncio.Semaphore(1)
    start_started = asyncio.Event()
    start_release = asyncio.Event()

    def make_start_proc() -> FakeProc:
        return FakeProc(mode="gate", started=start_started, release=start_release,
                        returncode=0, stdout=_ENVELOPE)

    recorder = SpawnRecorder(start_proc_factory=make_start_proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    t1 = asyncio.create_task(runner.run_metta("!(a)"))
    await start_started.wait()  # first admitted: created + started (now gated)
    assert _count(recorder, "create") == 1

    t2 = asyncio.create_task(runner.run_metta("!(b)"))
    await asyncio.sleep(0.05)
    # t2 is queued on the semaphore: it must NOT have created a container yet.
    assert _count(recorder, "create") == 1, "queued request created before admission"

    start_release.set()
    await asyncio.gather(t1, t2)
    assert _count(recorder, "create") == 2


async def test_force_remove_retries_not_found_then_succeeds(monkeypatch) -> None:
    """A transient 'No such container' (daemon still finishing create) is retried
    until the container appears and is removed."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        rm_responses=[
            (1, b"Error: No such container: magi-pg-x"),
            (0, b""),
        ],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    await runner._force_remove_container("magi-pg-x", retry_not_found=True)
    assert len(recorder.removed) == 2


async def test_force_remove_no_retry_by_default(monkeypatch) -> None:
    """Default removal (container known to exist) does not retry on not-found."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        rm_responses=[(1, b"Error: No such container: magi-pg-x")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    await runner._force_remove_container("magi-pg-x")  # retry_not_found=False
    assert len(recorder.removed) == 1


async def test_force_remove_real_failure_is_logged(monkeypatch, caplog) -> None:
    """A non-'No such container' failure is surfaced as a warning and not retried."""
    monkeypatch.setattr(runner, "_RM_RETRY_DELAY_S", 0)
    recorder = SpawnRecorder(
        rm_responses=[(1, b"Error response from daemon: permission denied")],
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    with caplog.at_level("WARNING", logger="app.runner"):
        await runner._force_remove_container("magi-pg-x", retry_not_found=True)

    assert len(recorder.removed) == 1, "non-not-found rc=1 must not be retried"
    assert any("permission denied" in r.getMessage() for r in caplog.records)


async def test_stale_cleanup_only_removes_old_labelled_containers(monkeypatch) -> None:
    """The sweep filters by ownership label and removes only containers past the TTL."""
    now = int(time.time())
    old_name = "magi-pg-old"
    fresh_name = "magi-pg-fresh"
    ps_output = (
        f"{old_name}\t{now - 9999}\n"
        f"{fresh_name}\t{now - 1}\n"
    ).encode("utf-8")

    recorder = SpawnRecorder(ps_output=ps_output)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", recorder)

    removed = await runner.cleanup_stale_containers(ttl_s=300)

    assert removed == 1
    assert recorder.removed == [old_name]

    ps_call = next(c for c in recorder.calls if len(c) > 1 and c[1] == "ps")
    assert f"label={runner.LABEL_MANAGED}=true" in ps_call


async def test_stale_cleanup_no_docker_is_noop(monkeypatch) -> None:
    """If docker isn't installed the sweep returns 0 and doesn't raise."""

    async def boom(*_argv, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", boom)
    assert await runner.cleanup_stale_containers() == 0


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
    await asyncio.sleep(0.01)
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
