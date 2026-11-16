from __future__ import annotations

import asyncio
import os
import sys

import pytest

from src.backend_ai.workers.latency_worker import _supervise_command


@pytest.mark.asyncio
async def test_child_timeout_is_terminated_and_reaped(tmp_path):
    pid_path = tmp_path / "child.pid"
    code = f"import os,time; open({str(pid_path)!r},'w').write(str(os.getpid())); time.sleep(30)"
    result = await _supervise_command(
        [sys.executable, "-c", code], timeout_seconds=0.1, max_rss_mb=512,
        stop_event=asyncio.Event(), lease_alive=lambda: True, poll_seconds=0.02,
    )

    assert result.status == "timeout"
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_child_pipe_output_is_bounded_and_drained():
    code = "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"
    result = await _supervise_command(
        [sys.executable, "-c", code], timeout_seconds=5, max_rss_mb=512,
        stop_event=asyncio.Event(), lease_alive=lambda: True, poll_seconds=0.02,
    )

    assert result.status == "completed"
    assert len(result.stdout) <= 64 * 1024
    assert len(result.stderr) <= 64 * 1024


@pytest.mark.asyncio
async def test_lost_lease_terminates_child_without_reporting_success():
    result = await _supervise_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=5,
        max_rss_mb=512,
        stop_event=asyncio.Event(),
        lease_alive=lambda: False,
        poll_seconds=0.02,
    )

    assert result.status == "lease_lost"


@pytest.mark.asyncio
async def test_child_gets_single_thread_environment_without_parent_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
    code = (
        "import os; print(os.getenv('OPENAI_API_KEY', 'unset')); "
        "print(os.getenv('OPENBLAS_NUM_THREADS'))"
    )
    result = await _supervise_command(
        [sys.executable, "-c", code],
        timeout_seconds=5,
        max_rss_mb=512,
        stop_event=asyncio.Event(),
        lease_alive=lambda: True,
        poll_seconds=0.02,
    )

    assert result.status == "completed"
    assert result.stdout.splitlines() == ["unset", "1"]
