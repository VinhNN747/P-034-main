from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from src.backend_ai.latency.contracts import LatencyConfig
from src.backend_ai.latency.dataset import DatasetSnapshot, build_dataset, write_dataset
from src.backend_ai.latency.history import LatencyHistoryCache, is_usable_latency_label
from src.backend_ai.latency.store import LatencyModelStore, validate_artifact
from src.backend_ai.latency.telemetry import LatencyCollector, run_observation_writer
from src.shared.config import Settings

logger = logging.getLogger(__name__)
WRITER_SHUTDOWN_SECONDS = 5.0
BACKGROUND_SHUTDOWN_SECONDS = 25.0


@dataclass(frozen=True, slots=True)
class TrainingProcessResult:
    status: Literal["completed", "failed", "timeout", "stopped", "memory_limit", "lease_lost"]
    exit_code: int | None
    peak_rss_mb: float
    stdout: str = ""
    stderr: str = ""


class LatencyRuntime:
    """Process-owned telemetry and immutable serving snapshots for the AI backend."""

    def __init__(self, config: LatencyConfig, session_factory):
        self.config = config
        self.session_factory = session_factory
        self.collector = LatencyCollector(config)
        self.history_cache = LatencyHistoryCache(config)
        self.model_store = LatencyModelStore()
        self.last_retention_at: datetime | None = None


def should_train(
    now: datetime,
    last_run: datetime | None,
    total_samples: int,
    new_samples: int,
    local_inflight: int,
    queue_fraction: float,
    config: LatencyConfig,
) -> bool:
    if config.mode != "auto" or not config.training_enabled:
        return False
    if total_samples < config.min_training_samples or new_samples < config.min_new_samples:
        return False
    if last_run is not None and (now - last_run).total_seconds() < config.training_interval_seconds:
        return False
    if local_inflight > config.training_max_api_inflight or queue_fraction >= 0.5:
        return False
    return True


async def _start_training_if_owner(claim_lease, now: datetime, *, owner: str, on_claimed, ttl_seconds: int = 120) -> bool:
    lease = await claim_lease(owner, now, ttl_seconds)
    if lease is None:
        return False
    await on_claimed(lease)
    return True


async def _read_bounded(reader: asyncio.StreamReader, max_bytes: int = 64 * 1024) -> str:
    kept = bytearray()
    while chunk := await reader.read(8192):
        remaining = max_bytes - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
    return kept.decode("utf-8", errors="replace")


def _rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


async def _terminate_and_reap(process: asyncio.subprocess.Process, grace_seconds: float = 5.0) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=2.0)


def _child_environment(project_root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(project_root),
        "PYTHONUNBUFFERED": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


async def _supervise_command(
    command: list[str],
    *,
    timeout_seconds: float,
    max_rss_mb: int,
    stop_event: asyncio.Event,
    lease_alive: Callable[[], bool],
    poll_seconds: float = 1.0,
) -> TrainingProcessResult:
    project_root = Path(__file__).resolve().parents[3]
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = lambda: os.nice(10)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=project_root,
        env=_child_environment(project_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout), name="latency-child-stdout")
    stderr_task = asyncio.create_task(_read_bounded(process.stderr), name="latency-child-stderr")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    peak_rss = 0.0
    forced_status: str | None = None
    while process.returncode is None:
        if stop_event.is_set():
            forced_status = "stopped"
        elif not lease_alive():
            forced_status = "lease_lost"
        elif asyncio.get_running_loop().time() >= deadline:
            forced_status = "timeout"
        current_rss = _rss_mb(process.pid)
        peak_rss = max(peak_rss, current_rss)
        if current_rss > max_rss_mb:
            forced_status = "memory_limit"
        if forced_status:
            await _terminate_and_reap(process)
            break
        try:
            await asyncio.wait_for(process.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
    exit_code = await process.wait()
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    status = forced_status or ("completed" if exit_code == 0 else "failed")
    return TrainingProcessResult(status, exit_code, peak_rss, stdout, stderr)


async def supervise_training(
    dataset_path: Path,
    result_path: Path,
    options_path: Path,
    *,
    timeout_seconds: float,
    max_rss_mb: int,
    stop_event: asyncio.Event,
    lease_alive: Callable[[], bool],
) -> TrainingProcessResult:
    return await _supervise_command(
        [
            sys.executable,
            "-m",
            "src.backend_ai.latency.trainer",
            "--dataset",
            str(dataset_path),
            "--result",
            str(result_path),
            "--options",
            str(options_path),
        ],
        timeout_seconds=timeout_seconds,
        max_rss_mb=max_rss_mb,
        stop_event=stop_event,
        lease_alive=lease_alive,
    )


def _write_private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


async def _repository_call(runtime: LatencyRuntime, method: str, *args, **kwargs):
    from src.backend_ai.repositories.latency import LatencyRepository

    async with runtime.session_factory() as session:
        return await getattr(LatencyRepository(session), method)(*args, **kwargs)


async def _renew_lease(runtime: LatencyRuntime, lease, lost: asyncio.Event, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set() and not lost.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
            return
        except TimeoutError:
            pass
        try:
            renewed = await _repository_call(runtime, "renew_training", lease, datetime.now(UTC), 120)
        except Exception as exc:  # noqa: BLE001 - loss of DB connection invalidates publication
            logger.warning("latency lease renewal failed: %s", type(exc).__name__)
            lost.set()
            return
        if not renewed:
            lost.set()
            return


async def _record_terminal_run(runtime, lease, status: str, cutoff: datetime, sample_count: int,
                              digest: str, report: dict, expected_active_generation: int,
                              candidate_version: str | None = None,
                              error_code: str | None = None) -> None:
    await _repository_call(
        runtime,
        "record_training_run",
        lease,
        status=status,
        expected_active_generation=expected_active_generation,
        cutoff=cutoff,
        sample_count=sample_count,
        dataset_digest=digest,
        report=report,
        candidate_version=candidate_version,
        error_code=error_code,
        now=datetime.now(UTC),
    )


async def _run_training_with_lease(
    runtime: LatencyRuntime,
    lease,
    rows,
    now: datetime,
    expected_active_generation: int,
    stop_event: asyncio.Event,
) -> None:
    from src.backend_ai.latency.history import is_usable_latency_label

    run_dir = Path(runtime.config.work_dir) / lease.token
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    dataset_path = run_dir / "dataset.json"
    options_path = run_dir / "options.json"
    result_path = run_dir / "result.json"
    lost = asyncio.Event()
    renewal = asyncio.create_task(_renew_lease(runtime, lease, lost, stop_event), name="latency-lease-renewal")
    success_count = sum(is_usable_latency_label(row) for row in rows)
    digest = ""
    report: dict = {"accepted": False}
    training_started = time.monotonic()
    try:
        dataset: DatasetSnapshot = await asyncio.to_thread(
            build_dataset,
            rows,
            cutoff=now,
            config=runtime.config,
        )
        digest = dataset.digest
        write_dataset(dataset, dataset_path)
        _write_private_json(options_path, asdict(runtime.config))
        result = await supervise_training(
            dataset_path,
            result_path,
            options_path,
            timeout_seconds=runtime.config.training_timeout_seconds,
            max_rss_mb=runtime.config.training_max_rss_mb,
            stop_event=stop_event,
            lease_alive=lambda: not lost.is_set(),
        )
        runtime_metrics = {
            "sampled_at": datetime.now(UTC).isoformat(),
            "duration_seconds": max(0.0, time.monotonic() - training_started),
            "peak_rss_mb": result.peak_rss_mb,
            "exit_code": result.exit_code,
            "process_status": result.status,
            "collector": dict(runtime.collector.metrics),
            "resolution_reasons": dict(runtime.collector.resolution_reasons),
            "queue_depth": runtime.collector.queue.qsize(),
        }
        if result.status != "completed":
            terminal = "abandoned" if result.status in {"stopped", "lease_lost"} else "failed"
            await _record_terminal_run(runtime, lease, terminal, now, success_count, digest, {
                "accepted": False,
                "runtime_metrics": runtime_metrics,
            }, expected_active_generation, error_code=result.status)
            return
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        report = {**payload.get("report", {}), "runtime_metrics": runtime_metrics}
        if not payload.get("accepted") or not report.get("accepted"):
            await _record_terminal_run(
                runtime, lease, "rejected", now, success_count, digest, report, expected_active_generation
            )
            return
        artifact = validate_artifact(payload["artifact"])
        published = await _repository_call(
            runtime,
            "publish",
            lease,
            artifact,
            {**report, "sample_count": success_count, "dataset_digest": digest},
            expected_active_generation,
            datetime.now(UTC),
        )
        if not published:
            await _record_terminal_run(
                runtime, lease, "abandoned", now, success_count, digest, report,
                expected_active_generation, candidate_version=artifact.version, error_code="fenced_publish_rejected",
            )
            return
        from src.backend_ai.repositories.latency import LatencyRepository

        async with runtime.session_factory() as session:
            await runtime.model_store.refresh(LatencyRepository(session))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - failed training preserves the active artifact
        logger.warning("latency training run failed: %s", type(exc).__name__)
        try:
            await _record_terminal_run(
                runtime, lease, "failed", now, success_count, digest, {
                    **report,
                    "runtime_metrics": {
                        "sampled_at": datetime.now(UTC).isoformat(),
                        "duration_seconds": max(0.0, time.monotonic() - training_started),
                        "collector": dict(runtime.collector.metrics),
                        "resolution_reasons": dict(runtime.collector.resolution_reasons),
                        "queue_depth": runtime.collector.queue.qsize(),
                    },
                },
                expected_active_generation, error_code=type(exc).__name__[:80],
            )
        except Exception:  # noqa: BLE001 - preserve the serving path if reporting also fails
            logger.warning("latency training failure report could not be persisted")
    finally:
        renewal.cancel()
        try:
            await renewal
        except asyncio.CancelledError:
            pass
        shutil.rmtree(run_dir, ignore_errors=True)
        try:
            await _repository_call(runtime, "release_training", lease)
        except Exception as exc:  # noqa: BLE001
            logger.warning("latency lease release failed: %s", type(exc).__name__)


async def _training_cycle(runtime: LatencyRuntime, stop_event: asyncio.Event) -> None:
    from src.backend_ai.repositories.latency import LatencyRepository

    now = datetime.now(UTC)
    async with runtime.session_factory() as session:
        repository = LatencyRepository(session)
        rows = await repository.read_window(
            now,
            days=runtime.config.training_window_days,
            limit=runtime.config.max_training_rows,
        )
        progress = await repository.read_training_progress()
        generation, _ = await repository.read_active()
    usable = [row for row in rows if is_usable_latency_label(row)]
    last_run = progress.get("finished_at") if progress else None
    last_cutoff = progress.get("dataset_cutoff") if progress else None
    new_count = len(usable) if last_cutoff is None else sum(row.dispatched_at > last_cutoff for row in usable)
    inflight = runtime.collector.inflight_count
    if not should_train(now, last_run, len(usable), new_count, inflight, runtime.collector.queue_fraction, runtime.config):
        return

    owner = f"{os.getpid()}:{uuid4().hex[:12]}"

    async def train(lease):
        await _repository_call(
            runtime,
            "start_training_run",
            lease,
            expected_active_generation=generation,
            cutoff=now,
            sample_count=len(usable),
            now=datetime.now(UTC),
        )
        await _run_training_with_lease(runtime, lease, rows, now, generation, stop_event)

    async def claim_lease(lease_owner, lease_now, ttl_seconds):
        async with runtime.session_factory() as session:
            return await LatencyRepository(session).claim_training(lease_owner, lease_now, ttl_seconds)

    await _start_training_if_owner(claim_lease, now, owner=owner, on_claimed=train, ttl_seconds=120)


async def _refresh_loop(runtime: LatencyRuntime, stop_event: asyncio.Event) -> None:
    from src.backend_ai.repositories.latency import LatencyRepository

    while not stop_event.is_set():
        now = datetime.now(UTC)
        try:
            async with runtime.session_factory() as session:
                repository = LatencyRepository(session)
                await runtime.model_store.refresh(repository)
                runtime.history_cache.pin_model_version(
                    runtime.model_store.snapshot().version if runtime.model_store.snapshot() else None
                )
                await runtime.history_cache.refresh(repository, now)
                if runtime.last_retention_at is None or (now - runtime.last_retention_at).total_seconds() >= 3600:
                    await repository.delete_observations_before(now - timedelta(days=runtime.config.retention_days))
                    runtime.last_retention_at = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the old caches on storage errors
            logger.warning("latency snapshot refresh failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=runtime.config.history_refresh_seconds)
        except TimeoutError:
            continue


async def _cleanup_orphan_run_dirs(runtime: LatencyRuntime) -> None:
    root = Path(runtime.config.work_dir)
    if not root.exists() or root.is_symlink():
        return
    try:
        from src.backend_ai.repositories.latency import LatencyRepository

        async with runtime.session_factory() as session:
            active_token, expires_at = await LatencyRepository(session).read_training_lease()
    except Exception as exc:  # noqa: BLE001 - no cleanup without a trustworthy lease snapshot
        logger.warning("latency orphan cleanup deferred: %s", type(exc).__name__)
        return
    now = datetime.now(UTC)
    active_token = active_token if expires_at and expires_at > now else None
    stale_before = now.timestamp() - max(600, runtime.config.training_timeout_seconds * 2)
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name == active_token:
            continue
        try:
            if entry.stat().st_mtime < stale_before:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


async def _training_loop(runtime: LatencyRuntime, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if runtime.config.mode == "auto" and runtime.config.training_enabled:
            try:
                await _training_cycle(runtime, stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - training is best-effort
                logger.warning("latency training scheduler tick failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=runtime.config.training_poll_seconds)
        except TimeoutError:
            continue


async def run_latency_worker(settings: Settings, stop_event: asyncio.Event, runtime: LatencyRuntime) -> None:
    """Run writer, snapshot refresh and supervised training inside the AI process."""
    await _cleanup_orphan_run_dirs(runtime)
    writer = asyncio.create_task(
        run_observation_writer(runtime.collector, runtime.session_factory, stop_event),
        name="latency-observation-writer",
    )
    background = [
        asyncio.create_task(_refresh_loop(runtime, stop_event), name="latency-snapshot-refresh"),
        asyncio.create_task(_training_loop(runtime, stop_event), name="latency-training-scheduler"),
    ]
    await stop_event.wait()
    try:
        try:
            await asyncio.wait_for(writer, timeout=WRITER_SHUTDOWN_SECONDS)
        except TimeoutError:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            while True:
                try:
                    runtime.collector.queue.get_nowait()
                    runtime.collector.queue.task_done()
                    runtime.collector.metrics["dropped_shutdown"] += 1
                except asyncio.QueueEmpty:
                    break
        try:
            await asyncio.wait_for(asyncio.gather(*background), timeout=BACKGROUND_SHUTDOWN_SECONDS)
        except TimeoutError:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
    finally:
        for task in [writer, *background]:
            if not task.done():
                task.cancel()
        await asyncio.gather(writer, *background, return_exceptions=True)
